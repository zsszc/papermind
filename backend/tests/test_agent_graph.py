"""Agent 编排图（LangGraph StateGraph）单元测试。

覆盖：图可编译、节点顺序、三要素输出（system_prompt / context_chunks /
history_messages）、Skill prompt 注入、空检索降级、联网开关。
全程 monkeypatch 向量库，不调用真实 LLM / embedding。
"""

import pytest
from langgraph.graph import END, START

from app.models import Conversation, MemorySummary, Message
from app.services import agent_graph
from app.services.agent_graph import (
    SYSTEM_PROMPT,
    WEB_SEARCH_HINT,
    get_agent_graph,
    run_pre_orchestration,
)


class _FakeStore:
    """假向量库：available 可控，search 返回固定片段。"""

    def __init__(self, chunks=None, available=True):
        self._chunks = chunks or []
        self._available = available
        self.search_calls = []

    def available(self):
        return self._available

    def search(self, query, top_k, filters):
        self.search_calls.append({"query": query, "top_k": top_k, "filters": filters})
        return self._chunks


@pytest.fixture()
def chunk():
    """一条带完整引用字段的检索片段。"""
    return {
        "source": "p1_c0",
        "paper_id": 1,
        "title": "结直肠癌T分期研究",
        "authors": "张三",
        "year": 2024,
        "page_number": 3,
        "content": "多实例学习在病理图像上的应用……",
    }


@pytest.fixture()
def conversation(db):
    """一个带一条 user 消息的会话（模拟 chat 路由保存用户消息后的状态）。"""
    conv = Conversation(title="测试会话", message_count=0)
    db.add(conv)
    db.flush()
    db.add(
        Message(conversation_id=conv.id, role="user", content="什么是MIL？", citations=[])
    )
    db.commit()
    return conv


def _patch_store(monkeypatch, store):
    """把 agent_graph 内的向量库替换为假实现，避免触碰真实 embedding。"""
    monkeypatch.setattr(agent_graph, "get_vector_store", lambda: store)


class TestGraphStructure:
    """图可编译，节点与边的顺序为 load_memory → retrieve → build_messages。"""

    def test_compiles_and_is_singleton(self):
        graph = get_agent_graph()
        assert graph is not None
        assert get_agent_graph() is graph  # 单例

    def test_nodes_present(self):
        nodes = set(get_agent_graph().get_graph().nodes.keys())
        assert {"load_memory", "retrieve", "build_messages"} <= nodes

    def test_edge_order(self):
        edges = {
            (e.source, e.target) for e in get_agent_graph().get_graph().edges
        }
        assert (START, "load_memory") in edges
        assert ("load_memory", "retrieve") in edges
        # Phase G G2：retrieve 之后插入 graph_expand 节点
        assert ("retrieve", "graph_expand") in edges
        assert ("graph_expand", "external_tools") in edges
        # Phase E E2：graph_expand 与 build_messages 之间为 external_tools 节点
        assert ("external_tools", "build_messages") in edges
        assert ("build_messages", END) in edges

    def test_node_execution_order(self, db, conversation, monkeypatch):
        """通过插桩节点函数验证实际执行顺序。"""
        calls = []
        for name in ("load_memory", "retrieve", "build_messages"):
            original = getattr(agent_graph, name)

            def _wrap(state, _orig=original, _name=name):
                calls.append(_name)
                return _orig(state)

            monkeypatch.setattr(agent_graph, name, _wrap)
        # 节点函数在编译时绑定，需重新编译以生效
        monkeypatch.setattr(agent_graph, "_compiled_graph", None)
        _patch_store(monkeypatch, _FakeStore())

        run_pre_orchestration(
            db=db, conversation_id=conversation.id, user_message="什么是MIL？"
        )
        assert calls == ["load_memory", "retrieve", "build_messages"]


class TestGraphOutputs:
    """mock 记忆与检索后，图输出包含 system_prompt / context / history 三要素。"""

    def test_outputs_three_elements(self, db, conversation, monkeypatch, chunk):
        _patch_store(monkeypatch, _FakeStore(chunks=[chunk]))
        # 写入一条长期记忆（纯 DB 写入，不触发 LLM）
        db.add(
            MemorySummary(memory_type="fact", content="用户研究结直肠癌T分期", importance=8)
        )
        db.commit()

        state = run_pre_orchestration(
            db=db, conversation_id=conversation.id, user_message="什么是MIL？"
        )

        # system_prompt：基础设定 + 记忆
        assert state["system_prompt"].startswith(SYSTEM_PROMPT)
        assert "用户研究结直肠癌T分期" in state["system_prompt"]
        # context：检索片段原样透出（含引用信息）
        assert state["context_chunks"] == [chunk]
        # history：包含当前 user 消息
        assert state["history_messages"] == [{"role": "user", "content": "什么是MIL？"}]
        assert state["history_total"] == 1

        # 最终消息列表：system（含记忆）→ history → RAG system
        messages = state["messages"]
        assert messages[0] == {"role": "system", "content": state["system_prompt"]}
        assert {"role": "user", "content": "什么是MIL？"} in messages
        rag_msgs = [m for m in messages if m["role"] == "system" and "[1] 结直肠癌T分期研究" in m["content"]]
        assert len(rag_msgs) == 1
        assert "用户问题：什么是MIL？" in rag_msgs[0]["content"]

    def test_retrieve_filters_passed(self, db, conversation, monkeypatch, chunk):
        store = _FakeStore(chunks=[chunk])
        _patch_store(monkeypatch, store)
        run_pre_orchestration(
            db=db,
            conversation_id=conversation.id,
            user_message="什么是MIL？",
            paper_id=7,
        )
        assert store.search_calls[0]["filters"] == {"paper_id": 7}
        assert store.search_calls[0]["top_k"] == agent_graph.RETRIEVE_TOP_K


class TestSkillInjection:
    """skill 参数流经图后注入对应角色 prompt。"""

    def test_skill_prompt_injected(self, db, conversation, monkeypatch):
        _patch_store(monkeypatch, _FakeStore())
        state = run_pre_orchestration(
            db=db,
            conversation_id=conversation.id,
            user_message="请翻译这段文字",
            skill="translator",
        )
        assert state["skill_prompt"] is not None
        assert "学术翻译专家" in state["skill_prompt"]
        assert "请翻译这段文字" in state["skill_prompt"]
        assert state["messages"][-1] == {
            "role": "system",
            "content": state["skill_prompt"],
        }

    def test_no_skill_no_injection(self, db, conversation, monkeypatch):
        _patch_store(monkeypatch, _FakeStore())
        state = run_pre_orchestration(
            db=db, conversation_id=conversation.id, user_message="随便聊聊"
        )
        assert state["skill_prompt"] is None
        assert all("专家" not in m["content"] or m["content"] == state["system_prompt"]
                   for m in state["messages"])


class TestEmptyRetrieval:
    """检索为空 / 向量库不可用时图正常完成，不报错。"""

    def test_store_unavailable_returns_empty_context(self, db, conversation, monkeypatch):
        _patch_store(monkeypatch, _FakeStore(available=False))
        state = run_pre_orchestration(
            db=db, conversation_id=conversation.id, user_message="什么是MIL？"
        )
        assert state["context_chunks"] == []
        # 无 RAG system 消息：system（1 条）+ history（1 条）
        assert len(state["messages"]) == 2
        assert state["messages"][0]["role"] == "system"

    def test_store_raises_returns_empty_context(self, db, conversation, monkeypatch):
        class _BoomStore(_FakeStore):
            def search(self, query, top_k, filters):
                raise RuntimeError("embedding 挂了")

        _patch_store(monkeypatch, _BoomStore(chunks=[], available=True))
        state = run_pre_orchestration(
            db=db, conversation_id=conversation.id, user_message="什么是MIL？"
        )
        assert state["context_chunks"] == []


class TestWebSearchToggle:
    """联网搜索开关：显式开启与启发式命中均注入提示。"""

    def test_explicit_enable(self, db, conversation, monkeypatch):
        _patch_store(monkeypatch, _FakeStore())
        state = run_pre_orchestration(
            db=db,
            conversation_id=conversation.id,
            user_message="什么是MIL？",
            enable_web_search=True,
        )
        assert state["web_search_enabled"] is True
        assert {"role": "system", "content": WEB_SEARCH_HINT} in state["messages"]

    def test_heuristic_enable(self, db, conversation, monkeypatch):
        _patch_store(monkeypatch, _FakeStore())
        state = run_pre_orchestration(
            db=db,
            conversation_id=conversation.id,
            user_message="最新的MIL进展",
        )
        assert state["web_search_enabled"] is True

    def test_disabled_by_default(self, db, conversation, monkeypatch):
        _patch_store(monkeypatch, _FakeStore())
        state = run_pre_orchestration(
            db=db, conversation_id=conversation.id, user_message="什么是MIL？"
        )
        assert state["web_search_enabled"] is False
        assert WEB_SEARCH_HINT not in [m["content"] for m in state["messages"]]
