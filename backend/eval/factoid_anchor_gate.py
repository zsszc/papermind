"""Batch 22I factoid 锚点路由 train 配对 Gate。"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from eval.deterministic_vector_snapshot import _config_sha256
from eval.run import factoid_anchor_contract_metadata


_SHA_FIELDS = (
    "dataset_sha256", "qrels_sha256", "corpus_manifest_sha256",
    "database_logical_manifest_sha256", "page_text_manifest_sha256",
    "vector_manifest_sha256", "hnsw_config_sha256",
    "hnsw_binary_manifest_sha256", "factoid_anchor_formula_sha256",
    "shared_routes_sha256", "anchor_decisions_sha256",
)
_QUESTION_TYPES = {
    "factoid", "summary", "method_detail", "experiment_data",
}
_EPSILON = 1e-12


def _sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError(f"报告缺少有效指纹: {field}")
    return value


def _number(block: dict[str, Any], field: str) -> float:
    value = block.get(field)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"报告缺少数值字段: {field}")
    return float(value)


def _report_sha256(report: dict[str, Any]) -> str:
    payload = json.dumps(
        report, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _type_rows(side: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = side.get("by_question_type") or []
    mapped = {str(row.get("question_type")): row for row in rows}
    if set(mapped) != _QUESTION_TYPES or len(rows) != 4:
        raise ValueError("配对 train 问题类型集合不完整")
    if any(row.get("n") != 6 for row in rows):
        raise ValueError("配对 train 每类必须恰好 6 题")
    return mapped


def _validate_protocol(report: dict[str, Any]) -> None:
    if report.get("report_schema") != "factoid-anchor-paired-v1":
        raise ValueError("不是 Batch 22I 配对报告")
    run = report.get("run") or {}
    if re.fullmatch(r"[0-9a-f]{40}", str(run.get("git_sha", ""))) is None:
        raise ValueError("配对 train 缺少有效 Git SHA")
    if run.get("git_tracked_clean") is not True:
        raise ValueError("配对 train 必须在 tracked Git clean 状态运行")

    benchmark = report.get("benchmark") or {}
    for field in _SHA_FIELDS:
        _sha(benchmark.get(field), field)
    if benchmark["database_logical_manifest_sha256"] != benchmark[
        "corpus_manifest_sha256"
    ]:
        raise ValueError("数据库逻辑指纹与语料指纹不一致")
    if benchmark.get("resolver_version") != "page-span-v2":
        raise ValueError("配对 train 必须使用 page-span-v2")

    contract = factoid_anchor_contract_metadata()
    pipeline = report.get("pipeline") or {}
    expected = {
        "baseline_profile": "hybrid",
        "candidate_profile": "hybrid-anchor-v1",
        "lexical_profile": "bm25-bilingual",
        "split": "train",
        "evidence_resolver": "page-span-v2",
        "top_k": 5,
        "route_limit": 10,
        "semantic_rerank": False,
    }
    for field, value in expected.items():
        if pipeline.get(field) != value:
            raise ValueError(f"train 管线字段不符合冻结契约: {field}")
    if (
        pipeline.get("factoid_anchor") != contract
        or benchmark["factoid_anchor_formula_sha256"] != contract["formula_sha256"]
    ):
        raise ValueError("factoid 锚点算法指纹与当前代码不一致")
    if report.get("with_llm") is not False:
        raise ValueError("配对 train 禁止 LLM")

    snapshot = report.get("snapshot") or {}
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
        raise ValueError("配对 train 未通过确定性 HNSW 快照审计")
    expected_config = _config_sha256(464)
    if (
        snapshot.get("vector_manifest_sha256") != benchmark["vector_manifest_sha256"]
        or snapshot.get("hnsw_config_sha256") != expected_config
        or benchmark["hnsw_config_sha256"] != expected_config
        or snapshot.get("hnsw_binary_manifest_sha256")
        != benchmark["hnsw_binary_manifest_sha256"]
    ):
        raise ValueError("配对 train HNSW/向量指纹不符合冻结契约")

    items = report.get("items") or []
    qa_ids = [item.get("qa_id") for item in items]
    if len(items) != 24 or len(set(qa_ids)) != 24 or None in qa_ids:
        raise ValueError("配对 train 必须包含 24 个唯一 QA")
    if any(item.get("degraded") is not False for item in items):
        raise ValueError("配对 train 存在逐题降级")
    for item in items:
        if not item.get("has_anchor") and item.get(
            "baseline_retrieved_ids"
        ) != item.get("candidate_retrieved_ids"):
            raise ValueError("无锚点问题未保持完整 top-5 顺序一致")

    summary = report.get("anchor_summary") or {}
    eligible = sum(bool(item.get("has_anchor")) for item in items)
    if (
        summary.get("eligible") != eligible
        or summary.get("no_anchor") != 24 - eligible
        or not isinstance(summary.get("routed"), int)
        or not 0 <= summary["routed"] <= eligible
    ):
        raise ValueError("锚点路由统计与逐题决策不一致")

    for label in ("baseline", "candidate"):
        side = report.get(label) or {}
        if side.get("runtime_degraded_count") != 0:
            raise ValueError("配对 train 存在运行时降级")
        if (side.get("latency") or {}).get("count") != 24:
            raise ValueError("配对 train 延迟样本必须为 24")
        _type_rows(side)


def evaluate_anchor_train(report: dict[str, Any]) -> dict[str, Any]:
    """执行预注册 train Gate；失败时不允许查看 dev。"""
    _validate_protocol(report)
    baseline = report["baseline"]
    candidate = report["candidate"]
    baseline_overall = baseline.get("overall") or {}
    candidate_overall = candidate.get("overall") or {}
    checks: dict[str, dict[str, Any]] = {}

    for metric in ("recall@5", "mrr", "ndcg@5"):
        gain = _number(candidate_overall, metric) - _number(baseline_overall, metric)
        checks[f"{metric}_non_regression"] = {
            "actual": gain, "threshold": 0.0, "operator": ">=",
            "passed": gain >= -_EPSILON,
        }
    factoid_gain = _number(candidate_overall, "factoid_recall") - _number(
        baseline_overall, "factoid_recall"
    )
    checks["factoid_recall_gain"] = {
        "actual": factoid_gain, "threshold": 1 / 6, "operator": ">=",
        "passed": factoid_gain + _EPSILON >= 1 / 6,
    }

    baseline_types = _type_rows(baseline)
    candidate_types = _type_rows(candidate)
    for qtype in sorted(_QUESTION_TYPES):
        gain = _number(candidate_types[qtype], "recall") - _number(
            baseline_types[qtype], "recall"
        )
        checks[f"{qtype}_recall_non_regression"] = {
            "actual": gain, "threshold": 0.0, "operator": ">=",
            "passed": gain >= -_EPSILON,
        }

    factoid_hit_gain = sum(
        float(item["candidate_recall"]) - float(item["baseline_recall"])
        for item in report["items"] if item["question_type"] == "factoid"
    )
    checks["factoid_hit_gain"] = {
        "actual": factoid_hit_gain, "threshold": 1.0, "operator": ">=",
        "passed": factoid_hit_gain + _EPSILON >= 1.0,
    }
    p95 = _number(candidate.get("latency") or {}, "p95")
    checks["candidate_p95_ms"] = {
        "actual": p95, "threshold": 1000.0, "operator": "<",
        "passed": p95 < 1000.0,
    }
    return {
        "gate_version": "factoid-anchor-train-gate-v1",
        "passed": all(check["passed"] for check in checks.values()),
        "checks": checks,
        "input_report_sha256": _report_sha256(report),
        "binding": {
            "git_sha": report["run"]["git_sha"],
            "dataset_sha256": report["benchmark"]["dataset_sha256"],
            "corpus_manifest_sha256": report["benchmark"][
                "corpus_manifest_sha256"
            ],
            "vector_manifest_sha256": report["benchmark"][
                "vector_manifest_sha256"
            ],
            "hnsw_config_sha256": report["benchmark"]["hnsw_config_sha256"],
            "formula_sha256": report["benchmark"][
                "factoid_anchor_formula_sha256"
            ],
        },
    }
