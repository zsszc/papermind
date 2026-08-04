"""LLM 辅助生成 RAG 评测 QA 候选集。

用法（在 backend/ 目录下）：
    env -u PYTHONPATH venv/bin/python -m eval.generate_qa                 # 全量：id=4..19 + 跨论文对比题
    env -u PYTHONPATH venv/bin/python -m eval.generate_qa --paper-ids 4 5 # 只跑指定论文
    env -u PYTHONPATH venv/bin/python -m eval.generate_qa --per-paper 3   # 每篇 3 条
    env -u PYTHONPATH venv/bin/python -m eval.generate_qa --dry-run       # 不调 LLM，只打印素材规模

流程：
1. 从 SQLite 读取指定论文（默认 id=4..19，id=1 已被种子集覆盖）及其前若干 chunk 文本；
2. 构造 prompt 调 llm_service.chat_completion_sync(json_mode=True) 生成候选 QA；
3. 严格解析 JSON（支持 ```json 围栏容错），逐条做 schema 与「摘录必须逐字命中原文」校验，
   失败自动重试，仍失败则跳过该篇，不阻塞其他论文；
4. 合格条目加 source="llm_generated" / reviewed=false 标记写入 qa_candidates.jsonl。

注意：dataset.SOURCES 暂未收录 "llm_generated"，因此候选集不能直接被
validate_dataset 接受；人工审稿通过、合并进种子集时，应把 source 改为
"imported_paper" 并去掉 reviewed 标记。normalize_for_validation() 即模拟这一步，
供测试断言「除审稿标记外 schema 与种子集一致」。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# 候选集默认输出路径（backend/eval/dataset/qa_candidates.jsonl）
DEFAULT_OUTPUT_PATH = Path(__file__).resolve().parent / "dataset" / "qa_candidates.jsonl"

# 默认处理的论文 id 区间（id=1..3 跳过：1 已被种子集覆盖，2/3 为早期导入）
DEFAULT_PAPER_IDS = list(range(4, 20))

# 每篇论文送给 LLM 的素材字符预算（远低于 llm_service.max_total_chars=200000，
# 自行控制 token 消耗；llm_service 的 _truncate_messages 仍是最后兜底）
MATERIAL_CHAR_BUDGET = 9000

# LLM 允许生成的正例问题类型（out_of_scope 负例需人工构造，不交给 LLM）
ALLOWED_TYPES = {"method_detail", "experiment_data", "factoid", "summary", "comparison"}

# 候选集的审稿标记来源值（合并进种子集时由人工改为 imported_paper）
CANDIDATE_SOURCE = "llm_generated"


# ---------------------------------------------------------------------------
# LLM 调用（延迟导入，保证加载本模块不触发配置读取与网络）
# ---------------------------------------------------------------------------

def _call_llm(messages: List[Dict[str, str]]) -> str:
    """调用 llm_service 生成 JSON（测试中 monkeypatch 本函数以避免真实 API 调用）。"""
    from app.services.llm import llm_service  # 延迟导入

    return llm_service.chat_completion_sync(messages, json_mode=True)


# ---------------------------------------------------------------------------
# 素材构造
# ---------------------------------------------------------------------------

def build_material(paper: Any, chunks: List[Any], budget: Optional[int] = None) -> str:
    """拼接待生成论文的素材文本：标题 + 摘要（如有）+ 前若干 chunk，控制在 budget 内。

    参数：
        paper: Paper ORM 对象（使用 title / abstract 字段）。
        chunks: 该论文的 Chunk 列表（需已按 chunk_index 升序）。
        budget: 字符预算，超出即停止追加 chunk；None 时用全局 MATERIAL_CHAR_BUDGET。
    """
    if budget is None:
        budget = MATERIAL_CHAR_BUDGET
    parts: List[str] = [f"论文标题: {paper.title or '(无标题)'}"]
    abstract = (getattr(paper, "abstract", None) or "").strip()
    if abstract:
        parts.append(f"摘要: {abstract}")
    used = sum(len(p) for p in parts)
    for ch in chunks:
        content = (ch.content or "").strip()
        if not content:
            continue
        if used + len(content) > budget:
            break
        parts.append(content)
        used += len(content)
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "你是学术评测数据集构建助手。根据给定论文内容设计「可由该论文内容直接回答」的"
    "问答评测题。严格只输出 JSON 对象，不要输出任何其他文字。"
)

_USER_PROMPT_TEMPLATE = """下面是论文「{title}」的内容摘录（可能只包含论文开头部分）。

---论文内容开始---
{material}
---论文内容结束---

请基于以上内容设计 {n} 条评测 QA，要求：
1. 每条问题必须能由上述论文内容回答，不得涉及内容中没有的信息；
2. question_type 从 method_detail / experiment_data / factoid / summary 中选择
   （method_detail 与 experiment_data 优先，factoid、summary 各至多 1 条；
   仅当内容中明确对比了多种方法时才可用 comparison）；
3. question 用中文，具体、聚焦（避免「这篇论文讲了什么」之外的空泛问法）；
4. ground_truth 为参考答案要点：用「、」分隔的若干短小关键短语（数值、模块名、
   结论词），总长不超过 120 字，供关键词命中率评测使用；
5. excerpts 为 2~3 个从上述论文内容中【逐字原样复制】的英文片段（每段 3~10 个词），
   用于定位答案所在的原文段落——必须与原文完全一致，不得改写或翻译；
6. 只输出如下 JSON：
{{"items": [{{"question": "...", "question_type": "...", "ground_truth": "...", "excerpts": ["...", "..."]}}]}}
"""

_CROSS_SYSTEM_PROMPT = (
    "你是学术评测数据集构建助手。根据多篇论文的简介设计「跨论文对比」评测题，"
    "问题必须能由所涉两篇论文的内容共同回答。严格只输出 JSON 对象。"
)

_CROSS_USER_TEMPLATE = """以下是 {n_papers} 篇论文（均为病理图像 / 多实例学习方向）的标题与开头摘录：

{overviews}

请设计 {n} 条跨论文 comparison 评测题，要求：
1. 每题对比恰好两篇论文（方法思路、技术路线或应用场景的差异），
   且答案要点能在对应论文的摘录中找到依据；
2. question 用中文，明确指出对比的两篇论文（可用简称）；
3. ground_truth 为用「、」分隔的短小关键要点短语，总长不超过 150 字；
4. 每题给出 locators：对所涉每篇论文给 paper_id 与 2 个【逐字原样复制】自
   该论文摘录的英文片段（3~10 个词），必须与原文完全一致；
5. 只输出如下 JSON：
{{"items": [{{"question": "...", "question_type": "comparison", "ground_truth": "...",
  "locators": [{{"paper_id": 4, "excerpts": ["..."]}}, {{"paper_id": 7, "excerpts": ["..."]}}]}}]}}
"""


def build_messages(title: str, material: str, n: int) -> List[Dict[str, str]]:
    """构造单篇论文 QA 生成的对话消息。"""
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": _USER_PROMPT_TEMPLATE.format(
            title=title, material=material, n=n)},
    ]


def build_cross_messages(overviews: str, n_papers: int, n: int) -> List[Dict[str, str]]:
    """构造跨论文 comparison 题生成的对话消息。"""
    return [
        {"role": "system", "content": _CROSS_SYSTEM_PROMPT},
        {"role": "user", "content": _CROSS_USER_TEMPLATE.format(
            overviews=overviews, n_papers=n_papers, n=n)},
    ]


# ---------------------------------------------------------------------------
# LLM 输出解析（绝不允许半截 JSON 进入候选集）
# ---------------------------------------------------------------------------

def parse_llm_json(text: str) -> Any:
    """从 LLM 输出中解析 JSON，容错 ```json 围栏与首尾杂质。

    异常：
        ValueError: 无法解析出完整 JSON（调用方负责重试/降级）。
    """
    if not text or not text.strip():
        raise ValueError("LLM 返回为空")
    cleaned = text.strip()
    # 去掉 markdown 代码围栏
    fence = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1).strip()
    # 截取第一个 {/[ 到最后一个 }/]，丢弃首尾杂质
    start_candidates = [i for i in (cleaned.find("{"), cleaned.find("[")) if i >= 0]
    if not start_candidates:
        raise ValueError("LLM 输出中找不到 JSON 起始符")
    start = min(start_candidates)
    end = max(cleaned.rfind("}"), cleaned.rfind("]"))
    if end <= start:
        raise ValueError("LLM 输出中 JSON 不完整")
    try:
        return json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON 解析失败: {e}") from e


def _extract_items(payload: Any) -> List[dict]:
    """从解析后的 payload 中取出候选条目列表（兼容 {"items": [...]} 与顶层列表）。"""
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        items = payload.get("items")
        if isinstance(items, list):
            return [x for x in items if isinstance(x, dict)]
        # 兜底：取第一个 list 类型的值
        for v in payload.values():
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
    return []


# ---------------------------------------------------------------------------
# 摘录校验与条目构造
# ---------------------------------------------------------------------------

_WS_RE = re.compile(r"\s+")


def _norm(text: str) -> str:
    """大小写不敏感 + 空白折叠，用于摘录与原文的包含匹配。"""
    return _WS_RE.sub(" ", (text or "").lower()).strip()


def _verify_excerpts(excerpts: Any, corpus_norm: str, max_keep: int = 3) -> List[str]:
    """过滤出逐字命中原文的摘录片段（保持 LLM 给出的原始大小写）。"""
    if not isinstance(excerpts, list):
        return []
    verified: List[str] = []
    for ex in excerpts:
        if not isinstance(ex, str):
            continue
        ex = ex.strip()
        if len(ex) < 8:  # 太短没有定位价值
            continue
        if _norm(ex) in corpus_norm and ex not in verified:
            verified.append(ex)
        if len(verified) >= max_keep:
            break
    return verified


def build_items_from_payload(
    payload: Any,
    paper_id: int,
    corpus_norm: str,
    qa_id_prefix: str,
    start_seq: int = 1,
) -> List[dict]:
    """把 LLM payload 转成符合种子集 schema 的候选条目（含审稿标记）。

    过滤规则：
    - question / ground_truth 必须为非空字符串；
    - question_type 必须在 ALLOWED_TYPES 中（LLM 不许生成负例）；
    - 至少有 1 条摘录逐字命中原文（保证 resolve_relevant_chunks 可解析）。

    返回：
        合格条目列表（可能为空）；qa_id 形如 f"{qa_id_prefix}-{seq:03d}"。
    """
    items: List[dict] = []
    seq = start_seq
    for raw in _extract_items(payload):
        question = raw.get("question")
        ground_truth = raw.get("ground_truth")
        qtype = raw.get("question_type")
        if not (isinstance(question, str) and question.strip()):
            continue
        if not (isinstance(ground_truth, str) and ground_truth.strip()):
            continue
        if qtype not in ALLOWED_TYPES:
            continue
        excerpts = _verify_excerpts(raw.get("excerpts"), corpus_norm)
        if not excerpts:
            continue
        items.append({
            "qa_id": f"{qa_id_prefix}-{seq:03d}",
            "question": question.strip(),
            "ground_truth": ground_truth.strip(),
            "relevant_chunks": [{"paper_id": paper_id, "keywords": excerpts}],
            "question_type": qtype,
            "source": CANDIDATE_SOURCE,
            "has_answer": True,
            "reviewed": False,
        })
        seq += 1
    return items


def normalize_for_validation(item: dict) -> dict:
    """模拟人工审稿后的合并动作：source 改为 imported_paper、去掉 reviewed 标记。

    供测试断言候选条目「除审稿标记外，schema 与种子集完全一致」。
    """
    merged = {k: v for k, v in item.items() if k != "reviewed"}
    merged["source"] = "imported_paper"
    return merged


# ---------------------------------------------------------------------------
# 单篇 / 跨论文生成（带重试）
# ---------------------------------------------------------------------------

def generate_for_paper(
    paper: Any,
    chunks: List[Any],
    per_paper: int = 4,
    max_attempts: int = 3,
    call_llm: Optional[Callable[[List[Dict[str, str]]], str]] = None,
    retry_sleep: float = 0.0,
) -> Tuple[List[dict], str]:
    """为单篇论文生成候选 QA，返回 (条目列表, 错误信息)。

    JSON 解析失败或全部条目被过滤时自动重试，最多 max_attempts 次，
    重试前休眠 retry_sleep 秒（应对 429 限流）；全部失败返回 ([], 原因)，
    绝不抛出、绝不写半截 JSON。
    """
    llm = call_llm or _call_llm
    corpus_norm = _norm("\n\n".join(ch.content or "" for ch in chunks))
    material = build_material(paper, chunks)
    messages = build_messages(paper.title or "(无标题)", material, per_paper)
    prefix = f"gen-p{paper.id:02d}"

    last_error = "未知错误"
    for attempt in range(1, max_attempts + 1):
        if attempt > 1 and retry_sleep > 0:
            time.sleep(retry_sleep)
        try:
            raw_text = llm(messages)
            payload = parse_llm_json(raw_text)
        except Exception as e:  # 含 LLM 调用异常与解析失败
            last_error = f"第 {attempt} 次调用/解析失败: {e}"
            continue
        items = build_items_from_payload(payload, paper.id, corpus_norm, prefix)
        if items:
            return items, ""
        last_error = f"第 {attempt} 次生成全部被过滤（schema/摘录校验未过）"
    return [], last_error


def generate_cross_paper(
    papers_with_chunks: List[Tuple[Any, List[Any]]],
    n: int = 4,
    max_attempts: int = 3,
    call_llm: Optional[Callable[[List[Dict[str, str]]], str]] = None,
    retry_sleep: float = 0.0,
) -> Tuple[List[dict], str]:
    """生成跨论文 comparison 题（每题对比恰好两篇论文，价值最高的题型）。"""
    llm = call_llm or _call_llm
    # 每篇取标题 + 首个 chunk 前 600 字符作为简介
    overviews_parts: List[str] = []
    corpus_by_id: Dict[int, str] = {}
    for paper, chunks in papers_with_chunks:
        intro = ""
        for ch in chunks:
            if (ch.content or "").strip():
                intro = ch.content.strip()[:600]
                break
        overviews_parts.append(f"[paper_id={paper.id}] {paper.title or '(无标题)'}\n{intro}")
        corpus_by_id[paper.id] = _norm("\n\n".join(ch.content or "" for ch in chunks))
    messages = build_cross_messages(
        "\n\n".join(overviews_parts), len(papers_with_chunks), n)

    last_error = "未知错误"
    for attempt in range(1, max_attempts + 1):
        if attempt > 1 and retry_sleep > 0:
            time.sleep(retry_sleep)
        try:
            payload = parse_llm_json(llm(messages))
        except Exception as e:
            last_error = f"第 {attempt} 次调用/解析失败: {e}"
            continue
        items: List[dict] = []
        for seq, raw in enumerate(_extract_items(payload), start=1):
            if raw.get("question_type") != "comparison":
                continue
            question = raw.get("question")
            ground_truth = raw.get("ground_truth")
            if not (isinstance(question, str) and question.strip()):
                continue
            if not (isinstance(ground_truth, str) and ground_truth.strip()):
                continue
            locators: List[dict] = []
            for loc in raw.get("locators") or []:
                if not isinstance(loc, dict):
                    continue
                pid = loc.get("paper_id")
                if not isinstance(pid, int) or pid not in corpus_by_id:
                    continue
                verified = _verify_excerpts(loc.get("excerpts"), corpus_by_id[pid])
                if verified:
                    locators.append({"paper_id": pid, "keywords": verified})
            if len(locators) < 2:  # 跨论文对比必须两篇都能定位
                continue
            items.append({
                "qa_id": f"gen-cross-{seq:03d}",
                "question": question.strip(),
                "ground_truth": ground_truth.strip(),
                "relevant_chunks": locators,
                "question_type": "comparison",
                "source": CANDIDATE_SOURCE,
                "has_answer": True,
                "reviewed": False,
            })
        if items:
            return items, ""
        last_error = f"第 {attempt} 次生成全部被过滤（schema/摘录校验未过）"
    return [], last_error


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def generate_all(
    db: Any,
    paper_ids: List[int],
    per_paper: int,
    output_path: Path,
    include_cross: bool = True,
    cross_n: int = 4,
    max_attempts: int = 3,
    material_budget: int = MATERIAL_CHAR_BUDGET,
    retry_sleep: float = 0.0,
    call_llm: Optional[Callable[[List[Dict[str, str]]], str]] = None,
    resume: bool = False,
) -> Dict[str, Any]:
    """全量生成入口：逐篇生成 + 跨论文对比题，逐行写 JSONL（带 flush）。

    resume=True 时读取已有输出文件，跳过已完成的论文与跨论文题，
    并以追加模式写入（429 频发环境下可反复重跑直至收敛）。

    返回汇总 dict：每篇成功/失败、总条数、question_type 分布、输出路径。
    """
    from app.models import Chunk, Paper  # 延迟导入，避免加载模块即连库

    global MATERIAL_CHAR_BUDGET
    MATERIAL_CHAR_BUDGET = material_budget  # build_material 默认值随 CLI 调整

    # 断点续跑：从已有输出中解析已完成的论文 id 与跨论文题
    done_paper_ids: set = set()
    done_cross = False
    write_mode = "w"
    if resume and output_path.exists():
        with output_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                m = re.match(r"gen-p(\d+)-", item.get("qa_id", ""))
                if m:
                    done_paper_ids.add(int(m.group(1)))
                if item.get("question_type") == "comparison":
                    done_cross = True
        if done_paper_ids or done_cross:
            write_mode = "a"
            print(f"[gen] 续跑：跳过已完成论文 {sorted(done_paper_ids)}"
                  + ("；跨论文题已完成" if done_cross else ""))

    papers_with_chunks: List[Tuple[Any, List[Any]]] = []
    for pid in paper_ids:
        if pid in done_paper_ids:
            continue
        paper = db.query(Paper).filter(Paper.id == pid).first()
        if paper is None:
            print(f"[gen] 论文 id={pid} 不存在，跳过")
            continue
        chunks = (db.query(Chunk).filter(Chunk.paper_id == pid)
                  .order_by(Chunk.chunk_index).all())
        if not chunks:
            print(f"[gen] 论文 id={pid} 无 chunk，跳过")
            continue
        papers_with_chunks.append((paper, chunks))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    per_paper_status: List[Dict[str, Any]] = []
    type_counts: Dict[str, int] = {}
    total = 0

    with output_path.open(write_mode, encoding="utf-8") as f:
        for paper, chunks in papers_with_chunks:
            items, error = generate_for_paper(
                paper, chunks, per_paper=per_paper,
                max_attempts=max_attempts, call_llm=call_llm,
                retry_sleep=retry_sleep)
            if error:
                print(f"[gen] 论文 id={paper.id}《{(paper.title or '')[:40]}》失败: {error}")
                per_paper_status.append(
                    {"paper_id": paper.id, "ok": False, "n": 0, "error": error})
            else:
                for it in items:
                    f.write(json.dumps(it, ensure_ascii=False) + "\n")
                f.flush()  # 逐篇落盘，中途崩溃不丢已完成部分
                total += len(items)
                for it in items:
                    type_counts[it["question_type"]] = \
                        type_counts.get(it["question_type"], 0) + 1
                print(f"[gen] 论文 id={paper.id}《{(paper.title or '')[:40]}》"
                      f"生成 {len(items)} 条")
                per_paper_status.append(
                    {"paper_id": paper.id, "ok": True, "n": len(items)})

        if include_cross and not done_cross and len(papers_with_chunks) >= 2:
            cross_items, cross_error = generate_cross_paper(
                papers_with_chunks, n=cross_n,
                max_attempts=max_attempts, call_llm=call_llm,
                retry_sleep=retry_sleep)
            if cross_error:
                print(f"[gen] 跨论文 comparison 题生成失败: {cross_error}")
            else:
                for it in cross_items:
                    f.write(json.dumps(it, ensure_ascii=False) + "\n")
                f.flush()
                total += len(cross_items)
                type_counts["comparison"] = \
                    type_counts.get("comparison", 0) + len(cross_items)
                print(f"[gen] 跨论文 comparison 题生成 {len(cross_items)} 条")

    return {
        "total": total,
        "type_counts": type_counts,
        "per_paper": per_paper_status,
        "n_ok": sum(1 for s in per_paper_status if s["ok"]),
        "n_fail": sum(1 for s in per_paper_status if not s["ok"]),
        "output": str(output_path),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m eval.generate_qa",
        description="LLM 辅助生成评测 QA 候选集（人工审稿后再合并进种子集）")
    parser.add_argument("--paper-ids", type=int, nargs="+", default=DEFAULT_PAPER_IDS,
                        help="要处理的论文 id（默认 4..19）")
    parser.add_argument("--per-paper", type=int, default=4,
                        help="每篇论文生成的 QA 条数（默认 4）")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_PATH),
                        help="候选集输出路径（默认 eval/dataset/qa_candidates.jsonl）")
    parser.add_argument("--max-attempts", type=int, default=3,
                        help="单篇 JSON 解析/校验失败的最大尝试次数（默认 3）")
    parser.add_argument("--retry-sleep", type=float, default=10.0,
                        help="重试前的休眠秒数，应对 429 限流（默认 10，0 表示不休眠）")
    parser.add_argument("--material-chars", type=int, default=9000,
                        help="每篇送给 LLM 的素材字符预算（默认 9000）")
    parser.add_argument("--no-cross-paper", action="store_true",
                        help="跳过跨论文 comparison 题生成")
    parser.add_argument("--cross-n", type=int, default=4,
                        help="跨论文 comparison 题目标条数（默认 4）")
    parser.add_argument("--dry-run", action="store_true",
                        help="不调用 LLM，只打印每篇素材规模后退出")
    parser.add_argument("--resume", action="store_true",
                        help="断点续跑：跳过输出文件中已完成的论文与跨论文题，追加写入")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.dry_run:
        # 只统计素材规模，不触碰 LLM
        from app.database import SessionLocal  # 延迟导入
        from app.models import Chunk, Paper

        db = SessionLocal()
        try:
            for pid in args.paper_ids:
                paper = db.query(Paper).filter(Paper.id == pid).first()
                if paper is None:
                    print(f"[dry-run] id={pid} 不存在")
                    continue
                chunks = (db.query(Chunk).filter(Chunk.paper_id == pid)
                          .order_by(Chunk.chunk_index).all())
                material = build_material(paper, chunks, args.material_chars)
                print(f"[dry-run] id={pid} chunks={len(chunks)} "
                      f"素材字符数={len(material)} 标题《{(paper.title or '')[:40]}》")
        finally:
            db.close()
        return 0

    from app.database import SessionLocal  # 延迟导入，连接真实 SQLite（只读）

    t0 = time.time()
    db = SessionLocal()
    try:
        summary = generate_all(
            db,
            paper_ids=args.paper_ids,
            per_paper=args.per_paper,
            output_path=Path(args.output),
            include_cross=not args.no_cross_paper,
            cross_n=args.cross_n,
            max_attempts=args.max_attempts,
            material_budget=args.material_chars,
            retry_sleep=args.retry_sleep,
            resume=args.resume,
        )
    finally:
        db.close()

    elapsed = time.time() - t0
    print("\n========== 生成汇总 ==========")
    print(f"成功论文 {summary['n_ok']} 篇，失败 {summary['n_fail']} 篇")
    print(f"候选 QA 总数: {summary['total']}")
    print(f"question_type 分布: "
          + ", ".join(f"{k}={v}" for k, v in sorted(summary["type_counts"].items())))
    print(f"输出文件: {summary['output']}")
    print(f"耗时 {elapsed:.1f}s")
    print("提示: 候选集带 source=llm_generated / reviewed=false 标记，"
          "请人工审稿后再合并进种子集（合并时 source 改为 imported_paper）")
    # 全部失败时退出码 1，供脚本化调用方感知
    return 0 if summary["total"] > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
