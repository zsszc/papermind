"""Benchmark v2 旧/新分块报告的可复现配对 Gate。"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


_PAIR_BENCHMARK_FIELDS = (
    "dataset_sha256",
    "qrels_sha256",
    "page_text_manifest_sha256",
    "resolver_version",
)
_PAIR_PIPELINE_FIELDS = (
    "profile",
    "effective_profile",
    "lexical_profile",
    "semantic_rerank",
    "split",
    "top_k",
    "evidence_resolver",
)


def _pair_payload(report: dict[str, Any]) -> dict[str, Any]:
    benchmark = report.get("benchmark") or {}
    pipeline = report.get("pipeline") or {}
    return {
        "benchmark": {key: benchmark.get(key) for key in _PAIR_BENCHMARK_FIELDS},
        "pipeline": {key: pipeline.get(key) for key in _PAIR_PIPELINE_FIELDS},
    }


def _factoid_coverage(report: dict[str, Any]) -> float:
    row = next(
        (
            item for item in report.get("by_question_type", [])
            if item.get("question_type") == "factoid"
        ),
        None,
    )
    if row is None or not isinstance(row.get("span_coverage"), (int, float)):
        raise ValueError("配对报告缺少 factoid span_coverage")
    return float(row["span_coverage"])


def evaluate_span_pair(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    *,
    minimum_coverage_gain: float = 1 / 24,
    maximum_rank_regression: float = 0.02,
    maximum_p95_ms: float = 1000.0,
) -> dict[str, Any]:
    """执行 Batch 22D 冻结 Gate，只返回去标识化聚合。"""
    baseline_pair = _pair_payload(baseline)
    candidate_pair = _pair_payload(candidate)
    if baseline_pair != candidate_pair:
        raise ValueError("配对配置不一致，禁止计算跨粒度差值")
    if baseline_pair["benchmark"]["resolver_version"] != "page-span-v2":
        raise ValueError("配对报告必须使用 page-span-v2")

    payload_bytes = json.dumps(
        baseline_pair, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    pair_key = hashlib.sha256(payload_bytes).hexdigest()
    top_k = baseline_pair["pipeline"]["top_k"]
    coverage_key = f"span_coverage@{top_k}"
    any_hit_key = f"any_hit@{top_k}"
    ndcg_key = f"ndcg@{top_k}"
    baseline_overall = baseline.get("overall") or {}
    candidate_overall = candidate.get("overall") or {}

    def number(block: dict, key: str) -> float:
        value = block.get(key)
        if not isinstance(value, (int, float)):
            raise ValueError(f"配对报告缺少数值字段: {key}")
        return float(value)

    base_coverage = number(baseline_overall, coverage_key)
    cand_coverage = number(candidate_overall, coverage_key)
    base_any = number(baseline_overall, any_hit_key)
    cand_any = number(candidate_overall, any_hit_key)
    base_mrr = number(baseline_overall, "mrr")
    cand_mrr = number(candidate_overall, "mrr")
    base_ndcg = number(baseline_overall, ndcg_key)
    cand_ndcg = number(candidate_overall, ndcg_key)
    base_factoid = _factoid_coverage(baseline)
    cand_factoid = _factoid_coverage(candidate)
    candidate_p95 = number(candidate.get("latency") or {}, "p95")
    runtime_degraded = int(
        (candidate.get("diagnostics") or {}).get("runtime_degraded_count", -1)
    )

    def minimum(name: str, actual: float, threshold: float) -> tuple[str, dict]:
        return name, {
            "actual": actual,
            "threshold": threshold,
            "operator": ">=",
            "passed": actual >= threshold,
        }

    checks = dict((
        minimum(
            "span_coverage_gain",
            cand_coverage - base_coverage,
            minimum_coverage_gain,
        ),
        minimum("any_hit_non_regression", cand_any - base_any, 0.0),
        minimum(
            "factoid_non_regression", cand_factoid - base_factoid, 0.0
        ),
        minimum(
            "mrr_regression_limit",
            cand_mrr - base_mrr,
            -maximum_rank_regression,
        ),
        minimum(
            "ndcg_regression_limit",
            cand_ndcg - base_ndcg,
            -maximum_rank_regression,
        ),
    ))
    checks["p95_ms"] = {
        "actual": candidate_p95,
        "threshold": maximum_p95_ms,
        "operator": "<",
        "passed": candidate_p95 < maximum_p95_ms,
    }
    checks["runtime_degraded_count"] = {
        "actual": runtime_degraded,
        "threshold": 0,
        "operator": "==",
        "passed": runtime_degraded == 0,
    }
    return {
        "gate_version": "page-span-pair-v1",
        "pair_key": pair_key,
        "passed": all(check["passed"] for check in checks.values()),
        "baseline": {
            coverage_key: base_coverage,
            any_hit_key: base_any,
            "factoid_span_coverage": base_factoid,
            "mrr": base_mrr,
            ndcg_key: base_ndcg,
        },
        "candidate": {
            coverage_key: cand_coverage,
            any_hit_key: cand_any,
            "factoid_span_coverage": cand_factoid,
            "mrr": cand_mrr,
            ndcg_key: cand_ndcg,
            "p95_ms": candidate_p95,
        },
        "checks": checks,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m eval.span_pair_gate",
        description="比较两份 page-span-v2 train 报告",
    )
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--output", default=None, help="可选聚合 Gate JSON 输出路径")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    candidate = json.loads(Path(args.candidate).read_text(encoding="utf-8"))
    gate = evaluate_span_pair(baseline, candidate)
    rendered = json.dumps(gate, ensure_ascii=False, indent=2)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if gate["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
