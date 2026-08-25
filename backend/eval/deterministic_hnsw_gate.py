"""Batch 22H 确定性 HNSW train 重复性与配对 dev 晋级 Gate。"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from eval.deterministic_vector_snapshot import _config_sha256


_BENCHMARK_FIELDS = (
    "dataset_sha256",
    "qrels_sha256",
    "corpus_manifest_sha256",
    "database_logical_manifest_sha256",
    "page_text_manifest_sha256",
    "vector_manifest_sha256",
    "hnsw_config_sha256",
    "hnsw_binary_manifest_sha256",
)
_PIPELINE = {
    "profile": "hybrid",
    "effective_profile": "hybrid",
    "lexical_profile": "bm25-bilingual",
    "evidence_resolver": "page-span-v2",
    "top_k": 5,
}
_TRAIN_THRESHOLDS = {
    "recall@5": 0.6666666666666666,
    "factoid_recall": 0.5,
    "mrr": 0.4236111111111111,
    "ndcg@5": 0.4852888182323138,
}


def _number(block: dict[str, Any], key: str) -> float:
    value = block.get(key)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"报告缺少数值字段: {key}")
    return float(value)


def _sha(value: Any, name: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError(f"报告缺少有效指纹: {name}")
    return value


def _report_sha256(report: dict[str, Any]) -> str:
    payload = json.dumps(
        report, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _factoid_recall(report: dict[str, Any]) -> float:
    rows = [
        row for row in report.get("by_question_type", [])
        if row.get("question_type") == "factoid"
    ]
    if len(rows) != 1 or rows[0].get("n") != 6:
        raise ValueError("train 报告必须包含恰好 6 条 factoid")
    return _number(rows[0], "recall")


def _validate_train_report(report: dict[str, Any]) -> None:
    run = report.get("run") or {}
    git_sha = run.get("git_sha")
    if not isinstance(git_sha, str) or re.fullmatch(r"[0-9a-f]{40}", git_sha) is None:
        raise ValueError("报告缺少有效 Git SHA")
    if run.get("git_tracked_clean") is not True:
        raise ValueError("评测必须在 tracked Git clean 状态运行")

    pipeline = report.get("pipeline") or {}
    for key, expected in _PIPELINE.items():
        if pipeline.get(key) != expected:
            raise ValueError(f"train 管线字段不符合冻结契约: {key}")
    if pipeline.get("split") != "train":
        raise ValueError("报告必须使用完整 train 分区")
    if report.get("with_llm") is not False:
        raise ValueError("确定性 HNSW Gate 禁止 LLM")

    benchmark = report.get("benchmark") or {}
    for field in _BENCHMARK_FIELDS:
        _sha(benchmark.get(field), field)
    if (
        benchmark["database_logical_manifest_sha256"]
        != benchmark["corpus_manifest_sha256"]
    ):
        raise ValueError("数据库逻辑指纹与语料指纹不一致")
    if benchmark.get("resolver_version") != "page-span-v2":
        raise ValueError("报告解析器不是 page-span-v2")

    overall = report.get("overall") or {}
    if overall.get("n_positive") != 24 or overall.get("n_negative") != 0:
        raise ValueError("train 报告必须包含 24 条正例且无负例")
    for key in ("recall@5", "span_coverage@5", "mrr", "ndcg@5"):
        _number(overall, key)
    _factoid_recall(report)

    diagnostics = report.get("diagnostics") or {}
    if diagnostics.get("runtime_degraded_count") != 0:
        raise ValueError("train 报告存在运行时降级")
    snapshot = diagnostics.get("vector_snapshot") or {}
    if (
        snapshot.get("database_chunk_count") != 464
        or snapshot.get("vector_count") != 464
        or snapshot.get("missing_vector_ids") != 0
        or snapshot.get("extra_vector_ids") != 0
        or snapshot.get("embedding_dimension") != 1024
        or snapshot.get("hnsw_space") != "cosine"
        or snapshot.get("hnsw_num_threads") != 1
        or snapshot.get("hnsw_search_ef") != 464
    ):
        raise ValueError("train 报告未通过确定性 HNSW 快照审计")
    expected_config = _config_sha256(464)
    if (
        snapshot.get("vector_manifest_sha256")
        != benchmark["vector_manifest_sha256"]
        or snapshot.get("hnsw_binary_manifest_sha256")
        != benchmark["hnsw_binary_manifest_sha256"]
        or snapshot.get("hnsw_config_sha256") != expected_config
        or benchmark["hnsw_config_sha256"] != expected_config
    ):
        raise ValueError("train 报告 HNSW/向量指纹不符合冻结契约")

    items = report.get("items") or []
    qa_ids = [item.get("qa_id") for item in items]
    if len(items) != 24 or len(set(qa_ids)) != 24 or None in qa_ids:
        raise ValueError("train 报告必须包含 24 个唯一 QA")
    if any(item.get("degraded") is not False for item in items):
        raise ValueError("train 报告逐题存在降级")
    if (report.get("latency") or {}).get("count") != 24:
        raise ValueError("train 报告延迟样本必须为 24")


def _binding(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "git_sha": report["run"]["git_sha"],
        "benchmark": {
            key: report["benchmark"].get(key)
            for key in (*_BENCHMARK_FIELDS, "resolver_version")
        },
        "pipeline": {
            key: report["pipeline"].get(key)
            for key in (*_PIPELINE, "split")
        },
    }


def evaluate_train_repeatability(
    first: dict[str, Any], second: dict[str, Any]
) -> dict[str, Any]:
    """验证两个独立进程的完整 train 排序、指标与冻结身份。"""
    _validate_train_report(first)
    _validate_train_report(second)
    if _binding(first) != _binding(second):
        raise ValueError("两份 train 报告的 Git、指纹或管线身份不一致")

    first_items = {item["qa_id"]: item for item in first["items"]}
    second_items = {item["qa_id"]: item for item in second["items"]}
    identical = sum(
        first_items[qa_id].get("retrieved_ids")
        == second_items[qa_id].get("retrieved_ids")
        for qa_id in first_items
    )
    metrics = {
        "recall@5": _number(first["overall"], "recall@5"),
        "factoid_recall": _factoid_recall(first),
        "mrr": _number(first["overall"], "mrr"),
        "ndcg@5": _number(first["overall"], "ndcg@5"),
    }
    second_metrics = {
        "recall@5": _number(second["overall"], "recall@5"),
        "factoid_recall": _factoid_recall(second),
        "mrr": _number(second["overall"], "mrr"),
        "ndcg@5": _number(second["overall"], "ndcg@5"),
    }
    checks = {
        key: {
            "actual": value,
            "threshold": _TRAIN_THRESHOLDS[key],
            "operator": ">=",
            "passed": value >= _TRAIN_THRESHOLDS[key],
        }
        for key, value in metrics.items()
    }
    checks["metrics_exactly_equal"] = {
        "actual": metrics == second_metrics,
        "threshold": True,
        "operator": "==",
        "passed": metrics == second_metrics,
    }
    checks["top5_exactly_equal"] = {
        "actual": identical,
        "threshold": 24,
        "operator": "==",
        "passed": identical == 24,
    }
    for label, report in (("first", first), ("second", second)):
        p95 = _number(report.get("latency") or {}, "p95")
        checks[f"{label}_p95_ms"] = {
            "actual": p95,
            "threshold": 1000.0,
            "operator": "<",
            "passed": p95 < 1000.0,
        }
    return {
        "gate_version": "deterministic-hnsw-train-repeatability-v1",
        "passed": all(check["passed"] for check in checks.values()),
        "identical_top5": identical,
        "checks": checks,
        "binding": _binding(first),
        "input_report_sha256": {
            "first": _report_sha256(first),
            "second": _report_sha256(second),
        },
    }


def evaluate_paired_dev(report: dict[str, Any]) -> dict[str, Any]:
    """验证一次遍历内的当前生产/确定性候选 dev 非回退。"""
    if report.get("gate_version") != "deterministic-hnsw-paired-dev-v1":
        raise ValueError("不是 Batch 22H 配对 dev 报告")
    run = report.get("run") or {}
    if run.get("git_tracked_clean") is not True:
        raise ValueError("配对 dev 必须在 tracked Git clean 状态运行")
    if re.fullmatch(r"[0-9a-f]{40}", str(run.get("git_sha", ""))) is None:
        raise ValueError("配对 dev 缺少有效 Git SHA")
    pipeline = report.get("pipeline") or {}
    expected = {key: value for key, value in _PIPELINE.items() if key != "effective_profile"}
    for key, value in expected.items():
        if pipeline.get(key) != value:
            raise ValueError(f"配对 dev 管线字段错误: {key}")
    if pipeline.get("split") != "dev":
        raise ValueError("配对评测只允许完整 dev，禁止 holdout")
    benchmark = report.get("benchmark") or {}
    for field in _BENCHMARK_FIELDS[:5]:
        _sha(benchmark.get(field), field)
    if benchmark.get("resolver_version") != "page-span-v2":
        raise ValueError("配对 dev 必须使用 page-span-v2")

    baseline = report.get("baseline") or {}
    candidate = report.get("candidate") or {}
    for field in ("vector_manifest_sha256", "hnsw_binary_manifest_sha256"):
        if _sha(baseline.get(field), field) != _sha(candidate.get(field), field):
            raise ValueError("基线与候选向量/HNSW 结构指纹不一致")
    if baseline.get("hnsw_num_threads") is not None or baseline.get("hnsw_search_ef") is not None:
        raise ValueError("配对基线必须是当前未冻结生产 HNSW")
    if candidate.get("hnsw_num_threads") != 1 or candidate.get("hnsw_search_ef") != 464:
        raise ValueError("配对候选未使用确定性 HNSW")
    for side in (baseline, candidate):
        if side.get("runtime_degraded_count") != 0:
            raise ValueError("配对 dev 存在运行时降级")
        if (side.get("latency") or {}).get("count") != 24:
            raise ValueError("配对 dev 延迟样本必须为 24")
    items = report.get("items") or []
    qa_ids = [item.get("qa_id") for item in items]
    if len(items) != 24 or len(set(qa_ids)) != 24 or None in qa_ids:
        raise ValueError("配对 dev 必须一次遍历 24 个唯一 QA")

    baseline_metrics = baseline.get("overall") or {}
    candidate_metrics = candidate.get("overall") or {}
    metric_names = ("recall@5", "factoid_recall", "mrr", "ndcg@5")
    checks: dict[str, dict[str, Any]] = {}
    gains = []
    for name in metric_names:
        gain = _number(candidate_metrics, name) - _number(baseline_metrics, name)
        gains.append(gain)
        checks[f"{name}_non_regression"] = {
            "actual": gain,
            "threshold": 0.0,
            "operator": ">=",
            "passed": gain >= 0.0,
        }
    checks["strict_quality_gain"] = {
        "actual": max(gains),
        "threshold": 0.0,
        "operator": ">",
        "passed": any(gain > 0.0 for gain in gains),
    }
    p95 = _number(candidate.get("latency") or {}, "p95")
    checks["candidate_p95_ms"] = {
        "actual": p95,
        "threshold": 1000.0,
        "operator": "<",
        "passed": p95 < 1000.0,
    }
    return {
        "gate_version": "deterministic-hnsw-paired-dev-gate-v1",
        "passed": all(check["passed"] for check in checks.values()),
        "checks": checks,
        "input_report_sha256": _report_sha256(report),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Batch 22H 确定性 HNSW Gate")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--paired-dev", help="配对 dev JSON 报告")
    mode.add_argument("--train-first", help="第一次 train 报告")
    parser.add_argument("--train-second", help="第二次 train 报告")
    parser.add_argument("--output", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output = Path(args.output)
    if output.exists():
        raise FileExistsError(f"Gate 输出已存在，拒绝覆盖: {output}")
    if args.paired_dev:
        report = json.loads(Path(args.paired_dev).read_text(encoding="utf-8"))
        gate = evaluate_paired_dev(report)
    else:
        if not args.train_second:
            raise ValueError("train Gate 必须同时提供 --train-second")
        first = json.loads(Path(args.train_first).read_text(encoding="utf-8"))
        second = json.loads(Path(args.train_second).read_text(encoding="utf-8"))
        gate = evaluate_train_repeatability(first, second)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(gate, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(gate, ensure_ascii=False, indent=2))
    return 0 if gate["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
