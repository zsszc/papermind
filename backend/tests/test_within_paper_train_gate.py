"""Batch 30：论文内全块查询候选的完整 train 配对 Gate。"""

from __future__ import annotations

import pytest

from eval.run import within_paper_query_contract_metadata
from eval.within_paper_train_gate import evaluate_within_paper_train


def _report(profile: str, *, coverage: float = 0.46) -> dict:
    candidate = profile == "within-paper-query-rerank-v1"
    contract = within_paper_query_contract_metadata()
    return {
        "report_schema": "2.0",
        "run": {"git_sha": "a" * 40, "git_tracked_clean": True},
        "benchmark": {
            "dataset_sha256": "1" * 64,
            "qrels_sha256": "2" * 64,
            "corpus_manifest_sha256": "3" * 64,
            "database_logical_manifest_sha256": "3" * 64,
            "page_text_manifest_sha256": "4" * 64,
            "vector_manifest_sha256": "5" * 64,
            "hnsw_config_sha256": "6" * 64,
            "hnsw_binary_manifest_sha256": "7" * 64,
            **({"within_paper_formula_sha256": contract["formula_sha256"]}
               if candidate else {}),
        },
        "pipeline": {
            "profile": profile,
            "effective_profile": profile,
            "lexical_profile": "bm25-bilingual",
            "semantic_rerank": None,
            "split": "train", "evidence_resolver": "page-span-v2",
            "top_k": 5,
            **({"within_paper_query": contract} if candidate else {}),
        },
        "diagnostics": {"runtime_degraded_count": 0},
        "with_llm": False,
        "overall": {
            "n_positive": 13, "n_negative": 0,
            "recall@5": 0.4, "mrr": 0.3, "ndcg@5": 0.3,
            "span_coverage@5": coverage,
        },
        "by_question_type": [
            {"question_type": "factoid", "n": 8, "recall": 0.4,
             "mrr": 0.3, "ndcg": 0.3, "span_coverage": coverage},
            {"question_type": "method_detail", "n": 4, "recall": 0.4,
             "mrr": 0.3, "ndcg": 0.3, "span_coverage": coverage},
            {"question_type": "summary", "n": 1, "recall": 0.0,
             "mrr": 0.0, "ndcg": 0.0, "span_coverage": 0.0},
        ],
        "latency": {"p95": 900.0, "count": 13},
        "items": [
            {"qa_id": f"private-{index}", "degraded": False}
            for index in range(13)
        ],
    }


def test_gate_passes_only_with_one_item_coverage_gain_and_no_regression():
    baseline = _report("hybrid", coverage=0.45)
    candidate = _report(
        "within-paper-query-rerank-v1", coverage=0.45 + 1 / 13
    )

    gate = evaluate_within_paper_train(baseline, candidate)

    assert gate["passed"] is True
    assert gate["checks"]["span_coverage_gain"]["passed"] is True
    assert "private-" not in str(gate)


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        (lambda base, cand: cand["run"].update(git_sha="b" * 40), "提交"),
        (lambda base, cand: cand["pipeline"].update(split="dev"), "train"),
        (lambda base, cand: cand["diagnostics"].update(
            runtime_degraded_count=1), "降级"),
        (lambda base, cand: cand["items"].pop(), "13"),
        (lambda base, cand: cand["benchmark"].update(
            dataset_sha256="9" * 64), "指纹"),
    ],
)
def test_gate_fails_closed_for_unpaired_or_incomplete_reports(mutation, match):
    baseline = _report("hybrid")
    candidate = _report("within-paper-query-rerank-v1", coverage=0.55)
    mutation(baseline, candidate)
    with pytest.raises(ValueError, match=match):
        evaluate_within_paper_train(baseline, candidate)


def test_gate_rejects_rank_type_or_latency_regression():
    baseline = _report("hybrid", coverage=0.45)
    candidate = _report("within-paper-query-rerank-v1", coverage=0.55)
    candidate["overall"]["ndcg@5"] = 0.29
    candidate["by_question_type"][1]["span_coverage"] = 0.39
    candidate["latency"]["p95"] = 1000.0

    gate = evaluate_within_paper_train(baseline, candidate)

    assert gate["passed"] is False
    assert gate["checks"]["ndcg_non_regression"]["passed"] is False
    assert gate["checks"]["method_detail_span_coverage_non_regression"][
        "passed"
    ] is False
    assert gate["checks"]["candidate_p95_ms"]["passed"] is False
