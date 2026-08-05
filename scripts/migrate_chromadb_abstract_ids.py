"""一次性数据迁移：ChromaDB 摘要 chunk id 对齐 + 孤儿向量清理。

背景：
1. 初版摘要 chunk 的 ChromaDB id 为 p{pid}_abstract，破坏 id 不变式（eval 命中不计）；
   代码已修为 p{pid}_c-1，本脚本迁移存量数据。
2. 已删除论文（p26/p45/p49）的 110 个孤儿向量仍存 ChromaDB，污染检索 top-k。

运行：cd backend && env -u PYTHONPATH venv/bin/python ../scripts/migrate_chromadb_abstract_ids.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.database import SessionLocal
from app.models import Chunk, Paper
from app.services.retrieval import get_vector_store


def main():
    store = get_vector_store()
    coll = store.collection
    db = SessionLocal()

    # 1) 孤儿向量清理
    db_pids = {p.id for p in db.query(Paper).all()}
    chroma_pids = {m["paper_id"] for m in coll.get(include=["metadatas"])["metadatas"]}
    orphans = sorted(chroma_pids - db_pids)
    for pid in orphans:
        coll.delete(where={"paper_id": pid})
        print(f"[migrate] 已清理孤儿向量 p{pid}")

    # 2) 摘要 chunk id 迁移：p{pid}_abstract -> p{pid}_c-1
    rows = db.query(Chunk).filter(Chunk.chunk_index == -1).all()
    for row in rows:
        old_id = f"p{row.paper_id}_abstract"
        new_id = f"p{row.paper_id}_c-1"
        existing = coll.get(ids=[old_id])
        if not existing["ids"]:
            print(f"[migrate] p{row.paper_id} 无旧 id，跳过")
            continue
        emb = coll.get(ids=[old_id], include=["embeddings", "metadatas"])
        coll.delete(ids=[old_id])
        coll.add(
            ids=[new_id],
            embeddings=emb["embeddings"],
            documents=[row.content],
            metadatas=[{"paper_id": row.paper_id, "chunk_index": -1, "chunk_type": "abstract"}],
        )
        print(f"[migrate] p{row.paper_id}: {old_id} -> {new_id}")

    # 验证
    rest = coll.get(include=["metadatas"])
    bad = [i for i in rest["ids"] if i.endswith("_abstract")]
    print(f"[migrate] 完成。残留 _abstract id: {len(bad)}；孤儿 paper 已清: {orphans}")


if __name__ == "__main__":
    main()
