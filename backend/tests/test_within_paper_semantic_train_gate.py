"""Batch 32：论文内语义候选完整 train 配对 Gate。"""

from copy import deepcopy

from eval.run import within_paper_semantic_rerank_contract_metadata
from eval.within_paper_semantic_train_gate_fixtures import report
from eval.within_paper_train_gate import evaluate_within_paper_semantic_train


def test_semantic_gate_passes_only_with_coverage_gain_and_no_regression():
    baseline = report("hybrid", coverage=0.45)
    candidate = report(
        "within-paper-semantic-rerank-v1", coverage=0.45 + 1 / 13
    )
    gate = evaluate_within_paper_semantic_train(baseline, candidate)
    assert gate["passed"] is True
    assert gate["schema"] == "within-paper-semantic-train-gate-v1"
    assert "private-" not in str(gate)


def test_semantic_gate_rejects_regression_and_wrong_formula():
    baseline = report("hybrid", coverage=0.45)
    candidate = report("within-paper-semantic-rerank-v1", coverage=0.55)
    candidate["overall"]["mrr"] = 0.2
    assert evaluate_within_paper_semantic_train(baseline, candidate)["passed"] is False

    broken = deepcopy(candidate)
    broken["benchmark"]["within_paper_semantic_formula_sha256"] = "9" * 64
    try:
        evaluate_within_paper_semantic_train(baseline, broken)
    except ValueError as exc:
        assert "公式" in str(exc)
    else:
        raise AssertionError("错误公式必须 fail closed")


def test_fixture_contract_matches_current_formula():
    candidate = report("within-paper-semantic-rerank-v1")
    contract = within_paper_semantic_rerank_contract_metadata()
    assert candidate["pipeline"]["within_paper_semantic"] == contract
