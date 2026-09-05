"""Batch 22D：旧粗分块候选库的只读复制与坐标回填 RED。"""

import sqlite3

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Chunk, Paper
from app.services.staged_offset_backfill import (
    build_parser,
    build_staged_offset_database,
)


class _FakeParser:
    def __init__(self, text):
        self.text = text

    def extract_text(self, path):
        return [{"page_number": 1, "text": self.text}]


def _seed(tmp_path, content="unique legacy chunk content"):
    source = tmp_path / "source.db"
    corpus = tmp_path / "corpus"
    (corpus / "papers").mkdir(parents=True)
    (corpus / "papers" / "one.pdf").write_bytes(b"%PDF-fake")
    engine = create_engine(f"sqlite:///{source}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        db.add(Paper(
            id=1, title="one", doi="10.1/one", filename="one.pdf",
            file_path="papers/one.pdf", processed="done",
        ))
        db.add(Chunk(
            id=7, paper_id=1, chunk_index=0, page_number=1,
            content=content, page_start=None, page_end=None,
        ))
        db.commit()
    engine.dispose()
    return source, corpus


def _row(path):
    engine = create_engine(f"sqlite:///{path}")
    Session = sessionmaker(bind=engine)
    try:
        with Session() as db:
            row = db.query(Chunk).one()
            return (
                row.id, row.paper_id, row.chunk_index, row.page_number,
                row.content, row.page_start, row.page_end,
            )
    finally:
        engine.dispose()


def test_backfill_changes_only_offsets_in_candidate(tmp_path):
    content = "unique legacy chunk content"
    page = "prefix " + content + " suffix"
    source, corpus = _seed(tmp_path, content)
    candidate = tmp_path / "candidate.db"

    result = build_staged_offset_database(
        source, candidate, corpus_root=corpus, parser=_FakeParser(page)
    )

    assert _row(source)[-2:] == (None, None)
    assert _row(candidate) == (
        7, 1, 0, 1, content,
        page.index(content), page.index(content) + len(content),
    )
    assert result["body_chunks"] == 1
    assert result["source_unchanged"] is True


def test_duplicate_chunk_text_fails_closed_and_removes_candidate(tmp_path):
    content = "duplicate legacy chunk content"
    page = f"{content} gap {content}"
    source, corpus = _seed(tmp_path, content)
    candidate = tmp_path / "candidate.db"

    with pytest.raises(ValueError, match="多处命中"):
        build_staged_offset_database(
            source, candidate, corpus_root=corpus, parser=_FakeParser(page)
        )

    assert not candidate.exists()
    assert _row(source)[-2:] == (None, None)


def test_existing_valid_offsets_disambiguate_duplicate_chunk_text(tmp_path):
    content = "duplicate but already located"
    page = f"{content} gap {content}"
    source, corpus = _seed(tmp_path, content)
    with sqlite3.connect(source) as conn:
        conn.execute(
            "UPDATE chunks SET page_start = 0, page_end = ?",
            (len(content),),
        )
    candidate = tmp_path / "candidate.db"

    result = build_staged_offset_database(
        source, candidate, corpus_root=corpus, parser=_FakeParser(page)
    )

    assert _row(candidate)[-2:] == (0, len(content))
    assert result["preserved_body_chunks"] == 1
    assert result["backfilled_body_chunks"] == 0


def test_existing_invalid_offsets_fail_closed(tmp_path):
    content = "legacy chunk with invalid offsets"
    source, corpus = _seed(tmp_path, content)
    with sqlite3.connect(source) as conn:
        conn.execute("UPDATE chunks SET page_start = 0, page_end = 9999")
    candidate = tmp_path / "candidate.db"

    with pytest.raises(ValueError, match="既有正文 chunk 坐标无效"):
        build_staged_offset_database(
            source,
            candidate,
            corpus_root=corpus,
            parser=_FakeParser(content),
        )

    assert not candidate.exists()


def test_wal_source_snapshot_does_not_require_journal_mode_switch(tmp_path):
    content = "wal legacy chunk content"
    page = "prefix " + content + " suffix"
    source, corpus = _seed(tmp_path, content)
    with sqlite3.connect(source) as conn:
        assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"

    result = build_staged_offset_database(
        source,
        tmp_path / "candidate.db",
        corpus_root=corpus,
        parser=_FakeParser(page),
    )

    assert result["body_chunks"] == 1


def test_cli_has_no_activation_path():
    args = build_parser().parse_args([
        "--source", "/tmp/source.db",
        "--candidate", "/tmp/candidate.db",
        "--corpus-root", "/tmp/corpus",
    ])

    assert args.candidate == "/tmp/candidate.db"
    assert not hasattr(args, "activate")
