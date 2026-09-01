"""Batch 22L T2：Benchmark v2 QA 生成器（带证据页唯一校验与断点续跑）。

用法（在 backend/ 目录下）：
    env -u PYTHONPATH venv/bin/python -m eval.generate_qa_v2                    # 全量 34 篇
    env -u PYTHONPATH venv/bin/python -m eval.generate_qa_v2 --limit 2          # 冒烟 2 篇
    env -u PYTHONPATH venv/bin/python -m eval.generate_qa_v2 --resume           # 断点续跑
    env -u PYTHONPATH venv/bin/python -m eval.generate_qa_v2 \\
        --splits eval/private/benchmark_v2_splits.json \\
        --output eval/private/qa_v2_candidates.jsonl

流程：
1. 读取 T1 冻结的 split 制品（paper_uid + pdf_sha256 + split）；
2. 复用 eval.private_benchmark.paper_uid 算法把冻结 UID 映射回 DB Paper；
3. pdfplumber 按页提取文本（页码标签进入 prompt，供 LLM 标注 evidence_page）；
4. 每篇调 llm_service.chat_completion_sync 生成 3 条 QA
   （question_type 轮换 factoid/method_detail/summary）；
5. 校验器逐条把关：evidence_quote 长度 10-200、在指定页恰好出现 1 次、
   跨页不串（其他页 0 次）；paper 归属与冻结 split 一致；失败条目跳过并计数；
6. 合格条目写入 eval/private/qa_v2_candidates.jsonl：
   无 --resume 时 O_EXCL 排他创建（0600），已存在即拒绝覆盖；
   --resume 时读取已有 paper_uid 跳过并追加写入。

候选 schema 对齐 eval/dataset/README.md（qa_id/question/ground_truth/
relevant_evidence/question_type/source/has_answer），并附加
evidence_quote/evidence_page/paper_uid/split/reviewed:false。
审稿通过前 source=llm_generated，不能直接进入冻结集。
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# 复用 Phase A 的严格 JSON 解析与条目提取，避免重复实现
from eval.generate_qa import _extract_items, parse_llm_json

# 默认输入/输出（均位于 gitignore 的私有制品目录）
DEFAULT_SPLITS_PATH = (
    Path(__file__).resolve().parent / "private" / "benchmark_v2_splits.json")
DEFAULT_OUTPUT_PATH = (
    Path(__file__).resolve().parent / "private" / "qa_v2_candidates.jsonl")

# 每篇生成 3 条，question_type 按序轮换
QUESTION_TYPE_ROTATION = ("factoid", "method_detail", "summary")
PER_PAPER = 3

# 证据 quote 长度边界（T2 契约：10-200 字符，短而独特）
QUOTE_MIN_CHARS = 10
QUOTE_MAX_CHARS = 200

# 每篇送给 LLM 的按页素材字符预算（远低于 llm_service.max_total_chars=200000）
MATERIAL_CHAR_BUDGET = 12000

# 候选集审稿标记来源值（合并进冻结集时由人工改为 imported_paper）
CANDIDATE_SOURCE = "llm_generated"

_SPLITS = {"train", "dev", "holdout"}


# ---------------------------------------------------------------------------
# LLM 调用（延迟导入，保证加载本模块不触发配置读取与网络）
# ---------------------------------------------------------------------------

def _call_llm(messages: List[Dict[str, str]]) -> str:
    """调用 llm_service 生成 JSON（测试中注入 call_llm 避免真实 API 调用）。"""
    from app.services.llm import llm_service  # 延迟导入

    return llm_service.chat_completion_sync(messages, json_mode=True)


# ---------------------------------------------------------------------------
# 冻结 split 制品读取与 UID 映射
# ---------------------------------------------------------------------------

def load_frozen_splits(path: Any) -> List[dict]:
    """读取 T1 冻结的 split 制品，返回 assignments 列表。

    异常：
        FileNotFoundError: 制品不存在；
        ValueError: schema 不符或 assignment 缺字段/非法 split。
    """
    artifact = json.loads(Path(path).read_text(encoding="utf-8"))
    if artifact.get("split_schema") != "private-benchmark-v2-paper-splits-v1":
        raise ValueError("不是 Benchmark v2 split 冻结制品（schema 不符）")
    assignments = artifact.get("assignments")
    if not isinstance(assignments, list) or not assignments:
        raise ValueError("split 冻结制品缺少 assignments")
    for row in assignments:
        if not isinstance(row.get("paper_uid"), str) or not row["paper_uid"].strip():
            raise ValueError("assignment 缺少 paper_uid")
        if row.get("split") not in _SPLITS:
            raise ValueError("assignment split 必须为 train/dev/holdout")
    return assignments


def map_uids_to_papers(
    db: Any, runtime_root: Any, uids: set
) -> Tuple[Dict[str, Any], List[str]]:
    """把冻结 paper_uid 映射回 DB Paper（复用 paper_uid 稳定身份算法）。

    无 DOI 且源文件缺失的论文无法构造 UID，跳过而非抛异常；
    返回 (uid -> Paper 映射, 未命中 uid 排序列表)。
    """
    from app.models import Paper  # 延迟导入，避免加载模块即连库
    from eval.private_benchmark import paper_uid

    mapping: Dict[str, Any] = {}
    for paper in db.query(Paper).all():
        try:
            uid = paper_uid(paper, Path(runtime_root))
        except ValueError:
            continue  # 源文件缺失，无法构造稳定 UID
        if uid in uids and uid not in mapping:
            mapping[uid] = paper
    missing = sorted(uids - set(mapping))
    return mapping, missing


# ---------------------------------------------------------------------------
# 按页素材构造与 prompt
# ---------------------------------------------------------------------------

def build_material(
    title: Optional[str],
    abstract: Optional[str],
    pages: List[dict],
    budget: Optional[int] = None,
) -> str:
    """拼接按页素材：标题 + 摘要 + 分层抽样页（保留【第 N 页】标签），控制在预算内。

    分层抽样（首/中/尾 + 均匀补位）保证实验结果与结论页也能进入素材；
    截断只发生在页内，evidence_quote 校验仍对完整页文本进行。
    """
    if budget is None:
        budget = MATERIAL_CHAR_BUDGET
    parts: List[str] = [f"论文标题: {title or '(无标题)'}"]
    abstract = (abstract or "").strip()
    if abstract:
        parts.append(f"摘要: {abstract}")
    used = sum(len(p) for p in parts)
    nonempty = [p for p in pages if (p.get("text") or "").strip()]
    if not nonempty:
        return "\n\n".join(parts)

    # 与 Phase A 相同的确定性分层：首/中/尾 + 5 个均匀位置
    wanted = [0, len(nonempty) // 2, len(nonempty) - 1]
    wanted.extend(round(i * (len(nonempty) - 1) / 6) for i in range(1, 6))
    selected: List[int] = []
    for index in wanted:
        if index not in selected:
            selected.append(index)
    remaining = max(0, budget - used - 16 * len(selected))
    per_page = max(1, remaining // len(selected)) if selected else 0
    for index in selected:
        page = nonempty[index]
        text = (page.get("text") or "").strip()
        available = budget - used - 16
        if available <= 0:
            break
        excerpt = text[:min(per_page, available)]
        if excerpt:
            label = f"【第 {page.get('page_number')} 页】"
            parts.append(f"{label}\n{excerpt}")
            used += len(excerpt) + len(label) + 2
    return "\n\n".join(parts)


_SYSTEM_PROMPT = (
    "你是学术评测数据集构建助手。根据给定论文的按页内容设计「可由原文直接回答」的"
    "问答评测题。严格只输出 JSON 对象，不要输出任何其他文字。"
)

_USER_PROMPT_TEMPLATE = """下面是论文「{title}」的按页内容摘录（每页以【第 N 页】开头，N 为 1-based 页码）。

---论文内容开始---
{material}
---论文内容结束---

请基于以上内容设计 {n} 条评测 QA，要求：
{type_instruction}
2. 每条问题必须能由上述原文直接回答，不得涉及原文没有的信息；
3. question 用中文，具体、聚焦；
4. answer 为参考答案要点：用「、」分隔的短小关键短语（数值、模块名、结论词），
   总长不超过 120 字；
5. evidence_quote 为从某一页中【逐字原样复制】的连续片段，长度 10~200 字符，
   必须【短而独特】——在该页内只出现一次、其他页不出现，足以唯一定位，
   不得改写或翻译；
6. evidence_page 为 evidence_quote 所在页的 1-based 页码（整数）；
7. 只输出如下 JSON：
{{"items": [{{"question": "...", "question_type": "...", "answer": "...",
  "evidence_quote": "...", "evidence_page": 1}}]}}
"""

_ROTATION_INSTRUCTION = (
    "1. 第 1 条 question_type 为 factoid（单一事实/数值），第 2 条为 method_detail\n"
    "   （方法细节），第 3 条为 summary（总结概括）；"
)


def build_messages(
    title: str, material: str, n: int, first_type: Optional[str] = None
) -> List[Dict[str, str]]:
    """构造单篇论文 QA 生成的对话消息。

    first_type（仅 n=1 时生效）：指定本条的问题类型——逐条生成模式下由调用方轮换。
    """
    if first_type is not None and n == 1:
        type_instruction = f"1. 本条 question_type 为 {first_type}；"
    else:
        type_instruction = _ROTATION_INSTRUCTION
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": _USER_PROMPT_TEMPLATE.format(
            title=title, material=material, n=n, type_instruction=type_instruction)},
    ]


# ---------------------------------------------------------------------------
# 证据唯一校验器与候选条目构造
# ---------------------------------------------------------------------------

def validate_evidence_quote(
    quote: Any, page_texts: List[str], evidence_page: Any
) -> str:
    """校验证据 quote：长度 10-200、页码合法、指定页恰好出现 1 次、跨页不串。

    返回 strip 后的 quote；任一校验失败抛 ValueError（调用方跳过并计数）。
    跨页不串保证下游 resolve_relevant_spans_v2 的全文唯一解析。
    """
    if not isinstance(quote, str):
        raise ValueError("evidence_quote 必须是字符串")
    stripped = quote.strip()
    if not (QUOTE_MIN_CHARS <= len(stripped) <= QUOTE_MAX_CHARS):
        raise ValueError(
            f"evidence_quote 长度必须在 {QUOTE_MIN_CHARS}-{QUOTE_MAX_CHARS} 字符")
    if not isinstance(evidence_page, int) or isinstance(evidence_page, bool):
        raise ValueError("evidence_page 必须是整数页码")
    if evidence_page < 1 or evidence_page > len(page_texts):
        raise ValueError("evidence_page 超出页范围")
    on_page = page_texts[evidence_page - 1].count(stripped)
    if on_page == 0:
        raise ValueError("evidence_quote 在指定页未命中")
    if on_page > 1:
        raise ValueError("evidence_quote 在指定页出现多次，不唯一")
    elsewhere = sum(
        text.count(stripped)
        for index, text in enumerate(page_texts)
        if index != evidence_page - 1
    )
    if elsewhere:
        raise ValueError("evidence_quote 跨页出现，不唯一")
    return stripped


def validate_candidate(
    item: dict, *, expected_uid: str, expected_split: str
) -> None:
    """校验候选条目的 paper 归属与 split 和冻结制品一致，失败抛 ValueError。"""
    if item.get("paper_uid") != expected_uid:
        raise ValueError("paper_uid 与论文归属不一致")
    if item.get("split") != expected_split:
        raise ValueError("split 与冻结分配不一致")


def build_items_from_payload(
    payload: Any,
    *,
    paper_uid: str,
    split: str,
    page_texts: List[str],
    qa_id_prefix: str,
    start_seq: int = 1,
) -> Tuple[List[dict], int]:
    """把 LLM payload 转成 v2 候选条目，返回 (合格条目, 被拒条数)。

    过滤规则：question/answer 非空；question_type ∈ 轮换三型；
    evidence_quote 通过唯一校验；归属与 split 一致。qa_id 序号随保留条目递增。
    """
    items: List[dict] = []
    rejected = 0
    seq = start_seq
    for raw in _extract_items(payload):
        question = raw.get("question")
        answer = raw.get("answer")
        qtype = raw.get("question_type")
        if not (isinstance(question, str) and question.strip()):
            rejected += 1
            continue
        if not (isinstance(answer, str) and answer.strip()):
            rejected += 1
            continue
        if qtype not in QUESTION_TYPE_ROTATION:
            rejected += 1
            continue
        try:
            quote = validate_evidence_quote(
                raw.get("evidence_quote"), page_texts, raw.get("evidence_page"))
        except ValueError:
            rejected += 1
            continue
        item = {
            "qa_id": f"{qa_id_prefix}-{seq:03d}",
            "question": question.strip(),
            "ground_truth": answer.strip(),
            "relevant_evidence": [{"paper_uid": paper_uid, "quote": quote}],
            "question_type": qtype,
            "source": CANDIDATE_SOURCE,
            "has_answer": True,
            "reviewed": False,
            "split": split,
            "paper_uid": paper_uid,
            "evidence_quote": quote,
            "evidence_page": raw.get("evidence_page"),
        }
        try:
            validate_candidate(item, expected_uid=paper_uid, expected_split=split)
        except ValueError:
            rejected += 1
            continue
        items.append(item)
        seq += 1
    return items, rejected


def normalize_for_validation(item: dict) -> dict:
    """模拟人工审稿后的合并动作：source 改为 imported_paper、去掉 reviewed 标记。

    供测试断言候选条目「除审稿标记外 schema 与种子集一致」。
    """
    merged = {k: v for k, v in item.items() if k != "reviewed"}
    merged["source"] = "imported_paper"
    return merged


# ---------------------------------------------------------------------------
# 单篇生成（带重试）
# ---------------------------------------------------------------------------

def _default_page_loader(runtime_root: Any) -> Callable[[Any], List[dict]]:
    """创建安全 PDF 页加载器：路径不得逃逸语料根目录。"""
    from app.services.pdf_parser import PDFParser

    parser = PDFParser()
    root = Path(runtime_root).resolve()

    def load(paper: Any) -> List[dict]:
        source = (root / paper.file_path).resolve()
        if not source.is_relative_to(root):
            raise ValueError("论文路径不得逃逸语料根目录")
        if not source.is_file():
            raise FileNotFoundError(f"论文源文件不存在: {paper.file_path}")
        return parser.extract_text(str(source))

    return load


def generate_for_paper(
    paper: Any,
    pages: List[dict],
    *,
    paper_uid: str,
    split: str,
    per_paper: int = PER_PAPER,
    max_attempts: int = 3,
    call_llm: Optional[Callable[[List[Dict[str, str]]], str]] = None,
    retry_sleep: float = 0.0,
    material_budget: Optional[int] = None,
    question_types: Optional[List[str]] = None,
    start_seq: int = 1,
) -> Tuple[List[dict], str, int]:
    """为单篇论文生成候选 QA，返回 (合格条目, 错误信息, 被拒条数)。

    JSON 解析失败或全部条目被过滤时自动重试，最多 max_attempts 次；
    全部失败返回 ([], 原因, 被拒计数)，绝不抛出、绝不写半截 JSON。
    """
    llm = call_llm or _call_llm
    ordered = sorted(pages, key=lambda p: p.get("page_number") or 0)
    page_texts = [(p.get("text") or "") for p in ordered]
    material = build_material(
        paper.title, getattr(paper, "abstract", None), ordered, material_budget)
    prefix = f"gen2-p{paper.id:02d}"

    # Kimi 实测（2026-08-31）：单次请求多条长 JSON 输出易整包返回空；
    # 改为逐条生成（每条独立重试），类型按 QUESTION_TYPE_ROTATION 轮换。
    collected: List[dict] = []
    rejected_total = 0
    last_error = "未知错误"
    requested_types = question_types or [
        QUESTION_TYPE_ROTATION[idx % len(QUESTION_TYPE_ROTATION)]
        for idx in range(per_paper)
    ]
    for idx, qtype in enumerate(requested_types):
        messages = build_messages(paper.title or "(无标题)", material, 1, first_type=qtype)
        for attempt in range(1, max_attempts + 1):
            if attempt > 1 and retry_sleep > 0:
                time.sleep(retry_sleep)
            try:
                raw_text = llm(messages)
                payload = parse_llm_json(raw_text)
            except Exception as e:  # 含 LLM 调用异常与解析失败
                last_error = f"第 {idx + 1} 条第 {attempt} 次调用/解析失败: {e}"
                continue
            items, rejected = build_items_from_payload(
                payload,
                paper_uid=paper_uid,
                split=split,
                page_texts=page_texts,
                qa_id_prefix=prefix,
                start_seq=start_seq + len(collected),
            )
            rejected_total += rejected
            # 只收与本次请求类型一致的条目（类型不匹配不落库，避免重复事实题）
            matched = [it for it in items if it.get("question_type") == qtype]
            if matched:
                collected.append(matched[0])
                break
            last_error = f"第 {idx + 1} 条第 {attempt} 次生成全部被过滤（schema/证据校验未过）"
    if not collected:
        return [], last_error, rejected_total
    return collected, "", rejected_total


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def _open_output(output_path: Path, resume: bool):
    """打开输出文件：resume 且已存在则追加；否则 O_EXCL + 0600 排他创建。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if resume and output_path.is_symlink():
        raise ValueError("resume 输出文件不得为符号链接")
    if resume and output_path.exists():
        mode = stat.S_IMODE(output_path.stat().st_mode)
        if mode != 0o600:
            raise PermissionError("resume 输出文件权限必须精确为 0600")
        return output_path.open("a", encoding="utf-8")
    try:
        descriptor = os.open(
            output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise FileExistsError(
            f"输出文件已存在，拒绝覆盖（使用 --resume 断点续跑）: {output_path}")
    return os.fdopen(descriptor, "w", encoding="utf-8")


def _required_question_types(per_paper: int) -> List[str]:
    if isinstance(per_paper, bool) or not isinstance(per_paper, int) or per_paper < 1:
        raise ValueError("per_paper 必须是正整数")
    return [
        QUESTION_TYPE_ROTATION[index % len(QUESTION_TYPE_ROTATION)]
        for index in range(per_paper)
    ]


def _load_resume_items(
    output_path: Path,
    assignments: List[dict],
    per_paper: int,
) -> Dict[str, List[dict]]:
    """严格读取既有候选；任何损坏、越界或重复都拒绝继续追加。"""
    if output_path.is_symlink():
        raise ValueError("resume 输出文件不得为符号链接")
    mode = stat.S_IMODE(output_path.stat().st_mode)
    if mode != 0o600:
        raise PermissionError("resume 输出文件权限必须精确为 0600")

    split_by_uid = {row["paper_uid"]: row["split"] for row in assignments}
    required = _required_question_types(per_paper)
    allowed_counts = {
        qtype: required.count(qtype) for qtype in QUESTION_TYPE_ROTATION
    }
    by_uid: Dict[str, List[dict]] = {}
    qa_ids: set[str] = set()
    with output_path.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            if not raw_line.strip():
                raise ValueError(f"resume JSONL 第 {line_number} 行为空")
            try:
                item = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"resume JSONL 第 {line_number} 行损坏"
                ) from exc
            if not isinstance(item, dict):
                raise ValueError(f"resume JSONL 第 {line_number} 行不是对象")
            qa_id = item.get("qa_id")
            uid = item.get("paper_uid")
            split = item.get("split")
            qtype = item.get("question_type")
            if not isinstance(qa_id, str) or not qa_id:
                raise ValueError(f"resume JSONL 第 {line_number} 行缺 qa_id")
            if qa_id in qa_ids:
                raise ValueError("resume JSONL 含重复 qa_id")
            if uid not in split_by_uid:
                raise ValueError("resume JSONL paper_uid 不在冻结 split 中")
            if split != split_by_uid[uid]:
                raise ValueError("resume JSONL split 与冻结分配不一致")
            if qtype not in QUESTION_TYPE_ROTATION:
                raise ValueError("resume JSONL question_type 无效")
            existing = by_uid.setdefault(uid, [])
            if sum(row["question_type"] == qtype for row in existing) >= allowed_counts[qtype]:
                raise ValueError("resume JSONL 同论文 question_type 数量超出契约")
            qa_ids.add(qa_id)
            existing.append(item)
    return by_uid


def _missing_question_types(existing: List[dict], per_paper: int) -> List[str]:
    remaining = _required_question_types(per_paper)
    for item in existing:
        remaining.remove(item["question_type"])
    return remaining


def _next_qa_sequence(existing: List[dict], prefix: str) -> int:
    maximum = 0
    marker = prefix + "-"
    for item in existing:
        qa_id = item["qa_id"]
        if qa_id.startswith(marker) and qa_id[len(marker):].isdigit():
            maximum = max(maximum, int(qa_id[len(marker):]))
    return maximum + 1


def generate_all(
    db: Any,
    *,
    splits_path: Any,
    output_path: Any,
    runtime_root: Any,
    per_paper: int = PER_PAPER,
    limit: Optional[int] = None,
    max_attempts: int = 3,
    retry_sleep: float = 0.0,
    material_budget: int = MATERIAL_CHAR_BUDGET,
    call_llm: Optional[Callable[[List[Dict[str, str]]], str]] = None,
    resume: bool = False,
    page_loader: Optional[Callable[[Any], List[dict]]] = None,
) -> Dict[str, Any]:
    """全量生成入口：逐篇生成、逐行写 JSONL（带 flush），失败跳过并计数。

    resume=True 时读取已有输出中的 paper_uid，跳过已完成论文并追加写入；
    否则排他创建输出文件，已存在即抛 FileExistsError。

    返回汇总 dict：总条数、被拒条数、每篇状态、question_type 分布等。
    """
    assignments = load_frozen_splits(splits_path)
    _required_question_types(per_paper)
    output_path = Path(output_path)

    # 断点续跑：严格解析已有条目，只跳过已满足全部类型契约的论文。
    resume_items: Dict[str, List[dict]] = {}
    done_uids: set = set()
    if resume and output_path.exists():
        resume_items = _load_resume_items(output_path, assignments, per_paper)
        done_uids = {
            uid
            for uid, items in resume_items.items()
            if not _missing_question_types(items, per_paper)
        }
        if done_uids:
            print(f"[gen2] 续跑：跳过已完成论文 {len(done_uids)} 篇")

    uids = {row["paper_uid"] for row in assignments}
    uid_map, missing = map_uids_to_papers(db, Path(runtime_root), uids)
    if missing:
        print(f"[gen2] {len(missing)} 篇冻结论文未在 DB 命中，跳过")

    loader = page_loader or _default_page_loader(runtime_root)

    total = 0
    rejected_total = 0
    type_counts: Dict[str, int] = {}
    per_paper_status: List[Dict[str, Any]] = []
    processed = 0

    with _open_output(output_path, resume) as f:
        for row in assignments:
            uid = row["paper_uid"]
            split = row["split"]
            if uid in done_uids:
                continue
            if limit is not None and processed >= limit:
                break
            paper = uid_map.get(uid)
            if paper is None:
                continue  # 已计入 missing
            processed += 1
            existing_items = resume_items.get(uid, [])
            missing_types = _missing_question_types(existing_items, per_paper)
            try:
                pages = loader(paper)
            except Exception as e:
                print(f"[gen2] uid={uid} PDF 页提取失败: {e}")
                per_paper_status.append({
                    "paper_uid": uid, "ok": False, "n": 0,
                    "error": f"页提取失败: {e}",
                })
                continue
            items, error, rejected = generate_for_paper(
                paper, pages,
                paper_uid=uid, split=split,
                per_paper=per_paper, max_attempts=max_attempts,
                call_llm=call_llm, retry_sleep=retry_sleep,
                material_budget=material_budget,
                question_types=missing_types,
                start_seq=_next_qa_sequence(
                    existing_items, f"gen2-p{paper.id:02d}"
                ))
            rejected_total += rejected
            if error:
                print(f"[gen2] uid={uid}《{(paper.title or '')[:40]}》失败: {error}")
                per_paper_status.append({
                    "paper_uid": uid, "ok": False, "n": 0, "error": error})
            else:
                for it in items:
                    f.write(json.dumps(it, ensure_ascii=False) + "\n")
                f.flush()  # 逐篇落盘，中途崩溃不丢已完成部分
                total += len(items)
                for it in items:
                    type_counts[it["question_type"]] = \
                        type_counts.get(it["question_type"], 0) + 1
                print(f"[gen2] uid={uid}《{(paper.title or '')[:40]}》"
                      f"生成 {len(items)} 条（拒绝 {rejected} 条）")
                per_paper_status.append({
                    "paper_uid": uid, "ok": True, "n": len(items),
                    "rejected": rejected})

    return {
        "total": total,
        "rejected": rejected_total,
        "type_counts": type_counts,
        "per_paper": per_paper_status,
        "n_ok": sum(1 for s in per_paper_status if s["ok"]),
        "n_fail": sum(1 for s in per_paper_status if not s["ok"]),
        "n_missing": len(missing),
        "n_skipped": len(done_uids),
        "output": str(output_path),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m eval.generate_qa_v2",
        description="Batch 22L：Benchmark v2 QA 候选生成（带证据页唯一校验）")
    parser.add_argument("--splits", default=str(DEFAULT_SPLITS_PATH),
                        help="T1 冻结 split 制品路径")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_PATH),
                        help="候选集输出路径（默认 eval/private/qa_v2_candidates.jsonl）")
    parser.add_argument("--resume", action="store_true",
                        help="断点续跑：跳过已完成论文，追加写入已有输出文件")
    parser.add_argument("--limit", type=int, default=None,
                        help="最多处理的论文篇数（冒烟用，默认不限）")
    parser.add_argument("--per-paper", type=int, default=PER_PAPER,
                        help="每篇论文生成的 QA 条数（默认 3）")
    parser.add_argument("--max-attempts", type=int, default=3,
                        help="单篇 JSON 解析/校验失败的最大尝试次数（默认 3）")
    parser.add_argument("--retry-sleep", type=float, default=10.0,
                        help="重试前休眠秒数，应对 429 限流（默认 10，0 不休眠）")
    parser.add_argument("--material-chars", type=int, default=MATERIAL_CHAR_BUDGET,
                        help="每篇送给 LLM 的按页素材字符预算（默认 12000）")
    parser.add_argument(
        "--confirm-content-egress",
        action="store_true",
        help="确认把真实论文摘录发送给外部 LLM；缺少时 CLI 在读取配置前退出",
    )
    return parser


def _require_private_cli_path(path: Path, label: str) -> Path:
    private_root = (Path(__file__).resolve().parent / "private").resolve()
    resolved = path.resolve()
    if resolved == private_root or private_root not in resolved.parents:
        raise ValueError(f"{label} 必须位于 backend/eval/private/ 内")
    return resolved


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if not args.confirm_content_egress:
        print(
            "[gen2] REFUSED: 缺少 --confirm-content-egress，未读取配置或发送内容",
            file=sys.stderr,
        )
        return 2
    try:
        splits_path = _require_private_cli_path(Path(args.splits), "splits")
        output_path = _require_private_cli_path(Path(args.output), "output")
    except ValueError as exc:
        print(f"[gen2] REFUSED: {exc}", file=sys.stderr)
        return 2

    from app.core.config import config  # 延迟导入，连接真实配置
    from app.database import SessionLocal

    t0 = time.time()
    db = SessionLocal()
    try:
        summary = generate_all(
            db,
            splits_path=splits_path,
            output_path=output_path,
            runtime_root=config.runtime_root,
            per_paper=args.per_paper,
            limit=args.limit,
            max_attempts=args.max_attempts,
            retry_sleep=args.retry_sleep,
            material_budget=args.material_chars,
            resume=args.resume,
        )
    finally:
        db.close()

    elapsed = time.time() - t0
    print("\n========== 生成汇总 ==========")
    print(f"成功论文 {summary['n_ok']} 篇，失败 {summary['n_fail']} 篇，"
          f"未命中 {summary['n_missing']} 篇，续跑跳过 {summary['n_skipped']} 篇")
    print(f"候选 QA 总数: {summary['total']}（被拒 {summary['rejected']} 条）")
    print(f"question_type 分布: "
          + ", ".join(f"{k}={v}" for k, v in sorted(summary["type_counts"].items())))
    print(f"输出文件: {summary['output']}")
    print(f"耗时 {elapsed:.1f}s")
    print("提示: 候选集带 source=llm_generated / reviewed=false 标记，"
          "请人工审稿后再合并进冻结集")
    # 全部失败时退出码 1，供脚本化调用方感知
    return 0 if summary["total"] > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
