"""Batch 22F Weighted-RRF train 自动选择器与条件 dev Gate。"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


_WEIGHTS = (1.0, 1.25, 1.5, 2.0)
_COMMON_BENCHMARK_FIELDS = (
    "dataset_sha256",
    "qrels_sha256",
    "corpus_manifest_sha256",
    "page_text_manifest_sha256",
    "resolver_version",
    "vector_manifest_sha256",
    "weighted_rrf_formula_sha256",
)


def _number(block: dict[str, Any], key: str) -> float:
    value = block.get(key)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"报告缺少数值字段: {key}")
    return float(value)


def _factoid_recall(report: dict[str, Any]) -> float:
    rows = [
        row for row in report.get("by_question_type", [])
        if row.get("question_type") == "factoid"
    ]
    if len(rows) != 1 or rows[0].get("n") != 6:
        raise ValueError("报告必须包含恰好 6 条 factoid")
    return _number(rows[0], "recall")


def _validate_complete_report(
    report: dict[str, Any], *, split: str, weighted: bool
) -> None:
    pipeline = report.get("pipeline") or {}
    expected_profile = "weighted-rrf-v1" if weighted else "hybrid"
    if (
        pipeline.get("profile") != expected_profile
        or pipeline.get("effective_profile") != expected_profile
    ):
        raise ValueError(f"报告必须有效运行 {expected_profile}")
    if pipeline.get("split") != split:
        raise ValueError(f"报告必须使用完整 {split} 分区")
    if pipeline.get("top_k") != 5:
        raise ValueError("报告必须使用 top-k=5")
    if pipeline.get("evidence_resolver") != "page-span-v2":
        raise ValueError("报告必须使用 page-span-v2")
    if pipeline.get("lexical_profile") != "bm25-bilingual":
        raise ValueError("报告必须使用 bm25-bilingual")
    if report.get("with_llm") is not False:
        raise ValueError("Weighted-RRF 报告禁止 LLM")

    overall = report.get("overall") or {}
    if overall.get("n_positive") != 24 or overall.get("n_negative") != 0:
        raise ValueError("报告必须包含完整 24 条正例且无负例")
    type_rows = report.get("by_question_type") or []
    if sum(row.get("n", 0) for row in type_rows) != 24:
        raise ValueError("问题类型计数必须合计 24")
    _factoid_recall(report)

    diagnostics = report.get("diagnostics") or {}
    if diagnostics.get("runtime_degraded_count") != 0:
        raise ValueError("报告存在运行时降级")
    snapshot = diagnostics.get("vector_snapshot") or {}
    if (
        snapshot.get("database_chunk_count") != 464
        or snapshot.get("vector_count") != 464
        or snapshot.get("missing_vector_ids") != 0
        or snapshot.get("extra_vector_ids") != 0
        or snapshot.get("embedding_dimension") != 1024
        or snapshot.get("vector_manifest_sha256")
        != (report.get("benchmark") or {}).get("vector_manifest_sha256")
    ):
        raise ValueError("报告向量快照审计不完整")

    items = report.get("items") or []
    qa_ids = [item.get("qa_id") for item in items]
    if len(items) != 24 or len(set(qa_ids)) != 24 or None in qa_ids:
        raise ValueError("报告必须包含 24 个唯一 QA")
    if any(item.get("degraded") is not False for item in items):
        raise ValueError("报告逐题存在降级")
    latency = report.get("latency") or {}
    if latency.get("count") != 24:
        raise ValueError("报告延迟样本必须为 24")

    if weighted:
        contract = pipeline.get("weighted_rrf") or {}
        if (
            contract.get("semantic_weight") != 1.0
            or contract.get("lexical_weight") not in _WEIGHTS
            or contract.get("rrf_k") != 60
        ):
            raise ValueError("Weighted-RRF 权重或 k 不在冻结契约")
        for key in ("formula_sha256", "configuration_sha256"):
            value = contract.get(key)
            if not isinstance(value, str) or len(value) != 64:
                raise ValueError(f"Weighted-RRF 缺少有效 {key}")
        benchmark_formula = (report.get("benchmark") or {}).get(
            "weighted_rrf_formula_sha256"
        )
        if benchmark_formula != contract["formula_sha256"]:
            raise ValueError("Weighted-RRF 公式指纹不一致")


def _common_payload(report: dict[str, Any]) -> dict[str, Any]:
    benchmark = report.get("benchmark") or {}
    pipeline = report.get("pipeline") or {}
    return {
        "git_sha": (report.get("run") or {}).get("git_sha"),
        "benchmark": {
            key: benchmark.get(key) for key in _COMMON_BENCHMARK_FIELDS
        },
        "pipeline": {
            "lexical_profile": pipeline.get("lexical_profile"),
            "top_k": pipeline.get("top_k"),
            "evidence_resolver": pipeline.get("evidence_resolver"),
            "formula_sha256": (
                (pipeline.get("weighted_rrf") or {}).get("formula_sha256")
            ),
        },
    }


def evaluate_weighted_baseline_parity(
    production: dict[str, Any], weighted: dict[str, Any]
) -> dict[str, Any]:
    """在运行权重网格前独立验证旧 hybrid 与新等权逐题顺序。"""
    _validate_complete_report(production, split="train", weighted=False)
    _validate_complete_report(weighted, split="train", weighted=True)
    weight = weighted["pipeline"]["weighted_rrf"]["lexical_weight"]
    if weight != 1.0:
        raise ValueError("baseline parity 必须使用词法权重 1.0")
    production_common = _common_payload(production)
    weighted_common = _common_payload(weighted)
    for key in (
        "dataset_sha256", "qrels_sha256", "corpus_manifest_sha256",
        "page_text_manifest_sha256", "resolver_version",
        "vector_manifest_sha256",
    ):
        if (
            production_common["benchmark"].get(key)
            != weighted_common["benchmark"].get(key)
        ):
            raise ValueError("baseline parity 快照指纹不一致")
    if production_common["git_sha"] != weighted_common["git_sha"]:
        raise ValueError("baseline parity git_sha 不一致")

    production_items = {
        item["qa_id"]: item.get("retrieved_ids")
        for item in production.get("items", [])
    }
    weighted_items = {
        item["qa_id"]: item.get("retrieved_ids")
        for item in weighted.get("items", [])
    }
    if set(production_items) != set(weighted_items):
        raise ValueError("baseline parity QA 集合不一致")
    matched = sum(
        production_items[qa_id] == weighted_items[qa_id]
        for qa_id in production_items
    )
    result = {
        "gate_version": "weighted-rrf-baseline-parity-v1",
        "passed": matched == len(production_items),
        "matched_queries": matched,
        "total_queries": len(production_items),
    }
    if not result["passed"]:
        return result
    payload = json.dumps(
        sorted(production_items.items()),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    result["retrieved_ids_sha256"] = hashlib.sha256(payload).hexdigest()
    return result


def _candidate_gate(
    baseline: dict[str, Any], candidate: dict[str, Any]
) -> dict[str, Any]:
    base = baseline["overall"]
    cand = candidate["overall"]
    changes = {
        "recall_non_regression": _number(cand, "recall@5")
        - _number(base, "recall@5"),
        "any_hit_non_regression": _number(cand, "any_hit@5")
        - _number(base, "any_hit@5"),
        "factoid_gain": _factoid_recall(candidate) - _factoid_recall(baseline),
        "mrr_regression_limit": _number(cand, "mrr") - _number(base, "mrr"),
        "ndcg_regression_limit": _number(cand, "ndcg@5")
        - _number(base, "ndcg@5"),
    }
    thresholds = {
        "recall_non_regression": 0.0,
        "any_hit_non_regression": 0.0,
        "factoid_gain": 1 / 6,
        "mrr_regression_limit": -0.02,
        "ndcg_regression_limit": -0.02,
    }
    checks = {
        name: {
            "actual": value,
            "threshold": thresholds[name],
            "operator": ">=",
            "passed": value + 1e-12 >= thresholds[name],
        }
        for name, value in changes.items()
    }
    p95 = _number(candidate.get("latency") or {}, "p95")
    checks["p95_ms"] = {
        "actual": p95,
        "threshold": 1000.0,
        "operator": "<",
        "passed": p95 < 1000.0,
    }
    return {"passed": all(row["passed"] for row in checks.values()), "checks": checks}


def select_weighted_rrf_train(
    production_baseline: dict[str, Any],
    weighted_baseline: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    """验证完整冻结网格，并按预注册词典序选择唯一 train 胜者。"""
    _validate_complete_report(production_baseline, split="train", weighted=False)
    _validate_complete_report(weighted_baseline, split="train", weighted=True)
    for candidate in candidates:
        _validate_complete_report(candidate, split="train", weighted=True)

    reports = [weighted_baseline, *candidates]
    weights = [
        report["pipeline"]["weighted_rrf"]["lexical_weight"]
        for report in reports
    ]
    if sorted(weights) != list(_WEIGHTS) or len(candidates) != 3:
        raise ValueError("报告不满足完整冻结权重网格")
    baseline_weight = weighted_baseline["pipeline"]["weighted_rrf"][
        "lexical_weight"
    ]
    if baseline_weight != 1.0:
        raise ValueError("Weighted-RRF baseline 必须使用词法权重 1.0")

    common = _common_payload(weighted_baseline)
    if any(_common_payload(report) != common for report in candidates):
        raise ValueError("Weighted-RRF 公共配对指纹不一致")
    production_common = _common_payload(production_baseline)
    for key in (
        "dataset_sha256", "qrels_sha256", "corpus_manifest_sha256",
        "page_text_manifest_sha256", "resolver_version",
        "vector_manifest_sha256",
    ):
        if production_common["benchmark"].get(key) != common["benchmark"].get(key):
            raise ValueError("生产 baseline 与 Weighted-RRF 快照指纹不一致")
    if production_common["git_sha"] != common["git_sha"]:
        raise ValueError("生产 baseline 与 Weighted-RRF git_sha 不一致")
    parity = evaluate_weighted_baseline_parity(
        production_baseline, weighted_baseline
    )
    if not parity["passed"]:
        raise ValueError("生产 hybrid 与 weighted 1.0 baseline parity 失败")

    rows: list[dict[str, Any]] = []
    passed_reports: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for report in sorted(
        candidates,
        key=lambda value: value["pipeline"]["weighted_rrf"]["lexical_weight"],
    ):
        gate = _candidate_gate(weighted_baseline, report)
        weight = report["pipeline"]["weighted_rrf"]["lexical_weight"]
        row = {"lexical_weight": weight, **gate}
        rows.append(row)
        if gate["passed"]:
            passed_reports.append((report, row))

    winner = None
    if passed_reports:
        report, _ = max(
            passed_reports,
            key=lambda pair: (
                _factoid_recall(pair[0]),
                _number(pair[0]["overall"], "recall@5"),
                _number(pair[0]["overall"], "mrr"),
                _number(pair[0]["overall"], "ndcg@5"),
                -pair[0]["pipeline"]["weighted_rrf"]["lexical_weight"],
            ),
        )
        winner = {
            "lexical_weight": report["pipeline"]["weighted_rrf"][
                "lexical_weight"
            ],
            "recall@5": _number(report["overall"], "recall@5"),
            "factoid_recall": _factoid_recall(report),
            "mrr": _number(report["overall"], "mrr"),
            "ndcg@5": _number(report["overall"], "ndcg@5"),
        }

    pair_payload = json.dumps(
        common, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return {
        "selector_version": "weighted-rrf-train-v1",
        "pair_key": hashlib.sha256(pair_payload).hexdigest(),
        "baseline_parity": parity,
        "passed": winner is not None,
        "winner": winner,
        "candidates": rows,
    }


def evaluate_weighted_rrf_dev(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    selection: dict[str, Any],
) -> dict[str, Any]:
    """只允许 train 胜者执行一次 dev 非回退 Gate。"""
    if not selection.get("passed") or not selection.get("winner"):
        raise ValueError("train selector 没有 winner")
    _validate_complete_report(baseline, split="dev", weighted=True)
    _validate_complete_report(candidate, split="dev", weighted=True)
    if _common_payload(baseline) != _common_payload(candidate):
        raise ValueError("dev 配对公共指纹不一致")
    if baseline["pipeline"]["weighted_rrf"]["lexical_weight"] != 1.0:
        raise ValueError("dev baseline 必须使用词法权重 1.0")
    candidate_weight = candidate["pipeline"]["weighted_rrf"]["lexical_weight"]
    if candidate_weight != selection["winner"]["lexical_weight"]:
        raise ValueError("dev candidate 必须使用 train winner 权重")

    diffs = {
        "recall": _number(candidate["overall"], "recall@5")
        - _number(baseline["overall"], "recall@5"),
        "factoid_recall": _factoid_recall(candidate) - _factoid_recall(baseline),
        "mrr": _number(candidate["overall"], "mrr")
        - _number(baseline["overall"], "mrr"),
        "ndcg": _number(candidate["overall"], "ndcg@5")
        - _number(baseline["overall"], "ndcg@5"),
    }
    checks = {
        name: {"actual": value, "threshold": 0.0, "passed": value >= 0.0}
        for name, value in diffs.items()
    }
    strict_gain = any(value > 0.0 for value in diffs.values())
    checks["strict_gain"] = {
        "actual": strict_gain, "threshold": True, "passed": strict_gain,
    }
    p95 = _number(candidate["latency"], "p95")
    checks["p95_ms"] = {
        "actual": p95, "threshold": 1000.0, "passed": p95 < 1000.0,
    }
    return {
        "gate_version": "weighted-rrf-dev-v1",
        "passed": all(row["passed"] for row in checks.values()),
        "lexical_weight": candidate_weight,
        "checks": checks,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m eval.weighted_rrf_selector",
        description="选择 Batch 22F 完整 train 权重网格",
    )
    parser.add_argument("--production-baseline", required=True)
    parser.add_argument("--weighted-baseline", required=True)
    parser.add_argument("--candidate", action="append", default=[])
    parser.add_argument("--parity-only", action="store_true")
    parser.add_argument("--output", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load = lambda path: json.loads(Path(path).read_text(encoding="utf-8"))
    production = load(args.production_baseline)
    weighted = load(args.weighted_baseline)
    if args.parity_only:
        if args.candidate:
            raise ValueError("--parity-only 不得指定 --candidate")
        result = evaluate_weighted_baseline_parity(production, weighted)
    else:
        result = select_weighted_rrf_train(
            production, weighted, [load(path) for path in args.candidate]
        )
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
