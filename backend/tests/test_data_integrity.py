"""SQLite 一致快照、审计与仅副本修复的 TDD 契约。"""

import hashlib
import sqlite3

import pytest

from app.services.data_integrity import (
    audit_database,
    create_sqlite_snapshot,
    repair_database_copy,
)


def _create_library(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        PRAGMA foreign_keys=OFF;
        CREATE TABLE papers (id INTEGER PRIMARY KEY);
        CREATE TABLE tags (id INTEGER PRIMARY KEY);
        CREATE TABLE paper_tags (
            paper_id INTEGER NOT NULL REFERENCES papers(id),
            tag_id INTEGER NOT NULL REFERENCES tags(id),
            PRIMARY KEY (paper_id, tag_id)
        );
        INSERT INTO papers VALUES (1);
        INSERT INTO tags VALUES (1);
        INSERT INTO paper_tags VALUES (1, 1);
        INSERT INTO paper_tags VALUES (999, 1);
        """
    )
    conn.commit()
    conn.close()


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_snapshot_is_readable_and_integral(tmp_path):
    source = tmp_path / "source.db"
    target = tmp_path / "snapshot.db"
    _create_library(source)

    assert create_sqlite_snapshot(source, target) == target
    assert audit_database(target)["quick_check_ok"] is True


def test_snapshot_failure_removes_partial_destination(tmp_path):
    source = tmp_path / "corrupt.db"
    target = tmp_path / "snapshot.db"
    source.write_bytes(b"not a sqlite database")

    with pytest.raises(sqlite3.DatabaseError):
        create_sqlite_snapshot(source, target)

    assert not target.exists()


def test_audit_counts_foreign_keys_without_exposing_row_content(tmp_path):
    source = tmp_path / "source.db"
    _create_library(source)

    report = audit_database(source)

    assert report == {
        "quick_check_ok": True,
        "foreign_key_violation_count": 1,
        "orphan_paper_tags_count": 1,
    }


def test_dry_run_repairs_only_copy_and_keeps_source_byte_identical(tmp_path):
    source = tmp_path / "source.db"
    copy = tmp_path / "dry-run.db"
    _create_library(source)
    before = _sha256(source)

    report = repair_database_copy(source, copy, dry_run=True)

    assert report["would_delete_orphan_paper_tags"] == 1
    assert report["deleted_orphan_paper_tags"] == 0
    assert audit_database(copy)["foreign_key_violation_count"] == 1
    assert _sha256(source) == before


def test_repair_copy_removes_only_orphans_and_is_idempotent(tmp_path):
    source = tmp_path / "source.db"
    repaired = tmp_path / "repaired.db"
    _create_library(source)
    before = _sha256(source)

    first = repair_database_copy(source, repaired, dry_run=False)

    assert first["deleted_orphan_paper_tags"] == 1
    assert first["after"]["quick_check_ok"] is True
    assert first["after"]["foreign_key_violation_count"] == 0
    conn = sqlite3.connect(repaired)
    assert conn.execute("SELECT * FROM paper_tags").fetchall() == [(1, 1)]
    conn.close()
    assert _sha256(source) == before

    second = repair_database_copy(repaired, tmp_path / "repaired-again.db", dry_run=False)
    assert second["deleted_orphan_paper_tags"] == 0
