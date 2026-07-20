from typing import Any, Dict, List

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.core.logger import logger
from app.database import get_db
from app.schemas import SearchRequest, SearchResponse, SearchResult
from app.services.retrieval import get_vector_store

router = APIRouter()


def _keyword_search(db: Session, query: str, limit: int = 20) -> List[Dict[str, Any]]:
    """基于 SQLite FTS5 的关键词检索，返回论文级别结果。"""
    if not query or not query.strip():
        return []
    try:
        rows = db.execute(
            text("""
                SELECT p.id, p.title, p.authors, p.year, p.abstract
                FROM papers_fts fts
                JOIN papers p ON p.id = fts.rowid
                WHERE papers_fts MATCH :query
                ORDER BY rank
                LIMIT :limit
            """),
            {"query": query.strip(), "limit": limit},
        ).fetchall()
        return [
            {
                "paper_id": row.id,
                "title": row.title,
                "authors": row.authors,
                "year": row.year,
                "content": row.abstract or row.title or "",
                "page_number": None,
                "chunk_type": "abstract",
                "score": 0.0,
                "source": "keyword",
            }
            for row in rows
        ]
    except Exception as e:
        logger.warning(f"[search] 关键词检索失败: {e}", exc_info=True)
        return []


def _reciprocal_rank_fusion(
    semantic_results: List[Dict[str, Any]],
    keyword_results: List[Dict[str, Any]],
    top_k: int,
    k: int = 60,
) -> List[Dict[str, Any]]:
    """RRF 融合：综合语义检索与关键词检索的排序。"""
    scores: Dict[int, float] = {}
    metas: Dict[int, Dict[str, Any]] = {}

    def _add(results: List[Dict[str, Any]]) -> None:
        for rank, item in enumerate(results):
            pid = item["paper_id"]
            if pid is None:
                continue
            scores[pid] = scores.get(pid, 0.0) + 1.0 / (k + rank + 1)
            if pid not in metas:
                metas[pid] = item

    _add(semantic_results)
    _add(keyword_results)

    sorted_pids = sorted(scores.keys(), key=lambda pid: scores[pid], reverse=True)
    return [metas[pid] for pid in sorted_pids[:top_k]]


@router.post("", response_model=SearchResponse)
def search(request: SearchRequest, db: Session = Depends(get_db)):
    store = get_vector_store()
    filters = request.filters or {}
    top_k = request.top_k or 10

    semantic_results: List[Dict[str, Any]] = []
    keyword_results: List[Dict[str, Any]] = []

    if request.use_semantic and store.available():
        semantic_results = store.search(
            query=request.query,
            top_k=top_k * 2,
            filters=filters,
        )
        for r in semantic_results:
            r["source"] = "semantic"

    if request.use_keyword:
        keyword_results = _keyword_search(db, request.query, limit=top_k * 2)

    if request.use_semantic and request.use_keyword:
        fused = _reciprocal_rank_fusion(semantic_results, keyword_results, top_k)
        for r in fused:
            r["source"] = "hybrid"
        results = fused
    else:
        results = (semantic_results + keyword_results)[:top_k]

    return SearchResponse(
        query=request.query,
        results=[SearchResult(**r) for r in results],
    )
