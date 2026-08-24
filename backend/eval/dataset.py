"""RAG 评测数据集加载与校验。

数据格式：JSONL，每行一条 QA。schema 详见 backend/eval/dataset/README.md。

用法：
    from eval.dataset import load_dataset, validate_dataset, resolve_relevant_chunks

    items = load_dataset()                # 默认加载种子集
    validate_dataset(items)               # 校验 schema，不通过抛 ValueError
    ids = resolve_relevant_chunks(db, items[0])  # 解析期望命中的 chunk id
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Optional, Union

# 默认种子集路径
DEFAULT_SEED_PATH = Path(__file__).resolve().parent / "dataset" / "qa_seed.jsonl"

# 所有数据集共享的必填字段；相关性标注允许旧 relevant_chunks 或新 relevant_evidence。
REQUIRED_FIELDS = {
    "qa_id",
    "question",
    "ground_truth",
    "question_type",
    "source",
    "has_answer",
}

# 合法的问题类型
QUESTION_TYPES = {
    "factoid",        # 事实型（单一事实/数值）
    "summary",        # 总结概括型
    "comparison",     # 对比型
    "method_detail",  # 方法细节型
    "experiment_data",  # 实验数据型
    "out_of_scope",   # 库中无答案的负例
}

# 合法的来源
SOURCES = {
    "demo_paper",      # 基于示例论文手工编写
    "synthetic",       # 人工构造（如负例）
    "imported_paper",  # 基于后续导入的真实论文
}


def load_dataset(path: Optional[Union[str, Path]] = None) -> list[dict]:
    """加载 JSONL 评测数据集，返回 list[dict]。

    参数：
        path: JSONL 文件路径，缺省为内置种子集 qa_seed.jsonl。

    异常：
        FileNotFoundError: 文件不存在。
        ValueError: 某一行不是合法 JSON 或不是 JSON 对象（报错信息带行号）。
    """
    path = Path(path) if path else DEFAULT_SEED_PATH
    if not path.exists():
        raise FileNotFoundError(f"数据集文件不存在: {path}")

    items: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue  # 跳过空行
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"{path.name} 第 {lineno} 行不是合法 JSON: {e}") from e
            if not isinstance(obj, dict):
                raise ValueError(f"{path.name} 第 {lineno} 行不是 JSON 对象")
            items.append(obj)
    return items


def _validate_locator(locator: Any, where: str) -> list[str]:
    """校验单条 relevant_chunks 定位信息。"""
    errors: list[str] = []
    if not isinstance(locator, dict):
        return [f"{where}: relevant_chunks 元素必须是对象"]
    paper_id = locator.get("paper_id")
    if not isinstance(paper_id, int) or isinstance(paper_id, bool):
        errors.append(f"{where}: locator.paper_id 必须是整数")
    section = locator.get("section")
    keywords = locator.get("keywords")
    if section is None and not keywords:
        errors.append(f"{where}: locator 至少需要 section 或 keywords 之一")
    if section is not None and not isinstance(section, str):
        errors.append(f"{where}: locator.section 必须是字符串")
    if keywords is not None and (
        not isinstance(keywords, list)
        or any(not isinstance(k, str) for k in keywords)
    ):
        errors.append(f"{where}: locator.keywords 必须是字符串列表")
    return errors


def _validate_evidence(evidence: Any, where: str) -> list[str]:
    """校验稳定 evidence qrels 定位信息。"""
    if not isinstance(evidence, dict):
        return [f"{where}: relevant_evidence 元素必须是对象"]
    errors: list[str] = []
    paper_uid = evidence.get("paper_uid")
    quote = evidence.get("quote")
    valid_doi = isinstance(paper_uid, str) and paper_uid.startswith("doi:") \
        and bool(paper_uid.removeprefix("doi:").strip())
    valid_sha = isinstance(paper_uid, str) and re.fullmatch(
        r"sha256:[0-9a-fA-F]{64}", paper_uid
    ) is not None
    if not (valid_doi or valid_sha):
        errors.append(
            f"{where}: evidence.paper_uid 必须使用 doi:<doi> 或 sha256:<64hex>"
        )
    if not isinstance(quote, str) or len(quote.strip()) < 20:
        errors.append(f"{where}: evidence.quote 至少 20 个字符")
    return errors


def validate_dataset(items: list[dict]) -> None:
    """校验数据集 schema 完整性。

    校验规则：
    - 每条样本为 dict 且包含全部必填字段；
    - qa_id 为非空字符串且全局唯一；
    - question / ground_truth 为非空字符串；
    - question_type ∈ QUESTION_TYPES，source ∈ SOURCES；
    - has_answer 为布尔值；负例（has_answer=False）的 relevant_chunks 必须为空；
    - relevant_chunks 为列表，元素含整数 paper_id 及 section/keywords 至少其一。

    异常：
        ValueError: 任一校验不通过，错误信息汇总全部问题。
    """
    errors: list[str] = []
    seen_ids: set[str] = set()

    for idx, item in enumerate(items, start=1):
        where = f"第 {idx} 条"
        if not isinstance(item, dict):
            errors.append(f"{where}: 样本必须是 JSON 对象")
            continue

        qa_id = item.get("qa_id")
        if isinstance(qa_id, str) and qa_id:
            where = f"第 {idx} 条 (qa_id={qa_id})"

        missing = REQUIRED_FIELDS - set(item.keys())
        if missing:
            errors.append(f"{where}: 缺少必填字段 {sorted(missing)}")
            continue  # 缺字段时跳过后续类型校验，避免级联误报

        if not isinstance(qa_id, str) or not qa_id:
            errors.append(f"{where}: qa_id 必须是非空字符串")
        elif qa_id in seen_ids:
            errors.append(f"{where}: qa_id 重复")
        else:
            seen_ids.add(qa_id)

        for field in ("question", "ground_truth"):
            if not isinstance(item[field], str) or not item[field].strip():
                errors.append(f"{where}: {field} 必须是非空字符串")

        if item["question_type"] not in QUESTION_TYPES:
            errors.append(
                f"{where}: 非法 question_type={item['question_type']!r}，"
                f"合法值: {sorted(QUESTION_TYPES)}"
            )
        if item["source"] not in SOURCES:
            errors.append(
                f"{where}: 非法 source={item['source']!r}，合法值: {sorted(SOURCES)}"
            )

        has_answer = item["has_answer"]
        if not isinstance(has_answer, bool):
            errors.append(f"{where}: has_answer 必须是布尔值")

        has_legacy = "relevant_chunks" in item
        has_evidence = "relevant_evidence" in item
        if has_legacy == has_evidence:
            errors.append(
                f"{where}: 必须且只能提供 relevant_chunks 或 relevant_evidence 之一"
            )
            continue

        if has_legacy:
            chunks = item["relevant_chunks"]
            if not isinstance(chunks, list):
                errors.append(f"{where}: relevant_chunks 必须是列表")
            else:
                if has_answer is False and chunks:
                    errors.append(f"{where}: 负例（has_answer=false）不应标注 relevant_chunks")
                for locator in chunks:
                    errors.extend(_validate_locator(locator, where))
        else:
            evidence_items = item["relevant_evidence"]
            if not isinstance(evidence_items, list):
                errors.append(f"{where}: relevant_evidence 必须是列表")
            else:
                if has_answer is True and not evidence_items:
                    errors.append(f"{where}: 正例 relevant_evidence 不得为空")
                if has_answer is False and evidence_items:
                    errors.append(f"{where}: 负例不应标注 relevant_evidence")
                for evidence in evidence_items:
                    errors.extend(_validate_evidence(evidence, where))

    if errors:
        raise ValueError("数据集校验失败:\n" + "\n".join(f"  - {e}" for e in errors))


def resolve_relevant_chunks(db, entry: dict, runtime_root: Optional[Path] = None) -> list[str]:
    """将一条 QA 的 relevant_chunks 定位信息解析为候选 chunk id 列表。

    匹配策略（骨架实现，供后续检索指标计算使用）：
    - 按 locator.paper_id 过滤 chunks 表；
    - locator.section 与 Chunk.section_title 做大小写不敏感的包含匹配（+2 分），
      同时 section 字符串本身也作为关键词在 content 中匹配（+1 分），
      以兼容 section_title 为 NULL 的库（当前示例论文即如此）；
    - locator.keywords 中每个关键词在 content 中大小写不敏感命中 +1 分；
    - 返回得分 > 0 的 chunk，id 形如 "p{paper_id}_c{chunk_index}"，按分数降序排列。

    参数：
        db: SQLAlchemy Session。
        entry: 一条 QA 样本（dict）。

    返回：
        去重后的 chunk id 列表，如 ["p1_c2", "p1_c0"]。负例返回空列表。
    """
    from app.models import Chunk, Paper  # 延迟导入，保证加载/校验可脱离 app 使用

    if "relevant_evidence" in entry:
        from app.core.config import config
        from eval.private_benchmark import normalize_doi, sha256_file

        root = Path(runtime_root) if runtime_root is not None else config.runtime_root
        resolved: list[str] = []
        for evidence in entry.get("relevant_evidence", []):
            paper_uid = evidence["paper_uid"]
            if paper_uid.startswith("doi:"):
                target_doi = normalize_doi(paper_uid.removeprefix("doi:"))
                matches = [
                    paper for paper in db.query(Paper).all()
                    if normalize_doi(paper.doi) == target_doi
                ]
            else:
                target_hash = paper_uid.removeprefix("sha256:").lower()
                matches = []
                for candidate in db.query(Paper).all():
                    source = root / candidate.file_path
                    if source.is_file() and sha256_file(source) == target_hash:
                        matches.append(candidate)
            if len(matches) > 1:
                raise ValueError(f"evidence paper_uid 多篇命中: {paper_uid}")
            paper = matches[0] if matches else None
            if paper is None:
                raise ValueError(f"evidence paper_uid 未命中: {paper_uid}")
            quote = evidence["quote"].strip()
            occurrences: list[Chunk] = []
            occurrence_count = 0
            for row in db.query(Chunk).filter(Chunk.paper_id == paper.id).all():
                count = (row.content or "").count(quote)
                occurrence_count += count
                if count:
                    occurrences.append(row)
            if occurrence_count == 0:
                raise ValueError(
                    f"evidence quote 未命中: qa_id={entry.get('qa_id')} paper_uid={paper_uid}"
                )
            if occurrence_count > 1:
                raise ValueError(
                    f"evidence quote 多处命中: qa_id={entry.get('qa_id')} paper_uid={paper_uid}"
                )
            chunk_id = f"p{paper.id}_c{occurrences[0].chunk_index}"
            if chunk_id not in resolved:
                resolved.append(chunk_id)
        return resolved

    scores: dict[tuple[int, int], int] = {}  # (paper_id, chunk_index) -> 得分

    for locator in entry.get("relevant_chunks", []):
        paper_id = locator["paper_id"]
        section = (locator.get("section") or "").strip()
        keywords = [k.strip() for k in locator.get("keywords", []) if k.strip()]

        rows = db.query(Chunk).filter(Chunk.paper_id == paper_id).all()
        for row in rows:
            score = 0
            title = (row.section_title or "").lower()
            content = (row.content or "").lower()
            if section:
                sec = section.lower()
                if sec in title:
                    score += 2
                if sec in content:
                    score += 1
            for kw in keywords:
                if kw.lower() in content:
                    score += 1
            if score > 0:
                key = (paper_id, row.chunk_index)
                scores[key] = max(scores.get(key, 0), score)

    ordered = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    return [f"p{pid}_c{ci}" for (pid, ci), _ in ordered]


def _find_all_occurrences(text: str, quote: str) -> list[int]:
    """返回包含重叠情况的全部逐字命中起点。"""
    starts: list[int] = []
    cursor = 0
    while True:
        index = text.find(quote, cursor)
        if index < 0:
            return starts
        starts.append(index)
        cursor = index + 1


def _resolve_evidence_paper(db, paper_uid: str, runtime_root: Path):
    """按稳定 UID 唯一解析论文，错误契约与 v1 保持一致。"""
    from app.models import Paper
    from eval.private_benchmark import normalize_doi, sha256_file

    if paper_uid.startswith("doi:"):
        target_doi = normalize_doi(paper_uid.removeprefix("doi:"))
        matches = [
            paper for paper in db.query(Paper).all()
            if normalize_doi(paper.doi) == target_doi
        ]
    else:
        target_hash = paper_uid.removeprefix("sha256:").lower()
        matches = []
        for candidate in db.query(Paper).all():
            source = runtime_root / candidate.file_path
            if source.is_file() and sha256_file(source) == target_hash:
                matches.append(candidate)
    if len(matches) > 1:
        raise ValueError(f"evidence paper_uid 多篇命中: {paper_uid}")
    if not matches:
        raise ValueError(f"evidence paper_uid 未命中: {paper_uid}")
    return matches[0]


def _default_page_loader(runtime_root: Path) -> Callable[[Any], list[dict]]:
    """创建单次 resolver 可用的安全 PDF 页加载器。"""
    from app.services.pdf_parser import PDFParser

    parser = PDFParser()
    root = runtime_root.resolve()

    def load(paper) -> list[dict]:
        source = (root / paper.file_path).resolve()
        if not source.is_relative_to(root):
            raise ValueError("论文路径不得逃逸语料根目录")
        if not source.is_file():
            raise FileNotFoundError(f"论文源文件不存在: {paper.file_path}")
        return parser.extract_text(str(source))

    return load


def resolve_relevant_spans_v2(
    db,
    entry: dict,
    runtime_root: Optional[Path] = None,
    page_loader: Optional[Callable[[Any], list[dict]]] = None,
) -> list[dict[str, Any]]:
    """按原始页唯一 quote 与页内坐标解析 Benchmark v2 evidence 组。

    返回值保留每条 evidence 的独立 chunk ID 组；负例返回空列表。目标页任一
    正文 chunk 坐标缺失/越界或相关区间无法完整覆盖 quote 时均 fail-close。
    """
    from app.core.config import config
    from app.models import Chunk

    root = Path(runtime_root) if runtime_root is not None else config.runtime_root
    loader = page_loader or _default_page_loader(root)
    groups: list[dict[str, Any]] = []
    for evidence in entry.get("relevant_evidence", []):
        paper_uid = evidence["paper_uid"]
        paper = _resolve_evidence_paper(db, paper_uid, root)
        pages = loader(paper)
        quote = evidence["quote"].strip()

        occurrences: list[tuple[int, int, str]] = []
        ordered_pages: list[tuple[int, str]] = []
        for page in pages:
            page_number = page.get("page_number")
            text = page.get("text") or ""
            if not isinstance(page_number, int):
                raise ValueError("原始页缺少有效页码")
            ordered_pages.append((page_number, text))
            occurrences.extend(
                (page_number, start, text)
                for start in _find_all_occurrences(text, quote)
            )

        if not occurrences:
            for (_, left), (_, right) in zip(ordered_pages, ordered_pages[1:]):
                if quote in left + right:
                    raise ValueError(
                        f"evidence quote 跨页: qa_id={entry.get('qa_id')} "
                        f"paper_uid={paper_uid}"
                    )
            raise ValueError(
                f"evidence quote 原文未命中: qa_id={entry.get('qa_id')} "
                f"paper_uid={paper_uid}"
            )
        if len(occurrences) > 1:
            raise ValueError(
                f"evidence quote 原文多处命中: qa_id={entry.get('qa_id')} "
                f"paper_uid={paper_uid}"
            )

        page_number, quote_start, page_text = occurrences[0]
        quote_end = quote_start + len(quote)
        rows = (
            db.query(Chunk)
            .filter(
                Chunk.paper_id == paper.id,
                Chunk.page_number == page_number,
                Chunk.chunk_index >= 0,
            )
            .order_by(Chunk.chunk_index, Chunk.id)
            .all()
        )
        if not rows:
            raise ValueError("evidence 目标页没有正文 chunk")
        for row in rows:
            if row.page_start is None or row.page_end is None:
                raise ValueError("evidence 目标页正文 chunk 坐标缺失")
            if not (0 <= row.page_start < row.page_end <= len(page_text)):
                raise ValueError("evidence 目标页正文 chunk 坐标越界")

        relevant = [
            row for row in rows
            if row.page_start < quote_end and row.page_end > quote_start
        ]
        if not relevant:
            raise ValueError("evidence span 未映射到正文 chunk")

        # 相关 chunk 与 quote 的交集并集必须无缝覆盖完整证据。
        intersections = sorted(
            (max(row.page_start, quote_start), min(row.page_end, quote_end))
            for row in relevant
        )
        covered_until = quote_start
        for start, end in intersections:
            if start > covered_until:
                raise ValueError("evidence span 的 chunk 坐标覆盖存在空洞")
            covered_until = max(covered_until, end)
        if covered_until < quote_end:
            raise ValueError("evidence span 未被 chunk 坐标完整覆盖")

        groups.append({
            "paper_id": paper.id,
            "page_number": page_number,
            "page_start": quote_start,
            "page_end": quote_end,
            "chunks": [
                {
                    "chunk_id": f"p{paper.id}_c{row.chunk_index}",
                    "page_start": max(row.page_start, quote_start),
                    "page_end": min(row.page_end, quote_end),
                }
                for row in relevant
            ],
        })
    return groups
