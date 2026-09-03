"""Batch 27B：论文优先证据重排候选的冻结算法契约。"""

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


def test_paper_prior_promotes_second_chunk_without_long_paper_monopoly():
    semantic = [
        _item("p1_c0"),
        _item("p2_c0"),
        _item("p3_c0"),
        _item("p4_c0"),
        _item("p5_c0"),
        _item("p1_c1"),
    ]
    keyword = [_item("p1_c0", source="keyword")]

    baseline = retrieval.rrf_fuse_chunks(semantic, keyword, 5)
    candidate = retrieval.paper_first_evidence_fuse_chunks(
        semantic, keyword, 5
    )

    assert [row["chunk_id"] for row in baseline] == [
        "p1_c0", "p2_c0", "p3_c0", "p4_c0", "p5_c0",
    ]
    assert [row["chunk_id"] for row in candidate][:2] == ["p1_c0", "p1_c1"]
    assert max(
        sum(row["paper_id"] == paper for row in candidate)
        for paper in {row["paper_id"] for row in candidate}
    ) == 2


def test_single_chunk_per_paper_preserves_production_order_and_inputs():
    semantic = [_item(f"p{paper}_c0") for paper in range(1, 7)]
    keyword = [_item("p3_c0", source="keyword")]
    original = deepcopy((semantic, keyword))

    baseline = retrieval.rrf_fuse_chunks(semantic, keyword, 5)
    candidate = retrieval.paper_first_evidence_fuse_chunks(
        semantic, keyword, 5
    )

    assert [row["chunk_id"] for row in candidate] == [
        row["chunk_id"] for row in baseline
    ]
    assert (semantic, keyword) == original


@pytest.mark.parametrize(
    "semantic",
    [
        [_item("p1_c0"), _item("p1_c0")],
        [{**_item("p1_c0"), "paper_id": 2}],
        [{**_item("p1_c0"), "chunk_id": "invalid"}],
    ],
)
def test_candidate_rejects_duplicate_or_malformed_route_items(semantic):
    with pytest.raises(retrieval.PaperFirstRerankError):
        retrieval.paper_first_evidence_fuse_chunks(semantic, [], 5)


def test_pipeline_exposes_candidate_only_by_explicit_profile(monkeypatch):
    semantic = [_item("p1_c0"), _item("p1_c1")]
    keyword = [_item("p1_c0", source="keyword")]

    class Store:
        def available(self):
            return True

        def search(self, **kwargs):
            assert kwargs["top_k"] == 20
            return deepcopy(semantic)

    monkeypatch.setattr(
        retrieval,
        "keyword_chunk_search",
        lambda *args, **kwargs: deepcopy(keyword),
    )
    diagnostics = {}
    results = retrieval.RetrievalPipeline(
        None, vector_store=Store()
    ).search(
        "query",
        top_k=5,
        profile="paper-first-evidence-rerank-v1",
        lexical_profile="bm25-bilingual",
        rerank=False,
        diagnostics=diagnostics,
    )

    assert [row["chunk_id"] for row in results] == ["p1_c0", "p1_c1"]
    assert diagnostics == {
        "requested_profile": "paper-first-evidence-rerank-v1",
        "effective_profile": "paper-first-evidence-rerank-v1",
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
        retrieval,
        "keyword_chunk_search",
        lambda *args, **kwargs: deepcopy(duplicate),
    )
    diagnostics = {}
    results = retrieval.RetrievalPipeline(
        None, vector_store=Store()
    ).search(
        "query",
        profile="paper-first-evidence-rerank-v1",
        lexical_profile="bm25-bilingual",
        diagnostics=diagnostics,
    )

    assert results == []
    assert diagnostics == {
        "requested_profile": "paper-first-evidence-rerank-v1",
        "effective_profile": "empty",
        "degraded": True,
        "reason": "paper_first_contract_invalid",
    }


def test_eval_candidate_requires_full_train_and_frozen_configuration(tmp_path):
    parser = run.build_parser()
    common = [
        "--dataset", "eval/private/qa_private_v2.jsonl",
        "--database", str(tmp_path / "papers.db"),
        "--corpus-root", str(tmp_path / "corpus"),
        "--vector-dir", str(tmp_path / "vectors"),
        "--retrieval-profile", "paper-first-evidence-rerank-v1",
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


def test_paper_first_contract_is_stable_and_complete():
    first = run.paper_first_contract_metadata()
    second = run.paper_first_contract_metadata()

    assert first == second
    assert first["algorithm"] == "paper-first-evidence-rerank-v1"
    assert first["route_limit"] == 20
    assert first["paper_bonus"] == 0.25
    assert first["max_chunks_per_paper"] == 2
    assert len(first["formula_sha256"]) == 64
