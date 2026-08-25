"""确定性 HNSW 评测副本构建 RED。"""

import hashlib
from pathlib import Path
import sqlite3

import pytest

from eval.deterministic_vector_snapshot import (
    _hnsw_binary_manifests,
    activate_deterministic_snapshot,
    audit_hnsw_sqlite_metadata,
    build_deterministic_snapshot,
)


def _source_snapshot(path):
    path.mkdir()
    (path / "index.bin").write_bytes(b"immutable-vector-index")
    connection = sqlite3.connect(path / "chroma.sqlite3")
    connection.executescript(
        """
        CREATE TABLE collections (id TEXT PRIMARY KEY, name TEXT);
        CREATE TABLE collection_metadata (
          collection_id TEXT, key TEXT, str_value TEXT,
          int_value INTEGER, float_value REAL,
          PRIMARY KEY (collection_id, key)
        );
        CREATE TABLE segments (id TEXT PRIMARY KEY, collection TEXT, scope TEXT);
        CREATE TABLE segment_metadata (
          segment_id TEXT, key TEXT, str_value TEXT,
          int_value INTEGER, float_value REAL,
          PRIMARY KEY (segment_id, key)
        );
        CREATE TABLE embeddings (
          id INTEGER PRIMARY KEY, segment_id TEXT, embedding_id TEXT
        );
        INSERT INTO collections VALUES ('collection-1', 'papers');
        INSERT INTO collection_metadata(collection_id,key,str_value)
          VALUES ('collection-1','hnsw:space','cosine');
        INSERT INTO segments VALUES ('vector-1','collection-1','VECTOR');
        INSERT INTO segments VALUES ('metadata-1','collection-1','METADATA');
        INSERT INTO segment_metadata(segment_id,key,str_value)
          VALUES ('vector-1','hnsw:space','cosine');
        INSERT INTO embeddings VALUES
          (1,'metadata-1','p1_c0'),
          (2,'metadata-1','p1_c1'),
          (3,'metadata-1','p1_c2');
        INSERT INTO collections VALUES ('collection-2', 'other');
        INSERT INTO segments VALUES ('metadata-2','collection-2','METADATA');
        INSERT INTO embeddings VALUES
          (4,'metadata-2','other-1'),
          (5,'metadata-2','other-2');
        """
    )
    connection.commit()
    connection.close()


def _metadata(path, table, id_column):
    connection = sqlite3.connect(path / "chroma.sqlite3")
    rows = connection.execute(
        f"SELECT key, str_value, int_value FROM {table} ORDER BY key"
    ).fetchall()
    connection.close()
    return rows


def _manifest(path):
    audit = audit_hnsw_sqlite_metadata(path)
    binary = (path / "index.bin").read_bytes()
    return {
        **audit,
        "embedding_dimension": 1024,
        "vector_manifest_sha256": hashlib.sha256(binary).hexdigest(),
        "hnsw_binary_manifest_sha256": hashlib.sha256(binary).hexdigest(),
    }


def test_hnsw_manifest_separates_volatile_runtime_length(tmp_path):
    segment = tmp_path / "segment-1"
    segment.mkdir()
    (segment / "data_level0.bin").write_bytes(b"vectors")
    (segment / "header.bin").write_bytes(b"header")
    (segment / "link_lists.bin").write_bytes(b"links")
    (segment / "length.bin").write_bytes(b"runtime-a")

    before = _hnsw_binary_manifests(tmp_path, "segment-1")
    (segment / "length.bin").write_bytes(b"runtime-b")
    after = _hnsw_binary_manifests(tmp_path, "segment-1")

    assert before["hnsw_binary_manifest_sha256"] == after[
        "hnsw_binary_manifest_sha256"
    ]
    assert before["hnsw_full_binary_manifest_sha256"] != after[
        "hnsw_full_binary_manifest_sha256"
    ]
    assert before["hnsw_volatile_files"] == ["length.bin"]


def test_builder_copies_then_freezes_threads_and_search_ef(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    _source_snapshot(source)

    result = build_deterministic_snapshot(
        source, target, manifest_reader=_manifest
    )

    assert result["vector_count"] == 3
    assert result["hnsw_num_threads"] == 1
    assert result["hnsw_search_ef"] == 3
    assert len(result["hnsw_config_sha256"]) == 64
    assert result["vector_manifest_sha256"] == hashlib.sha256(
        b"immutable-vector-index"
    ).hexdigest()
    assert result["hnsw_binary_manifest_sha256"] == result[
        "vector_manifest_sha256"
    ]
    assert (target / "index.bin").read_bytes() == b"immutable-vector-index"
    for table, id_column in (
        ("collection_metadata", "collection_id"),
        ("segment_metadata", "segment_id"),
    ):
        rows = _metadata(target, table, id_column)
        assert ("hnsw:num_threads", None, 1) in rows
        assert ("hnsw:search_ef", None, 3) in rows
    assert _metadata(source, "collection_metadata", "collection_id") == [
        ("hnsw:space", "cosine", None)
    ]
    assert audit_hnsw_sqlite_metadata(target)["hnsw_num_threads"] == 1
    assert audit_hnsw_sqlite_metadata(target)["hnsw_search_ef"] == 3


def test_builder_refuses_existing_or_overlapping_target(tmp_path):
    source = tmp_path / "source"
    _source_snapshot(source)
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(FileExistsError):
        build_deterministic_snapshot(source, existing)
    with pytest.raises(ValueError, match="内部"):
        build_deterministic_snapshot(source, source / "nested")


def test_builder_fails_closed_when_copied_vector_manifest_changes(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    _source_snapshot(source)

    def changed_manifest(path):
        manifest = _manifest(path)
        if path != source.resolve():
            manifest["vector_manifest_sha256"] = "0" * 64
        return manifest

    with pytest.raises(ValueError, match="向量内容指纹"):
        build_deterministic_snapshot(
            source, target, manifest_reader=changed_manifest
        )
    assert not target.exists()


def test_activate_preflight_and_postcheck_restore_old_target(tmp_path):
    target = tmp_path / "vector_db"
    stage = tmp_path / ".vector_db.stage-fixed"
    _source_snapshot(target)
    _source_snapshot(stage)
    connection = sqlite3.connect(stage / "chroma.sqlite3")
    for table, id_column, owner_id in (
        ("collection_metadata", "collection_id", "collection-1"),
        ("segment_metadata", "segment_id", "vector-1"),
    ):
        for key, value in (("hnsw:num_threads", 1), ("hnsw:search_ef", 3)):
            connection.execute(
                f"INSERT INTO {table} ({id_column},key,int_value) VALUES (?,?,?)",
                (owner_id, key, value),
            )
    connection.commit()
    connection.close()
    old_marker = (target / "index.bin").read_bytes()
    calls = 0

    def postcheck_fails(path):
        nonlocal calls
        calls += 1
        manifest = _manifest(path)
        # preflight stage + target 成功，激活后的第三次 target 审计失败。
        if calls == 3:
            manifest["vector_manifest_sha256"] = "f" * 64
        return manifest

    expected_sha = hashlib.sha256(old_marker).hexdigest()
    with pytest.raises(RuntimeError, match="已恢复旧库"):
        activate_deterministic_snapshot(
            stage,
            target,
            expected_vector_manifest_sha256=expected_sha,
            manifest_reader=postcheck_fails,
            suffix="rollback",
        )

    assert (target / "index.bin").read_bytes() == old_marker
    assert (tmp_path / "vector_db.failed-rollback").is_dir()
    assert not (tmp_path / "vector_db.backup-rollback").exists()


def test_activate_rejects_same_path_symlink_and_unverified_stage(tmp_path):
    target = tmp_path / "vector_db"
    _source_snapshot(target)
    with pytest.raises(ValueError, match="不同目录"):
        activate_deterministic_snapshot(
            target, target,
            expected_vector_manifest_sha256="a" * 64,
            manifest_reader=_manifest,
        )

    link = tmp_path / "stage-link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="符号链接"):
        activate_deterministic_snapshot(
            link, target,
            expected_vector_manifest_sha256="a" * 64,
            manifest_reader=_manifest,
        )

    stage = tmp_path / ".vector_db.stage-unverified"
    _source_snapshot(stage)
    with pytest.raises(ValueError, match="HNSW"):
        activate_deterministic_snapshot(
            stage, target,
            expected_vector_manifest_sha256=hashlib.sha256(
                b"immutable-vector-index"
            ).hexdigest(),
            manifest_reader=_manifest,
        )
