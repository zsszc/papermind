"""Benchmark v2 train 检索失败的去标识化归因工具。"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


SCHEMA = "train-failure-diagnostics-v1"
PRIVATE_ROOT = Path(__file__).resolve().parent / "private"
_SHA_RE = re.compile(r"[0-9a-f]{64}")
_GIT_SHA_RE = re.compile(r"[0-9a-f]{40}")
_CHUNK_ID_RE = re.compile(r"p([1-9][0-9]*)_c-?[0-9]+")
_QUESTION_TYPES = frozenset({
    "experiment_data", "factoid", "method_detail", "summary",
})
_CATEGORY_ORDER = (
    "cross_paper_miss",
    "same_paper_miss",
    "partial_coverage",
    "empty_retrieval",
    "full_coverage",
)
_FAILURE_PRIORITY = (
    "cross_paper_miss",
    "same_paper_miss",
    "partial_coverage",
    "empty_retrieval",
)
_CANDIDATE_BY_CATEGORY = {
    "cross_paper_miss": "query-document-expansion-v1",
    "same_paper_miss": "paper-first-evidence-rerank-v1",
    "partial_coverage": "boundary-aware-evidence-v1",
    "empty_retrieval": "runtime-integrity-audit-v1",
}
_EPSILON = 1e-12


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def report_sha256(report: dict[str, Any]) -> str:
    """返回未改写输入报告的稳定 SHA-256。"""
    return _sha256(report)


def _require_sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SHA_RE.fullmatch(value) is None:
        raise ValueError(f"报告缺少有效 SHA-256: {field}")
    return value


def _unit_number(value: Any, field: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or not 0.0 <= float(value) <= 1.0
    ):
        raise ValueError(f"{field} 必须是 [0, 1] 有限数值")
    return float(value)


def _non_negative_number(value: Any, field: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or float(value) < 0.0
    ):
        raise ValueError(f"{field} 必须是非负有限数值")
    return float(value)


def _paper_ids(chunk_ids: list[str], field: str) -> set[int]:
    papers: set[int] = set()
    for chunk_id in chunk_ids:
        if not isinstance(chunk_id, str):
            raise ValueError(f"{field} 含非字符串 chunk ID")
        match = _CHUNK_ID_RE.fullmatch(chunk_id)
        if match is None:
            raise ValueError(f"{field} 含畸形 chunk ID")
        papers.add(int(match.group(1)))
    return papers


def _validate_historical_vector_contract(report: dict[str, Any]) -> None:
    """历史 dirty 报告必须以完整冻结指纹和确定性向量快照补强来源。"""
    benchmark = report.get("benchmark") or {}
    required = (
        "dataset_sha256",
        "qrels_sha256",
        "corpus_manifest_sha256",
        "database_logical_manifest_sha256",
        "page_text_manifest_sha256",
        "vector_manifest_sha256",
        "hnsw_config_sha256",
        "hnsw_binary_manifest_sha256",
    )
    for field in required:
        _require_sha(benchmark.get(field), field)
    if (
        benchmark["database_logical_manifest_sha256"]
        != benchmark["corpus_manifest_sha256"]
    ):
        raise ValueError("历史报告数据库与语料指纹不一致")

    snapshot = (report.get("diagnostics") or {}).get("vector_snapshot") or {}
    database_count = snapshot.get("database_chunk_count")
    vector_count = snapshot.get("vector_count")
    valid_counts = (
        isinstance(database_count, int)
        and not isinstance(database_count, bool)
        and database_count > 0
        and vector_count == database_count
    )
    if (
        not valid_counts
        or snapshot.get("missing_vector_ids") != 0
        or snapshot.get("extra_vector_ids") != 0
        or snapshot.get("embedding_dimension") != 1024
        or snapshot.get("hnsw_space") != "cosine"
        or snapshot.get("hnsw_num_threads") != 1
        or snapshot.get("hnsw_search_ef") != vector_count
    ):
        raise ValueError("历史报告向量快照不完整")
    for field in (
        "vector_manifest_sha256",
        "hnsw_config_sha256",
        "hnsw_binary_manifest_sha256",
    ):
        if _require_sha(snapshot.get(field), f"vector_snapshot.{field}") != benchmark[field]:
            raise ValueError("历史报告向量快照指纹不一致")


def _validate_report(
    report: dict[str, Any],
    *,
    allow_historical_dirty: bool = False,
    historical_commit_verified: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not isinstance(report, dict) or report.get("report_schema") != "2.0":
        raise ValueError("只接受 eval.run report_schema=2.0 报告")

    run = report.get("run") or {}
    if not isinstance(run.get("git_sha"), str) or _GIT_SHA_RE.fullmatch(
        run["git_sha"]
    ) is None:
        raise ValueError("train 报告必须绑定 clean Git SHA")
    source_clean = run.get("git_tracked_clean") is True
    if not source_clean:
        if not allow_historical_dirty:
            raise ValueError("train 报告必须绑定 clean Git SHA")
        if not historical_commit_verified:
            raise ValueError("历史报告 Git SHA 未验证为当前 HEAD 祖先")
        _validate_historical_vector_contract(report)
    provenance = {
        "source_git_tracked_clean": source_clean,
        "historical_dirty_override": not source_clean,
        "historical_commit_verified": (
            historical_commit_verified if not source_clean else False
        ),
        "usage": "candidate-selection-only" if not source_clean else "diagnostics",
        "promotion_eligible": source_clean,
    }

    benchmark = report.get("benchmark") or {}
    for field in (
        "dataset_sha256", "qrels_sha256", "corpus_manifest_sha256",
    ):
        _require_sha(benchmark.get(field), field)

    pipeline = report.get("pipeline") or {}
    expected = {
        "split": "train",
        "top_k": 5,
        "evidence_resolver": "page-span-v2",
    }
    for field, value in expected.items():
        if pipeline.get(field) != value:
            raise ValueError(f"只允许完整 train 报告，{field} 必须为 {value}")
    if not isinstance(pipeline.get("profile"), str) or not isinstance(
        pipeline.get("lexical_profile"), str
    ):
        raise ValueError("train 报告缺少检索 profile")
    if report.get("with_llm") is not False:
        raise ValueError("失败归因禁止使用 LLM 报告")
    if (report.get("diagnostics") or {}).get("runtime_degraded_count") != 0:
        raise ValueError("train 报告存在运行时降级")

    overall = report.get("overall") or {}
    count = overall.get("n_positive")
    if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
        raise ValueError("train 报告 n_positive 必须为正整数")
    if overall.get("n_negative") != 0:
        raise ValueError("train 失败归因不接受负例")
    for field in ("recall@5", "mrr", "ndcg@5", "span_coverage@5"):
        _unit_number(overall.get(field), field)

    latency = report.get("latency") or {}
    if latency.get("count") != count:
        raise ValueError("延迟样本数必须等于 n_positive")
    _non_negative_number(latency.get("p95"), "latency.p95")

    items = report.get("items")
    if not isinstance(items, list) or len(items) != count:
        raise ValueError("逐题 items 数量必须等于 n_positive")
    qa_ids: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("items 必须全部为对象")
        qa_id = item.get("qa_id")
        if not isinstance(qa_id, str) or not qa_id or qa_id in qa_ids:
            raise ValueError("qa_id 必须非空且全局唯一")
        qa_ids.add(qa_id)
        question_type = item.get("question_type")
        if question_type not in _QUESTION_TYPES:
            raise ValueError("question_type 不属于冻结枚举")
        if item.get("has_answer") is not True:
            raise ValueError("train 归因只接受正例")
        if item.get("degraded") is not False:
            raise ValueError("逐题结果存在运行时降级")

        relevant = item.get("relevant_ids")
        retrieved = item.get("retrieved_ids")
        if not isinstance(relevant, list) or not relevant:
            raise ValueError("relevant_ids 必须为非空列表")
        if not isinstance(retrieved, list):
            raise ValueError("retrieved_ids 必须为列表")
        if len(retrieved) > 5:
            raise ValueError("retrieved_ids 超过 top_k")
        if len(relevant) != len(set(relevant)) or len(retrieved) != len(
            set(retrieved)
        ):
            raise ValueError("chunk ID 列表不得重复")
        _paper_ids(relevant, "relevant_ids")
        _paper_ids(retrieved, "retrieved_ids")

        coverage = _unit_number(item.get("span_coverage"), "span_coverage")
        any_hit = _unit_number(item.get("any_hit"), "any_hit")
        for field in ("recall", "mrr", "ndcg"):
            _unit_number(item.get(field), field)
        if bool(coverage > _EPSILON) != bool(any_hit > _EPSILON):
            raise ValueError("any_hit 与 span_coverage 不一致")
        if not retrieved and (coverage > _EPSILON or any_hit > _EPSILON):
            raise ValueError("空检索结果不能产生证据覆盖")
    return items, provenance


def _classify(item: dict[str, Any]) -> str:
    retrieved = item["retrieved_ids"]
    if not retrieved:
        return "empty_retrieval"
    coverage = float(item["span_coverage"])
    if math.isclose(coverage, 1.0, abs_tol=_EPSILON):
        return "full_coverage"
    if coverage > _EPSILON:
        return "partial_coverage"
    relevant_papers = _paper_ids(item["relevant_ids"], "relevant_ids")
    retrieved_papers = _paper_ids(retrieved, "retrieved_ids")
    if relevant_papers & retrieved_papers:
        return "same_paper_miss"
    return "cross_paper_miss"


def _category_rows(counts: Counter[str], total: int) -> list[dict[str, Any]]:
    return [
        {
            "category": category,
            "count": counts.get(category, 0),
            "share": counts.get(category, 0) / total,
        }
        for category in _CATEGORY_ORDER
    ]
def _recommendation(
    counts: Counter[str], total: int, report: dict[str, Any]
) -> dict[str, Any]:
    dominant = max(
        _FAILURE_PRIORITY,
        key=lambda category: (
            counts.get(category, 0),
            -_FAILURE_PRIORITY.index(category),
        ),
    )
    support = counts.get(dominant, 0)
    candidate = _CANDIDATE_BY_CATEGORY[dominant] if support else "none"
    overall = report["overall"]
    qtypes = sorted({item["question_type"] for item in report["items"]})
    return {
        "dominant_failure": dominant if support else None,
        "candidate": candidate,
        "support_count": support,
        "support_share": support / total,
        "train_gate": {
            "baseline": {
                "recall@5": float(overall["recall@5"]),
                "mrr": float(overall["mrr"]),
                "ndcg@5": float(overall["ndcg@5"]),
                "span_coverage@5": float(overall["span_coverage@5"]),
            },
            "minimum_span_coverage_gain": 1 / total,
            "non_regression_metrics": ["recall@5", "mrr", "ndcg@5"],
            "question_type_non_regression": qtypes,
            "maximum_p95_ms": 1000.0,
            "runtime_degraded_count": 0,
            "requires_fresh_clean_baseline": True,
            "dev_policy": "run-once-only-after-train-pass",
            "holdout_policy": "forbidden",
        },
    }


def analyze_train_report(
    report: dict[str, Any],
    *,
    allow_historical_dirty: bool = False,
    historical_commit_verified: bool = False,
) -> dict[str, Any]:
    """验证完整 train 报告并返回不含逐题身份的确定性聚合。"""
    items, provenance = _validate_report(
        report,
        allow_historical_dirty=allow_historical_dirty,
        historical_commit_verified=historical_commit_verified,
    )
    counts: Counter[str] = Counter()
    by_type: dict[str, Counter[str]] = defaultdict(Counter)
    type_coverage: dict[str, list[float]] = defaultdict(list)
    for item in items:
        category = _classify(item)
        counts[category] += 1
        qtype = item["question_type"]
        by_type[qtype][category] += 1
        type_coverage[qtype].append(float(item["span_coverage"]))

    total = len(items)
    if sum(counts.values()) != total:
        raise AssertionError("失败归因总数不守恒")
    benchmark = report["benchmark"]
    pipeline = report["pipeline"]
    return {
        "schema": SCHEMA,
        "input_report_sha256": report_sha256(report),
        "binding": {
            "git_sha": report["run"]["git_sha"],
            "dataset_sha256": benchmark["dataset_sha256"],
            "qrels_sha256": benchmark["qrels_sha256"],
            "corpus_manifest_sha256": benchmark["corpus_manifest_sha256"],
            "profile": pipeline["profile"],
            "lexical_profile": pipeline["lexical_profile"],
            "split": "train",
            "top_k": 5,
            "evidence_resolver": "page-span-v2",
        },
        "total_items": total,
        "failure_items": total - counts.get("full_coverage", 0),
        "provenance": provenance,
        "categories": _category_rows(counts, total),
        "by_question_type": [
            {
                "question_type": qtype,
                "n": len(type_coverage[qtype]),
                "mean_span_coverage": sum(type_coverage[qtype])
                / len(type_coverage[qtype]),
                "categories": {
                    category: by_type[qtype].get(category, 0)
                    for category in _CATEGORY_ORDER
                },
            }
            for qtype in sorted(by_type)
        ],
        "recommendation": _recommendation(counts, total, report),
        "privacy": {
            "content_fields_emitted": False,
            "item_identifiers_emitted": False,
        },
    }


def render_report(report: dict[str, Any]) -> str:
    """以稳定键序列化归因报告。"""
    return json.dumps(
        report, ensure_ascii=False, indent=2, sort_keys=True
    ) + "\n"


def validate_cli_path(
    path: Path,
    *,
    private_root: Path = PRIVATE_ROOT,
    must_exist: bool,
) -> Path:
    """限制 CLI 输入输出位于真实私有目录，并拒绝 symlink。"""
    candidate = Path(path)
    root = Path(private_root).resolve(strict=True)
    if candidate.is_symlink():
        raise ValueError("CLI 路径禁止 symlink")
    try:
        resolved = candidate.resolve(strict=must_exist)
    except FileNotFoundError as exc:
        raise ValueError("CLI 输入文件不存在") from exc
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("CLI 路径必须位于私有目录") from exc
    if must_exist and not resolved.is_file():
        raise ValueError("CLI 输入必须是普通文件")
    return resolved


def write_report_exclusive(path: Path, payload: dict[str, Any]) -> None:
    """以 0600 排他创建报告，避免覆盖既有诊断证据。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(target, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            stream.write(render_report(payload))
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        target.unlink(missing_ok=True)
        raise


def verify_commit_ancestor(commit: str, repo_root: Path) -> bool:
    """仅当 commit 是当前 HEAD 的真实祖先时返回 true。"""
    if not isinstance(commit, str) or _GIT_SHA_RE.fullmatch(commit) is None:
        return False
    try:
        completed = subprocess.run(
            ["git", "merge-base", "--is-ancestor", commit, "HEAD"],
            cwd=Path(repo_root),
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="从完整 train 报告生成去标识化失败归因"
    )
    parser.add_argument("--report", required=True, help="私有 train eval 报告")
    parser.add_argument("--output", required=True, help="私有聚合输出路径")
    parser.add_argument(
        "--allow-historical-dirty-report",
        action="store_true",
        help="仅用于候选选择；仍禁止作为晋级证据",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report_path = validate_cli_path(
            Path(args.report), must_exist=True
        )
        output_path = validate_cli_path(
            Path(args.output), must_exist=False
        )
        report = json.loads(report_path.read_text(encoding="utf-8"))
        historical_verified = False
        if args.allow_historical_dirty_report:
            historical_verified = verify_commit_ancestor(
                (report.get("run") or {}).get("git_sha"),
                Path(__file__).resolve().parents[2],
            )
        result = analyze_train_report(
            report,
            allow_historical_dirty=args.allow_historical_dirty_report,
            historical_commit_verified=historical_verified,
        )
        write_report_exclusive(output_path, result)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"[train-diagnostics] FAIL: {type(exc).__name__}")
        return 2
    summary = {
        "schema": result["schema"],
        "input_report_sha256": result["input_report_sha256"],
        "total_items": result["total_items"],
        "failure_items": result["failure_items"],
        "candidate": result["recommendation"]["candidate"],
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
