"""MCP Server：将 PaperMind 文献库的只读能力暴露为 MCP 工具。

设计说明：
- 基于 mcp 1.3.0 的 FastMCP 注册工具。该版本的 FastMCP 未提供 sse_app()，
  因此 _create_sse_app() 参照其 run_sse_async() 的实现，手工构建 Starlette
  SSE 应用，供 main.py 通过 app.mount("/mcp", ...) 挂载进现有 FastAPI。
- 所有工具内部用 SessionLocal 自建会话（MCP 请求不走 FastAPI 的 Depends
  依赖注入），用完即关；且全程只读，不写库。
- 挂载时 SSE 的消息回传端点必须使用客户端可见的完整路径（/mcp/messages/），
  否则 MCP 客户端 POST 消息会落到错误路径上。
"""

from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP
from sqlalchemy import func, text

from app.core.logger import logger
from app.database import SessionLocal
from app.models import Paper
from app.routers.search import _sanitize_fts_query

# 摘要截断长度：避免单条工具返回体过大
_ABSTRACT_MAX_LEN = 200

mcp = FastMCP("papermind")


def _project_root() -> Path:
    """定位项目根目录（backend/app/services/mcp_server.py 上溯三级）。"""
    return Path(__file__).resolve().parents[3]


def _paper_brief(p: Paper) -> Dict[str, Any]:
    """文献的简要表示（列表/检索结果用）。"""
    abstract = p.abstract or ""
    if len(abstract) > _ABSTRACT_MAX_LEN:
        abstract = abstract[:_ABSTRACT_MAX_LEN] + "..."
    return {
        "id": p.id,
        "title": p.title,
        "authors": p.authors,
        "year": p.year,
        "journal": p.journal,
        "abstract": abstract,
        "status": p.status,
    }


def _fts_search_ids(db, query: str, limit: int) -> Optional[List[int]]:
    """FTS5 关键词检索，返回按相关度排序的 paper id 列表。

    复用 search 路由的查询串清洗逻辑，杜绝 MATCH 语法错误与注入。
    FTS 不可用（如虚拟表缺失）时返回 None，由调用方回退到 LIKE。
    """
    safe_query = _sanitize_fts_query(query)
    if not safe_query:
        return []
    try:
        rows = db.execute(
            text(
                "SELECT rowid FROM papers_fts WHERE papers_fts MATCH :query "
                "ORDER BY rank LIMIT :limit"
            ),
            {"query": safe_query, "limit": limit},
        ).fetchall()
        return [r[0] for r in rows]
    except Exception as e:
        logger.warning(f"[mcp] FTS5 检索失败，回退 LIKE: {e}")
        return None


@mcp.tool()
def search_papers(query: str, limit: int = 5) -> List[Dict[str, Any]]:
    """关键词检索文献库：在标题、作者、摘要中全文匹配。

    参数：
        query: 检索关键词（支持多词，词间为 AND 语义）
        limit: 最多返回条数，默认 5
    返回：文献简要信息列表（id/标题/作者/年份/期刊/摘要截断/阅读状态）
    """
    db = SessionLocal()
    try:
        limit = max(1, min(int(limit), 50))
        ids = _fts_search_ids(db, query, limit)
        if ids is None:
            # FTS 不可用时的兜底：ORM LIKE 模糊匹配
            like = f"%{query}%"
            papers = (
                db.query(Paper)
                .filter(
                    (Paper.title.like(like))
                    | (Paper.authors.like(like))
                    | (Paper.abstract.like(like))
                )
                .limit(limit)
                .all()
            )
            return [_paper_brief(p) for p in papers]
        if not ids:
            return []
        # 按 FTS 相关度顺序返回
        papers = db.query(Paper).filter(Paper.id.in_(ids)).all()
        by_id = {p.id: p for p in papers}
        return [_paper_brief(by_id[i]) for i in ids if i in by_id]
    finally:
        db.close()


@mcp.tool()
def list_papers(
    skip: int = 0, limit: int = 20, status: Optional[str] = None
) -> Dict[str, Any]:
    """分页列出文献库中的文献。

    参数：
        skip: 跳过的条数（偏移量），默认 0
        limit: 每页条数，默认 20，最大 100
        status: 可选的阅读状态过滤（unread / read / important / todo）
    返回：{"total": 总条数, "papers": [文献简要信息...]}，按收录时间倒序
    """
    db = SessionLocal()
    try:
        skip = max(0, int(skip))
        limit = max(1, min(int(limit), 100))
        q = db.query(Paper)
        if status:
            q = q.filter(Paper.status == status)
        total = q.count()
        papers = (
            q.order_by(Paper.created_at.desc(), Paper.id.desc())
            .offset(skip)
            .limit(limit)
            .all()
        )
        return {"total": total, "papers": [_paper_brief(p) for p in papers]}
    finally:
        db.close()


@mcp.tool()
def get_paper(paper_id: int) -> Dict[str, Any]:
    """获取单篇文献的完整详情。

    参数：
        paper_id: 文献 ID
    返回：文献全部元数据（含标签、PDF 路径、笔记路径、阅读状态、
    阅读进度等）；文献不存在时返回 {"error": ...}
    """
    db = SessionLocal()
    try:
        p = db.query(Paper).filter(Paper.id == paper_id).first()
        if p is None:
            return {"error": f"文献不存在: paper_id={paper_id}"}
        note_path = _project_root() / "notes" / f"{p.id}.md"
        return {
            "id": p.id,
            "title": p.title,
            "authors": p.authors,
            "year": p.year,
            "journal": p.journal,
            "abstract": p.abstract,
            "doi": p.doi,
            "status": p.status,
            "source": p.source,
            "processed": p.processed,
            "last_read_page": p.last_read_page,
            "file_path": p.file_path,
            "filename": p.filename,
            "tags": [t.name for t in p.tags],
            "note_path": str(note_path) if note_path.exists() else None,
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "updated_at": p.updated_at.isoformat() if p.updated_at else None,
        }
    finally:
        db.close()


@mcp.tool()
def get_library_stats() -> Dict[str, Any]:
    """获取文献库整体统计：文献总数、各阅读状态与处理状态计数。"""
    db = SessionLocal()
    try:
        total = db.query(func.count(Paper.id)).scalar() or 0
        status_rows = (
            db.query(Paper.status, func.count(Paper.id)).group_by(Paper.status).all()
        )
        processed_rows = (
            db.query(Paper.processed, func.count(Paper.id))
            .group_by(Paper.processed)
            .all()
        )
        return {
            "total": total,
            "by_status": {str(s or "unknown"): c for s, c in status_rows},
            "by_processed": {str(s or "unknown"): c for s, c in processed_rows},
        }
    finally:
        db.close()


def _create_sse_app(messages_endpoint: str = "/mcp/messages/"):
    """构建 MCP SSE 传输的 Starlette 子应用。

    mcp 1.3.0 的 FastMCP 没有 sse_app()，此处镜像 run_sse_async() 的路由
    结构：GET {mount}/sse 建立 SSE 长连接，POST {mount}/messages/ 回传消息。
    messages_endpoint 必须是客户端可见的完整路径（含挂载前缀），
    因为 SseServerTransport 会把它原样写进 endpoint 事件发给客户端。
    """
    from mcp.server.sse import SseServerTransport
    from starlette.applications import Starlette
    from starlette.routing import Mount, Route

    sse = SseServerTransport(messages_endpoint)

    async def handle_sse(request):
        async with sse.connect_sse(
            request.scope, request.receive, request._send
        ) as streams:
            await mcp._mcp_server.run(
                streams[0],
                streams[1],
                mcp._mcp_server.create_initialization_options(),
            )

    return Starlette(
        routes=[
            Route("/sse", endpoint=handle_sse),
            Mount("/messages/", app=sse.handle_post_message),
        ]
    )


_mcp_app = None


def get_mcp_app():
    """返回可挂载的 MCP Starlette 应用（懒加载单例）。"""
    global _mcp_app
    if _mcp_app is None:
        _mcp_app = _create_sse_app()
        logger.info("[mcp] MCP SSE 应用已初始化（挂载路径 /mcp）")
    return _mcp_app
