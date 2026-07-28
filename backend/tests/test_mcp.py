"""MCP Server 单元测试：工具函数直调 + 挂载不影响现有路由。

不经过真实 MCP 客户端与 SSE 连接（避免长连接阻塞），直接调用工具函数；
通过 monkeypatch 将 mcp_server.SessionLocal 替换为内存 SQLite 会话工厂，
全程不触发 LLM / embedding。
"""

import pytest

from app.main import app
from app.models import Paper, Tag
from app.services import mcp_server

from .conftest import TestingSessionLocal


@pytest.fixture()
def mcp_db(db, monkeypatch):
    """把 MCP 工具的会话工厂替换为测试内存库，返回已建表的会话。"""
    monkeypatch.setattr(mcp_server, "SessionLocal", TestingSessionLocal)
    return db


def _make_paper(db, **kwargs) -> Paper:
    """快速造一条 Paper 记录（file_path/filename 为必填，给默认占位值）。"""
    defaults = {
        "title": "未命名文献",
        "file_path": "papers/placeholder.pdf",
        "filename": "placeholder.pdf",
    }
    defaults.update(kwargs)
    p = Paper(**defaults)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


# ---------- search_papers ----------


def test_search_papers_hit(mcp_db):
    """关键词检索应命中标题/摘要，且返回结构完整。"""
    _make_paper(
        mcp_db,
        title="Colorectal cancer staging with vision transformer",
        authors="Zhou, Li",
        year=2024,
        journal="Nature Medicine",
        abstract="A multiple instance learning approach for T staging.",
    )
    _make_paper(mcp_db, title="完全无关的另一篇", abstract="毫无关联的内容")

    results = mcp_server.search_papers("colorectal", limit=5)
    assert len(results) == 1
    r = results[0]
    assert r["title"].startswith("Colorectal")
    assert r["authors"] == "Zhou, Li"
    assert r["year"] == 2024
    assert r["journal"] == "Nature Medicine"
    assert "multiple instance learning" in r["abstract"]
    assert set(r) >= {"id", "title", "authors", "year", "journal", "abstract", "status"}


def test_search_papers_no_match(mcp_db):
    """无命中时返回空列表，不抛异常。"""
    _make_paper(mcp_db, title="一篇文献")
    assert mcp_server.search_papers("不存在的词xyz", limit=5) == []


def test_search_papers_special_chars(mcp_db):
    """FTS 特殊字符输入不应导致语法错误（清洗后无命中即空列表）。"""
    _make_paper(mcp_db, title="test paper")
    assert mcp_server.search_papers('"(:*^', limit=5) == []


# ---------- list_papers ----------


def test_list_papers_pagination_and_filter(mcp_db):
    """分页与状态过滤应正确工作，并返回总数。"""
    for i in range(3):
        _make_paper(mcp_db, title=f"文献{i}", status="unread")
    _make_paper(mcp_db, title="已读文献", status="read")

    page = mcp_server.list_papers(skip=0, limit=2)
    assert page["total"] == 4
    assert len(page["papers"]) == 2

    rest = mcp_server.list_papers(skip=2, limit=10)
    assert len(rest["papers"]) == 2

    read_only = mcp_server.list_papers(status="read")
    assert read_only["total"] == 1
    assert read_only["papers"][0]["title"] == "已读文献"


# ---------- get_paper ----------


def test_get_paper_detail(mcp_db):
    """单篇详情应包含标签、阅读状态与笔记路径字段。"""
    tag = Tag(name="MIL")
    mcp_db.add(tag)
    p = _make_paper(mcp_db, title="详情文献", status="important", abstract="完整摘要")
    p.tags.append(tag)
    mcp_db.commit()

    detail = mcp_server.get_paper(p.id)
    assert detail["id"] == p.id
    assert detail["title"] == "详情文献"
    assert detail["abstract"] == "完整摘要"
    assert detail["status"] == "important"
    assert detail["tags"] == ["MIL"]
    # 笔记路径：文件存在时给路径、不存在时为 None（本机 notes/ 可能有真实笔记）
    assert "note_path" in detail
    if detail["note_path"] is not None:
        assert detail["note_path"].endswith(f"{p.id}.md")


def test_get_paper_not_found(mcp_db):
    """不存在的 ID 应返回 error 字典而不是抛异常。"""
    result = mcp_server.get_paper(99999)
    assert "error" in result


# ---------- get_library_stats ----------


def test_get_library_stats(mcp_db):
    """统计应给出总数与各状态计数。"""
    _make_paper(mcp_db, title="A", status="unread")
    _make_paper(mcp_db, title="B", status="unread")
    _make_paper(mcp_db, title="C", status="read")

    stats = mcp_server.get_library_stats()
    assert stats["total"] == 3
    assert stats["by_status"] == {"unread": 2, "read": 1}
    assert "by_processed" in stats


# ---------- 空库行为 ----------


def test_empty_library(mcp_db):
    """空库下所有工具返回空结构，不抛异常。"""
    assert mcp_server.search_papers("anything") == []
    assert mcp_server.list_papers() == {"total": 0, "papers": []}
    assert mcp_server.get_library_stats()["total"] == 0
    assert "error" in mcp_server.get_paper(1)


# ---------- 挂载不影响现有路由 ----------


def test_mcp_mounted_and_health_ok(client):
    """/mcp 已挂载，且 /api/health 等现有路由不受影响。"""
    assert any(getattr(r, "path", "") == "/mcp" for r in app.routes)
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
