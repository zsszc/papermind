"""B1：RerankerService + VectorStore.search() 重排集成契约测试（Phase B / T1）。

契约（specs/phases/phase-b-retrieval/spec.md §3.1）：
1. retrieval.rerank=true 且模型可用：召回候选（前 20 个）经 RerankerService._score
   重排后截断 top_k；候选不足 20 个时对现有全部候选重排；
2. rerank=true 但模型不可用 / 打分失败：回退原始排序（不抛异常），记 [reranker] warning；
3. rerank=false（默认）：行为与现状完全一致（特征化），完全不触碰 reranker。

测试策略：
- VectorStore 经 ``__new__`` 绕过 __init__ 构造，注入假 collection 与假
  embedding_service，避免加载真实模型 / 访问真实 vector_db；
- RerankerService 单例每个用例前后重置，防止跨用例状态污染；
- 语义检索 60 秒缓存每个用例前后清空。
"""

import logging

import pytest

from app.core.config import config
from app.services.cache import cache
from app.services.reranker import RerankerService
from app.services.retrieval import VectorStore

QUERY = "T staging prediction"


# ---------- 测试替身 ----------


class _FakeCollection:
    """假 ChromaDB collection：返回 n 个确定性候选，原始名次 = 序号（c0 最相似）。"""

    def __init__(self, n: int):
        self._n = n

    def query(self, query_embeddings, n_results, include, where=None):
        n = min(self._n, n_results)
        return {
            "ids": [[f"p1_c{i}" for i in range(n)]],
            "documents": [[f"chunk content {i}" for i in range(n)]],
            "metadatas": [
                [
                    {
                        "paper_id": 1,
                        "chunk_index": i,
                        "chunk_type": "paragraph",
                        "title": "Paper T",
                        "authors": "Zhang San",
                        "year": 2024,
                        "page_number": 1,
                    }
                    for i in range(n)
                ]
            ],
            # 距离递增：c0 距离最小（cosine 最相似），原始排序即序号序
            "distances": [[0.10 + 0.01 * i for i in range(n)]],
        }


class _FakeEmbedding:
    def embed_query(self, query):
        return [0.1, 0.2, 0.3]


def _make_store(n_candidates: int) -> VectorStore:
    """绕过 __init__ 构造 VectorStore，避免真实 ChromaDB / Embedding 模型。"""
    store = VectorStore.__new__(VectorStore)
    store.collection = _FakeCollection(n_candidates)
    store.embedding_service = _FakeEmbedding()
    return store


def _score_table(scores_by_index):
    """生成按 content 查表的假 _score：scores_by_index 为 {候选序号: 重排分}。"""

    def _fake(self, pairs):
        return [scores_by_index[int(content.rsplit(" ", 1)[1])] for (_, content) in pairs]

    return _fake


# ---------- 共享夹具 ----------


@pytest.fixture(autouse=True)
def _reset_state():
    """每个用例前后：清空检索缓存 + 重置 RerankerService 单例（防状态污染）。"""
    cache._store.clear()
    RerankerService._instance = None
    RerankerService._model = None
    RerankerService._failed = False
    RerankerService._error = None
    yield
    cache._store.clear()
    RerankerService._instance = None
    RerankerService._model = None
    RerankerService._failed = False
    RerankerService._error = None


@pytest.fixture()
def rerank_on(monkeypatch):
    monkeypatch.setattr(
        config,
        "_config",
        {"retrieval": {"rerank": True, "rerank_model": "BAAI/test-reranker"}},
    )


@pytest.fixture()
def rerank_off(monkeypatch):
    monkeypatch.setattr(config, "_config", {"retrieval": {"rerank": False}})


# ---------- RerankerService 本体契约 ----------


class TestRerankerService:
    def test_model_name_read_from_config(self, monkeypatch):
        """模型名从 retrieval.rerank_model 读取，不硬编码。"""
        monkeypatch.setattr(
            config, "_config", {"retrieval": {"rerank_model": "BAAI/custom-reranker"}}
        )
        assert RerankerService().model_name == "BAAI/custom-reranker"

    def test_model_name_default_when_config_missing(self, monkeypatch):
        """配置缺失时默认 BAAI/bge-reranker-v2-m3（spec §3.1）。"""
        monkeypatch.setattr(config, "_config", {})
        assert RerankerService().model_name == "BAAI/bge-reranker-v2-m3"

    def test_singleton(self, monkeypatch):
        """单例：多次实例化返回同一对象。"""
        monkeypatch.setattr(config, "_config", {})
        assert RerankerService() is RerankerService()

    def test_load_failure_latches_and_does_not_retry(self, monkeypatch):
        """模型加载失败：available() 为 False 且进程内锁存，不重试。"""
        monkeypatch.setattr(config, "_config", {})
        calls = {"n": 0}

        def _raiser(*args, **kwargs):
            calls["n"] += 1
            raise RuntimeError("download failed")

        monkeypatch.setattr("sentence_transformers.CrossEncoder", _raiser)

        svc = RerankerService()
        assert svc.available() is False
        assert svc.available() is False
        assert calls["n"] == 1  # 失败锁存：只尝试加载一次

    def test_score_empty_pairs_short_circuits(self, monkeypatch):
        """空 pairs：直接返回 []，不触发模型加载。"""
        monkeypatch.setattr(config, "_config", {})

        def _raiser(*args, **kwargs):
            raise AssertionError("空 pairs 不应触发模型加载")

        monkeypatch.setattr("sentence_transformers.CrossEncoder", _raiser)
        assert RerankerService()._score([]) == []

    def test_score_delegates_to_cross_encoder_predict(self, monkeypatch):
        """_score 是可 mock 的测试钩子：委托 CrossEncoder.predict，返回 float 列表。"""

        class _FakeCrossEncoder:
            received = None

            def predict(self, pairs):
                _FakeCrossEncoder.received = list(pairs)
                return [0.9, 0.1]

        monkeypatch.setattr(config, "_config", {})
        svc = RerankerService()
        svc._model = _FakeCrossEncoder()

        scores = svc._score([("q", "passage a"), ("q", "passage b")])

        assert scores == [0.9, 0.1]
        assert all(isinstance(s, float) for s in scores)
        assert _FakeCrossEncoder.received == [("q", "passage a"), ("q", "passage b")]

    def test_score_raises_when_model_unavailable(self, monkeypatch):
        """模型不可用且被直接调用 _score：抛 RuntimeError（由调用方负责降级）。"""
        monkeypatch.setattr(config, "_config", {})
        svc = RerankerService()
        svc._failed = True
        svc._error = "boom"
        with pytest.raises(RuntimeError):
            svc._score([("q", "a")])


# ---------- VectorStore.search() 重排集成契约 ----------


class TestVectorStoreRerank:
    def test_rerank_reorders_candidates_and_truncates_top_k(
        self, rerank_on, monkeypatch
    ):
        """rerank=true 且模型可用：候选经 _score 重排后截断 top_k。

        5 个候选（不足 20 个 → 全部参与重排）；重排分使 c3 > c4 > c2 > c1 > c0，
        top_k=2 应返回 [c3, c4]，且保留原 cosine score 字段。
        """
        store = _make_store(5)
        captured = {"calls": []}

        base = _score_table({0: 0.1, 1: 0.2, 2: 0.3, 3: 0.95, 4: 0.9})

        def _recording_score(self, pairs):
            captured["calls"].append(list(pairs))
            return base(self, pairs)

        monkeypatch.setattr(RerankerService, "available", lambda self: True)
        monkeypatch.setattr(RerankerService, "_score", _recording_score)

        out = store.search(QUERY, top_k=2)

        assert [r["chunk_id"] for r in out] == ["p1_c3", "p1_c4"]
        # _score 恰好调用一次，且收到全部 5 个候选的 (query, content) 对
        assert len(captured["calls"]) == 1
        pairs = captured["calls"][0]
        assert pairs == [(QUERY, f"chunk content {i}") for i in range(5)]
        # 原 cosine score 字段保留（1 - distance）
        assert out[0]["score"] == pytest.approx(1.0 - 0.13)
        assert out[1]["score"] == pytest.approx(1.0 - 0.14)

    def test_rerank_pool_limited_to_20_candidates(self, rerank_on, monkeypatch):
        """重排候选数固定为 20：25 个召回候选只有前 20 个进入 _score。"""
        store = _make_store(25)
        captured = {"pairs": None}
        base = _score_table({i: 1.0 - 0.01 * i for i in range(25)})

        def _recording_score(self, pairs):
            captured["pairs"] = list(pairs)
            return base(self, pairs)

        monkeypatch.setattr(RerankerService, "available", lambda self: True)
        monkeypatch.setattr(RerankerService, "_score", _recording_score)

        out = store.search(QUERY, top_k=20)

        assert captured["pairs"] == [(QUERY, f"chunk content {i}") for i in range(20)]
        assert len(out) == 20

    def test_rerank_preserves_tail_beyond_pool(self, rerank_on, monkeypatch):
        """top_k 超过重排池（20）时：池内重排、池外候选项保持原始顺序追加。"""
        store = _make_store(25)
        base = _score_table({i: 0.01 * i for i in range(25)})  # 池内逆序

        monkeypatch.setattr(RerankerService, "available", lambda self: True)
        monkeypatch.setattr(RerankerService, "_score", base)

        out = store.search(QUERY, top_k=23)

        # 池内 20 个按重排分逆序：c19, c18, ..., c0
        assert [r["chunk_id"] for r in out[:20]] == [f"p1_c{i}" for i in range(19, -1, -1)]
        # 池外 3 个保持原始顺序：c20, c21, c22
        assert [r["chunk_id"] for r in out[20:]] == ["p1_c20", "p1_c21", "p1_c22"]

    def test_rerank_unavailable_falls_back_with_warning(
        self, rerank_on, monkeypatch, caplog
    ):
        """rerank=true 但模型不可用：回退原排序（不抛异常），记 [reranker] warning，且不调用 _score。"""
        store = _make_store(5)
        score_calls = []

        monkeypatch.setattr(RerankerService, "available", lambda self: False)

        def _forbidden_score(self, pairs):
            score_calls.append(pairs)
            return []

        monkeypatch.setattr(RerankerService, "_score", _forbidden_score)

        with caplog.at_level(logging.WARNING, logger="papermind"):
            out = store.search(QUERY, top_k=3)

        assert [r["chunk_id"] for r in out] == ["p1_c0", "p1_c1", "p1_c2"]
        assert score_calls == []
        assert any(
            "[reranker]" in r.message and r.levelno == logging.WARNING
            for r in caplog.records
        )

    def test_rerank_score_error_falls_back_with_warning(
        self, rerank_on, monkeypatch, caplog
    ):
        """_score 打分抛异常：回退原排序（不向外抛），记 [reranker] warning。"""
        store = _make_store(5)

        def _boom(self, pairs):
            raise RuntimeError("predict failed")

        monkeypatch.setattr(RerankerService, "available", lambda self: True)
        monkeypatch.setattr(RerankerService, "_score", _boom)

        with caplog.at_level(logging.WARNING, logger="papermind"):
            out = store.search(QUERY, top_k=3)

        assert [r["chunk_id"] for r in out] == ["p1_c0", "p1_c1", "p1_c2"]
        assert any("[reranker]" in r.message for r in caplog.records)

    def test_rerank_score_length_mismatch_falls_back(
        self, rerank_on, monkeypatch, caplog
    ):
        """_score 返回分数数与候选数不一致：防御性回退原排序并记 warning。"""
        store = _make_store(5)

        monkeypatch.setattr(RerankerService, "available", lambda self: True)
        monkeypatch.setattr(RerankerService, "_score", lambda self, pairs: [0.5])

        with caplog.at_level(logging.WARNING, logger="papermind"):
            out = store.search(QUERY, top_k=3)

        assert [r["chunk_id"] for r in out] == ["p1_c0", "p1_c1", "p1_c2"]
        assert any("[reranker]" in r.message for r in caplog.records)

    def test_rerank_off_never_touches_reranker(self, rerank_off, monkeypatch):
        """rerank=false：完全不实例化/调用 reranker（特征化）。"""
        store = _make_store(5)

        def _forbidden():
            raise AssertionError("rerank=false 时不应触碰 RerankerService")

        monkeypatch.setattr("app.services.retrieval.RerankerService", _forbidden)

        out = store.search(QUERY, top_k=3)

        assert [r["chunk_id"] for r in out] == ["p1_c0", "p1_c1", "p1_c2"]

    def test_rerank_off_characterization_matches_baseline(self, rerank_off, monkeypatch):
        """rerank=false：输出与现状一致——cosine 序、score=1-distance、截断 top_k。"""
        store = _make_store(5)
        monkeypatch.setattr(
            "app.services.retrieval.RerankerService",
            lambda: (_ for _ in ()).throw(AssertionError("不应触碰 reranker")),
        )

        out = store.search(QUERY, top_k=3)

        assert [r["chunk_id"] for r in out] == ["p1_c0", "p1_c1", "p1_c2"]
        assert [r["score"] for r in out] == [
            pytest.approx(0.90),
            pytest.approx(0.89),
            pytest.approx(0.88),
        ]
        assert all(r["source"] == "semantic" for r in out)

    def test_rerank_default_off_when_config_missing(self, monkeypatch):
        """配置缺失 retrieval.rerank 时默认关闭：不触碰 reranker，行为同现状。"""
        monkeypatch.setattr(config, "_config", {})
        store = _make_store(5)
        monkeypatch.setattr(
            "app.services.retrieval.RerankerService",
            lambda: (_ for _ in ()).throw(AssertionError("默认关闭，不应触碰 reranker")),
        )

        out = store.search(QUERY, top_k=3)

        assert [r["chunk_id"] for r in out] == ["p1_c0", "p1_c1", "p1_c2"]
