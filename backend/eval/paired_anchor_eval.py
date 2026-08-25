"""Batch 22I 生产 hybrid / factoid 锚点候选的同次遍历评测。"""

from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import platform
import time
from typing import Any, Callable

from eval.dataset import load_dataset, validate_dataset
from eval.factoid_anchor_gate import evaluate_anchor_train
from eval.metrics import latency_stats, mrr, ndcg_at_k, recall_at_k
from eval.run import (
    Retriever, _audit_vector_snapshot, _build_benchmark_metadata,
    _git_sha, _git_tracked_clean, _qrels_sha256,
    _resolve_span_qrels_or_raise, _select_split,
    factoid_anchor_contract_metadata,
)
from app.services.retrieval_pipeline import (
    anchor_chunk_search, anchor_rrf_fuse_chunks, extract_factoid_anchors,
    keyword_chunk_search, rrf_fuse_chunks,
)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _sha_json(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def evaluate_anchor_paired_routes(
    items: list[dict[str, Any]],
    resolved_qrels: dict[str, list[str]],
    semantic_search: Callable[[str], list[dict[str, Any]]],
    keyword_search: Callable[[str], list[dict[str, Any]]],
    anchor_search: Callable[[str], list[dict[str, Any]]],
    *,
    top_k: int,
) -> dict[str, Any]:
    """每题只计算一次共享 semantic/BM25，再派生两个排序。"""
    records: list[dict[str, Any]] = []
    metrics = {
        side: defaultdict(list) for side in ("baseline", "candidate")
    }
    shared_routes: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    eligible = routed = 0

    for entry in items:
        question = entry["question"]
        common_started = time.perf_counter()
        semantic = semantic_search(question)
        keyword = keyword_search(question)
        common_ms = (time.perf_counter() - common_started) * 1000.0
        fuse_started = time.perf_counter()
        baseline = rrf_fuse_chunks(semantic, keyword, top_k)
        baseline_ms = common_ms + (time.perf_counter() - fuse_started) * 1000.0

        anchors = extract_factoid_anchors(question)
        anchor_results: list[dict[str, Any]] = []
        anchor_started = time.perf_counter()
        if anchors:
            eligible += 1
            anchor_results = anchor_search(question)
            routed += int(bool(anchor_results))
        candidate = anchor_rrf_fuse_chunks(
            semantic, keyword, anchor_results, top_k
        )
        candidate_ms = common_ms + (time.perf_counter() - anchor_started) * 1000.0

        qa_id = entry["qa_id"]
        relevant = resolved_qrels[qa_id]
        record: dict[str, Any] = {
            "qa_id": qa_id,
            "question_type": entry["question_type"],
            "has_anchor": bool(anchors),
            "degraded": False,
        }
        for side, results, latency in (
            ("baseline", baseline, baseline_ms),
            ("candidate", candidate, candidate_ms),
        ):
            ids = [str(item["chunk_id"]) for item in results]
            rec = recall_at_k(ids, relevant, top_k)
            rr = mrr(ids, relevant)
            nd = ndcg_at_k(ids, relevant, top_k)
            record.update({
                f"{side}_retrieved_ids": ids,
                f"{side}_recall": rec,
                f"{side}_mrr": rr,
                f"{side}_ndcg": nd,
                f"{side}_latency_ms": round(latency, 3),
            })
            metrics[side]["recall"].append(rec)
            metrics[side]["mrr"].append(rr)
            metrics[side]["ndcg"].append(nd)
            metrics[side]["latency"].append(round(latency, 3))
            metrics[side][f"type:{entry['question_type']}"].append(rec)
            if entry["question_type"] == "factoid":
                metrics[side]["factoid"].append(rec)
        records.append(record)
        shared_routes.append({
            "qa_id": qa_id,
            "semantic": [str(row.get("chunk_id")) for row in semantic],
            "keyword": [str(row.get("chunk_id")) for row in keyword],
        })
        decisions.append({"qa_id": qa_id, "anchors": anchors})

    result: dict[str, Any] = {
        "items": records,
        "anchor_summary": {
            "eligible": eligible, "routed": routed,
            "no_anchor": len(items) - eligible,
        },
        "shared_routes_sha256": _sha_json(shared_routes),
        "anchor_decisions_sha256": _sha_json(decisions),
    }
    question_types = sorted({item["question_type"] for item in items})
    for side in ("baseline", "candidate"):
        values = metrics[side]
        result[side] = {
            "runtime_degraded_count": 0,
            "overall": {
                f"recall@{top_k}": _mean(values["recall"]),
                "factoid_recall": _mean(values["factoid"]),
                "mrr": _mean(values["mrr"]),
                f"ndcg@{top_k}": _mean(values["ndcg"]),
            },
            "by_question_type": [
                {"question_type": qtype,
                 "n": len(values[f"type:{qtype}"]),
                 "recall": _mean(values[f"type:{qtype}"])}
                for qtype in question_types
            ],
            "latency": latency_stats(values["latency"]),
        }
    return result


def run_paired_train(
    args: argparse.Namespace,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """运行 tracked-clean、去原文的真实论文 train 配对 Harness。"""
    if _git_tracked_clean() is not True:
        raise ValueError("配对 train 必须在 tracked Git clean 状态运行")
    items = _select_split(load_dataset(args.dataset), "train")
    validate_dataset(items)
    if len(items) != 24 or any(not item["has_answer"] for item in items):
        raise ValueError("配对 train 必须包含完整 24 条正例")

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
        resolved_qrels, _spans, page_manifest = _resolve_span_qrels_or_raise(
            db, items, runtime_root=runtime_root
        )
        benchmark["page_text_manifest_sha256"] = page_manifest

        retriever = Retriever(
            db, top_k=5, lexical_profile="bm25-bilingual",
            vector_dir=Path(args.vector_dir), retrieval_profile="hybrid",
        )
        if retriever.degraded or retriever._store is None:
            raise ValueError("配对 train 语义检索初始化降级")
        snapshot = _audit_vector_snapshot(db, retriever._store)
        for field in (
            "vector_manifest_sha256", "hnsw_config_sha256",
            "hnsw_binary_manifest_sha256",
        ):
            benchmark[field] = snapshot[field]

        route_limit = 10
        paired = evaluate_anchor_paired_routes(
            items,
            resolved_qrels,
            lambda query: retriever._store.search(
                query=query, top_k=route_limit, filters={}, rerank=False
            ),
            lambda query: keyword_chunk_search(
                db, query, route_limit, lexical_profile="bm25-bilingual", filters={}
            ),
            lambda query: anchor_chunk_search(
                db, query, limit=route_limit, filters={}
            ),
            top_k=5,
        )
    finally:
        db.close()
        engine.dispose()

    contract = factoid_anchor_contract_metadata()
    benchmark.update({
        "factoid_anchor_formula_sha256": contract["formula_sha256"],
        "shared_routes_sha256": paired["shared_routes_sha256"],
        "anchor_decisions_sha256": paired["anchor_decisions_sha256"],
    })
    report = {
        "report_schema": "factoid-anchor-paired-v1",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "run": {
            "git_sha": _git_sha(), "git_tracked_clean": _git_tracked_clean(),
            "python": platform.python_version(),
        },
        "benchmark": benchmark,
        "pipeline": {
            "baseline_profile": "hybrid",
            "candidate_profile": "hybrid-anchor-v1",
            "lexical_profile": "bm25-bilingual",
            "split": "train",
            "evidence_resolver": "page-span-v2",
            "top_k": 5,
            "route_limit": 10,
            "semantic_rerank": False,
            "factoid_anchor": contract,
        },
        "snapshot": snapshot,
        "baseline": paired["baseline"],
        "candidate": paired["candidate"],
        "anchor_summary": paired["anchor_summary"],
        "items": paired["items"],
        "with_llm": False,
    }
    gate = evaluate_anchor_train(report)
    return report, gate


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Batch 22I 锚点路由配对 train")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--corpus-root", required=True)
    parser.add_argument("--vector-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--selection-output", required=True)
    return parser


def _exclusive_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output = Path(args.output)
    selection_output = Path(args.selection_output)
    if output.exists() or selection_output.exists():
        raise FileExistsError("配对报告或选择制品已存在，拒绝覆盖")
    report, gate = run_paired_train(args)
    _exclusive_write(output, report)
    _exclusive_write(selection_output, gate)
    print(json.dumps({
        "passed": gate["passed"],
        "baseline": report["baseline"]["overall"],
        "candidate": report["candidate"]["overall"],
        "anchor_summary": report["anchor_summary"],
        "candidate_p95_ms": report["candidate"]["latency"]["p95"],
        "output": str(output), "selection_output": str(selection_output),
    }, ensure_ascii=False, indent=2))
    return 0 if gate["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
