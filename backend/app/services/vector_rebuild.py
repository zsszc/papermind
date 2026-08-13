"""Chroma 向量库的隔离重建、完整性校验与可回滚换入。"""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


def expected_chunk_records(db: Any) -> list[dict[str, Any]]:
    """从 SQLite 生成确定性向量记录，不读旧 Chroma。"""
    from app.models import Chunk, Paper

    papers = {paper.id: paper for paper in db.query(Paper).all()}
    rows = db.query(Chunk).order_by(Chunk.paper_id, Chunk.chunk_index).all()
    records: list[dict[str, Any]] = []
    for row in rows:
        paper = papers.get(row.paper_id)
        if paper is None:
            raise ValueError(f"chunk 引用不存在的 paper: {row.paper_id}")
        metadata: dict[str, Any] = {
            "paper_id": row.paper_id,
            "chunk_index": row.chunk_index,
            "chunk_type": row.chunk_type or "paragraph",
            "title": paper.title or "",
            "authors": paper.authors or "",
        }
        if paper.year is not None:
            metadata["year"] = paper.year
        if row.page_number is not None:
            metadata["page_number"] = row.page_number
        records.append({
            "id": f"p{row.paper_id}_c{row.chunk_index}",
            "document": row.content or "",
            "metadata": metadata,
        })
    return records


def validate_vector_collection(
    collection: Any,
    *,
    expected_ids: set[str],
    expected_dimension: int,
    smoke_embedding: list[float],
) -> dict[str, int]:
    """校验 ID 全等、embedding 维度和最小 query smoke。"""
    payload = collection.get(include=["embeddings"])
    actual_ids = payload.get("ids") or []
    if len(actual_ids) != len(set(actual_ids)) or set(actual_ids) != expected_ids:
        raise ValueError("Chroma ID 集合与 SQLite chunks 不一致")
    embeddings = payload.get("embeddings") or []
    if len(embeddings) != len(actual_ids):
        raise ValueError("Chroma embedding 数量与 ID 数量不一致")
    if any(len(vector) != expected_dimension for vector in embeddings):
        raise ValueError("Chroma embedding 维度不一致")
    if actual_ids:
        result = collection.query(
            query_embeddings=[smoke_embedding],
            n_results=1,
            include=["distances"],
        )
        returned = result.get("ids") or []
        if not returned or not returned[0]:
            raise ValueError("Chroma query smoke 未返回结果")
    return {"count": len(actual_ids), "dimension": expected_dimension}


def build_staged_vector_store(
    db: Any,
    stage_dir: Path,
    *,
    embedder: Any,
    client_factory: Callable[[Path], Any] | None = None,
    batch_size: int = 16,
) -> dict[str, int]:
    """在全新目录从 SQLite 重建 Chroma，完成校验后才返回。"""
    stage_dir = Path(stage_dir)
    if stage_dir.exists():
        raise FileExistsError(f"临时向量库已存在: {stage_dir}")
    if batch_size <= 0:
        raise ValueError("batch_size 必须大于 0")
    records = expected_chunk_records(db)
    stage_dir.mkdir(parents=True)
    try:
        if client_factory is None:
            import chromadb
            from chromadb.config import Settings

            client = chromadb.PersistentClient(
                path=str(stage_dir),
                settings=Settings(anonymized_telemetry=False),
            )
        else:
            client = client_factory(stage_dir)
        collection = client.get_or_create_collection(
            name="papers", metadata={"hnsw:space": "cosine"}
        )
        dimension: int | None = None
        smoke_embedding: list[float] = []
        for start in range(0, len(records), batch_size):
            batch = records[start:start + batch_size]
            documents = [item["document"] for item in batch]
            embeddings = embedder.embed(documents)
            if len(embeddings) != len(batch):
                raise ValueError("Embedding 输出数量与 chunk 数量不一致")
            for vector in embeddings:
                if dimension is None:
                    dimension = len(vector)
                    smoke_embedding = list(vector)
                elif len(vector) != dimension:
                    raise ValueError("Embedding 输出维度不一致")
            collection.upsert(
                ids=[item["id"] for item in batch],
                documents=documents,
                metadatas=[item["metadata"] for item in batch],
                embeddings=embeddings,
            )
        if records and not dimension:
            raise ValueError("Embedding 维度为空")
        return validate_vector_collection(
            collection,
            expected_ids={item["id"] for item in records},
            expected_dimension=dimension or 0,
            smoke_embedding=smoke_embedding,
        )
    except Exception:
        shutil.rmtree(stage_dir, ignore_errors=True)
        raise


def activate_staged_vector_store(
    stage_dir: Path,
    target_dir: Path,
    *,
    suffix: str | None = None,
) -> Path | None:
    """将已校验新库换入；第二次 rename 失败时恢复旧库。"""
    stage_dir = Path(stage_dir)
    target_dir = Path(target_dir)
    if not stage_dir.is_dir():
        raise FileNotFoundError(f"临时向量库不存在: {stage_dir}")
    if stage_dir.parent != target_dir.parent:
        raise ValueError("临时向量库必须与目标目录位于同一父目录")
    backup_dir: Path | None = None
    if target_dir.exists():
        suffix = suffix or datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        backup_dir = target_dir.with_name(f"{target_dir.name}.backup-{suffix}")
        if backup_dir.exists():
            raise FileExistsError(f"向量库备份目录已存在: {backup_dir}")
        target_dir.replace(backup_dir)
    try:
        stage_dir.replace(target_dir)
    except Exception:
        if backup_dir is not None and backup_dir.exists() and not target_dir.exists():
            backup_dir.replace(target_dir)
        raise
    return backup_dir


def build_parser():
    """构建管理 CLI；是否换入必须显式选择。"""
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m app.services.vector_rebuild",
        description="从 SQLite chunks 隔离重建并校验 Chroma 向量库",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--stage-only", dest="activate", action="store_false",
        help="只生成并校验临时新库，不换入",
    )
    mode.add_argument(
        "--activate", dest="activate", action="store_true",
        help="校验成功后保留旧库备份并换入新库",
    )
    parser.add_argument("--target", default=None, help="目标 vector_db 目录")
    parser.add_argument("--stage", default=None, help="临时新库目录")
    parser.add_argument("--batch-size", type=int, default=16)
    return parser


def _resolve_cli_paths(
    target_value: str | None, stage_value: str | None
) -> tuple[Path, Path]:
    """先规范化 CLI 路径，再执行同父目录安全检查。"""
    from app.core.config import config

    target = (
        Path(target_value).expanduser().resolve()
        if target_value
        else (config.runtime_root / "vector_db").resolve()
    )
    stage = (
        Path(stage_value).expanduser().resolve()
        if stage_value
        else target.with_name(
            f".{target.name}.stage-{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
        )
    )
    if stage.parent != target.parent:
        raise ValueError("临时向量库必须与目标目录同父目录")
    return target, stage


def main(argv: list[str] | None = None) -> int:
    """管理命令入口；默认不存在隐式换入路径。"""
    import json

    from app.database import SessionLocal
    from app.services.embedding import EmbeddingService

    args = build_parser().parse_args(argv)
    target, stage = _resolve_cli_paths(args.target, args.stage)
    with SessionLocal() as db:
        result = build_staged_vector_store(
            db,
            stage,
            embedder=EmbeddingService(),
            batch_size=args.batch_size,
        )
    payload: dict[str, Any] = {**result, "stage": str(stage), "activated": False}
    if args.activate:
        backup = activate_staged_vector_store(stage, target)
        payload.update({
            "activated": True,
            "target": str(target),
            "backup": str(backup) if backup else None,
        })
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
