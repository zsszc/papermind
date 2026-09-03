"""Benchmark v2 train 双路证据深度的去标识化诊断 Harness。"""

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
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from eval.metrics import evidence_any_hit_at_k, evidence_span_coverage_at_k
from eval.train_failure_diagnostics import (
    validate_cli_path,
    write_report_exclusive,
)


SCHEMA = "route-depth-diagnostics-v1"
PRIVATE_ROOT = Path(__file__).resolve().parent / "private"
_CHUNK_ID_RE = re.compile(r"p([1-9][0-9]*)_c(-1|[0-9]+)")
_GIT_SHA_RE = re.compile(r"[0-9a-f]{40}")
_SHA_RE = re.compile(r"[0-9a-f]{64}")
_TYPE_COUNTS = {"factoid": 8, "method_detail": 4, "summary": 1}
_CATEGORY_ORDER = (
    "baseline_full",
    "deep_route_recoverable",
    "correct_paper_only",
    "paper_absent",
)
_FAILURE_PRIORITY = (
    "deep_route_recoverable",
    "correct_paper_only",
    "paper_absent",
)
_CANDIDATES = {
    "deep_route_recoverable": "paper-preserving-deep-route-v1",
    "correct_paper_only": "within-paper-query-rerank-v1",
    "paper_absent": "query-document-expansion-v1",
}
_DEPTHS = (5, 10, 20)
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
    "diagnostic_route_limit",
})
_CONTRACT = {
    "version": SCHEMA,
    "split": "train",
    "item_count": 13,
    "question_type_counts": _TYPE_COUNTS,
    "routes": ["semantic", "bm25-bilingual"],
    "diagnostic_route_limit": 20,
    "production_route_limit": 10,
    "production_top_k": 5,
    "production_fusion": "legacy-rrf-k60-first-seen",
    "evidence_resolver": "page-span-v2",
    "semantic_rerank": False,
    "category_order": list(_CATEGORY_ORDER),
    "failure_priority": list(_FAILURE_PRIORITY),
    "candidate_mapping": _CANDIDATES,
}


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def route_depth_contract_metadata() -> dict[str, Any]:
    """返回冻结 route-depth 口径及稳定指纹。"""
    return {**_CONTRACT, "contract_sha256": _sha256(_CONTRACT)}


def _require_sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SHA_RE.fullmatch(value) is None:
        raise ValueError(f"缺少有效指纹: {field}")
    return value


def _canonical_chunk_ids(
    values: Any, field: str, *, expected_length: int | None = None
) -> list[str]:
    if not isinstance(values, list):
        raise ValueError(f"{field} 必须是列表")
    if expected_length is not None and len(values) != expected_length:
        raise ValueError(f"{field} 数量必须为 {expected_length}")
    if len(values) != len(set(values)):
        raise ValueError(f"{field} 含重复 chunk ID")
    for value in values:
        if not isinstance(value, str) or _CHUNK_ID_RE.fullmatch(value) is None:
            raise ValueError(f"{field} 含畸形 chunk ID")
    return values


def _paper_ids(values: list[str]) -> set[int]:
    return {
        int(_CHUNK_ID_RE.fullmatch(value).group(1))  # type: ignore[union-attr]
        for value in values
    }


def _evidence_ids(groups: Any) -> list[str]:
    if not isinstance(groups, list) or not groups:
        raise ValueError("evidence_groups 必须是非空列表")
    result: list[str] = []
    for group in groups:
        if not isinstance(group, dict):
            raise ValueError("evidence group 必须是对象")
        start = group.get("page_start")
        end = group.get("page_end")
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
            chunk_start = chunk.get("page_start")
            chunk_end = chunk.get("page_end")
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
    if not result:
        raise ValueError("evidence_groups 没有可解析 chunk")
    return list(dict.fromkeys(result))


def _legacy_baseline_ids(
    semantic_ids: list[str], lexical_ids: list[str]
) -> list[str]:
    from app.services.retrieval_pipeline import rrf_fuse_chunks

    semantic = [{"chunk_id": value} for value in semantic_ids[:10]]
    lexical = [{"chunk_id": value} for value in lexical_ids[:10]]
    return [
        str(item["chunk_id"])
        for item in rrf_fuse_chunks(semantic, lexical, 5)
    ]


def validate_route_depth_records(
    records: Any, binding: Any
) -> list[dict[str, Any]]:
    """验证完整 train 观察记录和冻结指纹，任何歧义均 fail closed。"""
    if not isinstance(binding, dict):
        raise ValueError("binding 必须是对象")
    unknown_binding = sorted(set(binding) - _BINDING_FIELDS)
    if unknown_binding:
        raise ValueError("binding 含未知字段，拒绝潜在隐私透传")
    if _GIT_SHA_RE.fullmatch(str(binding.get("git_sha", ""))) is None:
        raise ValueError("缺少有效 Git SHA")
    if binding.get("git_tracked_clean") is not True:
        raise ValueError("route-depth 必须在 tracked Git clean 状态运行")
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
        _require_sha(binding.get(field), field)
    if (
        binding["database_logical_manifest_sha256"]
        != binding["corpus_manifest_sha256"]
    ):
        raise ValueError("数据库与语料指纹不一致")
    expected = {
        "split": "train",
        "evidence_resolver": "page-span-v2",
        "lexical_profile": "bm25-bilingual",
        "top_k": 5,
        "production_route_limit": 10,
        "diagnostic_route_limit": 20,
        "semantic_rerank": False,
    }
    for field, value in expected.items():
        if binding.get(field) != value:
            raise ValueError(f"{field} 必须为 {value}")

    if not isinstance(records, list) or len(records) != 13:
        raise ValueError("必须提供完整 13 题 train 记录")
    type_counts: Counter[str] = Counter()
    validated: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("route-depth 记录必须是对象")
        qtype = record.get("question_type")
        if qtype not in _TYPE_COUNTS:
            raise ValueError("问题类型不属于冻结 train 枚举")
        type_counts[qtype] += 1
        semantic = _canonical_chunk_ids(
            record.get("semantic_ids"), "semantic_ids", expected_length=20
        )
        lexical = _canonical_chunk_ids(
            record.get("lexical_ids"), "lexical_ids", expected_length=20
        )
        baseline = _canonical_chunk_ids(
            record.get("baseline_ids"), "baseline_ids", expected_length=5
        )
        expected_baseline = _legacy_baseline_ids(semantic, lexical)
        if baseline != expected_baseline:
            raise ValueError("基线 top-5 与 production legacy RRF 不一致")
        _evidence_ids(record.get("evidence_groups"))
        validated.append(record)
    if dict(type_counts) != _TYPE_COUNTS:
        raise ValueError("完整 13 题 train 问题类型数量不一致")
    return validated


def _first_hit_bucket(route: list[str], relevant: set[str]) -> str:
    for rank, chunk_id in enumerate(route, start=1):
        if chunk_id in relevant:
            if rank <= 5:
                return "1-5"
            if rank <= 10:
                return "6-10"
            return "11-20"
    return "not_found"


def _route_summary(records: list[dict[str, Any]], field: str) -> dict[str, Any]:
    buckets: Counter[str] = Counter()
    any_hits: dict[int, list[float]] = defaultdict(list)
    coverages: dict[int, list[float]] = defaultdict(list)
    for record in records:
        route = record[field]
        groups = record["evidence_groups"]
        relevant = set(_evidence_ids(groups))
        buckets[_first_hit_bucket(route, relevant)] += 1
        for depth in _DEPTHS:
            any_hits[depth].append(evidence_any_hit_at_k(route, groups, depth))
            coverages[depth].append(
                evidence_span_coverage_at_k(route, groups, depth)
            )
    count = len(records)
    return {
        "first_hit_depth": {
            bucket: buckets.get(bucket, 0)
            for bucket in ("1-5", "6-10", "11-20", "not_found")
        },
        **{
            f"any_hit@{depth}": sum(any_hits[depth]) / count
            for depth in _DEPTHS
        },
        **{
            f"span_coverage@{depth}": sum(coverages[depth]) / count
            for depth in _DEPTHS
        },
    }


def _classify(record: dict[str, Any]) -> str:
    groups = record["evidence_groups"]
    baseline_coverage = evidence_span_coverage_at_k(
        record["baseline_ids"], groups, 5
    )
    if math.isclose(baseline_coverage, 1.0, abs_tol=_EPSILON):
        return "baseline_full"
    union = list(dict.fromkeys(
        record["semantic_ids"] + record["lexical_ids"]
    ))
    union_coverage = evidence_span_coverage_at_k(union, groups, len(union))
    if union_coverage > baseline_coverage + _EPSILON:
        return "deep_route_recoverable"
    relevant_papers = _paper_ids(_evidence_ids(groups))
    route_papers = _paper_ids(union)
    if relevant_papers & route_papers:
        return "correct_paper_only"
    return "paper_absent"


def analyze_route_depth(
    records: Any, binding: Any
) -> dict[str, Any]:
    """生成不含逐题身份的确定性 route-depth 聚合。"""
    validated = validate_route_depth_records(records, binding)
    counts: Counter[str] = Counter()
    by_type: dict[str, Counter[str]] = defaultdict(Counter)
    union_coverage: dict[str, list[float]] = defaultdict(list)
    for record in validated:
        category = _classify(record)
        counts[category] += 1
        qtype = record["question_type"]
        by_type[qtype][category] += 1
        union = list(dict.fromkeys(
            record["semantic_ids"] + record["lexical_ids"]
        ))
        union_coverage[qtype].append(evidence_span_coverage_at_k(
            union, record["evidence_groups"], len(union)
        ))

    dominant = max(
        _FAILURE_PRIORITY,
        key=lambda category: (
            counts.get(category, 0),
            -_FAILURE_PRIORITY.index(category),
        ),
    )
    support = counts.get(dominant, 0)
    total = len(validated)
    contract = route_depth_contract_metadata()
    return {
        "schema": SCHEMA,
        "observation_sha256": _sha256(validated),
        "binding": {
            **{key: binding[key] for key in sorted(_BINDING_FIELDS)},
            "route_depth_contract_sha256": contract["contract_sha256"],
        },
        "total_items": total,
        "categories": [
            {
                "category": category,
                "count": counts.get(category, 0),
                "share": counts.get(category, 0) / total,
            }
            for category in _CATEGORY_ORDER
        ],
        "routes": {
            "semantic": _route_summary(validated, "semantic_ids"),
            "bm25_bilingual": _route_summary(validated, "lexical_ids"),
        },
        "by_question_type": [
            {
                "question_type": qtype,
                "n": _TYPE_COUNTS[qtype],
                "categories": {
                    category: by_type[qtype].get(category, 0)
                    for category in _CATEGORY_ORDER
                },
                "union_span_coverage@20": (
                    sum(union_coverage[qtype]) / len(union_coverage[qtype])
                ),
            }
            for qtype in sorted(_TYPE_COUNTS)
        ],
        "recommendation": {
            "dominant_failure": dominant if support else None,
            "candidate": _CANDIDATES[dominant] if support else "none",
            "support_count": support,
            "support_share": support / total,
            "policy": {
                "single_candidate": True,
                "next_step": "new-sdd-before-implementation",
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


def _tree_sha256(root: Path) -> str:
    manifest: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink():
            raise ValueError("向量冻结源禁止 symlink")
        if not path.is_file():
            continue
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        manifest.append({
            "relative": path.relative_to(root).as_posix(),
            "size": path.stat().st_size,
            "sha256": digest.hexdigest(),
        })
    if not manifest:
        raise ValueError("向量冻结源为空")
    return _sha256(manifest)


def require_offline_environment(environ: dict[str, str] | None = None) -> None:
    """真实路由采集必须显式禁用 HuggingFace/Transformers 联网。"""
    values = environ if environ is not None else os.environ
    if (
        values.get("HF_HUB_OFFLINE") != "1"
        or values.get("TRANSFORMERS_OFFLINE") != "1"
    ):
        raise ValueError("必须设置 HF_HUB_OFFLINE=1 和 TRANSFORMERS_OFFLINE=1")


def _validate_runtime_path(path: Path, *, directory: bool, label: str) -> Path:
    source = Path(path)
    if source.is_symlink():
        raise ValueError(f"{label} 禁止 symlink")
    resolved = source.resolve(strict=True)
    if directory and not resolved.is_dir():
        raise ValueError(f"{label} 必须是目录")
    if not directory and not resolved.is_file():
        raise ValueError(f"{label} 必须是文件")
    return resolved


def _route_items_to_ids(items: list[dict[str, Any]], field: str) -> list[str]:
    ids = [item.get("chunk_id") for item in items]
    canonical = _canonical_chunk_ids(ids, field, expected_length=20)
    for item, chunk_id in zip(items, canonical):
        paper_id = int(_CHUNK_ID_RE.fullmatch(chunk_id).group(1))  # type: ignore[union-attr]
        if item.get("paper_id") != paper_id:
            raise ValueError(f"{field} chunk ID 与 paper_id 不一致")
    return canonical


def collect_route_depth_records(
    db: Any,
    items: list[dict[str, Any]],
    span_qrels: dict[str, list[dict[str, Any]]],
    store: Any,
) -> list[dict[str, Any]]:
    """使用真实 semantic/BM25 路由采集私有内存记录，不输出正文。"""
    from app.services.retrieval_pipeline import (
        keyword_chunk_search,
        rrf_fuse_chunks,
    )

    records: list[dict[str, Any]] = []
    for entry in items:
        semantic_items = store.search(
            query=entry["question"],
            top_k=20,
            filters={},
            rerank=False,
        )
        lexical_items = keyword_chunk_search(
            db,
            entry["question"],
            20,
            lexical_profile="bm25-bilingual",
            filters={},
        )
        semantic_ids = _route_items_to_ids(semantic_items, "semantic_ids")
        lexical_ids = _route_items_to_ids(lexical_items, "lexical_ids")
        baseline_items = rrf_fuse_chunks(
            semantic_items[:10], lexical_items[:10], 5
        )
        baseline_ids = [str(item["chunk_id"]) for item in baseline_items]
        records.append({
            "question_type": entry["question_type"],
            "evidence_groups": span_qrels[entry["qa_id"]],
            "semantic_ids": semantic_ids,
            "lexical_ids": lexical_ids,
            "baseline_ids": baseline_ids,
        })
    return records


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="生成 Benchmark v2 完整 train 的去标识化 route-depth 聚合"
    )
    parser.add_argument("--dataset", required=True, help="私有 Benchmark v2 JSONL")
    parser.add_argument("--database", required=True, help="只读 SQLite 路径")
    parser.add_argument("--corpus-root", required=True, help="只读语料根目录")
    parser.add_argument("--vector-dir", required=True, help="冻结 Chroma 源目录")
    parser.add_argument("--output", required=True, help="私有聚合输出路径")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    engine = None
    db = None
    store = None
    try:
        require_offline_environment()
        from app.services.data_integrity import open_readonly_sqlalchemy_database
        from eval.dataset import load_dataset, validate_dataset
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
            raise ValueError("route-depth 真实运行要求 tracked Git clean")
        git_sha = _git_sha()
        if not isinstance(git_sha, str) or _GIT_SHA_RE.fullmatch(git_sha) is None:
            raise ValueError("无法读取有效 Git SHA")
        dataset_path = validate_cli_path(
            Path(args.dataset), private_root=PRIVATE_ROOT, must_exist=True
        )
        output_path = validate_cli_path(
            Path(args.output), private_root=PRIVATE_ROOT, must_exist=False
        )
        database_path = _validate_runtime_path(
            Path(args.database), directory=False, label="database"
        )
        corpus_root = _validate_runtime_path(
            Path(args.corpus_root), directory=True, label="corpus-root"
        )
        vector_source = _validate_runtime_path(
            Path(args.vector_dir), directory=True, label="vector-dir"
        )
        source_sha_before = _tree_sha256(vector_source)

        all_items = load_dataset(dataset_path)
        items = _select_split(all_items, "train")
        validate_dataset(items)
        engine, session_factory = open_readonly_sqlalchemy_database(database_path)
        db = session_factory()
        benchmark = _build_benchmark_metadata(
            db, dataset_path, runtime_root=corpus_root
        )
        _, span_qrels, page_manifest = _resolve_span_qrels_or_raise(
            db, items, runtime_root=corpus_root
        )
        benchmark.update({
            "qrels_sha256": _qrels_sha256(items),
            "page_text_manifest_sha256": page_manifest,
        })

        with tempfile.TemporaryDirectory(prefix="papermind-route-depth-") as temp:
            isolated_vector = Path(temp) / "vector"
            shutil.copytree(vector_source, isolated_vector)
            store = _open_eval_vector_store(isolated_vector)
            if not store.available():
                raise ValueError("本地 Embedding 不可用")
            vector_audit = _audit_vector_snapshot(db, store)
            records = collect_route_depth_records(db, items, span_qrels, store)
            store = None
            gc.collect()

        source_sha_after = _tree_sha256(vector_source)
        if source_sha_before != source_sha_after:
            raise ValueError("向量冻结源在诊断过程中发生变化")
        binding = {
            "git_sha": git_sha,
            "git_tracked_clean": True,
            "dataset_sha256": benchmark["dataset_sha256"],
            "qrels_sha256": benchmark["qrels_sha256"],
            "corpus_manifest_sha256": benchmark["corpus_manifest_sha256"],
            "database_logical_manifest_sha256": benchmark[
                "database_logical_manifest_sha256"
            ],
            "page_text_manifest_sha256": benchmark[
                "page_text_manifest_sha256"
            ],
            "vector_manifest_sha256": vector_audit["vector_manifest_sha256"],
            "hnsw_config_sha256": vector_audit["hnsw_config_sha256"],
            "hnsw_binary_manifest_sha256": vector_audit[
                "hnsw_binary_manifest_sha256"
            ],
            "vector_source_tree_sha256": source_sha_before,
            "split": "train",
            "evidence_resolver": "page-span-v2",
            "lexical_profile": "bm25-bilingual",
            "semantic_rerank": False,
            "top_k": 5,
            "production_route_limit": 10,
            "diagnostic_route_limit": 20,
        }
        result = analyze_route_depth(records, binding)
        write_report_exclusive(output_path, result)
    except (KeyError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"[route-depth] FAIL: {type(exc).__name__}")
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
