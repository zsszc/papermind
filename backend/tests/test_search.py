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
