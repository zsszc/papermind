"""Batch 21：论文内语义邻域传播的 RED 契约。

全部用例使用内存 SQLite 与假向量库，不加载 Embedding、不访问网络。
"""

from copy import deepcopy

import pytest
from sqlalchemy import event

from app.models import Chunk, Paper
from app.services import retrieval_pipeline


def _add_paper(db, paper_id: int, *, year: int = 2024, indexes=range(7)):
    db.add(Paper(
        id=paper_id,
        title=f"paper-{paper_id}",
        authors=f"author-{paper_id}",
        year=year,
        filename=f"paper-{paper_id}.pdf",
        file_path=f"papers/paper-{paper_id}.pdf",
    ))
    for index in indexes:
        db.add(Chunk(
            paper_id=paper_id,
            chunk_index=index,
            page_number=index + 2,
            content=f"paper {paper_id} chunk {index}",
            chunk_type="abstract" if index == -1 else "result",
        ))
    db.commit()


def _seed(paper_id: int, chunk_index: int, *, metadata_paper_id=None):
    return {
        "chunk_id": f"p{paper_id}_c{chunk_index}",
        "paper_id": paper_id if metadata_paper_id is None else metadata_paper_id,
        "title": "stale vector title",
        "authors": None,
        "year": 2000,
        "content": "stale vector content",
        "page_number": None,
        "chunk_type": "paragraph",
        "score": 0.99,
        "source": "semantic",
    }


def _expand(db, seeds, **kwargs):
    function = getattr(
        retrieval_pipeline, "expand_semantic_chunk_neighbors", None
    )
    if function is None:
        pytest.fail("Batch21 RED：尚未实现 expand_semantic_chunk_neighbors")
    return function(db, seeds, **kwargs)


class _Store:
    def __init__(self, results):
        self.results = results
        self.calls = []

    def available(self):
        return True

    def search(self, **kwargs):
        self.calls.append(dict(kwargs))
        return deepcopy(self.results)


def test_rank_prior_propagation_formula_radius_and_overlap(db):
    """冻结 0.5^d/rank、±2 与重叠取 max，不按 seed 求和。"""
    _add_paper(db, 1)
    _add_paper(db, 2, indexes=(0, 1, 2))
    seeds = [_seed(1, 2), _seed(1, 4)]
    original = deepcopy(seeds)

    results = _expand(db, seeds, filters={}, radius=2, decay=0.5, limit=20)
    by_id = {item["chunk_id"]: item for item in results}

    assert set(by_id) == {f"p1_c{i}" for i in range(7)}
    assert by_id["p1_c2"]["neighbor_score"] == pytest.approx(1.0)
    assert by_id["p1_c1"]["neighbor_score"] == pytest.approx(0.5)
    assert by_id["p1_c0"]["neighbor_score"] == pytest.approx(0.25)
    # c4 同时受 rank1/d2=.25 与 rank2/d0=.5 影响，只取最大值。
    assert by_id["p1_c4"]["neighbor_score"] == pytest.approx(0.5)
    assert len({item["chunk_id"] for item in results}) == len(results)
    assert all(item["paper_id"] == 1 for item in results)
    assert seeds == original


def test_neighbor_order_is_deterministic_for_equal_scores(db):
    """同分时依次按距离、seed rank、chunk_id 排序。"""
    _add_paper(db, 1, indexes=(0, 1, 2, 3, 4, 5))

    first = _expand(db, [_seed(1, 2), _seed(1, 4)], filters={})
    second = _expand(db, [_seed(1, 2), _seed(1, 4)], filters={})

    assert [item["chunk_id"] for item in first] == [
        item["chunk_id"] for item in second
    ]
    assert [item["chunk_id"] for item in first[:4]] == [
        "p1_c2", "p1_c4", "p1_c1", "p1_c3"
    ]


def test_summary_sentinel_does_not_propagate_into_body(db):
    """摘要 c-1 是独立哨兵，不能借相邻数字跨入正文 c0/c1。"""
    _add_paper(db, 1, indexes=(-1, 0, 1, 2))

    results = _expand(db, [_seed(1, -1)], filters={})

    assert [item["chunk_id"] for item in results] == ["p1_c-1"]


def test_neighbor_expansion_respects_paper_and_year_filters(db):
    """向量 seed 即使越界，邻域 SQL 仍必须以数据库过滤 fail-close。"""
    _add_paper(db, 1, year=2022, indexes=(0, 1, 2))
    _add_paper(db, 2, year=2019, indexes=(0, 1, 2))
    _add_paper(db, 3, year=2025, indexes=(0, 1, 2))
    filters = {"paper_id": 1, "year_gte": 2020, "year_lte": 2024}

    results = _expand(
        db,
        [_seed(1, 1), _seed(2, 1), _seed(3, 1)],
        filters=filters,
    )

    assert {item["paper_id"] for item in results} == {1}
    assert {item["chunk_id"] for item in results} == {
        "p1_c0", "p1_c1", "p1_c2"
    }


def test_malformed_and_metadata_mismatched_seeds_are_skipped(db):
    _add_paper(db, 1, indexes=(0, 1, 2))
    malformed = _seed(1, 1)
    malformed["chunk_id"] = "not-a-canonical-id"

    results = _expand(
        db,
        [malformed, _seed(1, 1, metadata_paper_id=9), _seed(1, 2)],
        filters={},
    )

    assert {item["chunk_id"] for item in results} == {
        "p1_c0", "p1_c1", "p1_c2"
    }
    assert all(item["best_seed_rank"] == 3 for item in results)


def test_neighbor_candidates_are_loaded_with_one_sql_query(db):
    """seed 数量增加不能退化成逐 seed/逐 chunk N+1。"""
    _add_paper(db, 1, indexes=range(20))
    statements = []

    def before_cursor_execute(
        conn, cursor, statement, parameters, context, executemany
    ):
        statements.append(statement)

    event.listen(db.bind, "before_cursor_execute", before_cursor_execute)
    try:
        _expand(db, [_seed(1, index) for index in range(20)], filters={})
    finally:
        event.remove(db.bind, "before_cursor_execute", before_cursor_execute)

    selects = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]
    assert len(selects) == 1


def test_candidate_profile_uses_top20_and_keeps_old_hybrid_unchanged(db):
    _add_paper(db, 1, indexes=(0, 1, 2))
    store = _Store([_seed(1, 1)])
    pipeline = retrieval_pipeline.RetrievalPipeline(db, vector_store=store)

    candidate = pipeline.search(
        "no lexical tokens",
        top_k=5,
        filters={},
        profile="hybrid-local-neighbor",
        lexical_profile="bm25-bilingual",
        diagnostics={},
    )
    baseline = pipeline.search(
        "no lexical tokens",
        top_k=5,
        filters={},
        profile="hybrid",
        lexical_profile="bm25-bilingual",
        diagnostics={},
    )

    assert store.calls[0]["top_k"] == 20
    assert store.calls[1]["top_k"] == 10
    assert {item["chunk_id"] for item in candidate} == {
        "p1_c0", "p1_c1", "p1_c2"
    }
    assert [item["chunk_id"] for item in baseline] == ["p1_c1"]


def test_neighbor_failure_falls_back_but_marks_diagnostics(db, monkeypatch):
    _add_paper(db, 1, indexes=(0, 1, 2))
    store = _Store([_seed(1, 1)])
    diagnostics = {}

    def fail(*args, **kwargs):
        raise RuntimeError("private implementation detail")

    monkeypatch.setattr(
        retrieval_pipeline,
        "expand_semantic_chunk_neighbors",
        fail,
        raising=False,
    )
    results = retrieval_pipeline.RetrievalPipeline(
        db, vector_store=store
    ).search(
        "no lexical tokens",
        top_k=5,
        filters={},
        profile="hybrid-local-neighbor",
        lexical_profile="bm25-bilingual",
        diagnostics=diagnostics,
    )

    assert [item["chunk_id"] for item in results] == ["p1_c1"]
    assert diagnostics == {
        "requested_profile": "hybrid-local-neighbor",
        "effective_profile": "hybrid",
        "degraded": True,
        "reason": "semantic_neighbor_expansion_failed",
    }
