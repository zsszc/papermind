"""Batch 27B 论文优先证据重排的完整 train 配对 Gate。"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

from eval.run import paper_first_contract_metadata
from eval.train_failure_diagnostics import (
    validate_cli_path,
    write_report_exclusive,
)


SCHEMA = "paper-first-train-gate-v1"
_GIT_SHA_RE = re.compile(r"[0-9a-f]{40}")
_SHA_RE = re.compile(r"[0-9a-f]{64}")
_QUESTION_TYPE_COUNTS = {
    "factoid": 8,
    "method_detail": 4,
    "summary": 1,
}
_SHARED_BENCHMARK_FIELDS = (
    "dataset_sha256",
    "qrels_sha256",
    "corpus_manifest_sha256",
    "database_logical_manifest_sha256",
    "page_text_manifest_sha256",
    "vector_manifest_sha256",
    "hnsw_config_sha256",
    "hnsw_binary_manifest_sha256",
)
_EPSILON = 1e-12


def _report_sha256(report: dict[str, Any]) -> str:
    payload = json.dumps(
        report, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SHA_RE.fullmatch(value) is None:
        raise ValueError(f"报告缺少有效指纹: {field}")
    return value


def _unit_number(block: dict[str, Any], field: str) -> float:
    value = block.get(field)
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or not 0.0 <= float(value) <= 1.0
    ):
        raise ValueError(f"报告字段不是 [0, 1] 有限数值: {field}")
    return float(value)


def _non_negative_number(block: dict[str, Any], field: str) -> float:
    value = block.get(field)
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or float(value) < 0.0
    ):
        raise ValueError(f"报告字段不是非负有限数值: {field}")
    return float(value)


def _type_rows(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = report.get("by_question_type")
    if not isinstance(rows, list):
        raise ValueError("完整 train 缺少问题类型聚合")
    mapped: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("问题类型聚合必须是对象")
        qtype = row.get("question_type")
        if qtype in mapped:
            raise ValueError("问题类型聚合存在重复")
        mapped[str(qtype)] = row
    if set(mapped) != set(_QUESTION_TYPE_COUNTS):
        raise ValueError("完整 train 问题类型集合不符合冻结契约")
    for qtype, expected_count in _QUESTION_TYPE_COUNTS.items():
        row = mapped[qtype]
        if row.get("n") != expected_count:
            raise ValueError("完整 train 问题类型数量不符合 13 题契约")
        for field in ("recall", "mrr", "ndcg", "span_coverage"):
            _unit_number(row, field)
    return mapped


def _validate_report(
    report: dict[str, Any], *, candidate: bool
) -> tuple[dict[str, dict[str, Any]], set[str]]:
    label = "候选" if candidate else "基线"
    if not isinstance(report, dict) or report.get("report_schema") != "2.0":
        raise ValueError(f"{label}不是 eval.run report_schema=2.0 报告")
    run = report.get("run") or {}
    if _GIT_SHA_RE.fullmatch(str(run.get("git_sha", ""))) is None:
        raise ValueError(f"{label}缺少有效 Git 提交")
    if run.get("git_tracked_clean") is not True:
        raise ValueError(f"{label}必须在 clean Git 提交运行")

    benchmark = report.get("benchmark") or {}
    for field in _SHARED_BENCHMARK_FIELDS:
        _sha(benchmark.get(field), field)
    if (
        benchmark["database_logical_manifest_sha256"]
        != benchmark["corpus_manifest_sha256"]
    ):
        raise ValueError(f"{label}数据库与语料指纹不一致")

    expected_profile = (
        "paper-first-evidence-rerank-v1" if candidate else "hybrid"
    )
    pipeline = report.get("pipeline") or {}
    expected_pipeline = {
        "profile": expected_profile,
        "effective_profile": expected_profile,
        "lexical_profile": "bm25-bilingual",
        "semantic_rerank": None,
        "split": "train",
        "evidence_resolver": "page-span-v2",
        "top_k": 5,
    }
    for field, expected in expected_pipeline.items():
        if pipeline.get(field) != expected:
            raise ValueError(f"{label}完整 train 管线字段不符: {field}")
    if candidate:
        contract = paper_first_contract_metadata()
        formula_sha = _sha(
            benchmark.get("paper_first_formula_sha256"),
            "paper_first_formula_sha256",
        )
        if formula_sha != contract["formula_sha256"]:
            raise ValueError("候选公式指纹与当前代码不一致")
        if pipeline.get("paper_first") != contract:
            raise ValueError("候选管线未绑定冻结公式")
    elif "paper_first_formula_sha256" in benchmark or "paper_first" in pipeline:
        raise ValueError("基线不得伪装论文优先候选")

    if report.get("with_llm") is not False:
        raise ValueError("完整 train 配对禁止 LLM")
    diagnostics = report.get("diagnostics") or {}
    if diagnostics.get("runtime_degraded_count") != 0:
        raise ValueError(f"{label}存在运行时降级")

    overall = report.get("overall") or {}
    if overall.get("n_positive") != 13 or overall.get("n_negative") != 0:
        raise ValueError(f"{label}必须包含完整 13 题正例 train")
    for field in ("recall@5", "mrr", "ndcg@5", "span_coverage@5"):
        _unit_number(overall, field)

    latency = report.get("latency") or {}
    if latency.get("count") != 13:
        raise ValueError(f"{label}延迟样本必须为 13")
    _non_negative_number(latency, "p95")
    rows = _type_rows(report)

    items = report.get("items")
    if not isinstance(items, list) or len(items) != 13:
        raise ValueError(f"{label}逐题集合必须为完整 13 题")
    qa_ids: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError(f"{label}逐题结果必须为对象")
        qa_id = item.get("qa_id")
        if not isinstance(qa_id, str) or not qa_id or qa_id in qa_ids:
            raise ValueError(f"{label}逐题 QA 身份必须非空且唯一")
        qa_ids.add(qa_id)
        if item.get("degraded") is not False:
            raise ValueError(f"{label}逐题结果存在降级")
    return rows, qa_ids


def _check(
    actual: float, threshold: float, operator: str, passed: bool
) -> dict[str, Any]:
    return {
        "actual": actual,
        "threshold": threshold,
        "operator": operator,
        "passed": passed,
    }


def evaluate_paper_first_train(
    baseline: dict[str, Any], candidate: dict[str, Any]
) -> dict[str, Any]:
    """验证同提交完整 train 报告，并返回去标识化晋级判定。"""
    baseline_types, baseline_ids = _validate_report(baseline, candidate=False)
    candidate_types, candidate_ids = _validate_report(candidate, candidate=True)
    if baseline["run"]["git_sha"] != candidate["run"]["git_sha"]:
        raise ValueError("基线与候选必须来自同一 Git 提交")
    for field in _SHARED_BENCHMARK_FIELDS:
        if baseline["benchmark"][field] != candidate["benchmark"][field]:
            raise ValueError(f"基线与候选冻结指纹不一致: {field}")
    if baseline_ids != candidate_ids:
        raise ValueError("基线与候选 13 题逐题集合不一致")

    baseline_overall = baseline["overall"]
    candidate_overall = candidate["overall"]
    checks: dict[str, dict[str, Any]] = {}
    coverage_gain = _unit_number(
        candidate_overall, "span_coverage@5"
    ) - _unit_number(baseline_overall, "span_coverage@5")
    checks["span_coverage_gain"] = _check(
        coverage_gain,
        1 / 13,
        ">=",
        coverage_gain + _EPSILON >= 1 / 13,
    )
    for metric in ("recall@5", "mrr", "ndcg@5"):
        gain = _unit_number(candidate_overall, metric) - _unit_number(
            baseline_overall, metric
        )
        checks[f"{metric.replace('@5', '')}_non_regression"] = _check(
            gain, 0.0, ">=", gain >= -_EPSILON
        )
    for qtype in sorted(_QUESTION_TYPE_COUNTS):
        for metric in ("recall", "span_coverage"):
            gain = _unit_number(candidate_types[qtype], metric) - _unit_number(
                baseline_types[qtype], metric
            )
            checks[f"{qtype}_{metric}_non_regression"] = _check(
                gain, 0.0, ">=", gain >= -_EPSILON
            )
    candidate_p95 = _non_negative_number(candidate["latency"], "p95")
    checks["candidate_p95_ms"] = _check(
        candidate_p95, 1000.0, "<", candidate_p95 < 1000.0
    )

    contract = paper_first_contract_metadata()
    return {
        "schema": SCHEMA,
        "passed": all(check["passed"] for check in checks.values()),
        "checks": checks,
        "input_report_sha256": {
            "baseline": _report_sha256(baseline),
            "candidate": _report_sha256(candidate),
        },
        "binding": {
            "git_sha": baseline["run"]["git_sha"],
            **{
                field: baseline["benchmark"][field]
                for field in _SHARED_BENCHMARK_FIELDS
            },
            "paper_first_formula_sha256": contract["formula_sha256"],
            "split": "train",
            "top_k": 5,
            "item_count": 13,
        },
        "policy": {
            "dev": "run-once-only-after-pass",
            "holdout": "forbidden",
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="比较同提交生产基线与论文优先候选的完整 train 报告"
    )
    parser.add_argument("--baseline", required=True, help="私有基线 train 报告")
    parser.add_argument("--candidate", required=True, help="私有候选 train 报告")
    parser.add_argument("--output", required=True, help="私有 Gate 聚合输出")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        baseline_path = validate_cli_path(Path(args.baseline), must_exist=True)
        candidate_path = validate_cli_path(Path(args.candidate), must_exist=True)
        output_path = validate_cli_path(Path(args.output), must_exist=False)
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
        result = evaluate_paper_first_train(baseline, candidate)
        write_report_exclusive(output_path, result)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"[paper-first-train-gate] FAIL: {type(exc).__name__}")
        return 2
    print(json.dumps({
        "schema": result["schema"],
        "passed": result["passed"],
        "input_report_sha256": result["input_report_sha256"],
    }, ensure_ascii=False, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
