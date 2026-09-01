"""为离线评测原子构建、审计并激活确定性 HNSW 快照。"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import struct
from typing import Any, Callable
import uuid

from app.services.vector_rebuild import activate_staged_vector_store


ManifestReader = Callable[[Path], dict[str, Any]]


def _sha256_payload(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _config_sha256(vector_count: int) -> str:
    return _sha256_payload({
        "version": "deterministic-hnsw-v1",
        "hnsw_num_threads": 1,
        "hnsw_search_ef": vector_count,
        "vector_count": vector_count,
    })


def _metadata_rows(
    connection: sqlite3.Connection,
    table: str,
    id_column: str,
    owner_id: str,
) -> dict[str, str | int | float | None]:
    rows = connection.execute(
        f"""
        SELECT key, str_value, int_value, float_value
        FROM {table}
        WHERE {id_column} = ?
        """,
        (owner_id,),
    ).fetchall()
    result: dict[str, str | int | float | None] = {}
    for key, str_value, int_value, float_value in rows:
        values = [
            value for value in (str_value, int_value, float_value)
            if value is not None
        ]
        if len(values) > 1:
            raise ValueError(f"{table} 元数据 {key} 同时包含多个值")
        result[str(key)] = values[0] if values else None
    return result


def audit_hnsw_sqlite_metadata(snapshot_dir: Path) -> dict[str, Any]:
    """从 Chroma SQLite 原始表审计 collection/segment 两层配置。"""
    snapshot_dir = Path(snapshot_dir).resolve()
    database = snapshot_dir / "chroma.sqlite3"
    if not database.is_file():
        raise FileNotFoundError(f"Chroma SQLite 不存在: {database}")

    connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
    try:
        quick_check = connection.execute("PRAGMA quick_check").fetchone()
        if quick_check is None or quick_check[0] != "ok":
            raise ValueError("Chroma SQLite quick_check 未通过")
        collections = connection.execute(
            "SELECT id FROM collections WHERE name = 'papers'"
        ).fetchall()
        if len(collections) != 1:
            raise ValueError("快照必须恰好包含一个 papers collection")
        collection_id = str(collections[0][0])
        segments = connection.execute(
            """
            SELECT id, scope FROM segments
            WHERE collection = ? AND scope IN ('VECTOR', 'METADATA')
            ORDER BY scope, id
            """,
            (collection_id,),
        ).fetchall()
        vector_ids = [str(row[0]) for row in segments if row[1] == "VECTOR"]
        metadata_ids = [
            str(row[0]) for row in segments if row[1] == "METADATA"
        ]
        if len(vector_ids) != 1 or len(metadata_ids) != 1:
            raise ValueError(
                "papers 必须恰好包含一个 VECTOR 和一个 METADATA segment"
            )
        vector_id = vector_ids[0]
        metadata_id = metadata_ids[0]
        vector_count = int(connection.execute(
            "SELECT COUNT(*) FROM embeddings WHERE segment_id = ?",
            (metadata_id,),
        ).fetchone()[0])
        if vector_count <= 0:
            raise ValueError("papers collection 没有向量")

        collection_metadata = _metadata_rows(
            connection,
            "collection_metadata",
            "collection_id",
            collection_id,
        )
        segment_metadata = _metadata_rows(
            connection,
            "segment_metadata",
            "segment_id",
            vector_id,
        )
    finally:
        connection.close()

    for key in ("hnsw:space", "hnsw:num_threads", "hnsw:search_ef"):
        if collection_metadata.get(key) != segment_metadata.get(key):
            raise ValueError(f"collection/segment HNSW 参数不一致: {key}")
    space = collection_metadata.get("hnsw:space")
    if space != "cosine":
        raise ValueError("papers HNSW 空间必须为 cosine")
    num_threads = collection_metadata.get("hnsw:num_threads")
    search_ef = collection_metadata.get("hnsw:search_ef")
    if num_threads is not None and not isinstance(num_threads, int):
        raise ValueError("hnsw:num_threads 必须为整数")
    if search_ef is not None and not isinstance(search_ef, int):
        raise ValueError("hnsw:search_ef 必须为整数")

    config_payload = {
        "version": "deterministic-hnsw-v1",
        "hnsw_num_threads": num_threads,
        "hnsw_search_ef": search_ef,
        "vector_count": vector_count,
    }
    return {
        "sqlite_quick_check": "ok",
        "collection_id": collection_id,
        "vector_segment_id": vector_id,
        "metadata_segment_id": metadata_id,
        "vector_count": vector_count,
        "hnsw_space": space,
        "hnsw_num_threads": num_threads,
        "hnsw_search_ef": search_ef,
        "hnsw_config_sha256": _sha256_payload(config_payload),
        "collection_metadata": collection_metadata,
        "segment_metadata": segment_metadata,
    }


def _hash_named_files(segment_dir: Path, names: list[str]) -> str:
    digest = hashlib.sha256()
    for name in names:
        path = segment_dir / name
        if not path.is_file():
            raise ValueError(f"HNSW 二进制文件不存在: {name}")
        content = path.read_bytes()
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(len(content).to_bytes(8, "little"))
        digest.update(content)
    return digest.hexdigest()


def _hnsw_binary_manifests(
    snapshot_dir: Path, segment_id: str
) -> dict[str, Any]:
    """区分稳定 HNSW 结构与 Chroma 运行时重写的 length.bin。"""
    segment_dir = snapshot_dir / segment_id
    if not segment_dir.is_dir():
        raise ValueError(f"HNSW 二进制目录不存在: {segment_id}")
    expected = {
        "data_level0.bin", "header.bin", "length.bin", "link_lists.bin"
    }
    # Chroma 0.4.24 向量增长后持久化 index_metadata.pickle（Batch 22L 实证）：
    # 内容随索引结构确定，归为可选结构文件；旧 4 文件快照保持兼容。
    optional_structural = {"index_metadata.pickle"}
    actual = {
        path.relative_to(segment_dir).as_posix()
        for path in segment_dir.rglob("*") if path.is_file()
    }
    if not expected.issubset(actual) or not actual.issubset(
        expected | optional_structural
    ):
        raise ValueError("HNSW 二进制文件集合不符合冻结契约")
    structural = sorted((expected - {"length.bin"}) | (actual & optional_structural))
    return {
        "hnsw_binary_manifest_sha256": _hash_named_files(
            segment_dir, structural
        ),
        "hnsw_full_binary_manifest_sha256": _hash_named_files(
            segment_dir, sorted(expected)
        ),
        "hnsw_structural_files": structural,
        "hnsw_volatile_files": ["length.bin"],
    }


def read_raw_snapshot_manifest(snapshot_dir: Path) -> dict[str, Any]:
    """不初始化 Chroma client，读取不会改写源目录的原始快照指纹。"""
    snapshot_dir = Path(snapshot_dir).resolve()
    audit = audit_hnsw_sqlite_metadata(snapshot_dir)
    database = snapshot_dir / "chroma.sqlite3"
    connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
    try:
        ids = [str(row[0]) for row in connection.execute(
            "SELECT embedding_id FROM embeddings WHERE segment_id = ? ORDER BY embedding_id",
            (audit["metadata_segment_id"],),
        ).fetchall()]
        collection_columns = {
            str(row[1]) for row in connection.execute(
                "PRAGMA table_info(collections)"
            ).fetchall()
        }
        dimension = None
        if "dimension" in collection_columns:
            row = connection.execute(
                "SELECT dimension FROM collections WHERE id = ?",
                (audit["collection_id"],),
            ).fetchone()
            dimension = row[0] if row else None
    finally:
        connection.close()
    if len(ids) != len(set(ids)) or len(ids) != audit["vector_count"]:
        raise ValueError("papers embedding ID 数量或唯一性审计失败")
    id_payload = json.dumps(ids, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    return {
        **audit,
        "embedding_dimension": dimension,
        "embedding_id_manifest_sha256": hashlib.sha256(id_payload).hexdigest(),
        **_hnsw_binary_manifests(
            snapshot_dir, audit["vector_segment_id"]
        ),
    }


def read_vector_snapshot_manifest(snapshot_dir: Path) -> dict[str, Any]:
    """用全新 Chroma client 校验 ID、维度、查询并生成双指纹。"""
    import chromadb
    from chromadb.config import Settings

    snapshot_dir = Path(snapshot_dir).resolve()
    raw_before = read_raw_snapshot_manifest(snapshot_dir)
    client = chromadb.PersistentClient(
        path=str(snapshot_dir),
        settings=Settings(anonymized_telemetry=False),
    )
    collection = client.get_collection("papers")
    payload = collection.get(include=["embeddings"])
    vector_ids = [str(value) for value in payload.get("ids") or []]
    embeddings = payload.get("embeddings")
    if embeddings is None:
        embeddings = []
    if len(vector_ids) != len(set(vector_ids)):
        raise ValueError("Chroma 存在重复向量 ID")
    if (
        len(vector_ids) != raw_before["vector_count"]
        or len(embeddings) != len(vector_ids)
    ):
        raise ValueError("Chroma API 与 SQLite 的向量数量不一致")
    dimensions = {len(embedding) for embedding in embeddings}
    if dimensions != {1024}:
        raise ValueError("评测向量维度必须统一为 1024")

    digest = hashlib.sha256()
    ordered = sorted(zip(vector_ids, embeddings), key=lambda pair: pair[0])
    for vector_id, embedding in ordered:
        digest.update(vector_id.encode("utf-8"))
        digest.update(b"\0")
        for value in embedding:
            digest.update(struct.pack("<f", float(value)))
    smoke = collection.query(
        query_embeddings=[[float(value) for value in ordered[0][1]]],
        n_results=1,
        include=["distances"],
    )
    if not (smoke.get("ids") or [[]])[0]:
        raise ValueError("Chroma query smoke 未返回结果")
    raw_after = read_raw_snapshot_manifest(snapshot_dir)
    if (
        raw_before["hnsw_binary_manifest_sha256"]
        != raw_after["hnsw_binary_manifest_sha256"]
    ):
        raise ValueError("Chroma 打开后 HNSW 结构文件发生变化")
    return {
        **raw_after,
        "embedding_dimension": next(iter(dimensions)),
        "vector_manifest_sha256": digest.hexdigest(),
        "hnsw_runtime_file_rewritten": (
            raw_before["hnsw_full_binary_manifest_sha256"]
            != raw_after["hnsw_full_binary_manifest_sha256"]
        ),
        "query_smoke": "ok",
    }


def _upsert_metadata(
    connection: sqlite3.Connection,
    *,
    table: str,
    id_column: str,
    owner_id: str,
    key: str,
    value: int,
) -> None:
    connection.execute(
        f"""
        INSERT INTO {table} ({id_column}, key, str_value, int_value, float_value)
        VALUES (?, ?, NULL, ?, NULL)
        ON CONFLICT ({id_column}, key) DO UPDATE SET
          str_value=NULL, int_value=excluded.int_value, float_value=NULL
        """,
        (owner_id, key, value),
    )


def _same_vector_payload(left: dict[str, Any], right: dict[str, Any]) -> bool:
    keys = (
        "vector_count",
        "embedding_dimension",
        "vector_manifest_sha256",
        "hnsw_binary_manifest_sha256",
    )
    return all(left.get(key) == right.get(key) for key in keys)


def _same_raw_payload(left: dict[str, Any], right: dict[str, Any]) -> bool:
    keys = (
        "vector_count",
        "embedding_dimension",
        "embedding_id_manifest_sha256",
        "hnsw_binary_manifest_sha256",
    )
    return all(left.get(key) == right.get(key) for key in keys)


def _require_deterministic_hnsw(manifest: dict[str, Any]) -> None:
    count = manifest.get("vector_count")
    if (
        manifest.get("hnsw_space") != "cosine"
        or manifest.get("hnsw_num_threads") != 1
        or manifest.get("hnsw_search_ef") != count
        or manifest.get("hnsw_config_sha256") != _config_sha256(count)
    ):
        raise ValueError("候选快照未通过确定性 HNSW 配置审计")


def build_deterministic_snapshot(
    source: Path,
    target: Path,
    *,
    manifest_reader: ManifestReader | None = None,
    expected_vector_manifest_sha256: str | None = None,
) -> dict[str, object]:
    """离线复制快照，只修改 stage 元数据并验证内容完全不变。"""
    source_input = Path(source)
    target_input = Path(target)
    if source_input.is_symlink() or target_input.is_symlink():
        raise ValueError("源和目标快照不能是符号链接")
    source = source_input.resolve()
    target = target_input.resolve()
    if source == target:
        raise ValueError("源和目标必须是不同目录")
    if not source.is_dir() or not (source / "chroma.sqlite3").is_file():
        raise FileNotFoundError(f"源 Chroma 快照不存在: {source}")
    if target.exists():
        raise FileExistsError(f"目标快照已存在: {target}")
    if target.is_relative_to(source):
        raise ValueError("目标不能位于源快照内部")

    if manifest_reader is None and not expected_vector_manifest_sha256:
        raise ValueError("真实快照构建必须提供预期 embedding 向量指纹")
    source_before = (
        manifest_reader(source)
        if manifest_reader else read_raw_snapshot_manifest(source)
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = target.parent / f".{target.name}.stage-{uuid.uuid4().hex}"
    try:
        shutil.copytree(source, stage)
        connection = sqlite3.connect(stage / "chroma.sqlite3")
        try:
            connection.execute("BEGIN IMMEDIATE")
            collection_rows = connection.execute(
                "SELECT id FROM collections WHERE name = 'papers'"
            ).fetchall()
            if len(collection_rows) != 1:
                raise ValueError("源快照必须恰好包含一个 papers collection")
            collection_id = str(collection_rows[0][0])
            segment_rows = connection.execute(
                """
                SELECT id, scope FROM segments
                WHERE collection = ? AND scope IN ('VECTOR', 'METADATA')
                """,
                (collection_id,),
            ).fetchall()
            vector_ids = [str(row[0]) for row in segment_rows if row[1] == "VECTOR"]
            metadata_ids = [
                str(row[0]) for row in segment_rows if row[1] == "METADATA"
            ]
            if len(vector_ids) != 1 or len(metadata_ids) != 1:
                raise ValueError(
                    "papers 必须恰好包含一个 VECTOR 和一个 METADATA segment"
                )
            vector_count = int(connection.execute(
                "SELECT COUNT(*) FROM embeddings WHERE segment_id = ?",
                (metadata_ids[0],),
            ).fetchone()[0])
            if vector_count <= 0:
                raise ValueError("源快照没有向量")

            for table, id_column, owner_id in (
                ("collection_metadata", "collection_id", collection_id),
                ("segment_metadata", "segment_id", vector_ids[0]),
            ):
                _upsert_metadata(
                    connection,
                    table=table,
                    id_column=id_column,
                    owner_id=owner_id,
                    key="hnsw:num_threads",
                    value=1,
                )
                _upsert_metadata(
                    connection,
                    table=table,
                    id_column=id_column,
                    owner_id=owner_id,
                    key="hnsw:search_ef",
                    value=vector_count,
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

        if manifest_reader:
            source_after = manifest_reader(source)
            stage_manifest = manifest_reader(stage)
            if not _same_vector_payload(source_before, source_after):
                raise ValueError("复制期间源快照发生变化，拒绝生成候选")
            if not _same_vector_payload(source_before, stage_manifest):
                raise ValueError("候选向量内容指纹或 HNSW 二进制指纹发生变化")
        else:
            stage_raw_before = read_raw_snapshot_manifest(stage)
            source_after = read_raw_snapshot_manifest(source)
            if source_before != source_after:
                raise ValueError("复制期间源快照发生变化，拒绝生成候选")
            if not _same_raw_payload(source_before, stage_raw_before):
                raise ValueError("候选 ID、维度或 HNSW 结构指纹发生变化")
            stage_manifest = read_vector_snapshot_manifest(stage)
            if not _same_raw_payload(source_before, stage_manifest):
                raise ValueError("候选复检后 ID、维度或 HNSW 结构发生变化")
            if (
                stage_manifest["vector_manifest_sha256"]
                != expected_vector_manifest_sha256
            ):
                raise ValueError("候选向量内容指纹与预期不一致")
        _require_deterministic_hnsw(stage_manifest)
        os.replace(stage, target)
    except Exception:
        if stage.exists():
            shutil.rmtree(stage)
        raise

    return {
        "version": "deterministic-hnsw-v1",
        "source": str(source),
        "target": str(target),
        "vector_count": vector_count,
        "embedding_dimension": stage_manifest["embedding_dimension"],
        "vector_manifest_sha256": stage_manifest["vector_manifest_sha256"],
        "hnsw_binary_manifest_sha256": stage_manifest[
            "hnsw_binary_manifest_sha256"
        ],
        "hnsw_num_threads": 1,
        "hnsw_search_ef": vector_count,
        "hnsw_config_sha256": _config_sha256(vector_count),
    }


def activate_deterministic_snapshot(
    stage_dir: Path,
    target_dir: Path,
    *,
    expected_vector_manifest_sha256: str,
    manifest_reader: ManifestReader | None = None,
    suffix: str | None = None,
) -> dict[str, Any]:
    """离线换入确定性候选；激活后复检失败则自动恢复旧库。"""
    stage_input = Path(stage_dir)
    target_input = Path(target_dir)
    if stage_input.is_symlink() or target_input.is_symlink():
        raise ValueError("stage 和目标快照不能是符号链接")
    stage = stage_input.resolve()
    target = target_input.resolve()
    if stage == target:
        raise ValueError("stage 和目标必须是不同目录")
    if not stage.is_dir() or not target.is_dir():
        raise FileNotFoundError("stage 和当前目标向量库都必须存在")
    if stage.parent != target.parent:
        raise ValueError("stage 必须与目标目录位于同一父目录")
    suffix = suffix or datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    failed_dir = target.with_name(f"{target.name}.failed-{suffix}")
    backup_dir = target.with_name(f"{target.name}.backup-{suffix}")
    if failed_dir.exists() or backup_dir.exists():
        raise FileExistsError("激活备份或失败隔离目录已存在")

    stage_manifest = (
        manifest_reader(stage)
        if manifest_reader else read_vector_snapshot_manifest(stage)
    )
    target_manifest = (
        manifest_reader(target)
        if manifest_reader else read_raw_snapshot_manifest(target)
    )
    if stage_manifest.get("vector_manifest_sha256") != expected_vector_manifest_sha256:
        raise ValueError("stage 向量内容指纹与预期不一致")
    if manifest_reader:
        if (
            target_manifest.get("vector_manifest_sha256")
            != expected_vector_manifest_sha256
        ):
            raise ValueError("当前生产向量内容指纹与预期不一致")
        same_payload = _same_vector_payload(stage_manifest, target_manifest)
    else:
        same_payload = _same_raw_payload(stage_manifest, target_manifest)
    if not same_payload:
        raise ValueError("stage 与当前生产向量内容或 HNSW 结构不一致")
    _require_deterministic_hnsw(stage_manifest)

    backup = activate_staged_vector_store(stage, target, suffix=suffix)
    if backup != backup_dir:
        raise RuntimeError("激活函数返回了非预期备份目录")
    try:
        activated_manifest = (
            manifest_reader(target)
            if manifest_reader else read_vector_snapshot_manifest(target)
        )
        if not _same_vector_payload(stage_manifest, activated_manifest):
            raise ValueError("激活后向量或 HNSW 二进制复检失败")
        _require_deterministic_hnsw(activated_manifest)
    except Exception as exc:
        try:
            target.replace(failed_dir)
            backup_dir.replace(target)
        except Exception as recovery_exc:
            raise RuntimeError(
                "激活后复检失败，且旧库自动恢复失败；请保持目录原状人工恢复"
            ) from recovery_exc
        raise RuntimeError("激活后复检失败，已恢复旧库并隔离失败候选") from exc

    return {
        "target": str(target),
        "backup": str(backup_dir),
        "manifest": activated_manifest,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m eval.deterministic_vector_snapshot",
        description="原子复制并冻结离线评测 HNSW 参数",
    )
    parser.add_argument("--source", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument(
        "--expected-vector-sha256",
        required=True,
        help="来自已审计生产报告的 embedding 向量内容 SHA256",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = build_deterministic_snapshot(
        Path(args.source),
        Path(args.target),
        expected_vector_manifest_sha256=args.expected_vector_sha256,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
