"""Batch 22H 确定性 HNSW train/dev Gate RED。"""

import copy

import pytest

from eval.deterministic_hnsw_gate import (
    evaluate_paired_dev,
    evaluate_train_repeatability,
)


def _items(prefix="p"):
    return [
        {
            "qa_id": f"q{index:02d}",
            "retrieved_ids": [f"{prefix}{index}_c0", f"{prefix}{index}_c1"],
            "degraded": False,
        }
        for index in range(24)
    ]


def _train_report():
    return {
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
            "resolver_version": "page-span-v2",
        },
        "pipeline": {
            "profile": "hybrid",
            "effective_profile": "hybrid",
            "lexical_profile": "bm25-bilingual",
            "split": "train",
            "evidence_resolver": "page-span-v2",
            "top_k": 5,
        },
        "diagnostics": {
            "runtime_degraded_count": 0,
            "vector_snapshot": {
                "database_chunk_count": 464,
                "vector_count": 464,
                "missing_vector_ids": 0,
                "extra_vector_ids": 0,
                "embedding_dimension": 1024,
                "vector_manifest_sha256": "5" * 64,
                "hnsw_binary_manifest_sha256": "7" * 64,
                "hnsw_num_threads": 1,
                "hnsw_search_ef": 464,
                "hnsw_config_sha256": "6" * 64,
                "hnsw_space": "cosine",
            },
        },
        "overall": {
            "recall@5": 2 / 3,
            "span_coverage@5": 2 / 3,
            "mrr": 0.4236111111111111,
            "ndcg@5": 0.4852888182323138,
            "n_positive": 24,
            "n_negative": 0,
        },
        "by_question_type": [
            {"question_type": "factoid", "n": 6, "recall": 0.5},
            {"question_type": "summary", "n": 6, "recall": 0.5},
            {"question_type": "method_detail", "n": 6, "recall": 5 / 6},
            {"question_type": "experiment_data", "n": 6, "recall": 5 / 6},
        ],
        "latency": {"count": 24, "p95": 320.0},
        "items": _items(),
        "with_llm": False,
    }


def test_train_repeatability_requires_exact_order_metrics_and_thresholds():
    first = _train_report()
    second = copy.deepcopy(first)
    second["latency"]["p95"] = 330.0

    gate = evaluate_train_repeatability(first, second)

    assert gate["passed"] is True
    assert gate["identical_top5"] == 24
    assert gate["checks"]["factoid_recall"]["actual"] == 0.5

    second["items"][3]["retrieved_ids"].reverse()
    assert evaluate_train_repeatability(first, second)["passed"] is False


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda report: report["run"].update(git_tracked_clean=False), "Git"),
        (
            lambda report: report["benchmark"].pop(
                "database_logical_manifest_sha256"
            ),
            "指纹",
        ),
        (
            lambda report: report["diagnostics"]["vector_snapshot"].update(
                hnsw_search_ef=10
            ),
            "HNSW",
        ),
    ],
)
def test_train_repeatability_fails_closed_on_unbound_report(mutation, message):
    first = _train_report()
    second = copy.deepcopy(first)
    mutation(second)
    with pytest.raises(ValueError, match=message):
        evaluate_train_repeatability(first, second)


def _paired_dev_report():
    report = {
        "gate_version": "deterministic-hnsw-paired-dev-v1",
        "run": {"git_sha": "a" * 40, "git_tracked_clean": True},
        "benchmark": {
            "dataset_sha256": "1" * 64,
            "qrels_sha256": "2" * 64,
            "corpus_manifest_sha256": "3" * 64,
            "database_logical_manifest_sha256": "3" * 64,
            "page_text_manifest_sha256": "4" * 64,
            "resolver_version": "page-span-v2",
        },
        "pipeline": {
            "profile": "hybrid",
            "lexical_profile": "bm25-bilingual",
            "split": "dev",
            "evidence_resolver": "page-span-v2",
            "top_k": 5,
        },
        "baseline": {
            "vector_manifest_sha256": "5" * 64,
            "hnsw_binary_manifest_sha256": "7" * 64,
            "hnsw_num_threads": None,
            "hnsw_search_ef": None,
            "runtime_degraded_count": 0,
            "overall": {
                "recall@5": 0.5,
                "factoid_recall": 0.0,
                "mrr": 0.25,
                "ndcg@5": 0.3,
            },
            "latency": {"count": 24, "p95": 250.0},
        },
        "candidate": {
            "vector_manifest_sha256": "5" * 64,
            "hnsw_binary_manifest_sha256": "7" * 64,
            "hnsw_num_threads": 1,
            "hnsw_search_ef": 464,
            "runtime_degraded_count": 0,
            "overall": {
                "recall@5": 0.55,
                "factoid_recall": 1 / 6,
                "mrr": 0.3,
                "ndcg@5": 0.35,
            },
            "latency": {"count": 24, "p95": 350.0},
        },
        "items": [
            {
                "qa_id": f"q{index:02d}",
                "baseline_retrieved_ids": [f"p{index}_c0"],
                "candidate_retrieved_ids": [f"p{index}_c0"],
            }
            for index in range(24)
        ],
    }
    return report


def test_paired_dev_requires_non_regression_and_one_strict_gain():
    report = _paired_dev_report()
    gate = evaluate_paired_dev(report)
    assert gate["passed"] is True
    assert gate["checks"]["strict_quality_gain"]["passed"] is True

    report["candidate"]["overall"] = copy.deepcopy(
        report["baseline"]["overall"]
    )
    assert evaluate_paired_dev(report)["passed"] is False


def test_paired_dev_rejects_holdout_or_snapshot_mismatch():
    report = _paired_dev_report()
    report["pipeline"]["split"] = "holdout"
    with pytest.raises(ValueError, match="dev"):
        evaluate_paired_dev(report)

    report = _paired_dev_report()
    report["candidate"]["vector_manifest_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="向量"):
        evaluate_paired_dev(report)
