"""确定性 HNSW 评测副本构建 RED。"""

import sqlite3

import pytest

from eval.deterministic_vector_snapshot import build_deterministic_snapshot


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
        CREATE TABLE embeddings (id INTEGER PRIMARY KEY);
        INSERT INTO collections VALUES ('collection-1', 'papers');
        INSERT INTO collection_metadata(collection_id,key,str_value)
          VALUES ('collection-1','hnsw:space','cosine');
        INSERT INTO segments VALUES ('segment-1','collection-1','VECTOR');
        INSERT INTO segment_metadata(segment_id,key,str_value)
          VALUES ('segment-1','hnsw:space','cosine');
        INSERT INTO embeddings VALUES (1), (2), (3);
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


def test_builder_copies_then_freezes_threads_and_search_ef(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    _source_snapshot(source)

    result = build_deterministic_snapshot(source, target)

    assert result["vector_count"] == 3
    assert result["hnsw_num_threads"] == 1
    assert result["hnsw_search_ef"] == 3
    assert len(result["hnsw_config_sha256"]) == 64
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


def test_builder_refuses_existing_or_overlapping_target(tmp_path):
    source = tmp_path / "source"
    _source_snapshot(source)
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(FileExistsError):
        build_deterministic_snapshot(source, existing)
    with pytest.raises(ValueError, match="内部"):
        build_deterministic_snapshot(source, source / "nested")
