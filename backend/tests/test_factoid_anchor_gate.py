"""Batch 22I factoid 锚点配对 Harness/Gate RED。"""

from __future__ import annotations

import copy

import pytest

from eval.factoid_anchor_gate import evaluate_anchor_train
from eval.paired_anchor_eval import evaluate_anchor_paired_routes
from eval.run import factoid_anchor_contract_metadata


def _paired_report() -> dict:
    contract = factoid_anchor_contract_metadata()
    types = ("factoid", "summary", "method_detail", "experiment_data")
    items = []
    for index in range(24):
        qtype = types[index // 6]
        baseline_hit = index not in {0, 1, 6, 12}
        candidate_hit = baseline_hit or index == 0
        items.append({
            "qa_id": f"q{index:02d}",
            "question_type": qtype,
            "has_anchor": index == 0,
            "baseline_retrieved_ids": [
                f"p{index}_c0" if baseline_hit else "p999_c0"
            ],
            "candidate_retrieved_ids": [
                f"p{index}_c0" if candidate_hit else "p999_c0"
            ],
            "baseline_recall": float(baseline_hit),
            "candidate_recall": float(candidate_hit),
            "baseline_mrr": float(baseline_hit),
            "candidate_mrr": float(candidate_hit),
            "baseline_ndcg": float(baseline_hit),
            "candidate_ndcg": float(candidate_hit),
            "degraded": False,
        })
    return {
        "report_schema": "factoid-anchor-paired-v1",
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
            "factoid_anchor_formula_sha256": contract["formula_sha256"],
            "shared_routes_sha256": "8" * 64,
            "anchor_decisions_sha256": "9" * 64,
        },
        "pipeline": {
            "baseline_profile": "hybrid",
            "candidate_profile": "hybrid-anchor-v1",
            "lexical_profile": "bm25-bilingual",
            "split": "train",
            "evidence_resolver": "page-span-v2",
            "top_k": 5,
            "route_limit": 10,
            "factoid_anchor": contract,
        },
        "snapshot": {
            "database_chunk_count": 464,
            "vector_count": 464,
            "missing_vector_ids": 0,
            "extra_vector_ids": 0,
            "embedding_dimension": 1024,
            "vector_manifest_sha256": "5" * 64,
            "hnsw_config_sha256": "6" * 64,
            "hnsw_binary_manifest_sha256": "7" * 64,
        },
        "baseline": {
            "runtime_degraded_count": 0,
            "overall": {"recall@5": 20 / 24, "factoid_recall": 4 / 6,
                        "mrr": 20 / 24, "ndcg@5": 20 / 24},
            "by_question_type": [],
            "latency": {"count": 24, "p95": 200.0},
        },
        "candidate": {
            "runtime_degraded_count": 0,
            "overall": {"recall@5": 21 / 24, "factoid_recall": 5 / 6,
                        "mrr": 21 / 24, "ndcg@5": 21 / 24},
            "by_question_type": [],
            "latency": {"count": 24, "p95": 300.0},
        },
        "anchor_summary": {"eligible": 1, "routed": 1, "no_anchor": 23},
        "items": items,
        "with_llm": False,
    }
    for qtype in types:
        selected = [row for row in items if row["question_type"] == qtype]
        for side in ("baseline", "candidate"):
            report[side]["by_question_type"].append({
                "question_type": qtype,
                "n": 6,
                "recall": sum(row[f"{side}_recall"] for row in selected) / 6,
            })
    return report


def test_factoid_anchor_contract_is_stable_and_bound():
    first = factoid_anchor_contract_metadata()
    assert first == factoid_anchor_contract_metadata()
    assert len(first["formula_sha256"]) == 64
    assert first["algorithm"] == "hybrid-anchor-v1"
    assert first["route_limit_multiplier"] == 2


def test_paired_routes_compute_shared_routes_once_and_keep_no_anchor_parity():
    calls = {"semantic": 0, "keyword": 0, "anchor": 0}

    def semantic(_question):
        calls["semantic"] += 1
        return [{"chunk_id": "p1_c0"}]

    def keyword(_question):
        calls["keyword"] += 1
        return [{"chunk_id": "p2_c0"}]

    def anchor(question):
        calls["anchor"] += 1
        return [{"chunk_id": "p3_c0"}] if "ABMIL" in question else []

    items = [
        {"qa_id": "q1", "question": "plain question", "question_type": "summary"},
        {"qa_id": "q2", "question": "ABMIL result", "question_type": "factoid"},
    ]
    qrels = {"q1": ["p1_c0"], "q2": ["p3_c0"]}
    result = evaluate_anchor_paired_routes(
        items, qrels, semantic, keyword, anchor, top_k=5
    )

    assert calls == {"semantic": 2, "keyword": 2, "anchor": 1}
    assert result["items"][0]["baseline_retrieved_ids"] == result["items"][0][
        "candidate_retrieved_ids"
    ]
    assert result["anchor_summary"] == {"eligible": 1, "routed": 1, "no_anchor": 1}


def test_train_gate_requires_factoid_gain_all_type_non_regression_and_parity():
    report = _paired_report()
    gate = evaluate_anchor_train(report)
    assert gate["passed"] is True
    assert gate["checks"]["factoid_recall_gain"]["passed"] is True

    regressed = copy.deepcopy(report)
    regressed["candidate"]["by_question_type"][1]["recall"] -= 1 / 6
    assert evaluate_anchor_train(regressed)["passed"] is False

    parity = copy.deepcopy(report)
    parity["items"][2]["candidate_retrieved_ids"] = ["p123_c0"]
    with pytest.raises(ValueError, match="无锚点"):
        evaluate_anchor_train(parity)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda report: report["run"].update(git_tracked_clean=False), "Git"),
        (lambda report: report["pipeline"].update(split="dev"), "train"),
        (lambda report: report["benchmark"].update(
            factoid_anchor_formula_sha256="0" * 64), "算法"),
        (lambda report: report["candidate"].update(
            runtime_degraded_count=1), "降级"),
    ],
)
def test_train_gate_fails_closed_on_protocol_drift(mutation, message):
    report = _paired_report()
    mutation(report)
    with pytest.raises(ValueError, match=message):
        evaluate_anchor_train(report)
