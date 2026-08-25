"""生成结果 Guardrail 纯逻辑。

本模块只依赖 Python 标准库，供生产聊天与离线评测共用；导入时不得
初始化 LLM、Embedding、检索、数据库或网络客户端。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Tuple


logger = logging.getLogger(__name__)

# 宽匹配生产 [^...^] 与历史评测 [p..._c...] 形态；后者一律视为未知协议。
_CITATION_MARKER_PATTERN = re.compile(
    r"\[\^([^\]\^]*)\^\]|\[(p[1-9]\d*_c\d+)\]"
)
_INTEGER_PATTERN = re.compile(r"-?\d+")
_CHUNK_ID_PATTERN = re.compile(r"p[1-9]\d*_c\d+")


def context_chunk_id(context: Any) -> str | None:
    """从检索片段中取规范 chunk id，优先 chunk_id，兼容生产 source。"""
    if isinstance(context, dict):
        value = context.get("chunk_id") or context.get("source")
    else:
        value = getattr(context, "chunk_id", None) or getattr(context, "source", None)
    if isinstance(value, str) and _CHUNK_ID_PATTERN.fullmatch(value):
        return value
    return None


def verify_citations_detailed(
    answer_text: str, retrieved_chunks: List[Dict[str, Any]]
) -> Tuple[str, Dict[str, Any], List[str]]:
    """按生产 ``[^n^]`` 协议校验引用，返回清洗文本、详细报告与实际引用 ID。

    ``valid`` 按出现次数计数；``unique_valid`` 只计首次出现的规范
    chunk id。越界、畸形以及指向畸形 chunk id 的标记会从文本中删除。
    """
    text = answer_text if isinstance(answer_text, str) else ""
    chunk_ids = [context_chunk_id(chunk) for chunk in (retrieved_chunks or [])]
    ambiguous_ids = {
        chunk_id for chunk_id in chunk_ids
        if chunk_id is not None and chunk_ids.count(chunk_id) > 1
    }
    seen: set[str] = set()
    cited_ids: List[str] = []
    removed_tokens: List[str] = []
    counters = {
        "total": 0,
        "valid": 0,
        "unique_valid": 0,
        "duplicate_valid": 0,
        "out_of_range": 0,
        "malformed": 0,
    }

    def replace(match: re.Match[str]) -> str:
        counters["total"] += 1
        if match.group(2) is not None:
            counters["malformed"] += 1
            removed_tokens.append(match.group(2))
            return ""
        raw_index = match.group(1)
        if not _INTEGER_PATTERN.fullmatch(raw_index):
            counters["malformed"] += 1
            removed_tokens.append(raw_index)
            return ""
        ordinal = int(raw_index)
        if ordinal < 1 or ordinal > len(chunk_ids):
            counters["out_of_range"] += 1
            removed_tokens.append(raw_index)
            return ""
        chunk_id = chunk_ids[ordinal - 1]
        if chunk_id is None or chunk_id in ambiguous_ids:
            counters["malformed"] += 1
            removed_tokens.append(raw_index)
            return ""
        counters["valid"] += 1
        if chunk_id in seen:
            counters["duplicate_valid"] += 1
        else:
            seen.add(chunk_id)
            cited_ids.append(chunk_id)
            counters["unique_valid"] += 1
        return match.group(0)

    cleaned = _CITATION_MARKER_PATTERN.sub(replace, text)
    removed = counters["out_of_range"] + counters["malformed"]
    report = {
        "total": counters["total"],
        "valid": counters["valid"],
        "removed": removed,
        "verified": removed == 0,
        "unique_valid": counters["unique_valid"],
        "duplicate_valid": counters["duplicate_valid"],
        "out_of_range": counters["out_of_range"],
        "malformed": counters["malformed"],
    }
    if removed:
        logger.warning(
            "[guardrails] 已剔除非法引用 tokens=%s：out_of_range=%d malformed=%d retrieved=%d",
            removed_tokens,
            report["out_of_range"],
            report["malformed"],
            len(chunk_ids),
        )
    return cleaned, report, cited_ids


def verify_citations(
    answer_text: str, retrieved_chunks: List[Dict[str, Any]]
) -> Tuple[str, Dict[str, Any]]:
    """兼容 Phase C 旧签名，内部始终走共享详细校验器。"""
    cleaned, detailed, _ = verify_citations_detailed(answer_text, retrieved_chunks)
    return cleaned, {
        key: detailed[key] for key in ("total", "valid", "removed", "verified")
    }


def select_cited_chunks(
    retrieved_chunks: List[Dict[str, Any]], cited_ids: List[str]
) -> List[Dict[str, Any]]:
    """按首次引用顺序返回实际被引用的检索片段。"""
    by_id = {
        chunk_id: chunk
        for chunk in (retrieved_chunks or [])
        if (chunk_id := context_chunk_id(chunk)) is not None
    }
    return [by_id[chunk_id] for chunk_id in cited_ids if chunk_id in by_id]


def build_rag_prompt(query: str, retrieved: List[Dict[str, Any]]) -> str:
    """构建生产 RAG system prompt；保持纯函数，供 Agent 与离线契约共用。"""
    context_parts = []
    for index, item in enumerate(retrieved or [], start=1):
        title = item.get("title") or "未知文献"
        authors = item.get("authors") or ""
        year = item.get("year") or ""
        page = item.get("page_number")
        content = item.get("content", "")
        header = f"[{index}] {title}"
        if authors:
            header += f" - {authors}"
        if year:
            header += f" ({year})"
        if page:
            header += f" 第{page}页"
        context_parts.append(f"{header}\n{content}\n")

    context = "\n---\n".join(context_parts)
    return f"""以下是可能相关的文献片段（每个片段开头 [i] 为引用编号，请在回答中需要引用时标注 [^i^]）：

{context}

---

用户问题：{query}

请基于以上片段回答，并在需要时标注引用来源 [^i^]。回答末尾请列出引用文献的标题与页码。"""
