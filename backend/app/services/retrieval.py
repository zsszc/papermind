import os
import threading
from pathlib import Path
from typing import List, Dict, Any, Optional

import chromadb
from chromadb.config import Settings

from app.core.config import config
from app.core.logger import logger
from app.services.embedding import EmbeddingService
from app.services.cache import cache


class VectorStore:
    def __init__(self):
        project_root = Path(__file__).resolve().parents[3]
        self.vector_dir = project_root / "vector_db"
        self.vector_dir.mkdir(parents=True, exist_ok=True)

        self.client = chromadb.PersistentClient(
            path=str(self.vector_dir),
            settings=Settings(anonymized_telemetry=False),
        )
        self.collection = self.client.get_or_create_collection(
            name="papers",
            metadata={"hnsw:space": "cosine"},
        )
        self.embedding_service = EmbeddingService()

    def available(self) -> bool:
        return self.embedding_service.available()

    def add_chunks(
        self,
        paper_id: int,
        chunks: List[Dict[str, Any]],
        paper_metadata: Optional[Dict[str, Any]] = None,
    ):
        if not chunks:
            return

        paper_metadata = paper_metadata or {}
        title = paper_metadata.get("title", "")
        authors = paper_metadata.get("authors", "")
        year = paper_metadata.get("year")

        ids = []
        documents = []
        metadatas = []
        for i, chunk in enumerate(chunks):
            cid = f"p{paper_id}_c{i}"
            ids.append(cid)
            documents.append(chunk["content"])
            meta = {
                "paper_id": paper_id,
                "chunk_index": i,
                "chunk_type": chunk.get("chunk_type", "paragraph"),
            }
            if title is not None:
                meta["title"] = title
            if authors is not None:
                meta["authors"] = authors
            if year is not None:
                meta["year"] = year
            if chunk.get("page_number") is not None:
                meta["page_number"] = chunk["page_number"]
            metadatas.append(meta)

        embeddings = self.embedding_service.embed(documents)
        self.collection.add(
            ids=ids,
            documents=documents,
            metadatas=metadatas,
            embeddings=embeddings,
        )

    def search(
        self,
        query: str,
        top_k: int = 10,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        # 缓存语义检索结果，减少 Embedding 计算与 ChromaDB 查询
        cache_key = f"semantic_search:{hash(query)}:{top_k}:{hash(str(sorted((filters or {}).items())))}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        query_embedding = self.embedding_service.embed_query(query)
        n_results = max(top_k * 2, 20)

        where = self._build_where(filters)
        results = self._query_with_fallback(
            self.collection, query_embedding, n_results, where
        )

        output = []
        ids = results.get("ids", [[]])[0]
        docs = results.get("documents", [[]])[0]
        metas = results.get("metadatas", [[]])[0]
        distances = results.get("distances", [[]])[0]

        for i in range(len(ids)):
            score = 1.0 - float(distances[i]) if distances else 0.0
            output.append({
                "chunk_id": ids[i],
                "paper_id": metas[i].get("paper_id"),
                "title": metas[i].get("title"),
                "authors": metas[i].get("authors"),
                "year": metas[i].get("year"),
                "content": docs[i],
                "page_number": metas[i].get("page_number"),
                "chunk_type": metas[i].get("chunk_type"),
                "score": score,
                "source": "semantic",
            })

        output = output[:top_k]
        cache.set(cache_key, output, ttl=60)
        return output

    def delete_by_paper_id(self, paper_id: int):
        try:
            self.collection.delete(where={"paper_id": paper_id})
        except Exception:
            logger.warning(f"[VectorStore] 删除 paper {paper_id} 向量失败", exc_info=True)

    @staticmethod
    def _query_with_fallback(collection, query_embedding, n_results, where):
        """带兜底的向量查询：where 子句被 ChromaDB 拒绝时降级为无过滤检索。

        防 500 契约：过滤条件异常不得冒泡为接口错误，退化为无过滤结果并记日志。
        """
        kwargs = dict(
            query_embeddings=[query_embedding],
            n_results=n_results,
            include=["documents", "metadatas", "distances"],
        )
        try:
            return collection.query(where=where, **kwargs)
        except ValueError:
            logger.warning(f"[VectorStore] where 子句被拒绝，降级为无过滤检索: {where}")
            return collection.query(where=None, **kwargs)

    @staticmethod
    def _build_where(filters: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """把业务过滤条件翻译为 ChromaDB where 子句。

        ChromaDB 0.4.24 只接受「单字段单操作符」或「$and/$or 组合」：
        多条件必须包装为 $and，否则 query 抛 ValueError。
        """
        if not filters:
            return None
        conditions = []
        if "year_gte" in filters:
            conditions.append({"year": {"$gte": filters["year_gte"]}})
        if "year_lte" in filters:
            conditions.append({"year": {"$lte": filters["year_lte"]}})
        if "paper_id" in filters:
            conditions.append({"paper_id": filters["paper_id"]})
        if not conditions:
            return None
        if len(conditions) == 1:
            return conditions[0]
        return {"$and": conditions}


_vector_store_instance: Optional[VectorStore] = None
_vector_store_lock = threading.Lock()


def get_vector_store() -> VectorStore:
    """获取全局单例 VectorStore，避免重复初始化 ChromaDB。"""
    global _vector_store_instance
    if _vector_store_instance is None:
        with _vector_store_lock:
            if _vector_store_instance is None:
                _vector_store_instance = VectorStore()
    return _vector_store_instance
