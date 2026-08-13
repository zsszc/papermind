"""私有真实语料 Benchmark 的只读盘点与稳定身份工具。"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any


_DOI_PREFIX_RE = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", re.IGNORECASE)


def sha256_file(path: Path) -> str:
    """流式计算文件 SHA-256，不把论文整体读入内存。"""
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def normalize_doi(value: str | None) -> str:
    """规范 DOI：移除 URL/doi 前缀、空白及常见尾随标点。"""
    normalized = _DOI_PREFIX_RE.sub("", (value or "").strip()).strip().lower()
    return normalized.rstrip(".,;:)]}")


def paper_uid(paper: Any, runtime_root: Path) -> str:
    """优先返回规范 DOI；无 DOI 时以源 PDF 内容哈希作为稳定 UID。"""
    doi = normalize_doi(getattr(paper, "doi", None))
    if doi:
        return f"doi:{doi}"
    source = Path(runtime_root) / paper.file_path
    if not source.is_file():
        raise ValueError(f"论文源文件不存在，无法构造稳定 UID: paper_id={paper.id}")
    return f"sha256:{sha256_file(source)}"


def audit_corpus(db: Any, runtime_root: Path) -> dict[str, Any]:
    """只读盘点 PDF/数据库/chunk，返回私有 manifest；绝不修改或删除文件。"""
    from app.models import Chunk, Paper

    runtime_root = Path(runtime_root)
    papers_dir = runtime_root / "papers"
    physical = sorted(
        (path for path in papers_dir.glob("*.pdf") if path.is_file()),
        key=lambda path: path.name,
    ) if papers_dir.is_dir() else []
    physical_hashes = [sha256_file(path) for path in physical]
    hash_counts = Counter(physical_hashes)

    papers = db.query(Paper).order_by(Paper.id).all()
    chunk_counts = Counter(
        paper_id for (paper_id,) in db.query(Chunk.paper_id).all()
    )
    missing: list[int] = []
    documents: list[dict[str, Any]] = []
    for paper in papers:
        source = runtime_root / paper.file_path
        if not source.is_file():
            missing.append(paper.id)
            source_hash = None
            uid = None
        else:
            source_hash = sha256_file(source)
            uid = f"doi:{normalize_doi(paper.doi)}" if normalize_doi(paper.doi) else f"sha256:{source_hash}"
        documents.append({
            "paper_uid": uid,
            "pdf_sha256": source_hash,
            "processed": paper.processed,
            "chunk_count": chunk_counts.get(paper.id, 0),
            "physical_copy_count": hash_counts.get(source_hash, 0),
        })

    manifest_payload = json.dumps(
        sorted(documents, key=lambda item: item["paper_uid"] or ""),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "manifest_sha256": hashlib.sha256(manifest_payload).hexdigest(),
        "physical_pdf_files": len(physical),
        "unique_pdf_contents": len(hash_counts),
        "duplicate_pdf_files": len(physical) - len(hash_counts),
        "database_papers": len(papers),
        "processed_done": sum(paper.processed == "done" for paper in papers),
        "chunks": db.query(Chunk).count(),
        "missing_source_files": missing,
        "documents": documents,
    }


def public_summary(manifest: dict[str, Any]) -> dict[str, Any]:
    """从私有 manifest 提取可提交的去标识化聚合摘要。"""
    return {
        "manifest_sha256": manifest["manifest_sha256"],
        "physical_pdf_files": manifest["physical_pdf_files"],
        "unique_pdf_contents": manifest["unique_pdf_contents"],
        "duplicate_pdf_files": manifest["duplicate_pdf_files"],
        "database_papers": manifest["database_papers"],
        "processed_done": manifest["processed_done"],
        "chunks": manifest["chunks"],
        "missing_source_file_count": len(manifest["missing_source_files"]),
    }
