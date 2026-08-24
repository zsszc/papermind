"""在隔离 SQLite 副本中重建 chunks，绝不连接或修改生产 Chroma。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.models import Chunk, Paper
from app.services.data_integrity import audit_database, repair_database_copy
from app.services.embedding import TextChunker
from app.services.pdf_parser import PDFParser
from app.services.processor import PaperProcessor


def _cleanup_sqlite_files(path: Path) -> None:
    """删除尚未发布的候选数据库及其 SQLite 辅助文件。"""
    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)


def _source_manifest(database_path: Path) -> str:
    """计算源库检索相关表的逻辑摘要，用于发现重建期间并发写入。"""
    uri = f"{Path(database_path).resolve().as_uri()}?mode=ro"
    digest = hashlib.sha256()
    with sqlite3.connect(uri, uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        for query in (
            "SELECT id, file_path, processed FROM papers ORDER BY id",
            "SELECT paper_id, chunk_index, content, page_number "
            "FROM chunks ORDER BY paper_id, chunk_index, id",
            "SELECT paper_id, tag_id FROM paper_tags ORDER BY paper_id, tag_id",
        ):
            for row in conn.execute(query):
                digest.update(json.dumps(row, ensure_ascii=False).encode("utf-8"))
                digest.update(b"\n")
    return digest.hexdigest()


def _candidate_engine(database_path: Path):
    """创建只绑定候选文件的 engine，并强制外键检查。"""
    engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_conn, connection_record):
        del connection_record
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=DELETE")
        cursor.close()

    return engine


def _resolve_source_pdf(corpus_root: Path, file_path: str) -> Path:
    """解析论文路径并拒绝逃逸语料根目录。"""
    root = Path(corpus_root).resolve()
    source = (root / file_path).resolve()
    if not source.is_relative_to(root):
        raise ValueError("论文路径不得逃逸语料根目录")
    if not source.is_file():
        raise FileNotFoundError(f"论文源文件不存在: {file_path}")
    return source


def _replace_paper_chunks(
    db: Session,
    paper: Paper,
    pages: list[dict[str, Any]],
    chunker: TextChunker,
) -> tuple[int, int]:
    """只在当前候选事务内替换单篇论文 chunks。"""
    chunks_data = chunker.chunk_pages(pages)
    nonempty_pages = {
        page.get("page_number")
        for page in pages
        if (page.get("text") or "").strip()
    }
    covered_pages = {item.get("page_number") for item in chunks_data}
    if not nonempty_pages.issubset(covered_pages):
        raise ValueError(f"paper_id={paper.id} 的非空页面未被 chunk 覆盖")

    db.query(Chunk).filter(Chunk.paper_id == paper.id).delete(
        synchronize_session=False
    )
    for index, item in enumerate(chunks_data):
        db.add(Chunk(
            paper_id=paper.id,
            content=item["content"],
            page_number=item.get("page_number"),
            chunk_index=index,
            section_title=item.get("section_title"),
            chunk_type=item.get("chunk_type", "paragraph"),
            token_count=item.get("token_count"),
        ))

    abstract = PaperProcessor._build_abstract_chunk(paper, pages)
    abstract_count = 0
    if abstract is not None:
        db.add(Chunk(
            paper_id=paper.id,
            content=abstract["content"],
            page_number=abstract.get("page_number"),
            chunk_index=-1,
            section_title=None,
            chunk_type="abstract",
            token_count=None,
        ))
        abstract_count = 1
    return len(chunks_data), abstract_count


def _validate_candidate_chunks(db: Session, *, chunk_size: int) -> dict[str, int]:
    """验证正文坐标、内容、页码与硬上限，返回去标识化聚合。"""
    processed = db.query(Paper).filter(Paper.processed == "done").order_by(Paper.id)
    body_count = 0
    abstract_count = 0
    max_length = 0
    coordinates: set[tuple[int, int]] = set()
    for paper in processed:
        rows = (
            db.query(Chunk)
            .filter(Chunk.paper_id == paper.id)
            .order_by(Chunk.chunk_index, Chunk.id)
            .all()
        )
        body = [row for row in rows if row.chunk_index >= 0]
        if [row.chunk_index for row in body] != list(range(len(body))):
            raise ValueError(f"paper_id={paper.id} 的正文 chunk_index 不连续")
        for row in rows:
            coordinate = (row.paper_id, row.chunk_index)
            if coordinate in coordinates:
                raise ValueError("候选数据库存在重复 chunk 坐标")
            coordinates.add(coordinate)
            if not (row.content or "").strip():
                raise ValueError("候选数据库存在空 chunk")
            if row.chunk_index == -1:
                abstract_count += 1
                continue
            length = len(row.content)
            if length > chunk_size:
                raise ValueError("候选正文 chunk 超过字符硬上限")
            if row.page_number is None:
                raise ValueError("候选正文 chunk 缺少页码")
            if row.token_count != length:
                raise ValueError("候选正文 token_count 未记录字符长度")
            body_count += 1
            max_length = max(max_length, length)
    return {
        "processed_papers": processed.count(),
        "body_chunks": body_count,
        "abstract_chunks": abstract_count,
        "max_body_chars": max_length,
    }


def build_staged_chunk_database(
    source: Path,
    candidate: Path,
    *,
    corpus_root: Path,
    parser: Any | None = None,
    chunker: TextChunker | None = None,
) -> dict[str, Any]:
    """在新候选 SQLite 中重建已处理论文 chunks，成功后才原子发布候选。

    源库始终只读；候选构建失败时清理临时 DB/WAL/SHM。此函数不实例化
    ``PaperProcessor``，因此不会触碰任何 VectorStore。
    """
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
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{candidate.name}.", suffix=".tmp", dir=candidate.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    temporary.unlink()
    engine = None
    try:
        # 修复仅发生在候选副本，使候选可通过 foreign_key_check；源库不变。
        repair = repair_database_copy(source, temporary, dry_run=False)
        engine = _candidate_engine(temporary)
        SessionLocal = sessionmaker(
            autocommit=False, autoflush=False, bind=engine
        )
        parser = parser or PDFParser()
        chunker = chunker or TextChunker()
        with SessionLocal.begin() as db:
            papers = (
                db.query(Paper)
                .filter(Paper.processed == "done")
                .order_by(Paper.id)
                .all()
            )
            for paper in papers:
                pdf_path = _resolve_source_pdf(corpus_root, paper.file_path)
                pages = parser.extract_text(str(pdf_path))
                _replace_paper_chunks(db, paper, pages, chunker)
            db.flush()
            summary = _validate_candidate_chunks(
                db, chunk_size=chunker.chunk_size
            )
        engine.dispose()
        engine = None

        integrity = audit_database(temporary)
        if not integrity["quick_check_ok"]:
            raise sqlite3.DatabaseError("候选数据库 quick_check 失败")
        if integrity["foreign_key_violation_count"]:
            raise sqlite3.IntegrityError("候选数据库仍存在外键违规")
        if _source_manifest(source) != source_before:
            raise RuntimeError("重建期间源数据库发生变化，候选已中止")

        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, candidate)
        return {
            **summary,
            "candidate": str(candidate),
            "chunk_size": chunker.chunk_size,
            "chunk_overlap": chunker.chunk_overlap,
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
    """构建只生成候选、没有生产激活开关的 CLI。"""
    parser = argparse.ArgumentParser(
        prog="python -m app.services.staged_chunk_rebuild",
        description="在隔离 SQLite 副本中重新分块，不修改生产数据库或向量库",
    )
    parser.add_argument("--source", required=True, help="只读源 SQLite")
    parser.add_argument("--candidate", required=True, help="新候选 SQLite")
    parser.add_argument("--corpus-root", required=True, help="只读论文语料根目录")
    parser.add_argument("--chunk-size", type=int, default=None)
    parser.add_argument("--chunk-overlap", type=int, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = build_staged_chunk_database(
        Path(args.source),
        Path(args.candidate),
        corpus_root=Path(args.corpus_root),
        chunker=TextChunker(
            chunk_size=args.chunk_size,
            chunk_overlap=args.chunk_overlap,
        ),
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
