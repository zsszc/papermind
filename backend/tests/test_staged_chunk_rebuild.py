"""Batch 22C：候选 SQLite 分块重建的失败隔离契约。"""

from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Chunk, Paper
from app.services.embedding import TextChunker
from app.services.staged_chunk_rebuild import (
    build_parser,
    build_staged_chunk_database,
)


class _FakeParser:
    def __init__(self, *, fail_name=None):
        self.fail_name = fail_name

    def extract_text(self, file_path):
        if self.fail_name and Path(file_path).name == self.fail_name:
            raise ValueError("injected parser failure")
        return [{
            "page_number": 1,
            "text": "Methods. " + "x" * 35,
            "width": 612,
            "height": 792,
        }]


def _seed_source(tmp_path, *, two_papers=False):
    source = tmp_path / "source.db"
    corpus_root = tmp_path / "corpus"
    papers_dir = corpus_root / "papers"
    papers_dir.mkdir(parents=True)
    engine = create_engine(f"sqlite:///{source}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        papers = [
            Paper(
                id=1,
                title="one",
                filename="one.pdf",
                file_path="papers/one.pdf",
                processed="done",
            )
        ]
        if two_papers:
            papers.append(Paper(
                id=2,
                title="two",
                filename="two.pdf",
                file_path="papers/two.pdf",
                processed="done",
            ))
        db.add_all(papers)
        db.flush()
        db.add_all([
            Chunk(
                paper_id=paper.id,
                chunk_index=0,
                page_number=1,
                content=f"legacy-{paper.id}",
                token_count=8,
            )
            for paper in papers
        ])
        db.commit()
    engine.dispose()
    for paper in ("one.pdf", "two.pdf"):
        if paper == "one.pdf" or two_papers:
            (papers_dir / paper).write_bytes(b"%PDF-fake")
    return source, corpus_root


def _rows(database):
    engine = create_engine(f"sqlite:///{database}")
    Session = sessionmaker(bind=engine)
    try:
        with Session() as db:
            return [
                (row.paper_id, row.chunk_index, row.content, row.page_number)
                for row in db.query(Chunk).order_by(
                    Chunk.paper_id, Chunk.chunk_index
                )
            ]
    finally:
        engine.dispose()


def test_candidate_rebuild_changes_only_copy_and_enforces_hard_limit(tmp_path):
    source, corpus_root = _seed_source(tmp_path)
    candidate = tmp_path / "candidate.db"

    result = build_staged_chunk_database(
        source,
        candidate,
        corpus_root=corpus_root,
        parser=_FakeParser(),
        chunker=TextChunker(chunk_size=10, chunk_overlap=2),
    )

    assert _rows(source) == [(1, 0, "legacy-1", 1)]
    candidate_rows = _rows(candidate)
    body = [row for row in candidate_rows if row[1] >= 0]
    assert [row[1] for row in body] == list(range(len(body)))
    assert all(0 < len(row[2]) <= 10 and row[3] == 1 for row in body)
    assert [row[1] for row in candidate_rows].count(-1) == 1
    assert result["processed_papers"] == 1
    assert result["body_chunks"] == len(body)
    assert result["source_unchanged"] is True


def test_parser_failure_removes_candidate_and_preserves_source(tmp_path):
    source, corpus_root = _seed_source(tmp_path, two_papers=True)
    candidate = tmp_path / "candidate.db"
    before = _rows(source)

    with pytest.raises(ValueError, match="injected parser failure"):
        build_staged_chunk_database(
            source,
            candidate,
            corpus_root=corpus_root,
            parser=_FakeParser(fail_name="two.pdf"),
            chunker=TextChunker(chunk_size=10, chunk_overlap=2),
        )

    assert not candidate.exists()
    assert not list(tmp_path.glob(".candidate.db.*.tmp*"))
    assert _rows(source) == before


def test_candidate_path_must_be_new_and_distinct(tmp_path):
    source, corpus_root = _seed_source(tmp_path)

    with pytest.raises(ValueError, match="不得覆盖源"):
        build_staged_chunk_database(
            source,
            source,
            corpus_root=corpus_root,
            parser=_FakeParser(),
        )

    candidate = tmp_path / "candidate.db"
    candidate.write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        build_staged_chunk_database(
            source,
            candidate,
            corpus_root=corpus_root,
            parser=_FakeParser(),
        )
    assert candidate.read_bytes() == b"keep"


def test_source_pdf_cannot_escape_corpus_root(tmp_path):
    source, corpus_root = _seed_source(tmp_path)
    engine = create_engine(f"sqlite:///{source}")
    Session = sessionmaker(bind=engine)
    with Session() as db:
        paper = db.get(Paper, 1)
        paper.file_path = "../outside.pdf"
        db.commit()
    engine.dispose()

    with pytest.raises(ValueError, match="语料根目录"):
        build_staged_chunk_database(
            source,
            tmp_path / "candidate.db",
            corpus_root=corpus_root,
            parser=_FakeParser(),
        )


def test_cli_only_builds_candidate_and_has_no_activate_flag():
    args = build_parser().parse_args([
        "--source", "/tmp/source.db",
        "--candidate", "/tmp/candidate.db",
        "--corpus-root", "/tmp/corpus",
    ])

    assert args.candidate == "/tmp/candidate.db"
    assert not hasattr(args, "activate")

