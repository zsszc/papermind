"""Batch 20：聊天与评测共享 chunk 检索管线的 RED 契约。

本文件只使用内存 SQLite 与假向量库，不加载真实 Embedding，不访问网络。
生产实现应由 ``app.services.retrieval_pipeline.RetrievalPipeline`` 提供：

- 构造：``RetrievalPipeline(db, vector_store=store)``；
- 检索：``search(query, top_k, filters, profile, rerank, diagnostics)``；
- ``hybrid-bilingual`` 对语义与 chunk 级 BM25 双路各超量召回，再按 chunk id
  做 RRF；限制性过滤必须同时施加到两路；
- 语义不可用或运行期失败时，聊天可降级为关键词结果，但必须通过 diagnostics
  明确披露，供 eval fail-close；
- 返回结果是调用方私有副本，调用方不得污染向量层 60 秒缓存。
"""

import importlib

import pytest

from app.models import Chunk, Paper


def _pipeline_class():
    """延迟导入，让缺少生产模块表现为逐用例 RED，而不是收集期中断。"""
    try:
        module = importlib.import_module("app.services.retrieval_pipeline")
    except ModuleNotFoundError:
        pytest.fail(
            "Batch20 RED：尚未实现 app.services.retrieval_pipeline",
            pytrace=False,
        )
    return module.RetrievalPipeline


def _add_paper(db, *, paper_id, year, content, title=None):
    db.add(Paper(
        id=paper_id,
        title=title or f"paper-{paper_id}",
        year=year,
        filename=f"paper-{paper_id}.pdf",
        file_path=f"papers/paper-{paper_id}.pdf",
    ))
    db.add(Chunk(
        paper_id=paper_id,
        chunk_index=0,
        page_number=paper_id,
        content=content,
        chunk_type="result",
    ))
    db.commit()


def _semantic_chunk(paper_id, content, *, year=2024):
    return {
        "chunk_id": f"p{paper_id}_c0",
        "paper_id": paper_id,
        "title": f"paper-{paper_id}",
        "authors": None,
        "year": year,
        "content": content,
        "page_number": paper_id,
        "chunk_type": "result",
        "score": 0.9,
        "source": "semantic",
    }


class _FakeVectorStore:
    def __init__(self, results=None, *, available=True, error=None):
        self.results = results or []
        self._available = available
        self.error = error
        self.search_calls = []

    def available(self):
        return self._available

    def search(self, **kwargs):
        self.search_calls.append(dict(kwargs))
        if self.error is not None:
            raise self.error
        diagnostics = kwargs.get("rerank_diagnostics")
        if diagnostics is not None:
            diagnostics.update({
                "requested": bool(kwargs.get("rerank")),
                "effective": False,
                "error": None,
            })
        # 故意返回同一批字典，验证 pipeline 必须做复制隔离。
        return self.results


def test_hybrid_bilingual_fuses_semantic_and_lexical_by_chunk_id(db):
    """两路都命中的 chunk 应经 RRF 升至首位，且候选池为 top_k*2。"""
    _add_paper(
        db,
        paper_id=1,
        year=2024,
        content="targetanchor targetanchor precise experimental evidence",
    )
    _add_paper(db, paper_id=2, year=2023, content="unrelated background")
    store = _FakeVectorStore([
        _semantic_chunk(2, "semantic-only evidence", year=2023),
        _semantic_chunk(1, "targetanchor targetanchor precise experimental evidence"),
    ])
    diagnostics = {}

    pipeline = _pipeline_class()(db, vector_store=store)
    results = pipeline.search(
        "targetanchor",
        top_k=2,
        filters={},
        profile="hybrid-bilingual",
        rerank=False,
        diagnostics=diagnostics,
    )

    assert [item["chunk_id"] for item in results] == ["p1_c0", "p2_c0"]
    assert len({item["chunk_id"] for item in results}) == len(results)
    assert store.search_calls == [{
        "query": "targetanchor",
        "top_k": 4,
        "filters": {},
        "rerank": False,
        "rerank_diagnostics": {
            "requested": False,
            "effective": False,
            "error": None,
        },
    }]
    assert diagnostics == {
        "requested_profile": "hybrid-bilingual",
        "effective_profile": "hybrid-bilingual",
        "degraded": False,
        "reason": None,
    }


def test_hybrid_applies_paper_and_year_filters_to_both_routes(db):
    """定向论文/年份检索不得由词法路泄漏其他论文，语义路也须收到原过滤。"""
    _add_paper(db, paper_id=1, year=2022, content="filteranchor eligible")
    _add_paper(db, paper_id=2, year=2019, content="filteranchor too old")
    _add_paper(db, paper_id=3, year=2025, content="filteranchor too new")
    filters = {"paper_id": 1, "year_gte": 2020, "year_lte": 2024}
    store = _FakeVectorStore([
        _semantic_chunk(1, "filteranchor eligible", year=2022),
    ])

    pipeline = _pipeline_class()(db, vector_store=store)
    results = pipeline.search(
        "filteranchor",
        top_k=5,
        filters=filters,
        profile="hybrid-bilingual",
        rerank=False,
        diagnostics={},
    )

    assert [item["chunk_id"] for item in results] == ["p1_c0"]
    assert {item["paper_id"] for item in results} == {1}
    assert store.search_calls[0]["filters"] == filters


def test_hybrid_unavailable_semantic_degrades_to_keyword_with_diagnostics(db):
    """聊天保持可用，但评测能从 diagnostics 识别非 hybrid 的运行结果。"""
    _add_paper(db, paper_id=1, year=2024, content="fallbackanchor evidence")
    store = _FakeVectorStore(available=False)
    diagnostics = {}

    pipeline = _pipeline_class()(db, vector_store=store)
    results = pipeline.search(
        "fallbackanchor",
        top_k=5,
        filters={},
        profile="hybrid-bilingual",
        rerank=False,
        diagnostics=diagnostics,
    )

    assert [item["chunk_id"] for item in results] == ["p1_c0"]
    assert store.search_calls == []
    assert diagnostics == {
        "requested_profile": "hybrid-bilingual",
        "effective_profile": "keyword-only",
        "degraded": True,
        "reason": "semantic_unavailable",
    }


def test_hybrid_runtime_semantic_error_degrades_without_leaking_exception(db):
    """单次 Chroma/Embedding 异常不得中断聊天，且不能伪装成有效 hybrid。"""
    _add_paper(db, paper_id=1, year=2024, content="runtimeanchor evidence")
    store = _FakeVectorStore(error=RuntimeError("chroma broken"))
    diagnostics = {}

    pipeline = _pipeline_class()(db, vector_store=store)
    results = pipeline.search(
        "runtimeanchor",
        top_k=5,
        filters={},
        profile="hybrid-bilingual",
        rerank=False,
        diagnostics=diagnostics,
    )

    assert [item["chunk_id"] for item in results] == ["p1_c0"]
    assert diagnostics == {
        "requested_profile": "hybrid-bilingual",
        "effective_profile": "keyword-only",
        "degraded": True,
        "reason": "semantic_search_failed",
    }


def test_semantic_profile_returns_private_copies(db):
    """调用方改写 source/content 不得污染向量缓存或下一次检索。"""
    original = _semantic_chunk(1, "immutable cached evidence")
    store = _FakeVectorStore([original])
    pipeline = _pipeline_class()(db, vector_store=store)

    first = pipeline.search(
        "same query",
        top_k=5,
        filters={},
        profile="semantic",
        rerank=False,
        diagnostics={},
    )
    first[0]["content"] = "poisoned by caller"
    first[0]["source"] = "hybrid"
    second = pipeline.search(
        "same query",
        top_k=5,
        filters={},
        profile="semantic",
        rerank=False,
        diagnostics={},
    )

    assert second[0]["content"] == "immutable cached evidence"
    assert second[0]["source"] == "semantic"
    assert original["content"] == "immutable cached evidence"

