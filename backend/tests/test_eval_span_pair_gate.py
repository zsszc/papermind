"""Batch 22D：旧/新分块 page-span-v2 配对 Gate。"""

import pytest

from eval.span_pair_gate import evaluate_span_pair


def _report(
    *, coverage, any_hit, factoid, mrr, ndcg, p95=400, manifest="page-a",
    profile="hybrid", parent_manifest=None, contract_sha=None,
    n_positive=24, degraded=0,
):
    return {
        "benchmark": {
            "dataset_sha256": "dataset",
            "qrels_sha256": "qrels",
            "page_text_manifest_sha256": manifest,
            "resolver_version": "page-span-v2",
            "parent_manifest_sha256": parent_manifest,
            "parent_child_contract_sha256": contract_sha,
        },
        "pipeline": {
            "profile": profile,
            "effective_profile": profile,
            "lexical_profile": "bm25-bilingual",
            "semantic_rerank": None,
            "split": "train",
            "top_k": 5,
            "evidence_resolver": "page-span-v2",
        },
        "overall": {
            "span_coverage@5": coverage,
            "any_hit@5": any_hit,
            "mrr": mrr,
            "ndcg@5": ndcg,
            "n_positive": n_positive,
        },
        "by_question_type": [{
            "question_type": "factoid", "span_coverage": factoid,
        }],
        "latency": {"p95": p95},
        "diagnostics": {"runtime_degraded_count": degraded},
    }


def test_pair_gate_passes_only_when_frozen_train_contract_is_met():
    baseline = _report(
        coverage=2 / 3, any_hit=2 / 3, factoid=0.5,
        mrr=0.42, ndcg=0.48,
    )
    candidate = _report(
        coverage=0.72, any_hit=0.70, factoid=0.5,
        mrr=0.41, ndcg=0.47,
    )

    gate = evaluate_span_pair(baseline, candidate)

    assert gate["passed"] is True
    assert gate["pair_key"]
    assert all(check["passed"] for check in gate["checks"].values())


def test_pair_gate_rejects_observed_512_candidate_regressions():
    baseline = _report(
        coverage=2 / 3, any_hit=2 / 3, factoid=0.5,
        mrr=0.4215, ndcg=0.4835,
    )
    candidate = _report(
        coverage=0.4534, any_hit=0.5, factoid=0.393,
        mrr=0.3438, ndcg=0.3162,
    )

    gate = evaluate_span_pair(baseline, candidate)

    assert gate["passed"] is False
    assert gate["checks"]["span_coverage_gain"]["passed"] is False
    assert gate["checks"]["any_hit_non_regression"]["passed"] is False
    assert gate["checks"]["factoid_non_regression"]["passed"] is False


def test_pair_gate_fails_closed_when_page_manifest_differs():
    baseline = _report(
        coverage=0.5, any_hit=0.5, factoid=0.5, mrr=0.5, ndcg=0.5,
    )
    candidate = _report(
        coverage=0.8, any_hit=0.8, factoid=0.8, mrr=0.8, ndcg=0.8,
        manifest="page-b",
    )

    with pytest.raises(ValueError, match="配对配置不一致"):
        evaluate_span_pair(baseline, candidate)


def test_parent_child_pair_gate_locks_parent_and_algorithm_contract():
    common = dict(
        coverage=0.7, any_hit=0.7, factoid=0.5, mrr=0.5, ndcg=0.5,
        profile="parent-child-v1", parent_manifest="a" * 64,
        contract_sha="b" * 64,
    )
    baseline = _report(**common)
    candidate = _report(**common)
    candidate["benchmark"]["parent_child_contract_sha256"] = "c" * 64
    with pytest.raises(ValueError, match="配对配置不一致"):
        evaluate_span_pair(baseline, candidate)

    candidate = _report(**common)
    candidate["pipeline"]["effective_profile"] = "runtime-degraded"
    with pytest.raises(ValueError, match="parent-child-v1"):
        evaluate_span_pair(baseline, candidate)


def test_pair_gate_rejects_subsets_and_degraded_baseline():
    baseline = _report(
        coverage=0.7, any_hit=0.7, factoid=0.5, mrr=0.5, ndcg=0.5,
        n_positive=1,
    )
    candidate = _report(
        coverage=0.8, any_hit=0.8, factoid=0.5, mrr=0.5, ndcg=0.5,
        n_positive=1,
    )
    with pytest.raises(ValueError, match="24 条"):
        evaluate_span_pair(baseline, candidate)

    baseline = _report(
        coverage=0.7, any_hit=0.7, factoid=0.5, mrr=0.5, ndcg=0.5,
        degraded=1,
    )
    candidate = _report(
        coverage=0.8, any_hit=0.8, factoid=0.5, mrr=0.5, ndcg=0.5,
    )
    gate = evaluate_span_pair(baseline, candidate)
    assert gate["passed"] is False
    assert gate["checks"]["baseline_runtime_degraded_count"]["passed"] is False
