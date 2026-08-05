import threading
from typing import List, Dict, Any, Optional

import chromadb
from chromadb.config import Settings

from app.core.config import config
from app.core.logger import logger
from app.services.embedding import EmbeddingService
from app.services.cache import cache
from app.services.reranker import RerankerService

# 重排候选池大小：RRF/语义召回的前 N 个候选进入 Reranker 精排（spec §3.1）
_RERANK_POOL_SIZE = 20


class VectorStore:
    def __init__(self):
        self.vector_dir = config.runtime_root / "vector_db"
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
            cid = chunk.get("id") or f"p{paper_id}_c{i}"
            ids.append(cid)
            documents.append(chunk["content"])
            meta = {
                "paper_id": paper_id,
                "chunk_index": chunk.get("chunk_index", i),
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
        cache.delete_prefix("semantic_search:")

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

        # B1：rerank 开启时对前 _RERANK_POOL_SIZE 个候选精排；reranker 不可用/打分失败
        # 时降级为原始排序（不抛异常），详见 _apply_rerank
        if self._rerank_enabled():
            output = self._apply_rerank(query, output)

        output = output[:top_k]
        cache.set(cache_key, output, ttl=60)
        return output

    @staticmethod
    def _rerank_enabled() -> bool:
        """retrieval.rerank 开关（默认 false）：每次调用时读取，配置重载即生效。"""
        return bool(config.get("retrieval.rerank", False))

    @staticmethod
    def _apply_rerank(query: str, output: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """对前 _RERANK_POOL_SIZE 个候选计算 (query, chunk) 相关性分数并重排。

        降级契约（不抛异常、不 500）：
        - 无候选：直接返回；
        - reranker 模型不可用：记 [reranker] warning，返回原始排序；
        - 打分异常或分数数与候选数不一致：记 [reranker] warning，返回原始排序。
        池外候选（第 21 个起）保持原始顺序追加在重排池之后。
        """
        if not output:
            return output
        reranker = RerankerService()
        if not reranker.available():
            logger.warning("[reranker] 模型不可用，跳过重排，返回原始排序")
            return output
        pool = output[:_RERANK_POOL_SIZE]
        pairs = [(query, item["content"]) for item in pool]
        try:
            scores = reranker._score(pairs)
        except Exception as e:
            logger.warning(f"[reranker] 重排打分失败，返回原始排序: {e}")
            return output
        if len(scores) != len(pool):
            logger.warning(
                f"[reranker] 重排分数数({len(scores)})与候选数({len(pool)})不一致，返回原始排序"
            )
            return output
        # sorted 稳定：同分候选保持原始相对顺序
        reranked = [
            item
            for _, item in sorted(zip(scores, pool), key=lambda x: x[0], reverse=True)
        ]
        return reranked + output[_RERANK_POOL_SIZE:]

    def delete_by_paper_id(self, paper_id: int):
        try:
            self.collection.delete(where={"paper_id": paper_id})
        except Exception:
            logger.warning(f"[VectorStore] 删除 paper {paper_id} 向量失败", exc_info=True)
        finally:
            cache.delete_prefix("semantic_search:")

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
