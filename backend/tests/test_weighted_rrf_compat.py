"""Batch 22G：Legacy-Compatible Weighted-RRF RED。"""

from copy import deepcopy
import importlib

import pytest


def _module():
    return importlib.import_module("app.services.retrieval_pipeline")


def _item(chunk_id="p1_c0", *, source="semantic", marker=None):
    item = {
        "content": marker or f"content-{chunk_id}",
        "source": source,
        "nested": {"value": marker or str(chunk_id)},
    }
    if chunk_id is not ...:
        item["chunk_id"] = chunk_id
    return item


@pytest.mark.parametrize("top_k", [-1, 0, 1, 2, 10])
@pytest.mark.parametrize("k", [1, 60, 60.0])
@pytest.mark.parametrize(
    ("semantic", "lexical"),
    [
        ([], []),
        ([_item("p2_c0"), _item("p1_c0")], [_item("p1_c0")]),
        ([_item("p1_c0"), _item("p1_c0", marker="duplicate")], []),
        ([_item(..., source="p3_c-1")], [_item(..., source="ordinary")]),
        ([_item(None, source="p4_c0")], [_item("legacy-id")]),
        ([_item(0), _item("")], [_item("p9_c0")]),
    ],
)
def test_compat_equal_weight_matches_legacy_for_accepted_inputs(
    semantic, lexical, top_k, k
):
    module = _module()
    before_semantic = deepcopy(semantic)
    before_lexical = deepcopy(lexical)

    expected = module.rrf_fuse_chunks(semantic, lexical, top_k, k=k)
    actual = module.weighted_rrf_compat_fuse_chunks(
        semantic,
        lexical,
        top_k,
        semantic_weight=1.0,
        keyword_weight=1.0,
        k=k,
    )

    assert actual == expected
    assert semantic == before_semantic
    assert lexical == before_lexical
    if actual:
        actual[0]["nested"]["value"] = "mutated"
        assert semantic == before_semantic
        assert lexical == before_lexical


def test_compat_preserves_raw_rank_duplicate_and_first_seen_metadata():
    fuse = _module().weighted_rrf_compat_fuse_chunks
    semantic = [
        _item(..., source="ordinary"),
        _item("p8_c0", marker="semantic-first"),
    ]
    lexical = [
        _item("p1_c0", source="keyword"),
        _item("p1_c0", source="keyword", marker="duplicate"),
        _item("p8_c0", source="keyword", marker="lexical-later"),
    ]

    results = fuse(semantic, lexical, 2, keyword_weight=1.5)

    assert [row["chunk_id"] for row in results] == ["p1_c0", "p8_c0"]
    assert results[1]["content"] == "semantic-first"


def test_compat_keeps_source_fallback_and_first_seen_tie():
    fuse = _module().weighted_rrf_compat_fuse_chunks
    semantic = [_item(..., source="p9_c0", marker="fallback-first")]
    lexical = [_item("p1_c0", source="keyword")]

    results = fuse(semantic, lexical, 2)

    assert [row.get("source") for row in results] == ["p9_c0", "keyword"]
    assert "chunk_id" not in results[0]


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan"), True, "1"])
def test_compat_rejects_invalid_new_weights(value):
    with pytest.raises(ValueError, match="有限正数"):
        _module().weighted_rrf_compat_fuse_chunks(
            [_item("p1_c0")], [], 5, keyword_weight=value
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


def test_compat_pipeline_isolated_and_equal_weight_matches_hybrid(db, monkeypatch):
    module = _module()
    semantic = [_item("p9_c0"), _item("p2_c0")]
    lexical = [_item("p2_c0", source="keyword"), _item("p1_c0", source="keyword")]
    store = _Store(semantic)
    monkeypatch.setattr(module, "keyword_chunk_search", lambda *a, **k: deepcopy(lexical))
    pipeline = module.RetrievalPipeline(db, vector_store=store)
    diagnostics = {}

    compat = pipeline.search(
        "query", top_k=2, profile="weighted-rrf-compat-v1",
        lexical_profile="bm25-bilingual", rrf_lexical_weight=1.0,
        diagnostics=diagnostics,
    )
    hybrid = pipeline.search(
        "query", top_k=2, profile="hybrid",
        lexical_profile="bm25-bilingual", diagnostics={},
    )

    assert compat == hybrid
    assert [call["top_k"] for call in store.calls] == [4, 4]
    assert diagnostics == {
        "requested_profile": "weighted-rrf-compat-v1",
        "effective_profile": "weighted-rrf-compat-v1",
        "degraded": False,
        "reason": None,
    }


@pytest.mark.parametrize(
    ("available", "keyword_raises", "reason"),
    [
        (False, False, "semantic_unavailable"),
        (True, True, "keyword_search_failed"),
    ],
)
def test_compat_pipeline_fails_closed(db, monkeypatch, available, keyword_raises, reason):
    module = _module()

    def keyword(*args, **kwargs):
        if keyword_raises:
            raise RuntimeError("offline failure")
        return [_item("p1_c0", source="keyword")]

    monkeypatch.setattr(module, "keyword_chunk_search", keyword)
    diagnostics = {}
    results = module.RetrievalPipeline(
        db, vector_store=_Store([_item("p9_c0")], available=available)
    ).search(
        "query", top_k=5, profile="weighted-rrf-compat-v1",
        lexical_profile="bm25-bilingual", rrf_lexical_weight=1.25,
        diagnostics=diagnostics,
    )

    assert results == []
    assert diagnostics == {
        "requested_profile": "weighted-rrf-compat-v1",
        "effective_profile": "empty",
        "degraded": True,
        "reason": reason,
    }
