"""Batch 20：RAG 晋级不能只看 Recall，必须同时约束排序、弱项和延迟。"""

import pytest

from eval import run


def _inputs():
    return {
        "overall": {
            "recall@5": 0.625,
            "mrr": 0.394,
            "ndcg@5": 0.452,
        },
        "type_rows": [
            {"question_type": "factoid", "recall": 0.333},
            {"question_type": "summary", "recall": 0.667},
        ],
        "latency": {"p50": 200.0, "p95": 999.0, "mean": 250.0, "count": 24},
        "top_k": 5,
        "recall_threshold": 0.625,
        "min_mrr": 0.394,
        "min_ndcg": 0.452,
        "min_factoid_recall": 0.333,
        "max_p95_ms": 1000.0,
        "runtime_valid": True,
    }


def test_quality_gate_records_each_check_and_passes_at_boundaries():
    gate = run._build_quality_gate(**_inputs())

    assert gate["passed"] is True
    assert gate["runtime_valid"] is True
    assert set(gate["checks"]) == {
        "recall@5", "mrr", "ndcg@5", "factoid_recall", "p95_ms"
    }
    assert all(check["passed"] for check in gate["checks"].values())
    assert gate["checks"]["factoid_recall"]["actual"] == pytest.approx(0.333)


@pytest.mark.parametrize(
    ("field", "value", "failed_check"),
    [
        ("recall_threshold", 0.626, "recall@5"),
        ("min_mrr", 0.395, "mrr"),
        ("min_ndcg", 0.453, "ndcg@5"),
        ("min_factoid_recall", 0.334, "factoid_recall"),
        # 延迟是严格小于预算；等于 999ms 时失败。
        ("max_p95_ms", 999.0, "p95_ms"),
    ],
)
def test_quality_gate_fails_when_any_metric_misses(field, value, failed_check):
    values = _inputs()
    values[field] = value

    gate = run._build_quality_gate(**values)

    assert gate["passed"] is False
    assert gate["checks"][failed_check]["passed"] is False


def test_quality_gate_fails_closed_when_factoid_group_missing():
    values = _inputs()
    values["type_rows"] = [
        {"question_type": "summary", "recall": 0.667},
    ]

    gate = run._build_quality_gate(**values)

    assert gate["passed"] is False
    assert gate["checks"]["factoid_recall"] == {
        "actual": None,
        "threshold": 0.333,
        "operator": ">=",
        "passed": False,
    }


def test_runtime_degradation_invalidates_otherwise_passing_gate():
    values = _inputs()
    values["runtime_valid"] = False

    gate = run._build_quality_gate(**values)

    assert gate["passed"] is False
    assert gate["runtime_valid"] is False


def test_parser_accepts_explicit_multi_metric_thresholds():
    args = run.build_parser().parse_args([
        "--min-mrr", "0.394",
        "--min-ndcg", "0.452",
        "--min-factoid-recall", "0.333",
        "--max-p95-ms", "1000",
    ])

    assert args.min_mrr == pytest.approx(0.394)
    assert args.min_ndcg == pytest.approx(0.452)
    assert args.min_factoid_recall == pytest.approx(0.333)
    assert args.max_p95_ms == pytest.approx(1000.0)
