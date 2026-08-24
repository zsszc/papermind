"""为离线评测原子构建确定性 HNSW 快照副本。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import uuid


def _config_sha256(vector_count: int) -> str:
    payload = {
        "version": "deterministic-hnsw-v1",
        "hnsw_num_threads": 1,
        "hnsw_search_ef": vector_count,
        "vector_count": vector_count,
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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


def build_deterministic_snapshot(source: Path, target: Path) -> dict[str, object]:
    """复制 Chroma 快照并冻结 HNSW 查询线程和搜索深度。

    目标必须不存在，所有修改只发生在唯一 stage 目录；SQLite 提交成功后才
    以原子 rename 激活，源快照始终只读。
    """
    source = Path(source).resolve()
    target = Path(target).resolve()
    if not source.is_dir() or not (source / "chroma.sqlite3").is_file():
        raise FileNotFoundError(f"源 Chroma 快照不存在: {source}")
    if target.exists():
        raise FileExistsError(f"目标快照已存在: {target}")
    if target.is_relative_to(source):
        raise ValueError("目标不能位于源快照内部")

    target.parent.mkdir(parents=True, exist_ok=True)
    stage = target.parent / f".{target.name}.stage-{uuid.uuid4().hex}"
    try:
        shutil.copytree(source, stage)
        connection = sqlite3.connect(stage / "chroma.sqlite3")
        try:
            connection.execute("BEGIN IMMEDIATE")
            collection_row = connection.execute(
                "SELECT id FROM collections WHERE name = 'papers'"
            ).fetchone()
            if collection_row is None:
                raise ValueError("源快照缺少 papers collection")
            collection_id = str(collection_row[0])
            segment_rows = connection.execute(
                "SELECT id FROM segments WHERE collection = ? AND scope = 'VECTOR'",
                (collection_id,),
            ).fetchall()
            if len(segment_rows) != 1:
                raise ValueError("源快照必须恰好包含一个 VECTOR segment")
            segment_id = str(segment_rows[0][0])
            vector_count = int(
                connection.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
            )
            if vector_count <= 0:
                raise ValueError("源快照没有向量")

            for table, id_column, owner_id in (
                ("collection_metadata", "collection_id", collection_id),
                ("segment_metadata", "segment_id", segment_id),
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
        "hnsw_num_threads": 1,
        "hnsw_search_ef": vector_count,
        "hnsw_config_sha256": _config_sha256(vector_count),
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
