"""Batch 29：保论文集合的深层证据候选冻结契约。"""

from __future__ import annotations

from copy import deepcopy

import pytest

from app.services import retrieval_pipeline as retrieval
from eval import run


def _item(chunk_id: str, *, source: str = "semantic") -> dict:
    paper_id = int(chunk_id.removeprefix("p").split("_c", 1)[0])
    return {
        "chunk_id": chunk_id,
        "paper_id": paper_id,
        "content": f"content-{chunk_id}",
        "score": 1.0,
        "source": source,
    }


def _deep_routes() -> tuple[list[dict], list[dict]]:
    semantic = [_item(f"p{paper}_c0") for paper in range(1, 11)]
    semantic.append(_item("p1_c1"))
    keyword = [
        _item(f"p{paper}_c0", source="keyword")
        for paper in range(11, 21)
    ]
    keyword.append(_item("p1_c1", source="keyword"))
    return semantic, keyword


def test_deep_cross_route_hit_replaces_chunk_without_changing_paper_slots():
    semantic, keyword = _deep_routes()
    original = deepcopy((semantic, keyword))
    baseline = retrieval.rrf_fuse_chunks(semantic[:10], keyword[:10], 5)

    candidate = retrieval.paper_preserving_deep_route_fuse_chunks(
        semantic, keyword, 5
    )

    assert [row["paper_id"] for row in candidate] == [
        row["paper_id"] for row in baseline
    ]
    assert [row["chunk_id"] for row in baseline] == [
        "p1_c0", "p11_c0", "p2_c0", "p12_c0", "p3_c0",
    ]
    assert [row["chunk_id"] for row in candidate] == [
        "p1_c1", "p11_c0", "p2_c0", "p12_c0", "p3_c0",
    ]
    assert (semantic, keyword) == original


def test_candidate_preserves_exact_output_when_deep_pool_adds_no_signal():
    semantic = [_item(f"p{paper}_c0") for paper in range(1, 11)]
    keyword = [_item("p3_c0", source="keyword")]
    baseline = retrieval.rrf_fuse_chunks(semantic[:10], keyword[:10], 5)

    candidate = retrieval.paper_preserving_deep_route_fuse_chunks(
        semantic, keyword, 5
    )

    assert [row["chunk_id"] for row in candidate] == [
        row["chunk_id"] for row in baseline
    ]


def test_candidate_preserves_repeated_paper_slot_sequence_and_quota():
    semantic = [
        _item("p1_c0"), _item("p1_c1"), _item("p2_c0"),
        _item("p3_c0"), _item("p4_c0"), _item("p5_c0"),
    ]
    keyword = [_item("p1_c0", source="keyword")]
    baseline = retrieval.rrf_fuse_chunks(semantic[:10], keyword[:10], 5)

    candidate = retrieval.paper_preserving_deep_route_fuse_chunks(
        semantic, keyword, 5
    )

    assert [row["paper_id"] for row in candidate] == [
        row["paper_id"] for row in baseline
    ]
    assert len({row["chunk_id"] for row in candidate}) == len(candidate)


@pytest.mark.parametrize(
    "semantic",
    [
        [_item("p1_c0"), _item("p1_c0")],
        [{**_item("p1_c0"), "paper_id": 2}],
        [{**_item("p1_c0"), "chunk_id": "invalid"}],
    ],
)
def test_candidate_rejects_duplicate_or_malformed_route_items(semantic):
    with pytest.raises(retrieval.DeepRouteContractError):
        retrieval.paper_preserving_deep_route_fuse_chunks(semantic, [], 5)


def test_pipeline_exposes_candidate_only_by_explicit_profile(monkeypatch):
    semantic, keyword = _deep_routes()

    class Store:
        def available(self):
            return True

        def search(self, **kwargs):
            assert kwargs["top_k"] == 20
            return deepcopy(semantic)

    def keyword_search(*args, **kwargs):
        assert args[2] == 20
        return deepcopy(keyword)

    monkeypatch.setattr(retrieval, "keyword_chunk_search", keyword_search)
    diagnostics = {}
    results = retrieval.RetrievalPipeline(
        None, vector_store=Store()
    ).search(
        "query",
        top_k=5,
        profile="paper-preserving-deep-route-v1",
        lexical_profile="bm25-bilingual",
        rerank=False,
        diagnostics=diagnostics,
    )

    assert [row["paper_id"] for row in results] == [1, 11, 2, 12, 3]
    assert results[0]["chunk_id"] == "p1_c1"
    assert diagnostics == {
        "requested_profile": "paper-preserving-deep-route-v1",
        "effective_profile": "paper-preserving-deep-route-v1",
        "degraded": False,
        "reason": None,
    }


def test_pipeline_fails_closed_when_candidate_contract_is_invalid(monkeypatch):
    duplicate = [_item("p1_c0"), _item("p1_c0")]

    class Store:
        def available(self):
            return True

        def search(self, **kwargs):
            return [_item("p1_c0")]

    monkeypatch.setattr(
        retrieval, "keyword_chunk_search", lambda *args, **kwargs: duplicate
    )
    diagnostics = {}
    results = retrieval.RetrievalPipeline(
        None, vector_store=Store()
    ).search(
        "query",
        profile="paper-preserving-deep-route-v1",
        lexical_profile="bm25-bilingual",
        diagnostics=diagnostics,
    )

    assert results == []
    assert diagnostics == {
        "requested_profile": "paper-preserving-deep-route-v1",
        "effective_profile": "empty",
        "degraded": True,
        "reason": "deep_route_contract_invalid",
    }


def test_eval_candidate_requires_full_train_and_frozen_configuration(tmp_path):
    parser = run.build_parser()
    common = [
        "--dataset", "eval/private/qa_private_v2.jsonl",
        "--database", str(tmp_path / "papers.db"),
        "--corpus-root", str(tmp_path / "corpus"),
        "--vector-dir", str(tmp_path / "vectors"),
        "--retrieval-profile", "paper-preserving-deep-route-v1",
        "--lexical-profile", "bm25-bilingual",
        "--evidence-resolver", "page-span-v2",
        "--split", "train",
    ]
    safe = parser.parse_args(common)
    assert run._validate_cli_args(safe) is None

    safe.split = "dev"
    assert "只允许完整 train" in run._validate_cli_args(safe)
    safe.split = "train"
    safe.qa_id = ["private-train-001"]
    assert "QA 子集" in run._validate_cli_args(safe)
    safe.qa_id = []
    safe.lexical_profile = "count"
    assert "bm25-bilingual" in run._validate_cli_args(safe)


def test_deep_route_contract_is_stable_and_complete():
    first = run.paper_preserving_deep_route_contract_metadata()
    second = run.paper_preserving_deep_route_contract_metadata()

    assert first == second
    assert first["algorithm"] == "paper-preserving-deep-route-v1"
    assert first["production_route_limit"] == 10
    assert first["candidate_route_limit"] == 20
    assert first["paper_slots"] == "production-legacy-rrf-order-and-quota"
    assert len(first["formula_sha256"]) == 64
