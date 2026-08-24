"""Batch 22F：Weighted-RRF 纯函数与隔离管线 RED。"""

from copy import deepcopy
import importlib

import pytest


def _module():
    return importlib.import_module("app.services.retrieval_pipeline")


def _item(chunk_id, *, source="semantic", marker=None):
    paper, chunk = chunk_id.removeprefix("p").split("_c")
    return {
        "chunk_id": chunk_id,
        "paper_id": int(paper),
        "content": marker or f"content-{chunk_id}",
        "source": source,
        "score": 1.0,
    }


def test_weighted_rrf_uses_exact_formula_compact_rank_and_chunk_id_tie():
    fuse = _module().weighted_rrf_fuse_chunks
    semantic = [_item("p8_c0"), _item("p8_c0"), _item("p1_c0")]
    lexical = [
        _item("p7_c0", source="keyword"),
        _item("p9_c0", source="keyword"),
    ]

    results = fuse(semantic, lexical, 5)

    assert [row["chunk_id"] for row in results] == [
        "p7_c0", "p8_c0", "p1_c0", "p9_c0",
    ]


def test_weighted_rrf_combines_routes_and_prefers_semantic_metadata():
    fuse = _module().weighted_rrf_fuse_chunks
    semantic = [
        _item("p9_c0"),
        _item("p2_c0", marker="semantic metadata"),
    ]
    lexical = [
        _item("p1_c0", source="keyword"),
        _item("p2_c0", source="keyword", marker="keyword metadata"),
    ]

    results = fuse(semantic, lexical, 3)

    assert [row["chunk_id"] for row in results] == [
        "p2_c0", "p1_c0", "p9_c0",
    ]
    assert results[0]["content"] == "semantic metadata"


def test_weighted_rrf_normative_equal_weight_parity_and_copy_isolation():
    module = _module()
    semantic = [_item("p3_c0"), _item("p2_c0")]
    lexical = [_item("p2_c0", source="keyword")]
    before_semantic = deepcopy(semantic)
    before_lexical = deepcopy(lexical)

    old = module.rrf_fuse_chunks(semantic, lexical, 2)
    new = module.weighted_rrf_fuse_chunks(
        semantic, lexical, 2, semantic_weight=1.0, keyword_weight=1.0
    )
    assert new == old
    new[0]["content"] = "mutated"
    assert semantic == before_semantic
    assert lexical == before_lexical


@pytest.mark.parametrize(
    "kwargs",
    [
        {"semantic_weight": 0},
        {"keyword_weight": float("inf")},
        {"k": 0},
    ],
)
def test_weighted_rrf_rejects_invalid_contract(kwargs):
    with pytest.raises(ValueError):
        _module().weighted_rrf_fuse_chunks(
            [_item("p1_c0")], [], 5, **kwargs
        )
    with pytest.raises(ValueError, match="canonical"):
        _module().weighted_rrf_fuse_chunks(
            [{"source": "p1_c0", "content": "legacy fallback"}], [], 5
        )


class _Store:
    def __init__(self, results, *, available=True):
        self.results = results
        self._available = available
        self.calls = []

    def available(self):
        return self._available

    def search(self, **kwargs):
        self.calls.append(dict(kwargs))
        return deepcopy(self.results)


def test_weighted_pipeline_is_explicit_and_keeps_hybrid_unchanged(db, monkeypatch):
    module = _module()
    store = _Store([_item("p9_c0")])
    monkeypatch.setattr(
        module,
        "keyword_chunk_search",
        lambda *args, **kwargs: [_item("p1_c0", source="keyword")],
    )
    pipeline = module.RetrievalPipeline(db, vector_store=store)
    diagnostics = {}

    weighted = pipeline.search(
        "query", top_k=1, profile="weighted-rrf-v1",
        lexical_profile="bm25-bilingual", rrf_lexical_weight=1.5,
        diagnostics=diagnostics,
    )
    hybrid = pipeline.search(
        "query", top_k=1, profile="hybrid",
        lexical_profile="bm25-bilingual", diagnostics={},
    )

    assert [row["chunk_id"] for row in weighted] == ["p1_c0"]
    assert [row["chunk_id"] for row in hybrid] == ["p9_c0"]
    assert [call["top_k"] for call in store.calls] == [2, 2]
    assert diagnostics == {
        "requested_profile": "weighted-rrf-v1",
        "effective_profile": "weighted-rrf-v1",
        "degraded": False,
        "reason": None,
    }


def test_weighted_pipeline_fails_closed_when_semantic_is_unavailable(
    db, monkeypatch
):
    module = _module()
    monkeypatch.setattr(
        module,
        "keyword_chunk_search",
        lambda *args, **kwargs: [_item("p1_c0", source="keyword")],
    )
    diagnostics = {}
    results = module.RetrievalPipeline(
        db, vector_store=_Store([], available=False)
    ).search(
        "query", top_k=5, profile="weighted-rrf-v1",
        lexical_profile="bm25-bilingual", rrf_lexical_weight=1.25,
        diagnostics=diagnostics,
    )

    assert results == []
    assert diagnostics == {
        "requested_profile": "weighted-rrf-v1",
        "effective_profile": "empty",
        "degraded": True,
        "reason": "semantic_unavailable",
    }
