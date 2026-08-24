"""Batch 22E：Parent-Child 映射与聚合检索 RED。"""

from copy import deepcopy

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Chunk, Paper
from app.services.parent_child import build_parent_map, parent_manifest_sha256
from app.services.retrieval_pipeline import (
    RetrievalPipeline,
    parent_child_fuse_chunks,
)


def _session(path, chunks):
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    db.add(Paper(
        id=1, title="fixture", filename="x.pdf", file_path="papers/x.pdf",
    ))
    for index, page, start, end in chunks:
        db.add(Chunk(
            paper_id=1,
            chunk_index=index,
            page_number=page,
            page_start=start,
            page_end=end,
            content=f"chunk-{index}-{start}-{end}",
        ))
    db.commit()
    return engine, db


def _item(chunk_id, score=1.0):
    paper, chunk = chunk_id.removeprefix("p").split("_c")
    return {
        "chunk_id": chunk_id,
        "paper_id": int(paper),
        "content": f"content-{chunk_id}",
        "score": score,
        "source": "semantic",
    }


def test_parent_map_uses_max_intersection_stable_tie_and_abstract(tmp_path):
    parent_engine, parent_db = _session(
        tmp_path / "parents.db",
        [(-1, 1, None, None), (0, 1, 0, 40), (1, 1, 40, 100)],
    )
    child_engine, child_db = _session(
        tmp_path / "children.db",
        [(-1, 1, None, None), (0, 1, 0, 60), (1, 1, 30, 80), (2, 1, 30, 50)],
    )
    try:
        mapping = build_parent_map(child_db, parent_db)
    finally:
        child_db.close()
        parent_db.close()
        child_engine.dispose()
        parent_engine.dispose()

    assert mapping == {
        "p1_c-1": "p1_c-1",
        "p1_c0": "p1_c0",
        "p1_c1": "p1_c1",
        # 两个 parent 各相交 10 字符，并列按较小 parent chunk_index。
        "p1_c2": "p1_c0",
    }


def test_parent_map_fails_closed_on_zero_overlap(tmp_path):
    parent_engine, parent_db = _session(
        tmp_path / "parents.db", [(0, 1, 0, 20)]
    )
    child_engine, child_db = _session(
        tmp_path / "children.db", [(0, 1, 30, 40)]
    )
    try:
        with pytest.raises(ValueError, match="没有相交 parent"):
            build_parent_map(child_db, parent_db)
    finally:
        child_db.close()
        parent_db.close()
        child_engine.dispose()
        parent_engine.dispose()


def test_parent_manifest_depends_on_stable_coordinates_not_row_id(tmp_path):
    first_engine, first = _session(
        tmp_path / "first.db", [(0, 1, 0, 20), (1, 1, 20, 40)]
    )
    second_engine, second = _session(
        tmp_path / "second.db", [(0, 1, 0, 20), (1, 1, 20, 40)]
    )
    try:
        rows = second.query(Chunk).order_by(Chunk.chunk_index).all()
        rows[0].id = 101
        rows[1].id = 102
        second.commit()
        assert parent_manifest_sha256(first) == parent_manifest_sha256(second)
    finally:
        first.close()
        second.close()
        first_engine.dispose()
        second_engine.dispose()


def test_parent_map_rejects_same_dynamic_id_for_different_paper(tmp_path):
    parent_engine, parent_db = _session(
        tmp_path / "parents.db", [(0, 1, 0, 20)]
    )
    child_engine, child_db = _session(
        tmp_path / "children.db", [(0, 1, 0, 20)]
    )
    child_db.query(Paper).one().title = "different paper"
    child_db.commit()
    try:
        with pytest.raises(ValueError, match="论文身份不一致"):
            build_parent_map(child_db, parent_db)
    finally:
        child_db.close()
        parent_db.close()
        child_engine.dispose()
        parent_engine.dispose()


def test_parent_manifest_includes_paper_identity_and_content_hash(tmp_path):
    first_engine, first = _session(
        tmp_path / "first.db", [(0, 1, 0, 20)]
    )
    second_engine, second = _session(
        tmp_path / "second.db", [(0, 1, 0, 20)]
    )
    try:
        assert parent_manifest_sha256(first) == parent_manifest_sha256(second)
        second.query(Chunk).one().content = "changed parent content"
        second.commit()
        assert parent_manifest_sha256(first) != parent_manifest_sha256(second)
    finally:
        first.close()
        second.close()
        first_engine.dispose()
        second_engine.dispose()


def test_parent_score_caps_child_count_and_uses_frozen_discounts():
    semantic = [_item(f"p1_c{i}") for i in range(4)] + [_item("p2_c0")]
    mapping = {f"p1_c{i}": "p1_c10" for i in range(4)}
    mapping["p2_c0"] = "p2_c10"

    with_four = parent_child_fuse_chunks(
        semantic, [], mapping, top_k=5
    )
    with_three = parent_child_fuse_chunks(
        semantic[:3] + semantic[4:], [], mapping, top_k=5
    )

    first_parent_score = next(
        row["parent_score"] for row in with_four
        if row["parent_chunk_id"] == "p1_c10"
    )
    assert first_parent_score == pytest.approx(
        1 / 61 + 0.5 / 62 + 0.25 / 63
    )
    assert first_parent_score == next(
        row["parent_score"] for row in with_three
        if row["parent_chunk_id"] == "p1_c10"
    )


def test_parent_round_robin_preserves_diversity_and_stable_order():
    semantic = [
        _item("p1_c0"), _item("p1_c1"), _item("p1_c2"),
        _item("p2_c0"), _item("p2_c1"), _item("p3_c0"),
    ]
    mapping = {
        "p1_c0": "p1_c9", "p1_c1": "p1_c9", "p1_c2": "p1_c9",
        "p2_c0": "p2_c9", "p2_c1": "p2_c9", "p3_c0": "p3_c9",
    }

    first = parent_child_fuse_chunks(semantic, [], mapping, top_k=5)
    second = parent_child_fuse_chunks(deepcopy(semantic), [], mapping, top_k=5)

    assert [row["chunk_id"] for row in first] == [
        "p1_c0", "p2_c0", "p3_c0", "p1_c1", "p2_c1",
    ]
    assert first == second


def test_parent_fusion_fails_when_any_recalled_child_is_unmapped():
    with pytest.raises(ValueError, match="缺少 parent 映射"):
        parent_child_fuse_chunks([_item("p1_c0")], [], {}, top_k=5)


def test_parent_fusion_rejects_invalid_id_and_duplicate_does_not_consume_rank():
    with pytest.raises(ValueError, match="非法 child id"):
        parent_child_fuse_chunks(
            [{"chunk_id": "bad", "content": "bad"}], [], {}, top_k=5
        )

    first = _item("p1_c0")
    second = _item("p2_c0")
    results = parent_child_fuse_chunks(
        [first, deepcopy(first), second],
        [],
        {"p1_c0": "p1_c9", "p2_c0": "p2_c9"},
        top_k=5,
    )
    scores = {row["chunk_id"]: row["child_rrf_score"] for row in results}
    assert scores["p1_c0"] == pytest.approx(1 / 61)
    assert scores["p2_c0"] == pytest.approx(1 / 62)


class _Store:
    def __init__(self, results):
        self.results = results
        self.calls = []

    def available(self):
        return True

    def search(self, **kwargs):
        self.calls.append(dict(kwargs))
        return deepcopy(self.results)


class _UnavailableStore(_Store):
    def available(self):
        return False


def test_pipeline_parent_child_profile_uses_top40_and_keeps_hybrid_unchanged(
    db, monkeypatch
):
    semantic = [_item("p1_c0")]
    keyword = [_item("p1_c1")]
    keyword[0]["source"] = "keyword"
    store = _Store(semantic)
    monkeypatch.setattr(
        "app.services.retrieval_pipeline.keyword_chunk_search",
        lambda *args, **kwargs: deepcopy(keyword),
    )
    pipeline = RetrievalPipeline(
        db,
        vector_store=store,
        parent_map={"p1_c0": "p1_c9", "p1_c1": "p1_c9"},
    )

    diagnostics = {}
    candidate = pipeline.search(
        "query", top_k=5, profile="parent-child-v1",
        lexical_profile="bm25-bilingual", diagnostics=diagnostics,
    )
    baseline = pipeline.search(
        "query", top_k=5, profile="hybrid",
        lexical_profile="bm25-bilingual", diagnostics={},
    )

    assert store.calls[0]["top_k"] == 40
    assert store.calls[1]["top_k"] == 10
    assert {row["chunk_id"] for row in candidate} == {"p1_c0", "p1_c1"}
    assert diagnostics == {
        "requested_profile": "parent-child-v1",
        "effective_profile": "parent-child-v1",
        "degraded": False,
        "reason": None,
    }


@pytest.mark.parametrize(
    ("store", "keyword_raises", "parent_map", "reason"),
    [
        (_UnavailableStore([]), False, {"p1_c1": "p1_c9"}, "semantic_unavailable"),
        (_Store([_item("p1_c0")]), True, {"p1_c0": "p1_c9"}, "keyword_search_failed"),
        (_Store([_item("p1_c0")]), False, {}, "parent_mapping_missing"),
    ],
)
def test_pipeline_parent_child_fail_closes_without_single_route_fallback(
    db, monkeypatch, store, keyword_raises, parent_map, reason
):
    def keyword_search(*args, **kwargs):
        if keyword_raises:
            raise RuntimeError("keyword failed")
        return [_item("p1_c1")]

    monkeypatch.setattr(
        "app.services.retrieval_pipeline.keyword_chunk_search", keyword_search
    )
    diagnostics = {}
    results = RetrievalPipeline(
        db, vector_store=store, parent_map=parent_map
    ).search(
        "query", top_k=5, profile="parent-child-v1", diagnostics=diagnostics
    )

    assert results == []
    assert diagnostics == {
        "requested_profile": "parent-child-v1",
        "effective_profile": "empty",
        "degraded": True,
        "reason": reason,
    }
