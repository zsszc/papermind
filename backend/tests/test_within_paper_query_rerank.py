"""Batch 30：正确论文内全块查询定位候选的冻结契约。"""

from __future__ import annotations

from copy import deepcopy

import pytest

from app.models import Chunk, Paper
from app.services import retrieval_pipeline as retrieval
from eval import run


def _item(
    chunk_id: str, *, score: float = 1.0, source: str = "semantic"
) -> dict:
    paper_id = int(chunk_id.removeprefix("p").split("_c", 1)[0])
    return {
        "chunk_id": chunk_id,
        "paper_id": paper_id,
        "content": f"content-{chunk_id}",
        "score": score,
        "source": source,
    }


def test_local_bm25_reads_only_selected_papers_and_uses_bilingual_terms(db):
    db.add_all([
        Paper(id=1, title="一", filename="1.pdf", file_path="papers/1.pdf"),
        Paper(id=2, title="二", filename="2.pdf", file_path="papers/2.pdf"),
    ])
    db.commit()
    db.add_all([
        Chunk(paper_id=1, chunk_index=0, content="prototype classification"),
        Chunk(paper_id=1, chunk_index=1, content="unrelated"),
        Chunk(paper_id=2, chunk_index=0, content="prototype classification"),
    ])
    db.commit()

    results = retrieval.within_paper_bm25_search(
        db, "原型分类", paper_ids=[1]
    )

    assert [row["chunk_id"] for row in results] == ["p1_c0"]
    assert {row["source"] for row in results} == {"within-paper-bm25-bilingual"}


def test_zero_score_slot_is_replaced_without_changing_paper_slots():
    semantic = [_item("p1_c0"), _item("p2_c0"), _item("p3_c0")]
    keyword = [_item("p1_c0", source="keyword")]
    local = [_item("p2_c1", score=3.0, source="local")]
    original = deepcopy((semantic, keyword, local))
    baseline = retrieval.rrf_fuse_chunks(semantic, keyword, 3)

    candidate = retrieval.within_paper_query_fuse_chunks(
        semantic, keyword, local, 3
    )

    assert [row["paper_id"] for row in candidate] == [
        row["paper_id"] for row in baseline
    ]
    assert [row["chunk_id"] for row in candidate] == [
        "p1_c0", "p2_c1", "p3_c0"
    ]
    assert (semantic, keyword, local) == original


def test_positive_incumbent_is_locked_even_when_alternative_scores_higher():
    semantic = [_item("p1_c0")]
    keyword = []
    local = [
        _item("p1_c1", score=9.0, source="local"),
        _item("p1_c0", score=1.0, source="local"),
    ]

    candidate = retrieval.within_paper_query_fuse_chunks(
        semantic, keyword, local, 1
    )

    assert [row["chunk_id"] for row in candidate] == ["p1_c0"]


@pytest.mark.parametrize(
    "local",
    [
        [_item("p1_c1"), _item("p1_c1")],
        [{**_item("p1_c1"), "paper_id": 2}],
        [_item("p9_c0")],
    ],
)
def test_candidate_rejects_duplicate_malformed_or_unselected_local_rows(local):
    with pytest.raises(retrieval.WithinPaperContractError):
        retrieval.within_paper_query_fuse_chunks(
            [_item("p1_c0")], [], local, 1
        )


def test_pipeline_batches_local_search_and_exposes_explicit_profile(monkeypatch):
    semantic = [_item("p1_c0"), _item("p2_c0")]
    keyword = [_item("p1_c0", source="keyword")]
    calls = []

    class Store:
        def available(self):
            return True

        def search(self, **kwargs):
            assert kwargs["top_k"] == 10
            return deepcopy(semantic)

    monkeypatch.setattr(
        retrieval, "keyword_chunk_search",
        lambda *args, **kwargs: deepcopy(keyword),
    )

    def local_search(db, query, *, paper_ids, filters):
        calls.append((query, tuple(paper_ids), filters))
        return [_item("p2_c1", score=3.0, source="local")]

    monkeypatch.setattr(retrieval, "within_paper_bm25_search", local_search)
    diagnostics = {}
    results = retrieval.RetrievalPipeline(
        None, vector_store=Store()
    ).search(
        "原型分类",
        top_k=5,
        profile="within-paper-query-rerank-v1",
        lexical_profile="bm25-bilingual",
        diagnostics=diagnostics,
    )

    assert calls == [("原型分类", (1, 2), {})]
    assert [row["chunk_id"] for row in results] == ["p1_c0", "p2_c1"]
    assert diagnostics["effective_profile"] == "within-paper-query-rerank-v1"
    assert diagnostics["degraded"] is False


def test_pipeline_fails_closed_when_local_query_raises(monkeypatch):
    class Store:
        def available(self):
            return True

        def search(self, **kwargs):
            return [_item("p1_c0")]

    monkeypatch.setattr(
        retrieval, "keyword_chunk_search", lambda *args, **kwargs: []
    )

    def fail(*args, **kwargs):
        raise RuntimeError("private detail")

    monkeypatch.setattr(retrieval, "within_paper_bm25_search", fail)
    diagnostics = {}
    results = retrieval.RetrievalPipeline(
        None, vector_store=Store()
    ).search(
        "query", profile="within-paper-query-rerank-v1",
        lexical_profile="bm25-bilingual", diagnostics=diagnostics,
    )

    assert results == []
    assert diagnostics == {
        "requested_profile": "within-paper-query-rerank-v1",
        "effective_profile": "empty",
        "degraded": True,
        "reason": "within_paper_query_failed",
    }


def test_eval_candidate_requires_full_train_and_frozen_configuration(tmp_path):
    parser = run.build_parser()
    common = [
        "--dataset", "eval/private/qa_private_v2.jsonl",
        "--database", str(tmp_path / "papers.db"),
        "--corpus-root", str(tmp_path / "corpus"),
        "--vector-dir", str(tmp_path / "vectors"),
        "--retrieval-profile", "within-paper-query-rerank-v1",
        "--lexical-profile", "bm25-bilingual",
        "--evidence-resolver", "page-span-v2", "--split", "train",
    ]
    safe = parser.parse_args(common)
    assert run._validate_cli_args(safe) is None
    safe.split = "dev"
    assert "只允许完整 train" in run._validate_cli_args(safe)
    safe.split = "train"
    safe.qa_id = ["private-train-001"]
    assert "QA 子集" in run._validate_cli_args(safe)


def test_within_paper_contract_is_stable_and_complete():
    first = run.within_paper_query_contract_metadata()
    second = run.within_paper_query_contract_metadata()

    assert first == second
    assert first["algorithm"] == "within-paper-query-rerank-v1"
    assert first["route_limit"] == 10
    assert first["local_scope"] == "production-selected-papers-all-chunks"
    assert first["positive_incumbent"] == "lock-in-place"
    assert len(first["formula_sha256"]) == 64
