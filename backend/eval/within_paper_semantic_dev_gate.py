"""Batch 32 论文内语义候选的一次性 dev 配对 Gate。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from eval.paper_first_train_gate import (
    _EPSILON, _GIT_SHA_RE, _SHARED_BENCHMARK_FIELDS, _check,
    _non_negative_number, _report_sha256, _sha, _unit_number,
)
from eval.run import within_paper_semantic_rerank_contract_metadata
from eval.train_failure_diagnostics import validate_cli_path, write_report_exclusive


SCHEMA = "within-paper-semantic-dev-gate-v1"
_DEV_TYPES = {"factoid": 6, "method_detail": 3, "summary": 3}


def _validate(report: dict[str, Any], *, candidate: bool) -> dict[str, dict]:
    label = "候选" if candidate else "基线"
    if report.get("report_schema") != "2.0":
        raise ValueError(f"{label}报告 schema 非法")
    run = report.get("run") or {}
    if _GIT_SHA_RE.fullmatch(str(run.get("git_sha", ""))) is None or run.get("git_tracked_clean") is not True:
        raise ValueError(f"{label}必须来自 clean Git")
    profile = "within-paper-semantic-rerank-v1" if candidate else "hybrid"
    pipeline = report.get("pipeline") or {}
    expected = {"profile": profile, "effective_profile": profile,
                "lexical_profile": "bm25-bilingual", "semantic_rerank": None,
                "split": "dev", "evidence_resolver": "page-span-v2", "top_k": 5}
    if any(pipeline.get(key) != value for key, value in expected.items()):
        raise ValueError(f"{label}dev 管线不符合冻结契约")
    if (report.get("diagnostics") or {}).get("runtime_degraded_count") != 0:
        raise ValueError(f"{label}存在运行时降级")
    overall = report.get("overall") or {}
    if overall.get("n_positive") != 12 or overall.get("n_negative") != 0:
        raise ValueError(f"{label}必须包含完整 12 题 dev")
    for field in ("recall@5", "mrr", "ndcg@5", "span_coverage@5"):
        _unit_number(overall, field)
    if (report.get("latency") or {}).get("count") != 12:
        raise ValueError(f"{label}延迟样本必须为 12")
    rows = report.get("by_question_type")
    if not isinstance(rows, list):
        raise ValueError(f"{label}缺少分型聚合")
    typed = {row.get("question_type"): row for row in rows if isinstance(row, dict)}
    if set(typed) != set(_DEV_TYPES) or any(typed[key].get("n") != count for key, count in _DEV_TYPES.items()):
        raise ValueError(f"{label}dev 分型数量不完整")
    for row in typed.values():
        _unit_number(row, "recall")
        _unit_number(row, "span_coverage")
    if candidate:
        contract = within_paper_semantic_rerank_contract_metadata()
        benchmark = report.get("benchmark") or {}
        if _sha(benchmark.get("within_paper_semantic_formula_sha256"), "formula") != contract["formula_sha256"]:
            raise ValueError("候选公式指纹不一致")
        if pipeline.get("within_paper_semantic") != contract:
            raise ValueError("候选未绑定当前公式")
        _sha(benchmark.get("semantic_dev_train_gate_sha256"), "train gate")
    return typed


def evaluate_dev(baseline: dict[str, Any], candidate: dict[str, Any], train_gate: dict[str, Any], claim: dict[str, Any]) -> dict[str, Any]:
    """验证唯一 dev 配对并输出无逐题身份的 Gate。"""
    base_types = _validate(baseline, candidate=False)
    cand_types = _validate(candidate, candidate=True)
    if baseline["run"]["git_sha"] != candidate["run"]["git_sha"]:
        raise ValueError("dev 基线与候选 Git 不一致")
    for field in _SHARED_BENCHMARK_FIELDS:
        if baseline["benchmark"].get(field) != candidate["benchmark"].get(field):
            raise ValueError(f"dev 冻结指纹不一致: {field}")
    train_sha = _report_sha256(train_gate)
    contract = within_paper_semantic_rerank_contract_metadata()
    if train_gate.get("schema") != "within-paper-semantic-train-gate-v1" or train_gate.get("passed") is not True:
        raise ValueError("缺少已通过 train Gate")
    if candidate["benchmark"].get("semantic_dev_train_gate_sha256") != train_sha:
        raise ValueError("候选 dev 未绑定 train Gate")
    if claim != {"schema": "within-paper-semantic-dev-claim-v1",
                 "train_gate_sha256": train_sha,
                 "git_sha": candidate["run"]["git_sha"],
                 "formula_sha256": contract["formula_sha256"]}:
        raise ValueError("一次性 dev claim 不匹配")

    checks: dict[str, dict[str, Any]] = {}
    for metric in ("recall@5", "mrr", "ndcg@5", "span_coverage@5"):
        gain = _unit_number(candidate["overall"], metric) - _unit_number(baseline["overall"], metric)
        checks[f"{metric}_non_regression"] = _check(gain, 0.0, ">=", gain >= -_EPSILON)
    for qtype in sorted(_DEV_TYPES):
        for metric in ("recall", "span_coverage"):
            gain = _unit_number(cand_types[qtype], metric) - _unit_number(base_types[qtype], metric)
            checks[f"{qtype}_{metric}_non_regression"] = _check(gain, 0.0, ">=", gain >= -_EPSILON)
    p95 = _non_negative_number(candidate["latency"], "p95")
    checks["candidate_p95_ms"] = _check(p95, 1000.0, "<", p95 < 1000.0)
    return {"schema": SCHEMA, "passed": all(row["passed"] for row in checks.values()),
            "checks": checks,
            "input_report_sha256": {"baseline": _report_sha256(baseline), "candidate": _report_sha256(candidate), "train_gate": train_sha},
            "binding": {"git_sha": candidate["run"]["git_sha"], "split": "dev", "top_k": 5, "item_count": 12,
                        "formula_sha256": contract["formula_sha256"]},
            "policy": {"production_activation": "only-if-pass", "holdout": "forbidden"}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Batch32 唯一 dev 配对 Gate")
    for name in ("baseline", "candidate", "train-gate", "claim", "output"):
        parser.add_argument(f"--{name}", required=True)
    args = parser.parse_args(argv)
    try:
        values = {}
        for name in ("baseline", "candidate", "train_gate", "claim"):
            path = validate_cli_path(Path(getattr(args, name)), must_exist=True)
            values[name] = json.loads(path.read_text(encoding="utf-8"))
        output = validate_cli_path(Path(args.output), must_exist=False)
        result = evaluate_dev(values["baseline"], values["candidate"], values["train_gate"], values["claim"])
        write_report_exclusive(output, result)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"[within-paper-semantic-dev-gate] FAIL: {type(exc).__name__}")
        return 2
    print(json.dumps({"schema": result["schema"], "passed": result["passed"], "checks": result["checks"]}, ensure_ascii=False, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
