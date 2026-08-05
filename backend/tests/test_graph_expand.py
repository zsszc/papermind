"""graph_expand 节点（Phase G G2）单元测试。

覆盖 spec 3.2 行为契约：
- 开关 retrieval.graph_expand 默认 false：关闭时节点返回空 dict，
  context_chunks 与最终 messages 字节级不回归；
- 开启时：沿 paper_citations 边扩展 1 跳（retrieval.graph_expand_hops 配置位），
  每篇扩展文献取至多 2 个代表 chunk（abstract 优先，其次 chunk_index 升序），
  与向量召回做 chunk 级 RRF 融合（键为 chunk_id），合并后 top_k 不变；
- 降级契约：无命中 / 无引用边 / 任何异常（如表不存在）→ 透传 retrieve 结果不变。

paper_citations 表由另一并行代理负责（models.py 不在本测试所有权内），
本测试按契约（id / citing_id / cited_id / created_at，唯一约束
(citing_id, cited_id)）在内存 SQLite 中以 DDL 造表。
"""

import types

import pytest
from sqlalchemy import text

from app.models import Chunk, Conversation, Message, Paper
from app.services import agent_graph
from app.services.agent_graph import (
    RETRIEVE_TOP_K,
    SYSTEM_PROMPT,
    build_rag_prompt,
    get_agent_graph,
    run_pre_orchestration,
)

# paper_citations 契约表结构（G1 代理的 ensure_schema 分支同构，见 spec 3.1）
_CITATION_DDL = """
CREATE TABLE IF NOT EXISTS paper_citations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    citing_id INTEGER NOT NULL,
    cited_id INTEGER NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (citing_id, cited_id)
)
"""


@pytest.fixture()
def citation_table(db):
    """按契约在内存库造 paper_citations 表（用例间隔离：先 DROP 再 CREATE）。"""
    db.execute(text("DROP TABLE IF EXISTS paper_citations"))
    db.execute(text(_CITATION_DDL))
    db.commit()
    yield db
    db.execute(text("DROP TABLE IF EXISTS paper_citations"))
    db.commit()


def _add_edge(db, citing_id, cited_id):
    db.execute(
        text("INSERT INTO paper_citations (citing_id, cited_id) VALUES (:a, :b)"),
        {"a": citing_id, "b": cited_id},
    )
    db.commit()


def _make_paper(db, title, year=2024):
    paper = Paper(
        title=title,
        authors="测试作者",
        year=year,
        file_path=f"papers/{title}.pdf",
        filename=f"{title}.pdf",
    )
    db.add(paper)
    db.flush()
    return paper


def _make_chunk(db, paper_id, content, chunk_index, chunk_type="paragraph", page=1):
    chunk = Chunk(
        paper_id=paper_id,
        content=content,
        page_number=page,
        chunk_index=chunk_index,
        chunk_type=chunk_type,
    )
    db.add(chunk)
    db.flush()
    return chunk


def _vchunk(paper_id, chunk_index, content, title=None):
    """仿 retrieval.py 向量检索返回的 chunk 结构（chunk_id 对齐 p{pid}_c{idx} 不变式）。"""
    return {
        "chunk_id": f"p{paper_id}_c{chunk_index}",
        "paper_id": paper_id,
        "title": title or f"文献{paper_id}",
        "authors": "测试作者",
        "year": 2024,
        "content": content,
        "page_number": 1,
        "chunk_type": "paragraph",
        "score": 0.9,
        "source": "semantic",
    }


class _FakeStore:
    """假向量库（与 test_agent_graph.py 同款，避免触碰真实 embedding）。"""

    def __init__(self, chunks=None, available=True):
        self._chunks = chunks or []
        self._available = available

    def available(self):
        return self._available

    def search(self, query, top_k, filters):
        return self._chunks


def _fake_config(graph_expand=False, hops=1):
    """假配置：仅覆盖 retrieval.graph_expand / retrieval.graph_expand_hops，其余走默认值。"""
    return types.SimpleNamespace(
        get=lambda key, default=None: {
            "retrieval.graph_expand": graph_expand,
            "retrieval.graph_expand_hops": hops,
        }.get(key, default)
    )


@pytest.fixture()
def conversation(db):
    """一个带一条 user 消息的会话（模拟 chat 路由落库用户消息后的状态）。"""
    conv = Conversation(title="图谱扩展测试", message_count=0)
    db.add(conv)
    db.flush()
    db.add(Message(conversation_id=conv.id, role="user", content="占位消息", citations=[]))
    db.commit()
    return conv


def _run(db, conversation, monkeypatch, chunks, graph_expand=False, hops=1):
    monkeypatch.setattr(agent_graph, "get_vector_store", lambda: _FakeStore(chunks))
    monkeypatch.setattr(agent_graph, "config", _fake_config(graph_expand, hops))
    return run_pre_orchestration(
        db=db, conversation_id=conversation.id, user_message="什么是多实例学习？"
    )


class TestSwitchOffNoRegression:
    """开关契约：retrieval.graph_expand 默认 false，关闭时字节级不回归。"""

    def test_switch_off_context_chunks_identity(
        self, db, citation_table, conversation, monkeypatch
    ):
        """关闭时 context_chunks 为向量召回原列表（同一对象），未做任何加工。"""
        vector_chunks = [_vchunk(1, 0, "命中片段")]
        state = _run(db, conversation, monkeypatch, vector_chunks, graph_expand=False)
        assert state["context_chunks"] is vector_chunks

    def test_switch_off_messages_byte_identical(
        self, db, citation_table, conversation, monkeypatch
    ):
        """关闭时最终 messages 与功能引入前逐字一致（system + history + RAG）。"""
        vector_chunks = [_vchunk(1, 0, "命中片段")]
        msg = "什么是多实例学习？"
        monkeypatch.setattr(agent_graph, "get_vector_store", lambda: _FakeStore(vector_chunks))
        monkeypatch.setattr(agent_graph, "config", _fake_config(False))
        state = run_pre_orchestration(
            db=db, conversation_id=conversation.id, user_message=msg
        )
        messages = state["messages"]
        assert [m["role"] for m in messages] == ["system", "user", "system"]
        assert messages[0]["content"] == SYSTEM_PROMPT
        assert messages[1]["content"] == "占位消息"
        assert messages[2]["content"] == build_rag_prompt(msg, vector_chunks)

    def test_switch_default_off_when_config_missing(
        self, db, citation_table, conversation, monkeypatch
    ):
        """config 无 retrieval.graph_expand 键 → 默认关闭（get 回退默认值）。"""
        vector_chunks = [_vchunk(1, 0, "命中片段")]
        monkeypatch.setattr(agent_graph, "get_vector_store", lambda: _FakeStore(vector_chunks))
        monkeypatch.setattr(
            agent_graph,
            "config",
            types.SimpleNamespace(get=lambda key, default=None: default),
        )
        state = run_pre_orchestration(
            db=db, conversation_id=conversation.id, user_message="什么是多实例学习？"
        )
        assert state["context_chunks"] is vector_chunks


class TestGraphExpansion:
    """扩展注入：1 跳扩展、代表 chunk 选取、RRF 融合序与 top_k 不变。"""

    def _build_library(self, db):
        """文献 1（命中）→ 引用 2；文献 3 → 引用 1；文献 2 ↔ 1 互引含入边。"""
        p1 = _make_paper(db, "命中文献")
        p2 = _make_paper(db, "被引文献")
        p3 = _make_paper(db, "施引文献")
        _add_edge(db, p1.id, p2.id)  # 出边：1 引用 2
        _add_edge(db, p3.id, p1.id)  # 入边：3 引用 1
        _add_edge(db, p2.id, p1.id)  # 互引：验证命中文献自身不被扩展注入
        # 文献2：abstract + 两个段落（验证 abstract 优先与每篇上限 2）
        _make_chunk(db, p2.id, "文献2摘要", -1, chunk_type="abstract")
        _make_chunk(db, p2.id, "文献2段落0", 0)
        _make_chunk(db, p2.id, "文献2段落1", 1)
        # 文献3：仅一个段落（无 abstract 时取首 chunk）
        _make_chunk(db, p3.id, "文献3段落0", 0)
        return p1, p2, p3

    def test_expanded_chunks_injected_with_graph_fields(
        self, db, citation_table, conversation, monkeypatch
    ):
        """开启时扩展 chunk 注入 context_chunks，字段结构与向量召回同构、source=graph。"""
        p1, p2, p3 = self._build_library(db)
        vector_chunks = [_vchunk(p1.id, 0, "命中片段", title="命中文献")]
        state = _run(db, conversation, monkeypatch, vector_chunks, graph_expand=True)
        graph_chunks = [c for c in state["context_chunks"] if c.get("source") == "graph"]
        assert graph_chunks, "扩展文献的代表 chunk 应注入"
        for gc in graph_chunks:
            for key in ("chunk_id", "paper_id", "title", "authors", "year", "content", "page_number", "chunk_type"):
                assert key in gc, f"扩展 chunk 缺字段 {key}"
        # 命中文献自身不被扩展注入
        assert all(gc["paper_id"] != p1.id for gc in graph_chunks)
        # 扩展覆盖出边（文献2）与入边（文献3）两侧
        assert {gc["paper_id"] for gc in graph_chunks} == {p2.id, p3.id}

    def test_abstract_preferred_and_max_two_per_paper(
        self, db, citation_table, conversation, monkeypatch
    ):
        """代表 chunk：abstract 优先，每篇至多 2 个，其后按 chunk_index 升序补齐。"""
        p1, p2, p3 = self._build_library(db)
        state = _run(
            db, conversation, monkeypatch, [_vchunk(p1.id, 0, "命中片段")], graph_expand=True
        )
        graph_chunks = [c for c in state["context_chunks"] if c.get("source") == "graph"]
        by_paper = {}
        for gc in graph_chunks:
            by_paper.setdefault(gc["paper_id"], []).append(gc)
        # 文献2：abstract 第一、段落0 第二，段落1 被上限截断
        assert [c["content"] for c in by_paper[p2.id]] == ["文献2摘要", "文献2段落0"]
        assert by_paper[p2.id][0]["chunk_id"] == f"p{p2.id}_c-1"  # 对齐向量库 id 不变式
        # 文献3：无 abstract 取首 chunk
        assert [c["content"] for c in by_paper[p3.id]] == ["文献3段落0"]

    def test_rrf_fusion_order_and_topk_unchanged(
        self, db, citation_table, conversation, monkeypatch
    ):
        """RRF 融合序：向量/图谱两路按 1/(60+rank+1) 计分，同分向量在前，top_k 不变。"""
        p1, p2, p3 = self._build_library(db)
        vector_chunks = [
            _vchunk(p1.id, 0, "A"),
            _vchunk(90, 0, "B"),
            _vchunk(90, 1, "C"),
            _vchunk(91, 0, "D"),
            _vchunk(92, 0, "E"),
        ]
        state = _run(db, conversation, monkeypatch, vector_chunks, graph_expand=True)
        fused = state["context_chunks"]
        assert len(fused) == RETRIEVE_TOP_K  # top_k 不变
        # RRF 期望序：A(1/61) 并列 G1(1/61) 向量在前 → B(1/62) 并列 G2 → C(1/63) 进前五
        assert [c["content"] for c in fused] == ["A", "文献2摘要", "B", "文献2段落0", "C"]

    def test_hit_papers_excluded_from_graph_channel(
        self, db, citation_table, conversation, monkeypatch
    ):
        """排除语义契约：命中文献不进入图谱通道（补充=新邻居，非重复自身）。

        设计取舍（spec 3.2「补充候选 chunk」）：向量命中的 paper 其 chunk 已在
        向量通道，图谱通道只带邻居文献的代表 chunk，因此两路 chunk_id 不重叠、
        RRF 不产生跨路叠加——排名提升只可能来自同路内序。本用例锁定该契约：
        命中文献 p2 的 chunk 在融合结果中恰出现一次（向量路原样保留），
        邻居文献 p3 的代表 chunk 进入结果。"""
        p1, p2, p3 = self._build_library(db)
        vector_chunks = [
            _vchunk(p1.id, 0, "A"),
            _vchunk(p2.id, 0, "文献2段落0"),
        ]
        state = _run(db, conversation, monkeypatch, vector_chunks, graph_expand=True)
        fused = state["context_chunks"]
        # 命中文献 p2 的段落0 只出现一次（向量路），未被图谱路复制
        assert [c["content"] for c in fused].count("文献2段落0") == 1
        assert all(
            not (c["paper_id"] == p2.id and c.get("source") == "graph") for c in fused
        )
        # 邻居文献（p3）的代表 chunk 进入结果
        assert any(c["paper_id"] == p3.id and c.get("source") == "graph" for c in fused)

    def test_two_hop_config_placeholder(
        self, db, citation_table, conversation, monkeypatch
    ):
        """跳数配置位 retrieval.graph_expand_hops：2 跳可达文献4，1 跳不可达。"""
        p1 = _make_paper(db, "命中文献")
        p2 = _make_paper(db, "一跳文献")
        p4 = _make_paper(db, "两跳文献")
        _add_edge(db, p1.id, p2.id)
        _add_edge(db, p2.id, p4.id)
        _make_chunk(db, p2.id, "一跳chunk", 0)
        _make_chunk(db, p4.id, "两跳chunk", 0)
        vector_chunks = [_vchunk(p1.id, 0, "命中片段")]

        state_1hop = _run(db, conversation, monkeypatch, vector_chunks, graph_expand=True, hops=1)
        contents_1 = [c["content"] for c in state_1hop["context_chunks"]]
        assert "一跳chunk" in contents_1
        assert "两跳chunk" not in contents_1

        state_2hop = _run(db, conversation, monkeypatch, vector_chunks, graph_expand=True, hops=2)
        contents_2 = [c["content"] for c in state_2hop["context_chunks"]]
        assert "两跳chunk" in contents_2


class TestDegradationPassthrough:
    """降级契约：无命中 / 无引用边 / 任何异常 → 透传 retrieve 结果不变。"""

    def test_no_edges_passthrough(self, db, citation_table, conversation, monkeypatch):
        """有命中但无任何引用边：原样透传（同一列表对象）。"""
        p1 = _make_paper(db, "孤立文献")
        vector_chunks = [_vchunk(p1.id, 0, "命中片段")]
        state = _run(db, conversation, monkeypatch, vector_chunks, graph_expand=True)
        assert state["context_chunks"] is vector_chunks

    def test_empty_vector_hits_passthrough(self, db, citation_table, conversation, monkeypatch):
        """向量零命中：无扩展对象，透传空列表，不报错。"""
        state = _run(db, conversation, monkeypatch, [], graph_expand=True)
        assert state["context_chunks"] == []

    def test_missing_table_exception_passthrough(
        self, db, conversation, monkeypatch
    ):
        """paper_citations 表不存在（查询抛异常）：透传向量召回，不阻断对话。"""
        db.execute(text("DROP TABLE IF EXISTS paper_citations"))
        db.commit()
        vector_chunks = [_vchunk(1, 0, "命中片段")]
        state = _run(db, conversation, monkeypatch, vector_chunks, graph_expand=True)
        assert state["context_chunks"] is vector_chunks
        # RAG 消息按向量召回原样组装（无扩展 chunk 混入）
        msg = "什么是多实例学习？"
        assert state["messages"][-1]["content"] == build_rag_prompt(msg, vector_chunks)


class TestGraphTopology:
    """图拓扑：graph_expand 位于 retrieve 之后、external_tools 之前。"""

    def test_graph_expand_node_present(self):
        nodes = set(get_agent_graph().get_graph().nodes.keys())
        assert "graph_expand" in nodes

    def test_graph_expand_edge_position(self):
        edges = {(e.source, e.target) for e in get_agent_graph().get_graph().edges}
        assert ("retrieve", "graph_expand") in edges
        assert ("graph_expand", "external_tools") in edges
        assert ("retrieve", "external_tools") not in edges
