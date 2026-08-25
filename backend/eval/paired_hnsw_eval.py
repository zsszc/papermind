"""一次遍历内配对评测当前生产 HNSW 与确定性候选。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import platform
import time
from typing import Any

from eval.dataset import load_dataset, validate_dataset
from eval.deterministic_hnsw_gate import evaluate_paired_dev
from eval.deterministic_vector_snapshot import read_raw_snapshot_manifest
from eval.metrics import latency_stats, mrr, ndcg_at_k, recall_at_k
from eval.run import (
    Retriever,
    _audit_vector_snapshot,
    _build_benchmark_metadata,
    _git_sha,
    _git_tracked_clean,
    _qrels_sha256,
    _resolve_span_qrels_or_raise,
    _select_split,
)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def evaluate_paired_retrieval(
    items: list[dict[str, Any]],
    resolved_qrels: dict[str, list[str]],
    baseline_retriever: Any,
    candidate_retriever: Any,
    *,
    top_k: int,
) -> dict[str, Any]:
    """只遍历问题一次，逐题依次查询两个显式向量快照。"""
    records: list[dict[str, Any]] = []
    side_metrics: dict[str, dict[str, list[float]]] = {
        "baseline": {"recall": [], "mrr": [], "ndcg": [], "latency": []},
        "candidate": {"recall": [], "mrr": [], "ndcg": [], "latency": []},
    }
    factoid: dict[str, list[float]] = {"baseline": [], "candidate": []}

    for entry in items:
        qa_id = entry["qa_id"]
        relevant_ids = resolved_qrels[qa_id]
        record: dict[str, Any] = {
            "qa_id": qa_id,
            "question_type": entry["question_type"],
        }
        for label, retriever in (
            ("baseline", baseline_retriever),
            ("candidate", candidate_retriever),
        ):
            started = time.perf_counter()
            results = retriever.search(entry["question"])
            elapsed = round((time.perf_counter() - started) * 1000.0, 3)
            retrieved_ids = [str(item["chunk_id"]) for item in results]
            rec = recall_at_k(retrieved_ids, relevant_ids, top_k)
            reciprocal_rank = mrr(retrieved_ids, relevant_ids)
            discounted_gain = ndcg_at_k(retrieved_ids, relevant_ids, top_k)
            side_metrics[label]["recall"].append(rec)
            side_metrics[label]["mrr"].append(reciprocal_rank)
            side_metrics[label]["ndcg"].append(discounted_gain)
            side_metrics[label]["latency"].append(elapsed)
            if entry["question_type"] == "factoid":
                factoid[label].append(rec)
            record[f"{label}_retrieved_ids"] = retrieved_ids
            record[f"{label}_recall"] = rec
            record[f"{label}_mrr"] = reciprocal_rank
            record[f"{label}_ndcg"] = discounted_gain
            record[f"{label}_latency_ms"] = elapsed
        records.append(record)

    result: dict[str, Any] = {"items": records}
    for label, retriever in (
        ("baseline", baseline_retriever),
        ("candidate", candidate_retriever),
    ):
        metrics = side_metrics[label]
        result[label] = {
            "runtime_degraded_count": retriever.runtime_degraded_count,
            "overall": {
                f"recall@{top_k}": _mean(metrics["recall"]),
                "factoid_recall": _mean(factoid[label]),
                "mrr": _mean(metrics["mrr"]),
                f"ndcg@{top_k}": _mean(metrics["ndcg"]),
            },
            "latency": latency_stats(metrics["latency"]),
        }
    return result


def _snapshot_binding(
    audit: dict[str, Any], preopen: dict[str, Any]
) -> dict[str, Any]:
    fields = (
        "vector_count",
        "embedding_dimension",
        "vector_manifest_sha256",
        "embedding_id_manifest_sha256",
        "hnsw_space",
        "hnsw_num_threads",
        "hnsw_search_ef",
        "hnsw_config_sha256",
        "collection_metadata",
        "segment_metadata",
    )
    return {
        **{field: audit.get(field) for field in fields},
        # 默认 HNSW 会在 client 打开/查询时重写 data_level0/length 的运行时
        # 区域；同源结构必须绑定首次打开前的原始副本指纹。
        "hnsw_binary_manifest_sha256": preopen[
            "hnsw_binary_manifest_sha256"
        ],
        "preopen_full_binary_manifest_sha256": preopen[
            "hnsw_full_binary_manifest_sha256"
        ],
        "postopen_binary_manifest_sha256": audit.get(
            "hnsw_binary_manifest_sha256"
        ),
    }


def run_paired_dev(args: argparse.Namespace) -> tuple[dict[str, Any], bool]:
    """运行真实论文 dev 配对 Harness，并返回去原文报告。"""
    if _git_tracked_clean() is not True:
        raise ValueError("配对 dev 必须在 tracked Git clean 状态运行")
    baseline_path = Path(args.baseline_vector).resolve()
    candidate_path = Path(args.candidate_vector).resolve()
    if baseline_path == candidate_path:
        raise ValueError("基线与候选必须是不同向量目录")

    items = _select_split(load_dataset(args.dataset), "dev")
    validate_dataset(items)
    if len(items) != 24 or any(not item["has_answer"] for item in items):
        raise ValueError("配对 dev 必须包含完整 24 条正例")

    from app.services.data_integrity import open_readonly_sqlalchemy_database

    engine, session_factory = open_readonly_sqlalchemy_database(
        Path(args.database)
    )
    db = session_factory()
    try:
        runtime_root = Path(args.corpus_root).resolve()
        benchmark = _build_benchmark_metadata(
            db, Path(args.dataset), runtime_root=runtime_root
        )
        benchmark.update({
            "qrels_sha256": _qrels_sha256(items),
            "resolver_version": "page-span-v2",
        })
        resolved_qrels, _span_qrels, page_manifest = (
            _resolve_span_qrels_or_raise(
                db, items, runtime_root=runtime_root
            )
        )
        benchmark["page_text_manifest_sha256"] = page_manifest

        baseline_preopen = read_raw_snapshot_manifest(baseline_path)
        candidate_preopen = read_raw_snapshot_manifest(candidate_path)
        for key in (
            "vector_count",
            "embedding_dimension",
            "embedding_id_manifest_sha256",
            "hnsw_binary_manifest_sha256",
        ):
            if baseline_preopen.get(key) != candidate_preopen.get(key):
                raise ValueError(f"配对快照首次打开前指纹不一致: {key}")

        baseline = Retriever(
            db,
            top_k=5,
            lexical_profile="bm25-bilingual",
            vector_dir=baseline_path,
            retrieval_profile="hybrid",
        )
        candidate = Retriever(
            db,
            top_k=5,
            lexical_profile="bm25-bilingual",
            vector_dir=candidate_path,
            retrieval_profile="hybrid",
        )
        if baseline.degraded or candidate.degraded:
            raise ValueError("配对 dev 初始化发生语义检索降级")
        baseline_audit = _audit_vector_snapshot(db, baseline._store)
        candidate_audit = _audit_vector_snapshot(db, candidate._store)
        paired = evaluate_paired_retrieval(
            items, resolved_qrels, baseline, candidate, top_k=5
        )
    finally:
        db.close()
        engine.dispose()

    report = {
        "gate_version": "deterministic-hnsw-paired-dev-v1",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "run": {
            "git_sha": _git_sha(),
            "git_tracked_clean": _git_tracked_clean(),
            "python": platform.python_version(),
        },
        "benchmark": benchmark,
        "pipeline": {
            "profile": "hybrid",
            "lexical_profile": "bm25-bilingual",
            "split": "dev",
            "evidence_resolver": "page-span-v2",
            "top_k": 5,
        },
        "baseline": {
            **_snapshot_binding(baseline_audit, baseline_preopen),
            **paired["baseline"],
        },
        "candidate": {
            **_snapshot_binding(candidate_audit, candidate_preopen),
            **paired["candidate"],
        },
        "items": paired["items"],
    }
    gate = evaluate_paired_dev(report)
    return report, bool(gate["passed"])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="在一次 dev 遍历中配对查询当前生产与确定性候选"
    )
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--corpus-root", required=True)
    parser.add_argument("--baseline-vector", required=True)
    parser.add_argument("--candidate-vector", required=True)
    parser.add_argument("--output", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output = Path(args.output)
    if output.exists():
        raise FileExistsError(f"配对 dev 报告已存在，拒绝覆盖: {output}")
    report, passed = run_paired_dev(args)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    summary = {
        "passed": passed,
        "baseline": report["baseline"]["overall"],
        "candidate": report["candidate"]["overall"],
        "candidate_p95_ms": report["candidate"]["latency"]["p95"],
        "output": str(output),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
