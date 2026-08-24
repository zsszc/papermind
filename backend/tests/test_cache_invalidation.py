"""向量库变更后的语义检索缓存失效测试。"""

from unittest.mock import MagicMock

import pytest

from app.services.cache import cache
from app.services.retrieval import VectorStore


@pytest.fixture(autouse=True)
def _clear_global_cache():
    cache._store.clear()
    yield
    cache._store.clear()


def _store() -> VectorStore:
    store = VectorStore.__new__(VectorStore)
    store.collection = MagicMock()
    store.embedding_service = MagicMock()
    store.embedding_service.embed.return_value = [[0.1, 0.2]]
    return store


def test_add_chunks_invalidates_only_semantic_search_cache():
    cache.set("semantic_search:old-query", ["旧结果"])
    cache.set("other:stable", "保留")
    store = _store()

    store.add_chunks(1, [{"content": "新文献内容"}])

    assert cache.get("semantic_search:old-query") is None
    assert cache.get("other:stable") == "保留"
    store.collection.upsert.assert_called_once()
    store.collection.add.assert_not_called()


def test_delete_vectors_invalidates_semantic_cache_even_when_chroma_fails():
    cache.set("semantic_search:old-query", ["已删除论文"])
    cache.set("other:stable", "保留")
    store = _store()
    store.collection.delete.side_effect = RuntimeError("模拟 ChromaDB 删除失败")

    store.delete_by_paper_id(1)

    assert cache.get("semantic_search:old-query") is None
    assert cache.get("other:stable") == "保留"


def test_semantic_cache_hit_returns_an_isolated_copy(tmp_path):
    """调用方改写首次结果，不得污染缓存内值和后续调用。

    search 路由与共享 hybrid pipeline 都会补写 ``source`` 等展示字段；若缓存
    直接返回同一列表/字典引用，一次请求即可污染随后 60 秒内的所有请求。
    """
    store = VectorStore.__new__(VectorStore)
    store.vector_dir = tmp_path / "vector-snapshot"
    store.embedding_service = MagicMock()
    store.embedding_service.embed_query.return_value = [0.1, 0.2]
    store.collection = MagicMock()
    store.collection.query.return_value = {
        "ids": [["p1_c0"]],
        "documents": [["original evidence"]],
        "metadatas": [[{
            "paper_id": 1,
            "title": "paper",
            "authors": "author",
            "year": 2024,
            "page_number": 1,
            "chunk_type": "result",
        }]],
        "distances": [[0.1]],
    }

    first = store.search("same query", top_k=5, rerank=False)
    first[0]["content"] = "poisoned by caller"
    first[0]["source"] = "hybrid"
    second = store.search("same query", top_k=5, rerank=False)

    assert store.collection.query.call_count == 1, "第二次应命中 60 秒缓存"
    assert second is not first
    assert second[0] is not first[0]
    assert second[0]["content"] == "original evidence"
    assert second[0]["source"] == "semantic"
