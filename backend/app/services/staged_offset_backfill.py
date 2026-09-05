"""在隔离 SQLite 副本中为旧粗分块回填页内坐标，不改变 chunk 身份。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from sqlalchemy.orm import sessionmaker

from app.database import apply_schema_migrations
from app.models import Chunk, Paper
from app.services.data_integrity import audit_database, repair_database_copy
from app.services.pdf_parser import PDFParser
from app.services.staged_chunk_rebuild import (
    _candidate_engine,
    _cleanup_sqlite_files,
    _resolve_source_pdf,
    _source_manifest,
)


def _chunk_identity_manifest(database: Path) -> str:
    """计算不含新 offset 的 chunk 身份摘要，确保候选只补坐标。"""
    uri = f"{Path(database).resolve().as_uri()}?mode=ro"
    digest = hashlib.sha256()
    with sqlite3.connect(uri, uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        rows = conn.execute(
            "SELECT id, paper_id, chunk_index, page_number, content, "
            "section_title, chunk_type, token_count FROM chunks "
            "ORDER BY paper_id, chunk_index, id"
        )
        for row in rows:
            digest.update(json.dumps(row, ensure_ascii=False).encode("utf-8"))
            digest.update(b"\n")
    return digest.hexdigest()


def _all_occurrences(text: str, target: str) -> list[int]:
    starts: list[int] = []
    cursor = 0
    while True:
        index = text.find(target, cursor)
        if index < 0:
            return starts
        starts.append(index)
        cursor = index + 1


def build_staged_offset_database(
    source: Path,
    candidate: Path,
    *,
    corpus_root: Path,
    parser: Any | None = None,
) -> dict[str, Any]:
    """复制旧库并唯一定位每个正文 chunk，失败时不发布候选。"""
    source = Path(source).resolve()
    candidate = Path(candidate).resolve()
    corpus_root = Path(corpus_root).resolve()
    if source == candidate:
        raise ValueError("候选数据库不得覆盖源数据库")
    if not source.is_file():
        raise FileNotFoundError(source)
    if candidate.exists():
        raise FileExistsError(f"候选数据库已存在: {candidate}")
    candidate.parent.mkdir(parents=True, exist_ok=True)

    source_before = _source_manifest(source)
    identity_before = _chunk_identity_manifest(source)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{candidate.name}.", suffix=".tmp", dir=candidate.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    temporary.unlink()
    engine = None
    try:
        repair = repair_database_copy(source, temporary, dry_run=False)
        engine = _candidate_engine(temporary)
        apply_schema_migrations(engine)
        Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        parser = parser or PDFParser()
        body_count = 0
        backfilled_body_count = 0
        preserved_body_count = 0
        page_count = 0
        with Session.begin() as db:
            papers = (
                db.query(Paper)
                .filter(Paper.processed == "done")
                .order_by(Paper.id)
                .all()
            )
            for paper in papers:
                source_pdf = _resolve_source_pdf(corpus_root, paper.file_path)
                pages = parser.extract_text(str(source_pdf))
                pages_by_number = {
                    page.get("page_number"): page.get("text") or ""
                    for page in pages
                }
                page_count += len(pages_by_number)
                rows = (
                    db.query(Chunk)
                    .filter(Chunk.paper_id == paper.id)
                    .order_by(Chunk.chunk_index, Chunk.id)
                    .all()
                )
                for row in rows:
                    if row.chunk_index < 0:
                        row.page_start = None
                        row.page_end = None
                        continue
                    if row.page_number not in pages_by_number:
                        raise ValueError(
                            f"paper_id={paper.id} chunk={row.chunk_index} 页码不存在"
                        )
                    content = row.content or ""
                    if not content:
                        raise ValueError("旧正文 chunk 为空")
                    page_text = pages_by_number[row.page_number]
                    if row.page_start is not None or row.page_end is not None:
                        if (
                            not isinstance(row.page_start, int)
                            or not isinstance(row.page_end, int)
                            or not 0 <= row.page_start < row.page_end <= len(page_text)
                        ):
                            raise ValueError("既有正文 chunk 坐标无效")
                        body_count += 1
                        preserved_body_count += 1
                        continue
                    starts = _all_occurrences(page_text, content)
                    if not starts:
                        raise ValueError(
                            f"paper_id={paper.id} chunk={row.chunk_index} 原页未命中"
                        )
                    if len(starts) > 1:
                        raise ValueError(
                            f"paper_id={paper.id} chunk={row.chunk_index} 原页多处命中"
                        )
                    row.page_start = starts[0]
                    row.page_end = starts[0] + len(content)
                    body_count += 1
                    backfilled_body_count += 1
            db.flush()

        engine.dispose()
        engine = None
        if _chunk_identity_manifest(temporary) != identity_before:
            raise RuntimeError("坐标回填改变了旧 chunk 身份")
        integrity = audit_database(temporary)
        if not integrity["quick_check_ok"]:
            raise sqlite3.DatabaseError("候选数据库 quick_check 失败")
        if integrity["foreign_key_violation_count"]:
            raise sqlite3.IntegrityError("候选数据库仍存在外键违规")
        if _source_manifest(source) != source_before:
            raise RuntimeError("回填期间源数据库发生变化，候选已中止")

        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, candidate)
        return {
            "candidate": str(candidate),
            "body_chunks": body_count,
            "backfilled_body_chunks": backfilled_body_count,
            "preserved_body_chunks": preserved_body_count,
            "parsed_pages": page_count,
            "chunk_identity_sha256": identity_before,
            "repaired_orphan_paper_tags": repair[
                "deleted_orphan_paper_tags"
            ],
            "source_unchanged": True,
            "integrity": integrity,
        }
    except Exception:
        if engine is not None:
            engine.dispose()
        _cleanup_sqlite_files(temporary)
        _cleanup_sqlite_files(candidate)
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.services.staged_offset_backfill",
        description="复制旧 SQLite 并隔离回填 chunk 页内坐标",
    )
    parser.add_argument("--source", required=True, help="只读源 SQLite")
    parser.add_argument("--candidate", required=True, help="必须不存在的候选 SQLite")
    parser.add_argument("--corpus-root", required=True, help="只读论文语料根目录")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = build_staged_offset_database(
        Path(args.source),
        Path(args.candidate),
        corpus_root=Path(args.corpus_root),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
