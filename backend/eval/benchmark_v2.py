"""Batch 22J 真实语料 Benchmark v2 覆盖、冻结与消费工具。"""

from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Callable

from eval.private_benchmark import validate_private_dataset


_SHA_RE = re.compile(r"[0-9a-f]{64}")
_SPLITS = {"train", "dev", "holdout"}


def _sha_json(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _valid_sha(value: Any, name: str) -> str:
    if not isinstance(value, str) or _SHA_RE.fullmatch(value) is None:
        raise ValueError(f"缺少有效 SHA-256: {name}")
    return value


def _evidence_uids(items: list[dict[str, Any]]) -> set[str]:
    return {
        evidence["paper_uid"]
        for item in items
        for evidence in item.get("relevant_evidence", [])
    }


def _stable_sha256_file(path: Path) -> str:
    """不跟随软链接且校验读取期间未变的流式 SHA-256。"""
    path = Path(path)
    if path.is_symlink():
        raise ValueError("papers 目录不得包含 PDF 软链接")
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
        after = os.fstat(stream.fileno())
    current = path.stat()
    before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    opened_identity = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
    after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    current_identity = (
        current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns
    )
    if before_identity != opened_identity or after_identity != current_identity:
        raise ValueError("PDF 在计算 SHA-256 期间发生变化")
    return digest.hexdigest()


def _exclusive_json(path: Path, value: dict[str, Any]) -> None:
    """以 O_EXCL + 0600 写入私有制品，存在即拒绝覆盖。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def audit_v2_coverage(
    papers_dir: Path,
    corpus_manifest: dict[str, Any],
    v1_items: list[dict[str, Any]],
) -> dict[str, Any]:
    """以 paper UID + PDF SHA 并集排除 v1，不输出文件名或原文。"""
    papers_dir = Path(papers_dir).resolve()
    if not papers_dir.is_dir():
        raise ValueError("papers 目录不存在")
    _valid_sha(corpus_manifest.get("manifest_sha256"), "corpus manifest")

    documents = corpus_manifest.get("documents")
    if not isinstance(documents, list):
        raise ValueError("语料 manifest 缺少 documents")
    uid_to_sha: dict[str, str] = {}
    sha_to_uid: dict[str, str] = {}
    normalized_documents: list[dict[str, str]] = []
    for document in documents:
        uid = document.get("paper_uid")
        pdf_sha = _valid_sha(document.get("pdf_sha256"), "document.pdf_sha256")
        if not isinstance(uid, str) or not uid:
            raise ValueError("语料 manifest 缺少 paper_uid")
        previous_sha = uid_to_sha.setdefault(uid, pdf_sha)
        if previous_sha != pdf_sha:
            raise ValueError("同一 paper_uid 对应多个 PDF SHA")
        previous_uid = sha_to_uid.setdefault(pdf_sha, uid)
        if previous_uid != uid:
            raise ValueError("同一 PDF 内容 SHA 对应多个 paper_uid")
        normalized_documents.append({"paper_uid": uid, "pdf_sha256": pdf_sha})

    v1_uids = sorted(_evidence_uids(v1_items))
    missing_v1 = sorted(set(v1_uids) - set(uid_to_sha))
    if missing_v1:
        raise ValueError(f"v1 paper_uid 在语料 manifest 未命中: {len(missing_v1)}")
    v1_pdf_hashes = sorted({uid_to_sha[uid] for uid in v1_uids})
    covered_hashes = set(v1_pdf_hashes)

    pdf_entries = [
        path for path in papers_dir.iterdir() if path.suffix.lower() == ".pdf"
    ]
    if any(path.is_symlink() for path in pdf_entries):
        raise ValueError("papers 目录不得包含 PDF 软链接")
    physical_paths = sorted(
        (path for path in pdf_entries if path.is_file()), key=lambda path: path.name
    )
    physical: list[tuple[Path, str]] = [
        (path, _stable_sha256_file(path)) for path in physical_paths
    ]
    physical_counts = Counter(digest for _path, digest in physical)
    manifest_hashes = set(sha_to_uid)
    files: list[dict[str, Any]] = []
    for path, digest in physical:
        copy_token = hashlib.sha256(
            f"{digest}\0{path.name}".encode("utf-8")
        ).hexdigest()
        files.append({
            "copy_token_sha256": copy_token,
            "pdf_sha256": digest,
            "duplicate_group_sha256": digest,
            "physical_copy_count": physical_counts[digest],
            "database_match_count": int(digest in manifest_hashes),
            "covered_by_v1": digest in covered_hashes,
        })

    v1_uid_set = set(v1_uids)
    eligible_documents = sorted(
        (
            document for document in normalized_documents
            if document["paper_uid"] not in v1_uid_set
            and document["pdf_sha256"] not in covered_hashes
        ),
        key=lambda row: row["paper_uid"],
    )
    audit: dict[str, Any] = {
        "coverage_schema": "private-benchmark-v2-coverage-v1",
        "corpus_manifest_sha256": corpus_manifest["manifest_sha256"],
        "physical_pdf_files": len(physical),
        "unique_pdf_contents": len(physical_counts),
        "duplicate_pdf_files": len(physical) - len(physical_counts),
        "covered_unique_contents": len(set(physical_counts) & covered_hashes),
        "eligible_imported_papers": len(eligible_documents),
        "unimported_unique_contents": len(set(physical_counts) - manifest_hashes),
        "v1_paper_uids": v1_uids,
        "v1_pdf_sha256s": v1_pdf_hashes,
        "v1_paper_uids_sha256": _sha_json(v1_uids),
        "v1_pdf_sha256s_sha256": _sha_json(v1_pdf_hashes),
        "eligible_documents": eligible_documents,
        "eligible_paper_uids_sha256": _sha_json(eligible_documents),
        "files": files,
    }
    audit["coverage_manifest_sha256"] = _sha_json(audit)
    return audit


def public_coverage_summary(audit: dict[str, Any]) -> dict[str, Any]:
    """返回可提交的去标识化覆盖摘要。"""
    fields = (
        "coverage_manifest_sha256", "corpus_manifest_sha256",
        "physical_pdf_files", "unique_pdf_contents", "duplicate_pdf_files",
        "covered_unique_contents", "eligible_imported_papers",
        "unimported_unique_contents",
    )
    return {field: audit[field] for field in fields}


def evaluate_v2_readiness(
    audit: dict[str, Any], *, min_new_papers: int = 12
) -> dict[str, Any]:
    """只在有足够未覆盖且已导入的真实论文时允许进入 QA 阶段。"""
    if audit.get("coverage_schema") != "private-benchmark-v2-coverage-v1":
        raise ValueError("不是 Benchmark v2 覆盖制品")
    _valid_sha(audit.get("coverage_manifest_sha256"), "coverage manifest")
    actual = audit.get("eligible_imported_papers")
    if not isinstance(actual, int) or isinstance(actual, bool):
        raise ValueError("覆盖制品缺少候选论文数")
    check = {
        "actual": actual, "threshold": min_new_papers,
        "operator": ">=", "passed": actual >= min_new_papers,
    }
    return {
        "gate_version": "private-benchmark-v2-readiness-v1",
        "passed": check["passed"],
        "checks": {"new_unique_imported_papers": check},
        "coverage_manifest_sha256": audit["coverage_manifest_sha256"],
    }


def validate_v2_dataset(
    items: list[dict[str, Any]],
    v1_items: list[dict[str, Any]],
    coverage: dict[str, Any],
    *,
    min_items: int = 48,
    min_papers: int = 12,
) -> dict[str, Any]:
    """校验 v2 审稿、论文 split 及与 v1/覆盖 Gate 隔离。"""
    summary = validate_private_dataset(
        items, min_items=min_items, min_papers=min_papers
    )
    v1_uids = _evidence_uids(v1_items)
    candidate_uids = _evidence_uids(items)
    overlap = candidate_uids & v1_uids
    if overlap:
        raise ValueError(f"v2 与 v1 paper_uid 重叠: {len(overlap)}")
    eligible = {
        document["paper_uid"]
        for document in coverage.get("eligible_documents", [])
    }
    outside = candidate_uids - eligible
    if outside:
        raise ValueError(f"v2 evidence 不属于未覆盖且已导入论文: {len(outside)}")
    return {
        **summary,
        "question_types": dict(sorted(Counter(
            item["question_type"] for item in items
        ).items())),
    }


def audit_v2_evidence(
    db: Any,
    items: list[dict[str, Any]],
    runtime_root: Path,
    *,
    resolver: Callable[..., list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """逐题审计 page-span-v2 唯一解析，不保存 evidence 原文。"""
    if resolver is None:
        from eval.dataset import resolve_relevant_spans_v2
        resolver = resolve_relevant_spans_v2
    resolved = 0
    for item in items:
        try:
            groups = resolver(db, item, runtime_root=Path(runtime_root))
        except ValueError as exc:
            raise ValueError(
                f"qa_id={item.get('qa_id')}: evidence 唯一解析失败: {exc}"
            ) from exc
        evidence_count = len(item.get("relevant_evidence", []))
        if item.get("has_answer"):
            if len(groups) != evidence_count or evidence_count == 0:
                raise ValueError(
                    f"qa_id={item.get('qa_id')}: evidence group 数量不一致"
                )
            if any(not group.get("chunks") for group in groups):
                raise ValueError(
                    f"qa_id={item.get('qa_id')}: evidence group 没有 chunk"
                )
            resolved += evidence_count
        elif groups:
            raise ValueError(f"qa_id={item.get('qa_id')}: 负例不得解析出 evidence")
    return {
        "items": len(items),
        "positive_evidence": sum(
            len(item.get("relevant_evidence", []))
            for item in items if item.get("has_answer")
        ),
        "uniquely_resolved_evidence": resolved,
    }


def freeze_paper_splits(
    coverage: dict[str, Any],
    assignments: list[dict[str, str]],
    output_path: Path,
    *,
    min_papers_per_split: int = 4,
) -> dict[str, Any]:
    """在编写 QA 之前排他冻结论文级 train/dev/holdout。"""
    eligible = {
        (row["paper_uid"], row["pdf_sha256"])
        for row in coverage.get("eligible_documents", [])
    }
    normalized: list[dict[str, str]] = []
    seen_uids: set[str] = set()
    seen_hashes: set[str] = set()
    counts: Counter[str] = Counter()
    for row in assignments:
        uid = row.get("paper_uid")
        pdf_sha = row.get("pdf_sha256")
        split = row.get("split")
        if split not in _SPLITS:
            raise ValueError("论文 split 必须为 train/dev/holdout")
        if (uid, pdf_sha) not in eligible:
            raise ValueError("论文 split 包含未注册的候选身份")
        if uid in seen_uids or pdf_sha in seen_hashes:
            raise ValueError("论文 split 包含重复 UID 或 PDF SHA")
        seen_uids.add(uid)
        seen_hashes.add(pdf_sha)
        counts[split] += 1
        normalized.append({
            "paper_uid": uid, "pdf_sha256": pdf_sha, "split": split,
        })
    frozen_identities = {
        (row["paper_uid"], row["pdf_sha256"]) for row in normalized
    }
    if frozen_identities != eligible:
        raise ValueError("论文 split 必须恰好覆盖所有 eligible 论文")
    if set(counts) != _SPLITS or any(
        counts[split] < min_papers_per_split for split in _SPLITS
    ):
        raise ValueError("每个 split 的论文数不足")
    normalized.sort(key=lambda row: (row["split"], row["paper_uid"]))
    artifact = {
        "split_schema": "private-benchmark-v2-paper-splits-v1",
        "coverage_manifest_sha256": coverage.get("coverage_manifest_sha256"),
        "paper_counts": dict(sorted(counts.items())),
        "assignments": normalized,
        "paper_split_sha256": _sha_json(normalized),
    }
    artifact["split_freeze_sha256"] = _sha_json(artifact)
    _exclusive_json(Path(output_path), artifact)
    return artifact


def build_v2_freeze_artifact(
    items: list[dict[str, Any]],
    coverage: dict[str, Any],
    *,
    dataset_bytes: bytes,
    corpus_manifest_sha256: str,
    database_logical_manifest_sha256: str,
    page_text_manifest_sha256: str,
    vector_manifest_sha256: str,
    holdout_gate_sha256: str | None = None,
) -> dict[str, Any]:
    """生成不含问题/答案/evidence 原文的完整冻结身份。"""
    from eval.run import _qrels_sha256

    for name, value in (
        ("coverage_manifest_sha256", coverage.get("coverage_manifest_sha256")),
        ("eligible_paper_uids_sha256", coverage.get("eligible_paper_uids_sha256")),
        ("corpus_manifest_sha256", corpus_manifest_sha256),
        ("database_logical_manifest_sha256", database_logical_manifest_sha256),
        ("page_text_manifest_sha256", page_text_manifest_sha256),
        ("vector_manifest_sha256", vector_manifest_sha256),
    ):
        _valid_sha(value, name)
    if holdout_gate_sha256 is not None:
        _valid_sha(holdout_gate_sha256, "holdout_gate_sha256")

    paper_splits: dict[str, str] = {}
    qa_splits: Counter[str] = Counter()
    paper_counts: defaultdict[str, set[str]] = defaultdict(set)
    qtypes: Counter[str] = Counter()
    for item in items:
        split = item.get("split")
        if split not in _SPLITS:
            raise ValueError("v2 item 缺少有效 split")
        qa_splits[split] += 1
        qtypes[item["question_type"]] += 1
        for evidence in item.get("relevant_evidence", []):
            uid = evidence["paper_uid"]
            previous = paper_splits.setdefault(uid, split)
            if previous != split:
                raise ValueError("同一论文跨 split")
            paper_counts[split].add(uid)
    paper_split_rows = sorted(paper_splits.items())
    artifact: dict[str, Any] = {
        "freeze_schema": "private-benchmark-v2-freeze-v1",
        "dataset_sha256": hashlib.sha256(dataset_bytes).hexdigest(),
        "qrels_sha256": _qrels_sha256(items),
        "coverage_manifest_sha256": coverage["coverage_manifest_sha256"],
        "eligible_paper_uids_sha256": coverage["eligible_paper_uids_sha256"],
        "corpus_manifest_sha256": corpus_manifest_sha256,
        "database_logical_manifest_sha256": database_logical_manifest_sha256,
        "page_text_manifest_sha256": page_text_manifest_sha256,
        "vector_manifest_sha256": vector_manifest_sha256,
        "paper_split_sha256": _sha_json(paper_split_rows),
        "splits": dict(sorted(qa_splits.items())),
        "paper_split_counts": {
            split: len(uids) for split, uids in sorted(paper_counts.items())
        },
        "question_type_counts": dict(sorted(qtypes.items())),
        "holdout_gate_sha256": holdout_gate_sha256,
    }
    artifact["freeze_sha256"] = _sha_json(artifact)
    return artifact


def consume_split_once(
    freeze: dict[str, Any],
    split: str,
    purpose: str,
    ledger_dir: Path,
    *,
    preregistered_gate_sha256: str | None = None,
) -> dict[str, Any]:
    """以 freeze SHA + split 固定路径排他 claim；调用方必须先 claim 再读数据。"""
    if freeze.get("freeze_schema") != "private-benchmark-v2-freeze-v1":
        raise ValueError("不是 Benchmark v2 freeze 制品")
    freeze_sha = _valid_sha(freeze.get("freeze_sha256"), "freeze_sha256")
    if split not in _SPLITS:
        raise ValueError("只允许 train/dev/holdout split")
    if not isinstance(purpose, str) or not purpose.strip():
        raise ValueError("消费 purpose 不得为空")
    if split == "holdout":
        expected_gate = freeze.get("holdout_gate_sha256")
        if (
            preregistered_gate_sha256 is None
            or preregistered_gate_sha256 != expected_gate
            or _SHA_RE.fullmatch(preregistered_gate_sha256) is None
        ):
            raise ValueError("holdout 只允许精确匹配预注册 Gate")
    ledger = {
        "ledger_schema": "private-benchmark-v2-split-claim-v1",
        "freeze_sha256": freeze_sha,
        "dataset_sha256": _valid_sha(
            freeze.get("dataset_sha256"), "dataset_sha256"
        ),
        "split": split,
        "purpose": purpose.strip(),
        "preregistered_gate_sha256": preregistered_gate_sha256,
        "claimed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    output = Path(ledger_dir) / f"{freeze_sha}-{split}.claim.json"
    _exclusive_json(output, ledger)
    return ledger


def build_parser():
    """构建只读覆盖盘点 CLI。"""
    import argparse

    parser = argparse.ArgumentParser(
        description="Batch 22J 真实语料 Benchmark v2 覆盖就绪 Gate"
    )
    parser.add_argument("--papers-dir", required=True)
    parser.add_argument("--corpus-manifest", required=True)
    parser.add_argument("--v1-dataset", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--gate-output", required=True)
    parser.add_argument("--min-new-papers", type=int, default=12)
    return parser


def _require_private_output(path: Path) -> Path:
    private_root = (Path(__file__).resolve().parent / "private").resolve()
    resolved = Path(path).resolve()
    if not resolved.is_relative_to(private_root) or resolved == private_root:
        raise ValueError("Benchmark v2 私有制品必须位于 eval/private 子目录")
    return resolved


def main(argv: list[str] | None = None) -> int:
    """产生私有覆盖制品与去标识化 readiness Gate。"""
    from eval.dataset import load_dataset

    args = build_parser().parse_args(argv)
    if args.min_new_papers < 12:
        raise ValueError("Benchmark v2 至少需要 12 篇未覆盖唯一论文")
    output = _require_private_output(Path(args.output))
    gate_output = _require_private_output(Path(args.gate_output))
    manifest = json.loads(Path(args.corpus_manifest).read_text(encoding="utf-8"))
    # 只从 v1 取证据 paper_uid 构建身份集；不输出问题/答案。
    v1_items = load_dataset(Path(args.v1_dataset))
    audit = audit_v2_coverage(Path(args.papers_dir), manifest, v1_items)
    gate = evaluate_v2_readiness(audit, min_new_papers=args.min_new_papers)
    _exclusive_json(output, audit)
    _exclusive_json(gate_output, gate)
    print(json.dumps({
        **public_coverage_summary(audit),
        "readiness_passed": gate["passed"],
        "minimum_new_papers": args.min_new_papers,
    }, ensure_ascii=False, sort_keys=True))
    return 0 if gate["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
