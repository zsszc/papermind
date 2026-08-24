"""Parent-Child 候选的确定性坐标映射与父库指纹。"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from typing import Any

from app.models import Chunk, Paper


_DOI_PREFIX_RE = re.compile(
    r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", re.IGNORECASE
)


def _chunk_id(row: Chunk) -> str:
    return f"p{row.paper_id}_c{row.chunk_index}"


def _paper_identity(paper: Paper) -> str:
    """生成不依赖动态主键的论文身份；优先 DOI，缺失时用元数据哈希。"""
    doi = _DOI_PREFIX_RE.sub("", (paper.doi or "").strip()).rstrip(".,; ").lower()
    if doi:
        return f"doi:{doi}"
    metadata = {
        "title": (paper.title or "").strip(),
        "authors": (paper.authors or "").strip(),
        "year": paper.year,
        "filename": (paper.filename or "").strip(),
    }
    payload = json.dumps(
        metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return f"metadata-sha256:{hashlib.sha256(payload).hexdigest()}"


def _paper_identities(db) -> dict[int, str]:
    rows = db.query(Paper).order_by(Paper.id).all()
    identities = {row.id: _paper_identity(row) for row in rows}
    if len(set(identities.values())) != len(identities):
        raise ValueError("论文稳定身份重复")
    return identities


def _validated_parent_rows(parent_db) -> tuple[
    dict[tuple[int, int], Chunk],
    dict[tuple[int, int], list[Chunk]],
]:
    """校验 parent 自然身份与坐标，返回摘要和正文页索引。"""
    rows = parent_db.query(Chunk).order_by(
        Chunk.paper_id, Chunk.chunk_index, Chunk.id
    ).all()
    identities: set[tuple[int, int]] = set()
    abstracts: dict[tuple[int, int], Chunk] = {}
    body_by_page: dict[tuple[int, int], list[Chunk]] = defaultdict(list)
    coordinates: set[tuple[int, int, int, int]] = set()
    for row in rows:
        identity = (row.paper_id, row.chunk_index)
        if identity in identities:
            raise ValueError("parent 存在重复自然身份")
        identities.add(identity)
        if row.chunk_index == -1:
            abstracts[identity] = row
            continue
        if row.chunk_index < -1:
            raise ValueError("parent 存在非法负 chunk_index")
        if (
            not isinstance(row.page_number, int)
            or not isinstance(row.page_start, int)
            or not isinstance(row.page_end, int)
            or not 0 <= row.page_start < row.page_end
        ):
            raise ValueError("parent 正文坐标无效")
        coordinate = (
            row.paper_id, row.page_number, row.page_start, row.page_end
        )
        if coordinate in coordinates:
            raise ValueError("parent 存在重复页内坐标")
        coordinates.add(coordinate)
        body_by_page[(row.paper_id, row.page_number)].append(row)
    for page_rows in body_by_page.values():
        page_rows.sort(key=lambda row: row.chunk_index)
    return abstracts, body_by_page


def build_parent_map(child_db, parent_db) -> dict[str, str]:
    """将每个 child 映射到相同论文/页内字符交集最大的 parent。"""
    child_papers = _paper_identities(child_db)
    parent_papers = _paper_identities(parent_db)
    if child_papers != parent_papers:
        raise ValueError("child/parent 论文身份不一致")
    abstracts, body_by_page = _validated_parent_rows(parent_db)
    children = child_db.query(Chunk).order_by(
        Chunk.paper_id, Chunk.chunk_index, Chunk.id
    ).all()
    child_identities: set[tuple[int, int]] = set()
    mapping: dict[str, str] = {}
    for child in children:
        identity = (child.paper_id, child.chunk_index)
        if identity in child_identities:
            raise ValueError("child 存在重复自然身份")
        child_identities.add(identity)
        if child.chunk_index == -1:
            parent = abstracts.get(identity)
            if parent is None:
                raise ValueError("child 摘要缺少同论文 parent 摘要")
            mapping[_chunk_id(child)] = _chunk_id(parent)
            continue
        if child.chunk_index < -1:
            raise ValueError("child 存在非法负 chunk_index")
        if (
            not isinstance(child.page_number, int)
            or not isinstance(child.page_start, int)
            or not isinstance(child.page_end, int)
            or not 0 <= child.page_start < child.page_end
        ):
            raise ValueError("child 正文坐标无效")
        candidates: list[tuple[int, int, Chunk]] = []
        for parent in body_by_page.get(
            (child.paper_id, child.page_number), []
        ):
            overlap = max(
                0,
                min(child.page_end, parent.page_end)
                - max(child.page_start, parent.page_start),
            )
            if overlap:
                candidates.append((overlap, parent.chunk_index, parent))
        if not candidates:
            raise ValueError(
                f"child {_chunk_id(child)} 没有相交 parent"
            )
        candidates.sort(key=lambda item: (-item[0], item[1]))
        mapping[_chunk_id(child)] = _chunk_id(candidates[0][2])
    return mapping


def parent_manifest_sha256(parent_db) -> str:
    """计算不依赖 SQLite 行主键、且不泄露正文的 parent 内容指纹。"""
    _validated_parent_rows(parent_db)
    paper_identities = _paper_identities(parent_db)
    rows = parent_db.query(Chunk).order_by(
        Chunk.paper_id, Chunk.chunk_index, Chunk.id
    ).all()
    manifest: list[dict[str, Any]] = [
        {
            "paper_id": row.paper_id,
            "paper_uid": paper_identities[row.paper_id],
            "chunk_index": row.chunk_index,
            "page_number": row.page_number,
            "page_start": row.page_start,
            "page_end": row.page_end,
            "content_sha256": hashlib.sha256(
                row.content.encode("utf-8")
            ).hexdigest(),
        }
        for row in rows
    ]
    payload = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
