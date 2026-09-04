"""Batch 31：论文内语义深度聚合与单向量复用契约。"""

from __future__ import annotations

from copy import deepcopy

import pytest

from eval.within_paper_semantic_diagnostics import (
    WithinPaperSemanticCollectionError,
    analyze_within_paper_semantic,
    collect_within_paper_semantic_records,
    require_offline_environment,
    validate_within_paper_semantic_records,
)


def _binding() -> dict:
    return {
        "git_sha": "a" * 40,
        "git_tracked_clean": True,
        "dataset_sha256": "1" * 64,
        "qrels_sha256": "2" * 64,
        "corpus_manifest_sha256": "3" * 64,
        "database_logical_manifest_sha256": "3" * 64,
        "page_text_manifest_sha256": "4" * 64,
        "vector_manifest_sha256": "5" * 64,
        "hnsw_config_sha256": "6" * 64,
        "hnsw_binary_manifest_sha256": "7" * 64,
        "vector_source_tree_sha256": "8" * 64,
        "split": "train",
        "evidence_resolver": "page-span-v2",
        "lexical_profile": "bm25-bilingual",
        "semantic_rerank": False,
        "top_k": 5,
        "production_route_limit": 10,
        "filtered_latency_p95_limit_ms": 250.0,
    }


def _group(chunk_id: str) -> dict:
    return {
        "page_start": 10,
        "page_end": 20,
        "chunks": [{
            "chunk_id": chunk_id,
            "page_start": 0,
            "page_end": 30,
        }],
    }


def _record(index: int, category: str, latency_ms: float = 40.0) -> dict:
    qtype = "factoid" if index < 8 else "method_detail" if index < 12 else "summary"
    relevant = f"p{index + 1}_c9"
    baseline = [f"p{index + 1}_c0"] + [
        f"p{100 + index + slot}_c0" for slot in range(4)
    ]
    within = [[f"p{index + 1}_c{rank}" for rank in range(12)]]
    if category == "baseline_full":
        baseline[0] = relevant
    elif category == "recoverable":
        pass
    elif category == "semantic_missing":
        within = [[f"p{index + 1}_c{rank}" for rank in range(9)]]
    elif category == "paper_absent":
        baseline[0] = f"p{300 + index}_c0"
        within = [[f"p{300 + index}_c{rank}" for rank in range(3)]]
    else:
        raise AssertionError(category)
    return {
        "question_type": qtype,
        "evidence_groups": [_group(relevant)],
        "baseline_ids": baseline,
        "within_paper_routes": within,
        "embedding_call_count": 1,
        "filtered_query_count": len(within),
        "filtered_latency_ms": latency_ms,
    }


def _records() -> list[dict]:
    categories = (
        ["baseline_full"] * 5
        + ["recoverable"] * 4
        + ["semantic_missing"] * 3
        + ["paper_absent"]
    )
    return [_record(index, value) for index, value in enumerate(categories)]


def test_aggregate_classifies_rank_latency_and_recommends_candidate():
    report = analyze_within_paper_semantic(_records(), _binding())

    assert report["schema"] == "within-paper-semantic-diagnostics-v1"
    assert report["total_items"] == 13
    assert report["categories"] == [
        {"category": "baseline_full", "count": 5, "share": 5 / 13},
        {"category": "within_paper_semantic_recoverable", "count": 4, "share": 4 / 13},
        {"category": "selected_paper_semantic_missing", "count": 3, "share": 3 / 13},
        {"category": "relevant_paper_not_selected", "count": 1, "share": 1 / 13},
    ]
    assert report["evidence_first_rank"] == {
        "1-5": 0,
        "6-10": 9,
        "11-20": 0,
        "21-50": 0,
        "over_50": 0,
        "not_found": 4,
    }
    assert report["coverage"]["recoverable_count"] == 4
    assert report["latency"]["filtered_query_total_ms"]["p95"] == 40.0
    assert report["recommendation"]["candidate"] == "within-paper-semantic-rerank-v1"


def test_gate_rejects_slow_or_nonrecoverable_diagnostic():
    slow = _records()
    slow[-1]["filtered_latency_ms"] = 400.0
    assert analyze_within_paper_semantic(slow, _binding())["recommendation"]["candidate"] == "none"

    no_gain = [_record(i, "baseline_full") for i in range(13)]
    assert analyze_within_paper_semantic(no_gain, _binding())["recommendation"]["candidate"] == "none"


def test_analysis_is_deterministic_and_emits_no_private_identity():
    records = _records()
    original = deepcopy(records)
    first = analyze_within_paper_semantic(records, _binding())
    assert first == analyze_within_paper_semantic(records, _binding())
    assert records == original
    rendered = str(first).lower()
    for forbidden in ("qa_id", "chunk_id", "paper_id", "question", "p1_c9", "/users/"):
        assert forbidden not in rendered


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        (lambda rows, binding: rows.pop(), "13"),
        (lambda rows, binding: rows[0].update(embedding_call_count=2), "一次"),
        (lambda rows, binding: rows[0].update(filtered_latency_ms=-1), "时延"),
        (lambda rows, binding: binding.update(split="dev"), "train"),
        (lambda rows, binding: binding.update(secret="value"), "未知字段"),
        (lambda rows, binding: rows[0]["within_paper_routes"][0].append("bad"), "畸形"),
    ],
)
def test_invalid_inputs_fail_closed(mutation, match):
    records = _records()
    binding = _binding()
    mutation(records, binding)
    with pytest.raises(ValueError, match=match):
        validate_within_paper_semantic_records(records, binding)


def test_collector_reuses_exactly_one_embedding_and_limits_paper_scope(monkeypatch):
    calls = []

    class Embedding:
        def embed_query(self, query):
            calls.append(("embed", query))
            return [0.1, 0.2]

    class Collection:
        def query(self, **kwargs):
            calls.append(("query", deepcopy(kwargs)))
            where = kwargs.get("where")
            if where is None:
                return {
                    "ids": [[f"p{index + 1}_c0" for index in range(20)]],
                    "metadatas": [[{"paper_id": index + 1} for index in range(20)]],
                    "distances": [[0.1] * 20],
                }
            paper_id = where["paper_id"]
            return {
                "ids": [[f"p{paper_id}_c{index}" for index in range(12)]],
                "metadatas": [[{"paper_id": paper_id} for _ in range(12)]],
                "distances": [[0.1] * 12],
            }

    class Store:
        embedding_service = Embedding()
        collection = Collection()

    def fake_keyword(db, query, limit, **kwargs):
        assert (db, query, limit) == ("readonly-db", "private question", 10)
        return [{"chunk_id": f"p{index + 101}_c0", "paper_id": index + 101} for index in range(10)]

    monkeypatch.setattr("app.services.retrieval_pipeline.keyword_chunk_search", fake_keyword)
    records = collect_within_paper_semantic_records(
        "readonly-db",
        [{"qa_id": "private-id", "question": "private question", "question_type": "factoid"}],
        {"private-id": [_group("p1_c9")]},
        Store(),
        {paper_id: 12 for paper_id in range(1, 201)},
    )

    assert len(records) == 1
    assert sum(call[0] == "embed" for call in calls) == 1
    query_calls = [call[1] for call in calls if call[0] == "query"]
    assert query_calls[0]["n_results"] == 20
    assert query_calls[0].get("where") is None
    selected = {call["where"]["paper_id"] for call in query_calls[1:]}
    assert selected == {1, 2, 3}
    assert all(call["query_embeddings"] == [[0.1, 0.2]] for call in query_calls)


def test_collector_fails_closed_on_scope_mismatch(monkeypatch):
    class Embedding:
        def embed_query(self, query):
            return [0.1]

    class Collection:
        count = lambda self: 20

        def query(self, **kwargs):
            if kwargs.get("where") is None:
                ids = [f"p{index + 1}_c0" for index in range(20)]
                papers = list(range(1, 21))
            else:
                ids = ["p999_c0"]
                papers = [999]
            return {"ids": [ids], "metadatas": [[{"paper_id": p} for p in papers]], "distances": [[0.1] * len(ids)]}

    class Store:
        embedding_service = Embedding()
        collection = Collection()

    monkeypatch.setattr(
        "app.services.retrieval_pipeline.keyword_chunk_search",
        lambda *args, **kwargs: [{"chunk_id": f"p{index + 101}_c0", "paper_id": index + 101} for index in range(10)],
    )
    with pytest.raises(WithinPaperSemanticCollectionError, match="filtered-contract"):
        collect_within_paper_semantic_records(
            None,
            [{"qa_id": "q", "question": "secret", "question_type": "factoid"}],
            {"q": [_group("p1_c9")]},
            Store(),
            {paper_id: 1 for paper_id in range(1, 201)},
        )


def test_offline_environment_is_mandatory():
    require_offline_environment({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"})
    with pytest.raises(ValueError, match="OFFLINE"):
        require_offline_environment({"HF_HUB_OFFLINE": "1"})
