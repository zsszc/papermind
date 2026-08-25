"""VectorStore 初始化不得覆盖已有 Chroma collection metadata。"""

from types import SimpleNamespace

import pytest

from app.services import retrieval


class _Client:
    def __init__(self, *, existing):
        self.existing = existing
        self.collection = SimpleNamespace(metadata={
            "hnsw:space": "cosine",
            "hnsw:num_threads": 1,
            "hnsw:search_ef": 464,
        })
        self.get_calls = []
        self.create_calls = []

    def get_collection(self, name):
        self.get_calls.append(name)
        if not self.existing:
            raise ValueError(f"Collection {name} does not exist.")
        return self.collection

    def create_collection(self, **kwargs):
        self.create_calls.append(kwargs)
        return self.collection


@pytest.mark.parametrize("existing", [True, False])
def test_vector_store_preserves_existing_metadata_and_only_configures_new_collection(
    monkeypatch, existing
):
    client = _Client(existing=existing)
    monkeypatch.setattr(retrieval.chromadb, "PersistentClient", lambda **kwargs: client)
    monkeypatch.setattr(retrieval, "EmbeddingService", lambda: object())

    store = retrieval.VectorStore()

    assert store.collection is client.collection
    assert client.get_calls == ["papers"]
    if existing:
        assert client.create_calls == []
        assert store.collection.metadata["hnsw:num_threads"] == 1
        assert store.collection.metadata["hnsw:search_ef"] == 464
    else:
        assert client.create_calls == [{
            "name": "papers",
            "metadata": {"hnsw:space": "cosine"},
        }]


def test_vector_store_does_not_hide_unrelated_collection_errors(monkeypatch):
    class BrokenClient(_Client):
        def get_collection(self, name):
            raise ValueError("database schema is corrupted")

    client = BrokenClient(existing=False)
    monkeypatch.setattr(retrieval.chromadb, "PersistentClient", lambda **kwargs: client)
    monkeypatch.setattr(retrieval, "EmbeddingService", lambda: object())

    with pytest.raises(ValueError, match="corrupted"):
        retrieval.VectorStore()
    assert client.create_calls == []
