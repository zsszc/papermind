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


def test_delete_vectors_invalidates_semantic_cache_even_when_chroma_fails():
    cache.set("semantic_search:old-query", ["已删除论文"])
    cache.set("other:stable", "保留")
    store = _store()
    store.collection.delete.side_effect = RuntimeError("模拟 ChromaDB 删除失败")

    store.delete_by_paper_id(1)

    assert cache.get("semantic_search:old-query") is None
    assert cache.get("other:stable") == "保留"
