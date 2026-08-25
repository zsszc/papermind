"""真实语料 Benchmark v2 就绪度的只读共享核心。

本模块位于 ``app`` 包内，确保 Electron 精简运行时可导入；评测 CLI 也从这里
复用相同的 PDF/UID 覆盖语义。任何公开调用只应返回严格白名单聚合结果。
"""

from __future__ import annotations

from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any

from app.core.config import config


MINIMUM_NEW_PAPERS = 12
_SHA_RE = re.compile(r"[0-9a-f]{64}")
_DOI_PREFIX_RE = re.compile(
    r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", re.IGNORECASE
)
PUBLIC_READINESS_FIELDS = frozenset({
    "status",
    "ready",
    "minimum_new_papers",
    "missing_new_papers",
    "physical_pdf_files",
    "unique_pdf_contents",
    "duplicate_pdf_files",
    "covered_unique_contents",
    "eligible_imported_papers",
    "unimported_unique_contents",
    "error_code",
})


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
        current.st_dev,
        current.st_ino,
        current.st_size,
        current.st_mtime_ns,
    )
    if before_identity != opened_identity or after_identity != current_identity:
        raise ValueError("PDF 在计算 SHA-256 期间发生变化")
    return digest.hexdigest()


def _pdf_inventory(papers_dir: Path) -> list[tuple[Path, tuple[int, int, int, int]]]:
    rows: list[tuple[Path, tuple[int, int, int, int]]] = []
    for path in papers_dir.iterdir():
        if path.suffix.lower() != ".pdf":
            continue
        if path.is_symlink():
            raise ValueError("papers 目录不得包含 PDF 软链接")
        if not path.is_file():
            continue
        stat = path.stat()
        rows.append(
            (path, (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns))
        )
    return sorted(rows, key=lambda row: row[0].name)


def _corpus_manifest_sha256(documents: list[dict[str, Any]]) -> str:
    payload = json.dumps(
        sorted(documents, key=lambda item: item.get("paper_uid") or ""),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def validate_corpus_manifest(corpus_manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """校验语料 manifest 的内容指纹，拒绝合法格式但被篡改的制品。"""
    documents = corpus_manifest.get("documents")
    if not isinstance(documents, list):
        raise ValueError("语料 manifest 缺少 documents")
    expected = _valid_sha(corpus_manifest.get("manifest_sha256"), "corpus manifest")
    if _corpus_manifest_sha256(documents) != expected:
        raise ValueError("语料 manifest 内容指纹不匹配")
    return documents


def audit_v2_coverage(
    papers_dir: Path,
    corpus_manifest: dict[str, Any],
    v1_items: list[dict[str, Any]],
) -> dict[str, Any]:
    """以 paper UID + PDF SHA 并集排除 v1，不输出文件名或原文。"""
    requested_dir = Path(papers_dir)
    if requested_dir.is_symlink():
        raise ValueError("papers 根目录不得为软链接")
    papers_dir = requested_dir.resolve()
    if not papers_dir.is_dir():
        raise ValueError("papers 目录不存在")

    documents = validate_corpus_manifest(corpus_manifest)
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

    inventory_before = _pdf_inventory(papers_dir)
    physical: list[tuple[Path, str]] = [
        (path, _stable_sha256_file(path)) for path, _identity in inventory_before
    ]
    inventory_after = _pdf_inventory(papers_dir)
    before_signature = [(path.name, identity) for path, identity in inventory_before]
    after_signature = [(path.name, identity) for path, identity in inventory_after]
    if before_signature != after_signature:
        raise ValueError("papers 目录在覆盖审计期间发生变化")

    physical_counts = Counter(digest for _path, digest in physical)
    manifest_hashes = set(sha_to_uid)
    missing_manifest_contents = manifest_hashes - set(physical_counts)
    if missing_manifest_contents:
        raise ValueError("语料 manifest 包含当前 papers 中不存在的 PDF 内容")

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
            document
            for document in normalized_documents
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
    """返回 CLI 可用的去标识化摘要；API 会使用更窄的独立白名单。"""
    fields = (
        "coverage_manifest_sha256",
        "corpus_manifest_sha256",
        "physical_pdf_files",
        "unique_pdf_contents",
        "duplicate_pdf_files",
        "covered_unique_contents",
        "eligible_imported_papers",
        "unimported_unique_contents",
    )
    return {field: audit[field] for field in fields}


def evaluate_v2_readiness(
    audit: dict[str, Any], *, min_new_papers: int = MINIMUM_NEW_PAPERS
) -> dict[str, Any]:
    """只在有足够未覆盖且已导入的真实论文时允许进入 QA 阶段。"""
    if audit.get("coverage_schema") != "private-benchmark-v2-coverage-v1":
        raise ValueError("不是 Benchmark v2 覆盖制品")
    _valid_sha(audit.get("coverage_manifest_sha256"), "coverage manifest")
    actual = audit.get("eligible_imported_papers")
    if not isinstance(actual, int) or isinstance(actual, bool):
        raise ValueError("覆盖制品缺少候选论文数")
    check = {
        "actual": actual,
        "threshold": min_new_papers,
        "operator": ">=",
        "passed": actual >= min_new_papers,
    }
    return {
        "gate_version": "private-benchmark-v2-readiness-v1",
        "passed": check["passed"],
        "checks": {"new_unique_imported_papers": check},
        "coverage_manifest_sha256": audit["coverage_manifest_sha256"],
    }


def calculate_benchmark_v2_readiness(
    papers_dir: Path,
    corpus_manifest: dict[str, Any],
    v1_paper_uids: list[str],
    *,
    minimum_new_papers: int = MINIMUM_NEW_PAPERS,
) -> dict[str, Any]:
    """计算只含聚合计数的应用层 readiness DTO。"""
    if minimum_new_papers < MINIMUM_NEW_PAPERS:
        raise ValueError("Benchmark v2 至少需要 12 篇未覆盖唯一论文")
    if not isinstance(v1_paper_uids, list) or any(
        not isinstance(uid, str) or not uid for uid in v1_paper_uids
    ):
        raise ValueError("v1 身份快照无效")
    v1_items = [
        {"relevant_evidence": [{"paper_uid": uid}]} for uid in v1_paper_uids
    ]
    audit = audit_v2_coverage(papers_dir, corpus_manifest, v1_items)
    gate = evaluate_v2_readiness(audit, min_new_papers=minimum_new_papers)
    eligible = audit["eligible_imported_papers"]
    result = {
        "status": "PASS" if gate["passed"] else "WAIT",
        "ready": bool(gate["passed"]),
        "minimum_new_papers": minimum_new_papers,
        "missing_new_papers": max(0, minimum_new_papers - eligible),
        "physical_pdf_files": audit["physical_pdf_files"],
        "unique_pdf_contents": audit["unique_pdf_contents"],
        "duplicate_pdf_files": audit["duplicate_pdf_files"],
        "covered_unique_contents": audit["covered_unique_contents"],
        "eligible_imported_papers": eligible,
        "unimported_unique_contents": audit["unimported_unique_contents"],
        "error_code": None,
    }
    if set(result) != PUBLIC_READINESS_FIELDS:
        raise RuntimeError("readiness 公开字段白名单失配")
    return result


def unavailable_benchmark_v2_readiness() -> dict[str, Any]:
    """返回未知计数为 null 的失败关闭状态。"""
    result = {
        "status": "UNAVAILABLE",
        "ready": False,
        "minimum_new_papers": MINIMUM_NEW_PAPERS,
        "missing_new_papers": None,
        "physical_pdf_files": None,
        "unique_pdf_contents": None,
        "duplicate_pdf_files": None,
        "covered_unique_contents": None,
        "eligible_imported_papers": None,
        "unimported_unique_contents": None,
        "error_code": "benchmark_data_unavailable",
    }
    return result


def normalize_doi(value: str | None) -> str:
    normalized = _DOI_PREFIX_RE.sub("", (value or "").strip()).strip().lower()
    return normalized.rstrip(".,;:)]}")


def build_live_corpus_manifest(db: Any, runtime_root: Path) -> dict[str, Any]:
    """从当前数据库与源 PDF 构建只读 manifest，不复用旧语料快照。"""
    from app.models import Paper

    runtime_root = Path(runtime_root).resolve()
    papers_dir = runtime_root / "papers"
    if papers_dir.is_symlink() or not papers_dir.is_dir():
        raise ValueError("papers 目录不可用")
    canonical_papers = papers_dir.resolve()
    documents: list[dict[str, str]] = []
    for paper in db.query(Paper).order_by(Paper.id).all():
        relative = Path(paper.file_path or "")
        if relative.is_absolute():
            raise ValueError("论文源路径必须为相对路径")
        source = runtime_root / relative
        if source.is_symlink() or not source.is_file():
            raise ValueError("论文源文件不可用")
        canonical_source = source.resolve()
        if not canonical_source.is_relative_to(canonical_papers):
            raise ValueError("论文源路径越界")
        pdf_sha = _stable_sha256_file(canonical_source)
        doi = normalize_doi(paper.doi)
        uid = f"doi:{doi}" if doi else f"sha256:{pdf_sha}"
        documents.append({"paper_uid": uid, "pdf_sha256": pdf_sha})
    return {
        "manifest_sha256": _corpus_manifest_sha256(documents),
        "documents": documents,
    }


def _read_stable_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("v1 身份制品不可用")
    before = path.stat()
    raw = path.read_bytes()
    after = path.stat()
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if identity_before != identity_after:
        raise ValueError("v1 身份制品读取期间发生变化")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("v1 身份制品格式无效")
    return value


def load_v1_identity_snapshot(private_dir: Path) -> list[str]:
    """只从既有 coverage 制品提取 v1 UID，不打开任何 QA split。"""
    private_dir = Path(private_dir)
    if private_dir.is_symlink() or not private_dir.is_dir():
        raise ValueError("v1 私有身份目录不可用")
    candidates = sorted(private_dir.glob("*v2-coverage-*.json"))
    if not candidates:
        raise ValueError("缺少 v1 身份 coverage 制品")
    snapshots: list[list[str]] = []
    for path in candidates:
        artifact = _read_stable_json(path)
        if artifact.get("coverage_schema") != "private-benchmark-v2-coverage-v1":
            continue
        stored_sha = _valid_sha(
            artifact.get("coverage_manifest_sha256"), "coverage manifest"
        )
        unhashed = dict(artifact)
        unhashed.pop("coverage_manifest_sha256", None)
        if _sha_json(unhashed) != stored_sha:
            raise ValueError("coverage 制品内容指纹不匹配")
        uids = artifact.get("v1_paper_uids")
        if not isinstance(uids, list) or any(
            not isinstance(uid, str) or not uid for uid in uids
        ):
            raise ValueError("coverage 制品缺少 v1 身份")
        if _sha_json(uids) != artifact.get("v1_paper_uids_sha256"):
            raise ValueError("v1 身份指纹不匹配")
        snapshots.append(sorted(uids))
    if not snapshots:
        raise ValueError("没有可用的 v1 身份 coverage 制品")
    first = snapshots[0]
    if any(snapshot != first for snapshot in snapshots[1:]):
        raise ValueError("多个 v1 身份制品不一致")
    return first


def _default_private_dir(runtime_root: Path) -> Path:
    runtime_private = Path(runtime_root) / "eval" / "private"
    source_private = Path(__file__).resolve().parents[2] / "eval" / "private"
    return runtime_private if runtime_private.is_dir() else source_private


def get_benchmark_v2_readiness(db: Any) -> dict[str, Any]:
    """应用只读入口；缺失开发私有身份制品时由路由失败关闭。"""
    runtime_root = config.runtime_root
    private_dir = _default_private_dir(runtime_root)
    v1_uids = load_v1_identity_snapshot(private_dir)
    manifest = build_live_corpus_manifest(db, runtime_root)
    return calculate_benchmark_v2_readiness(
        runtime_root / "papers",
        manifest,
        v1_uids,
        minimum_new_papers=MINIMUM_NEW_PAPERS,
    )
