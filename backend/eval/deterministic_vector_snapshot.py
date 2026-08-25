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


def _hnsw_binary_manifest(snapshot_dir: Path, segment_id: str) -> str:
    segment_dir = snapshot_dir / segment_id
    if not segment_dir.is_dir():
        raise ValueError(f"HNSW 二进制目录不存在: {segment_id}")
    files = sorted(path for path in segment_dir.rglob("*") if path.is_file())
    if not files:
        raise ValueError("HNSW 二进制目录为空")
    digest = hashlib.sha256()
    for path in files:
        relative = path.relative_to(segment_dir).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(relative)
        digest.update(b"\0")
        digest.update(len(content).to_bytes(8, "little"))
        digest.update(content)
    return digest.hexdigest()


def read_vector_snapshot_manifest(snapshot_dir: Path) -> dict[str, Any]:
    """用全新 Chroma client 校验 ID、维度、查询并生成双指纹。"""
    import chromadb
    from chromadb.config import Settings

    snapshot_dir = Path(snapshot_dir).resolve()
    audit = audit_hnsw_sqlite_metadata(snapshot_dir)
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
    if len(vector_ids) != audit["vector_count"] or len(embeddings) != len(vector_ids):
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
    return {
        **audit,
        "embedding_dimension": next(iter(dimensions)),
        "vector_manifest_sha256": digest.hexdigest(),
        "hnsw_binary_manifest_sha256": _hnsw_binary_manifest(
            snapshot_dir, audit["vector_segment_id"]
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

    reader = manifest_reader or read_vector_snapshot_manifest
    source_before = reader(source)
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

        source_after = reader(source)
        stage_manifest = reader(stage)
        if not _same_vector_payload(source_before, source_after):
            raise ValueError("复制期间源快照发生变化，拒绝生成候选")
        if not _same_vector_payload(source_before, stage_manifest):
            raise ValueError("候选向量内容指纹或 HNSW 二进制指纹发生变化")
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

    reader = manifest_reader or read_vector_snapshot_manifest
    stage_manifest = reader(stage)
    target_manifest = reader(target)
    if stage_manifest.get("vector_manifest_sha256") != expected_vector_manifest_sha256:
        raise ValueError("stage 向量内容指纹与预期不一致")
    if target_manifest.get("vector_manifest_sha256") != expected_vector_manifest_sha256:
        raise ValueError("当前生产向量内容指纹与预期不一致")
    if not _same_vector_payload(stage_manifest, target_manifest):
        raise ValueError("stage 与当前生产向量内容或 HNSW 二进制不一致")
    _require_deterministic_hnsw(stage_manifest)

    backup = activate_staged_vector_store(stage, target, suffix=suffix)
    if backup != backup_dir:
        raise RuntimeError("激活函数返回了非预期备份目录")
    try:
        activated_manifest = reader(target)
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = build_deterministic_snapshot(Path(args.source), Path(args.target))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
