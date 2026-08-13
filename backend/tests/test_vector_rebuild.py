"""Batch 18：Chroma 隔离重建、校验与原子换入契约。"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.models import Chunk, Paper
from app.services.vector_rebuild import (
    activate_staged_vector_store,
    build_staged_vector_store,
    expected_chunk_records,
    validate_vector_collection,
)


class _FakeCollection:
    def __init__(self, ids=None, embeddings=None):
        self.ids = list(ids or [])
        self.embeddings = list(embeddings or [])
        self.upsert_calls = []

    def upsert(self, **kwargs):
        self.upsert_calls.append(kwargs)
        self.ids.extend(kwargs["ids"])
        self.embeddings.extend(kwargs["embeddings"])

    def get(self, include=None):
        return {"ids": self.ids, "embeddings": self.embeddings}

    def query(self, **kwargs):
        return {"ids": [[self.ids[0]]]}


class _FakeClient:
    def __init__(self, collection):
        self.collection = collection

    def get_or_create_collection(self, **kwargs):
        return self.collection


def _seed_chunks(db):
    db.add(Paper(id=1, title="one", filename="one.pdf", file_path="papers/one.pdf"))
    db.add_all([
        Chunk(paper_id=1, chunk_index=-1, content="abstract", chunk_type="abstract"),
        Chunk(paper_id=1, chunk_index=0, content="first", page_number=1),
    ])
    db.commit()


def test_expected_chunk_records_preserve_ids_and_metadata(db):
    _seed_chunks(db)

    records = expected_chunk_records(db)

    assert [item["id"] for item in records] == ["p1_c-1", "p1_c0"]
    assert records[0]["metadata"]["chunk_index"] == -1
    assert records[1]["metadata"]["page_number"] == 1


def test_build_staged_store_batches_upsert_and_validates(db, tmp_path):
    _seed_chunks(db)
    collection = _FakeCollection()
    client = _FakeClient(collection)
    embedder = MagicMock()
    embedder.embed.side_effect = lambda texts: [[float(len(text)), 1.0] for text in texts]

    result = build_staged_vector_store(
        db,
        tmp_path / "stage",
        embedder=embedder,
        client_factory=lambda _: client,
        batch_size=1,
    )

    assert result == {"count": 2, "dimension": 2}
    assert len(collection.upsert_calls) == 2
    assert set(collection.ids) == {"p1_c-1", "p1_c0"}


def test_validation_fails_closed_on_id_or_dimension_mismatch():
    with pytest.raises(ValueError, match="ID 集合"):
        validate_vector_collection(
            _FakeCollection(["p1_c0"], [[0.1, 0.2]]),
            expected_ids={"p1_c0", "p1_c1"},
            expected_dimension=2,
            smoke_embedding=[0.1, 0.2],
        )

    with pytest.raises(ValueError, match="维度"):
        validate_vector_collection(
            _FakeCollection(["p1_c0"], [[0.1]]),
            expected_ids={"p1_c0"},
            expected_dimension=2,
            smoke_embedding=[0.1, 0.2],
        )


def test_activate_keeps_recoverable_backup(tmp_path):
    target = tmp_path / "vector_db"
    staged = tmp_path / ".vector-stage"
    target.mkdir()
    staged.mkdir()
    (target / "old").write_text("old", encoding="utf-8")
    (staged / "new").write_text("new", encoding="utf-8")

    backup = activate_staged_vector_store(staged, target, suffix="fixed")

    assert backup == tmp_path / "vector_db.backup-fixed"
    assert (backup / "old").read_text(encoding="utf-8") == "old"
    assert (target / "new").read_text(encoding="utf-8") == "new"
    assert not staged.exists()


def test_activate_rolls_back_when_second_rename_fails(tmp_path, monkeypatch):
    target = tmp_path / "vector_db"
    staged = tmp_path / ".vector-stage"
    target.mkdir()
    staged.mkdir()
    (target / "old").write_text("old", encoding="utf-8")
    (staged / "new").write_text("new", encoding="utf-8")
    real_replace = Path.replace

    def fail_staged_replace(self, destination):
        if self == staged:
            raise OSError("injected swap failure")
        return real_replace(self, destination)

    monkeypatch.setattr(Path, "replace", fail_staged_replace)

    with pytest.raises(OSError, match="injected"):
        activate_staged_vector_store(staged, target, suffix="failed")

    assert (target / "old").read_text(encoding="utf-8") == "old"
    assert (staged / "new").read_text(encoding="utf-8") == "new"
    assert not (tmp_path / "vector_db.backup-failed").exists()
