"""Benchmark v2 train 论文内语义深度的去标识化诊断 Harness。"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import math
import os
import re
import shutil
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any

from eval.metrics import evidence_span_coverage_at_k
from eval.train_failure_diagnostics import validate_cli_path, write_report_exclusive


SCHEMA = "within-paper-semantic-diagnostics-v1"
PRIVATE_ROOT = Path(__file__).resolve().parent / "private"
_CHUNK_ID_RE = re.compile(r"p([1-9][0-9]*)_c(-1|[0-9]+)")
_GIT_SHA_RE = re.compile(r"[0-9a-f]{40}")
_SHA_RE = re.compile(r"[0-9a-f]{64}")
_TYPE_COUNTS = {"factoid": 8, "method_detail": 4, "summary": 1}
_CATEGORY_ORDER = (
    "baseline_full",
    "within_paper_semantic_recoverable",
    "selected_paper_semantic_missing",
    "relevant_paper_not_selected",
)
_RANK_BUCKETS = ("1-5", "6-10", "11-20", "21-50", "over_50", "not_found")
_EPSILON = 1e-12
_BINDING_FIELDS = frozenset({
    "git_sha",
    "git_tracked_clean",
    "dataset_sha256",
    "qrels_sha256",
    "corpus_manifest_sha256",
    "database_logical_manifest_sha256",
    "page_text_manifest_sha256",
    "vector_manifest_sha256",
    "hnsw_config_sha256",
    "hnsw_binary_manifest_sha256",
    "vector_source_tree_sha256",
    "split",
    "evidence_resolver",
    "lexical_profile",
    "semantic_rerank",
    "top_k",
    "production_route_limit",
    "filtered_latency_p95_limit_ms",
})
_CONTRACT = {
    "version": SCHEMA,
    "split": "train",
    "item_count": 13,
    "type_counts": _TYPE_COUNTS,
    "production_top_k": 5,
    "production_route_limit": 10,
    "production_fusion": "legacy-rrf-k60-first-seen",
    "within_scope": "unique-production-selected-papers",
    "within_depth": "all-database-chunks-per-paper",
    "embedding_calls_per_item": 1,
    "filtered_latency_p95_limit_ms": 250.0,
    "minimum_recoverable_items": 1,
    "candidate": "within-paper-semantic-rerank-v1",
}


class WithinPaperSemanticCollectionError(ValueError):
    """真实采集失败，只携带固定原因码。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def within_paper_semantic_contract_metadata() -> dict[str, Any]:
    return {**_CONTRACT, "contract_sha256": _sha256(_CONTRACT)}


def _canonical_chunk_ids(
    values: Any,
    field: str,
    *,
    expected_length: int | None = None,
    max_length: int | None = None,
) -> list[str]:
    if not isinstance(values, list):
        raise ValueError(f"{field} 必须是列表")
    if expected_length is not None and len(values) != expected_length:
        raise ValueError(f"{field} 数量必须为 {expected_length}")
    if max_length is not None and len(values) > max_length:
        raise ValueError(f"{field} 数量不得超过 {max_length}")
    if len(values) != len(set(values)):
        raise ValueError(f"{field} 含重复 chunk ID")
    for value in values:
        if not isinstance(value, str) or _CHUNK_ID_RE.fullmatch(value) is None:
            raise ValueError(f"{field} 含畸形 chunk ID")
    return values


def _paper_id(chunk_id: str) -> int:
    match = _CHUNK_ID_RE.fullmatch(chunk_id)
    if match is None:
        raise ValueError("chunk ID 畸形")
    return int(match.group(1))


def _evidence_ids(groups: Any) -> list[str]:
    if not isinstance(groups, list) or not groups:
        raise ValueError("evidence_groups 必须是非空列表")
    result: list[str] = []
    for group in groups:
        if not isinstance(group, dict):
            raise ValueError("evidence group 必须是对象")
        start, end = group.get("page_start"), group.get("page_end")
        if (
            not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or start < 0
            or start >= end
        ):
            raise ValueError("evidence span 坐标非法")
        chunks = group.get("chunks")
        if not isinstance(chunks, list) or not chunks:
            raise ValueError("evidence group 缺少 chunks")
        for chunk in chunks:
            if not isinstance(chunk, dict):
                raise ValueError("evidence chunk 必须是对象")
            chunk_id = chunk.get("chunk_id")
            _canonical_chunk_ids([chunk_id], "evidence chunk")
            chunk_start, chunk_end = chunk.get("page_start"), chunk.get("page_end")
            if (
                not isinstance(chunk_start, int)
                or isinstance(chunk_start, bool)
                or not isinstance(chunk_end, int)
                or isinstance(chunk_end, bool)
                or chunk_start < 0
                or chunk_start >= chunk_end
                or chunk_start >= end
                or chunk_end <= start
            ):
                raise ValueError("evidence chunk 坐标非法")
            result.append(chunk_id)
    return list(dict.fromkeys(result))


def _validate_binding(binding: Any) -> None:
    if not isinstance(binding, dict):
        raise ValueError("binding 必须是对象")
    if sorted(set(binding) - _BINDING_FIELDS):
        raise ValueError("binding 含未知字段，拒绝潜在隐私透传")
    if _GIT_SHA_RE.fullmatch(str(binding.get("git_sha", ""))) is None:
        raise ValueError("缺少有效 Git SHA")
    if binding.get("git_tracked_clean") is not True:
        raise ValueError("诊断必须在 tracked Git clean 状态运行")
    for field in (
        "dataset_sha256",
        "qrels_sha256",
        "corpus_manifest_sha256",
        "database_logical_manifest_sha256",
        "page_text_manifest_sha256",
        "vector_manifest_sha256",
        "hnsw_config_sha256",
        "hnsw_binary_manifest_sha256",
        "vector_source_tree_sha256",
    ):
        if not isinstance(binding.get(field), str) or _SHA_RE.fullmatch(binding[field]) is None:
            raise ValueError(f"缺少有效指纹: {field}")
    if binding["database_logical_manifest_sha256"] != binding["corpus_manifest_sha256"]:
        raise ValueError("数据库与语料指纹不一致")
    expected = {
        "split": "train",
        "evidence_resolver": "page-span-v2",
        "lexical_profile": "bm25-bilingual",
        "semantic_rerank": False,
        "top_k": 5,
        "production_route_limit": 10,
        "filtered_latency_p95_limit_ms": 250.0,
    }
    for field, value in expected.items():
        if binding.get(field) != value:
            raise ValueError(f"{field} 必须为 {value}")


def validate_within_paper_semantic_records(
    records: Any, binding: Any
) -> list[dict[str, Any]]:
    """验证完整 train 内存记录；未知字段不会进入公开聚合。"""
    _validate_binding(binding)
    if not isinstance(records, list) or len(records) != 13:
        raise ValueError("必须提供完整 13 题 train 记录")
    type_counts: Counter[str] = Counter()
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("诊断记录必须是对象")
        qtype = record.get("question_type")
        if qtype not in _TYPE_COUNTS:
            raise ValueError("问题类型不属于冻结 train 枚举")
        type_counts[qtype] += 1
        baseline = _canonical_chunk_ids(
            record.get("baseline_ids"), "baseline_ids", expected_length=5
        )
        routes = record.get("within_paper_routes")
        if not isinstance(routes, list) or not routes or len(routes) > 5:
            raise ValueError("论文内路由数量必须为 1 至 5")
        route_papers: list[int] = []
        for index, route in enumerate(routes):
            ids = _canonical_chunk_ids(route, f"within route {index}")
            if not ids:
                raise ValueError("论文内路由不得为空")
            papers = {_paper_id(value) for value in ids}
            if len(papers) != 1:
                raise ValueError("论文内路由发生范围越界")
            route_papers.append(next(iter(papers)))
        if len(route_papers) != len(set(route_papers)):
            raise ValueError("论文内路由含重复论文")
        baseline_papers = {_paper_id(value) for value in baseline}
        if set(route_papers) != baseline_papers:
            raise ValueError("论文内路由范围与生产已选论文不一致")
        _evidence_ids(record.get("evidence_groups"))
        if record.get("embedding_call_count") != 1:
            raise ValueError("每题必须且只能生成一次查询向量")
        if record.get("filtered_query_count") != len(routes):
            raise ValueError("过滤查询计数与论文路由不一致")
        latency = record.get("filtered_latency_ms")
        if (
            not isinstance(latency, (int, float))
            or isinstance(latency, bool)
            or not math.isfinite(float(latency))
            or latency < 0
        ):
            raise ValueError("过滤查询时延非法")
    if dict(type_counts) != _TYPE_COUNTS:
        raise ValueError("完整 13 题 train 问题类型数量不一致")
    return records


def _percentile(values: list[float], proportion: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * proportion
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _first_rank_bucket(routes: list[list[str]], evidence: set[str]) -> str:
    ranks = [
        rank
        for route in routes
        for rank, chunk_id in enumerate(route, start=1)
        if chunk_id in evidence
    ]
    if not ranks:
        return "not_found"
    rank = min(ranks)
    if rank <= 5:
        return "1-5"
    if rank <= 10:
        return "6-10"
    if rank <= 20:
        return "11-20"
    if rank <= 50:
        return "21-50"
    return "over_50"


def analyze_within_paper_semantic(records: Any, binding: Any) -> dict[str, Any]:
    """输出不含逐题身份、路径或内容的确定性聚合。"""
    validated = validate_within_paper_semantic_records(records, binding)
    categories: Counter[str] = Counter()
    ranks: Counter[str] = Counter()
    baseline_coverages: list[float] = []
    exhaustive_coverages: list[float] = []
    latencies: list[float] = []
    filtered_queries = 0
    recoverable = 0
    for record in validated:
        groups = record["evidence_groups"]
        baseline = record["baseline_ids"]
        routes = record["within_paper_routes"]
        exhaustive = list(dict.fromkeys(value for route in routes for value in route))
        baseline_coverage = evidence_span_coverage_at_k(baseline, groups, 5)
        exhaustive_coverage = evidence_span_coverage_at_k(
            exhaustive, groups, len(exhaustive)
        )
        evidence = set(_evidence_ids(groups))
        selected_papers = {_paper_id(value) for value in baseline}
        relevant_papers = {_paper_id(value) for value in evidence}
        if math.isclose(baseline_coverage, 1.0, abs_tol=_EPSILON):
            category = "baseline_full"
        elif not selected_papers.intersection(relevant_papers):
            category = "relevant_paper_not_selected"
        elif exhaustive_coverage > baseline_coverage + _EPSILON:
            category = "within_paper_semantic_recoverable"
            recoverable += 1
        else:
            category = "selected_paper_semantic_missing"
        categories[category] += 1
        ranks[_first_rank_bucket(routes, evidence)] += 1
        baseline_coverages.append(baseline_coverage)
        exhaustive_coverages.append(exhaustive_coverage)
        latencies.append(float(record["filtered_latency_ms"]))
        filtered_queries += int(record["filtered_query_count"])

    total = len(validated)
    p95 = _percentile(latencies, 0.95)
    gate = recoverable >= 1 and p95 < float(binding["filtered_latency_p95_limit_ms"])
    contract = within_paper_semantic_contract_metadata()
    return {
        "schema": SCHEMA,
        "observation_sha256": _sha256(validated),
        "binding": {
            **{key: binding[key] for key in sorted(_BINDING_FIELDS)},
            "diagnostic_contract_sha256": contract["contract_sha256"],
        },
        "total_items": total,
        "categories": [
            {"category": category, "count": categories[category], "share": categories[category] / total}
            for category in _CATEGORY_ORDER
        ],
        "evidence_first_rank": {bucket: ranks[bucket] for bucket in _RANK_BUCKETS},
        "coverage": {
            "baseline_mean": sum(baseline_coverages) / total,
            "within_selected_papers_exhaustive_mean": sum(exhaustive_coverages) / total,
            "potential_gain": (sum(exhaustive_coverages) - sum(baseline_coverages)) / total,
            "recoverable_count": recoverable,
        },
        "latency": {
            "filtered_query_total_ms": {
                "p50": _percentile(latencies, 0.50),
                "p95": p95,
            },
            "filtered_query_count": filtered_queries,
            "embedding_calls": total,
            "additional_embedding_calls": 0,
        },
        "recommendation": {
            "candidate": "within-paper-semantic-rerank-v1" if gate else "none",
            "gate_passed": gate,
            "policy": {
                "minimum_recoverable_items": 1,
                "filtered_latency_p95_limit_ms": 250.0,
                "dev": "forbidden-before-candidate-train-pass",
                "holdout": "forbidden",
            },
        },
        "privacy": {
            "item_records_emitted": False,
            "item_identifiers_emitted": False,
            "content_fields_emitted": False,
        },
    }


def _query_ids(
    collection: Any,
    embedding: list[float],
    n_results: int,
    *,
    paper_id: int | None = None,
) -> list[str]:
    kwargs: dict[str, Any] = {
        "query_embeddings": [embedding],
        "n_results": n_results,
        "include": ["metadatas", "distances"],
    }
    if paper_id is not None:
        kwargs["where"] = {"paper_id": paper_id}
    result = collection.query(**kwargs)
    ids = result.get("ids", [[]])[0]
    metas = result.get("metadatas", [[]])[0]
    canonical = _canonical_chunk_ids(ids, "semantic query ids")
    if len(canonical) != len(metas):
        raise ValueError("semantic query metadata 数量不一致")
    for chunk_id, metadata in zip(canonical, metas):
        actual = metadata.get("paper_id") if isinstance(metadata, dict) else None
        if actual != _paper_id(chunk_id):
            raise ValueError("semantic query ID 与 metadata 范围不一致")
        if paper_id is not None and actual != paper_id:
            raise ValueError("论文过滤语义查询范围越界")
    return canonical


def collect_within_paper_semantic_records(
    db: Any,
    items: list[dict[str, Any]],
    span_qrels: dict[str, list[dict[str, Any]]],
    store: Any,
    paper_chunk_counts: dict[int, int],
) -> list[dict[str, Any]]:
    """复用单一查询向量，采集生产已选论文的全部语义排名。"""
    from app.services.retrieval_pipeline import keyword_chunk_search, rrf_fuse_chunks

    records: list[dict[str, Any]] = []
    for entry in items:
        try:
            embedding = store.embedding_service.embed_query(entry["question"])
            semantic_ids = _query_ids(store.collection, embedding, 20)[:10]
        except Exception as exc:
            raise WithinPaperSemanticCollectionError("semantic-search") from exc
        semantic_items = [
            {"chunk_id": value, "paper_id": _paper_id(value)} for value in semantic_ids
        ]
        try:
            lexical_items = keyword_chunk_search(
                db,
                entry["question"],
                10,
                lexical_profile="bm25-bilingual",
                filters={},
            )
            lexical_ids = _canonical_chunk_ids(
                [item.get("chunk_id") for item in lexical_items],
                "lexical ids",
                max_length=10,
            )
            for item, chunk_id in zip(lexical_items, lexical_ids):
                if item.get("paper_id") != _paper_id(chunk_id):
                    raise ValueError("lexical ID 与 paper 范围不一致")
        except Exception as exc:
            raise WithinPaperSemanticCollectionError("lexical-search") from exc
        baseline_items = rrf_fuse_chunks(semantic_items, lexical_items, 5)
        try:
            baseline_ids = _canonical_chunk_ids(
                [item.get("chunk_id") for item in baseline_items],
                "baseline ids",
                expected_length=5,
            )
        except ValueError as exc:
            raise WithinPaperSemanticCollectionError("baseline-contract") from exc
        selected_papers = list(dict.fromkeys(_paper_id(value) for value in baseline_ids))
        routes: list[list[str]] = []
        started = time.perf_counter()
        try:
            for selected_paper in selected_papers:
                count = paper_chunk_counts.get(selected_paper)
                if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
                    raise ValueError("已选论文缺少有效 chunk 计数")
                route = _query_ids(
                    store.collection,
                    embedding,
                    count,
                    paper_id=selected_paper,
                )
                if len(route) != count:
                    raise ValueError("论文过滤语义结果未覆盖全部 DB chunk")
                routes.append(route)
        except Exception as exc:
            raise WithinPaperSemanticCollectionError("filtered-contract") from exc
        records.append({
            "question_type": entry["question_type"],
            "evidence_groups": span_qrels[entry["qa_id"]],
            "baseline_ids": baseline_ids,
            "within_paper_routes": routes,
            "embedding_call_count": 1,
            "filtered_query_count": len(routes),
            "filtered_latency_ms": (time.perf_counter() - started) * 1000,
        })
    return records


def require_offline_environment(environ: dict[str, str] | None = None) -> None:
    values = environ if environ is not None else os.environ
    if values.get("HF_HUB_OFFLINE") != "1" or values.get("TRANSFORMERS_OFFLINE") != "1":
        raise ValueError("必须设置 HF_HUB_OFFLINE=1 和 TRANSFORMERS_OFFLINE=1")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="生成 v2 train 论文内语义深度脱敏聚合")
    parser.add_argument("--dataset", required=True, help="私有 Benchmark v2 JSONL")
    parser.add_argument("--database", required=True, help="只读 SQLite 路径")
    parser.add_argument("--corpus-root", required=True, help="只读语料根目录")
    parser.add_argument("--vector-dir", required=True, help="冻结 Chroma 源目录")
    parser.add_argument("--output", required=True, help="私有聚合输出路径")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    engine = db = store = None
    stage = "startup-contract"
    try:
        require_offline_environment()
        from sqlalchemy import func, select

        from app.models import Chunk
        from app.services.data_integrity import open_readonly_sqlalchemy_database
        from eval.dataset import load_dataset, validate_dataset
        from eval.route_depth_diagnostics import _tree_sha256, _validate_runtime_path
        from eval.run import (
            _audit_vector_snapshot,
            _build_benchmark_metadata,
            _git_sha,
            _git_tracked_clean,
            _open_eval_vector_store,
            _qrels_sha256,
            _resolve_span_qrels_or_raise,
            _select_split,
        )

        if _git_tracked_clean() is not True:
            raise ValueError("论文内语义诊断要求 tracked Git clean")
        git_sha = _git_sha()
        if not isinstance(git_sha, str) or _GIT_SHA_RE.fullmatch(git_sha) is None:
            raise ValueError("无法读取有效 Git SHA")
        stage = "path-contract"
        dataset_path = validate_cli_path(Path(args.dataset), private_root=PRIVATE_ROOT, must_exist=True)
        output_path = validate_cli_path(Path(args.output), private_root=PRIVATE_ROOT, must_exist=False)
        database_path = _validate_runtime_path(Path(args.database), directory=False, label="database")
        corpus_root = _validate_runtime_path(Path(args.corpus_root), directory=True, label="corpus-root")
        vector_source = _validate_runtime_path(Path(args.vector_dir), directory=True, label="vector-dir")
        source_sha_before = _tree_sha256(vector_source)

        stage = "dataset-contract"
        all_items = load_dataset(dataset_path)
        items = _select_split(all_items, "train")
        validate_dataset(items)
        stage = "corpus-binding"
        engine, session_factory = open_readonly_sqlalchemy_database(database_path)
        db = session_factory()
        benchmark = _build_benchmark_metadata(db, dataset_path, runtime_root=corpus_root)
        _, span_qrels, page_manifest = _resolve_span_qrels_or_raise(db, items, runtime_root=corpus_root)
        benchmark.update({
            "qrels_sha256": _qrels_sha256(items),
            "page_text_manifest_sha256": page_manifest,
        })
        paper_chunk_counts = {
            int(paper_id): int(count)
            for paper_id, count in db.execute(
                select(Chunk.paper_id, func.count(Chunk.id)).group_by(Chunk.paper_id)
            ).all()
        }

        stage = "vector-audit"
        with tempfile.TemporaryDirectory(prefix="papermind-within-semantic-") as temp:
            isolated_vector = Path(temp) / "vector"
            shutil.copytree(vector_source, isolated_vector)
            store = _open_eval_vector_store(isolated_vector)
            if not store.available():
                raise ValueError("本地 Embedding 不可用")
            vector_audit = _audit_vector_snapshot(db, store)
            stage = "semantic-collection"
            records = collect_within_paper_semantic_records(
                db, items, span_qrels, store, paper_chunk_counts
            )
            store = None
            gc.collect()

        stage = "source-integrity"
        if _tree_sha256(vector_source) != source_sha_before:
            raise ValueError("向量冻结源在诊断过程中发生变化")
        binding = {
            "git_sha": git_sha,
            "git_tracked_clean": True,
            "dataset_sha256": benchmark["dataset_sha256"],
            "qrels_sha256": benchmark["qrels_sha256"],
            "corpus_manifest_sha256": benchmark["corpus_manifest_sha256"],
            "database_logical_manifest_sha256": benchmark["database_logical_manifest_sha256"],
            "page_text_manifest_sha256": benchmark["page_text_manifest_sha256"],
            "vector_manifest_sha256": vector_audit["vector_manifest_sha256"],
            "hnsw_config_sha256": vector_audit["hnsw_config_sha256"],
            "hnsw_binary_manifest_sha256": vector_audit["hnsw_binary_manifest_sha256"],
            "vector_source_tree_sha256": source_sha_before,
            "split": "train",
            "evidence_resolver": "page-span-v2",
            "lexical_profile": "bm25-bilingual",
            "semantic_rerank": False,
            "top_k": 5,
            "production_route_limit": 10,
            "filtered_latency_p95_limit_ms": 250.0,
        }
        stage = "aggregate-contract"
        result = analyze_within_paper_semantic(records, binding)
        stage = "report-write"
        write_report_exclusive(output_path, result)
    except (KeyError, OSError, ValueError, json.JSONDecodeError) as exc:
        reason = getattr(exc, "reason", None)
        suffix = f" reason={reason}" if reason else ""
        print(f"[within-paper-semantic] FAIL stage={stage} type={type(exc).__name__}{suffix}")
        return 2
    finally:
        store = None
        if db is not None:
            db.close()
        if engine is not None:
            engine.dispose()
    print(json.dumps({
        "schema": result["schema"],
        "total_items": result["total_items"],
        "candidate": result["recommendation"]["candidate"],
        "observation_sha256": result["observation_sha256"],
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
