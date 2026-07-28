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
from pathlib import Path
from typing import Any, Optional, Union

# 默认种子集路径
DEFAULT_SEED_PATH = Path(__file__).resolve().parent / "dataset" / "qa_seed.jsonl"

# 必填字段
REQUIRED_FIELDS = {
    "qa_id",
    "question",
    "ground_truth",
    "relevant_chunks",
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

        chunks = item["relevant_chunks"]
        if not isinstance(chunks, list):
            errors.append(f"{where}: relevant_chunks 必须是列表")
        else:
            if has_answer is False and chunks:
                errors.append(f"{where}: 负例（has_answer=false）不应标注 relevant_chunks")
            for locator in chunks:
                errors.extend(_validate_locator(locator, where))

    if errors:
        raise ValueError("数据集校验失败:\n" + "\n".join(f"  - {e}" for e in errors))


def resolve_relevant_chunks(db, entry: dict) -> list[str]:
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
    from app.models import Chunk  # 延迟导入，保证加载/校验可脱离 app 使用

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
