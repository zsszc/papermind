"""检索路由测试：FTS5 查询清洗函数 + 关键词检索接口安全性。

说明：
- 语义检索依赖本地 Embedding 模型（BGE-M3），测试环境不加载模型，
  通过 monkeypatch 将 get_vector_store 替换为不可用桩，验证语义路径优雅降级；
- papers_fts 虚拟表与触发器由 ensure_papers_fts 手动创建（测试不触发 lifespan，
  虽然 Paper 表的 after_create 事件也会建，这里显式调用以保证幂等、语义明确）。
"""

import pytest

from app.models import Paper, ensure_papers_fts
from app.routers.search import _sanitize_fts_query
from tests.conftest import engine


class _StubVectorStore:
    """语义检索桩：模拟 Embedding 模型未加载，优雅降级为不可用。"""

    def available(self) -> bool:
        return False

    def search(self, **kwargs):
        return []


@pytest.fixture(autouse=True)
def stub_vector_store(monkeypatch):
    """所有检索接口测试都走桩向量库，避免加载模型/访问真实 vector_db。"""
    monkeypatch.setattr(
        "app.routers.search.get_vector_store", lambda: _StubVectorStore()
    )


@pytest.fixture()
def paper(db):
    """建 FTS 表并插入一条文献记录（触发器自动同步进 papers_fts）。"""
    ensure_papers_fts(engine)
    p = Paper(
        title="Deep learning for colorectal cancer staging",
        authors="Zhang San; Li Si",
        year=2024,
        abstract="We predict T staging of colorectal cancer with multiple instance learning.",
        file_path="papers/test.pdf",
        filename="test.pdf",
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


# ---------- _sanitize_fts_query 纯函数单测 ----------

class TestSanitizeFtsQuery:
    def test_normal_word(self):
        assert _sanitize_fts_query("cancer") == '"cancer"'

    def test_multi_token_and_semantics(self):
        assert _sanitize_fts_query("colorectal cancer") == '"colorectal" "cancer"'

    def test_special_chars_stripped(self):
        assert _sanitize_fts_query('title:"cancer"*') == '"title" "cancer"'

    def test_operators_neutralized(self):
        # NEAR/AND/OR 等语法符被短语化后只是普通词，不再具有语法含义
        assert _sanitize_fts_query("NEAR(cancer, 5)") == '"NEAR" "cancer" "5"'
        assert _sanitize_fts_query("cancer OR tumor") == '"cancer" "OR" "tumor"'

    def test_pure_special_chars_returns_empty(self):
        assert _sanitize_fts_query('"*^:()') == ""
        assert _sanitize_fts_query("---") == ""

    def test_empty_and_blank_returns_empty(self):
        assert _sanitize_fts_query("") == ""
        assert _sanitize_fts_query("   ") == ""

    def test_hyphen_and_punctuation_as_separator(self):
        assert _sanitize_fts_query("bge-m3") == '"bge" "m3"'
        assert _sanitize_fts_query("cancer,tumor") == '"cancer" "tumor"'

    def test_chinese_tokens(self):
        assert _sanitize_fts_query("结直肠癌 分期") == '"结直肠癌" "分期"'


# ---------- 检索接口级测试 ----------

# 含 FTS5 语法符/注入特征的查询，均不应导致 500 或异常
_SPECIAL_QUERIES = [
    '" OR "1"="1',
    "*",
    "title:cancer",
    "(cancer OR tumor)",
    "NEAR(cancer, tumor, 5)",
    "cancer*",
    "^title",
    '"unclosed quote',
    "---",
    "cancer AND) (tumor",
]


class TestSearchApi:
    @pytest.mark.parametrize("query", _SPECIAL_QUERIES)
    def test_special_char_queries_do_not_500(self, client, paper, query):
        """特殊字符查询：关键词检索应安全降级，接口返回 200 且不抛异常。"""
        resp = client.post(
            "/api/search",
            json={"query": query, "use_keyword": True, "use_semantic": False},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["query"] == query
        assert isinstance(body["results"], list)

    def test_pure_special_query_returns_empty(self, client, paper):
        """清洗后无有效 token：跳过关键词检索，返回空列表而非报错。"""
        resp = client.post(
            "/api/search",
            json={"query": '"*^:()', "use_keyword": True, "use_semantic": False},
        )
        assert resp.status_code == 200
        assert resp.json()["results"] == []

    def test_keyword_hit(self, client, paper):
        """正常关键词（keyword-only）能命中插入的文献。"""
        resp = client.post(
            "/api/search",
            json={
                "query": "colorectal cancer",
                "use_keyword": True,
                "use_semantic": False,
            },
        )
        assert resp.status_code == 200
        results = resp.json()["results"]
        assert len(results) == 1
        assert results[0]["paper_id"] == paper.id
        assert results[0]["source"] == "keyword"

    def test_multi_token_and_semantics_no_hit(self, client, paper):
        """AND 语义：部分词不命中时应返回空结果。"""
        resp = client.post(
            "/api/search",
            json={
                "query": "colorectal nonexistentterm",
                "use_keyword": True,
                "use_semantic": False,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["results"] == []

    def test_hybrid_with_semantic_unavailable(self, client, paper):
        """混合模式 + 语义模型未加载：优雅降级，仅靠关键词结果融合返回。"""
        resp = client.post(
            "/api/search",
            json={"query": "colorectal", "use_keyword": True, "use_semantic": True},
        )
        assert resp.status_code == 200
        results = resp.json()["results"]
        assert len(results) == 1
        assert results[0]["paper_id"] == paper.id


# ---------- _build_where 组合过滤契约（Batch 7 / F1） ----------
# ChromaDB 0.4.24 的 where 只接受「单字段单操作符」或「$and/$or 组合」，
# 多条件必须包装为 $and，否则 query 抛 ValueError → /api/search 500。

class TestBuildWhere:
    @staticmethod
    def _chroma_collection():
        import chromadb

        client = chromadb.EphemeralClient()
        coll = client.get_or_create_collection("t_where")
        coll.add(
            ids=["a", "b", "c"],
            documents=["x", "y", "z"],
            metadatas=[
                {"year": 2019, "paper_id": 1},
                {"year": 2021, "paper_id": 2},
                {"year": 2025, "paper_id": 2},
            ],
            embeddings=[[1.0, 0.0], [0.9, 0.1], [0.0, 1.0]],
        )
        return coll

    def test_year_range_filter_accepted(self):
        """year_gte+year_lte 组合过滤可被 ChromaDB 接受且过滤正确。"""
        from app.services.retrieval import VectorStore

        where = VectorStore._build_where({"year_gte": 2020, "year_lte": 2024})
        res = self._chroma_collection().query(
            query_embeddings=[[1.0, 0.0]], n_results=3, where=where
        )
        years = sorted(m["year"] for m in res["metadatas"][0])
        assert years == [2021]

    def test_multi_field_filter_accepted(self):
        """year_gte+paper_id 多字段组合过滤可被接受且过滤正确。"""
        from app.services.retrieval import VectorStore

        where = VectorStore._build_where({"year_gte": 2020, "paper_id": 2})
        res = self._chroma_collection().query(
            query_embeddings=[[1.0, 0.0]], n_results=3, where=where
        )
        rows = sorted((m["year"], m["paper_id"]) for m in res["metadatas"][0])
        assert rows == [(2021, 2), (2025, 2)]

    def test_single_condition_kept_flat(self):
        """单条件保持扁平形式（不包 $and），空过滤返回 None。"""
        from app.services.retrieval import VectorStore

        assert VectorStore._build_where(None) is None
        assert VectorStore._build_where({}) is None
        assert VectorStore._build_where({"paper_id": 7}) == {"paper_id": 7}

    def test_query_degrades_on_invalid_where(self):
        """非法 where 不向上抛异常（防 500），降级为无过滤查询并返回结果。"""
        from app.services.retrieval import VectorStore

        coll = self._chroma_collection()
        bad_where = {"year": {"$gte": 2020, "$lte": 2024}}  # 修复前的非法形状
        res = VectorStore._query_with_fallback(coll, [1.0, 0.0], 3, bad_where)
        assert len(res["ids"][0]) == 3  # 降级为无过滤，返回全部
