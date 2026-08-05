"""external_tools 节点（Phase E E2）单元测试。

覆盖 spec 3.2 行为契约：
- 信号词命中（含小写匹配）且工具有用时才调用外部 MCP 工具；
- 调用契约：arxiv.* 工具、query 原样、limit=3；
- 结果以「外部检索补充」段注入 RAG 上下文（独立 system 消息，不进 context_chunks/citations）；
- 未命中信号 / 未配置（available()=False）/ 无 arxiv.* 工具 / 空结果不触发不注入；
- discover / call_tool 任何异常与 10s 耗时预算超时均降级为纯本地路径；
- get_mcp_client_manager 懒加载单例与 mcp_servers 配置透传。

全程用 _FakeMCPManager 替身隔离 MCPClientManager（spec 3.1 契约），
不实例化真实 manager、不发起任何真实网络/子进程调用。
"""

import asyncio
import sys
import time
import types
from dataclasses import dataclass, field

import pytest
from langgraph.graph import END

from app.models import Conversation, Message
from app.services import agent_graph
from app.services.agent_graph import get_agent_graph, run_pre_orchestration


@dataclass
class _FakeExternalTool:
    """spec 3.1 ExternalTool 契约的测试替身（节点只读 .name）。"""

    name: str
    description: str = "假外部工具"
    server: str = "arxiv"
    schema: dict = field(default_factory=dict)


class _FakeMCPManager:
    """spec 3.1 MCPClientManager 契约的测试替身（异步方法与真实契约一致）。"""

    def __init__(
        self,
        tools=None,
        available=True,
        call_result="",
        discover_exc=None,
        call_exc=None,
        discover_delay=0.0,
    ):
        self._tools = tools if tools is not None else []
        self._available = available
        self._call_result = call_result
        self._discover_exc = discover_exc
        self._call_exc = call_exc
        self._discover_delay = discover_delay
        self.available_calls = 0
        self.discover_calls = 0
        self.call_tool_calls = []

    def available(self):
        self.available_calls += 1
        return self._available

    async def discover(self):
        self.discover_calls += 1
        if self._discover_delay:
            await asyncio.sleep(self._discover_delay)
        if self._discover_exc:
            raise self._discover_exc
        return self._tools

    async def call_tool(self, tool_name, args):
        self.call_tool_calls.append({"tool_name": tool_name, "args": args})
        if self._call_exc:
            raise self._call_exc
        return self._call_result


class _FakeStore:
    """假向量库（与 test_agent_graph.py 同款，避免触碰真实 embedding）。"""

    def __init__(self, chunks=None, available=True):
        self._chunks = chunks or []
        self._available = available

    def available(self):
        return self._available

    def search(self, query, top_k, filters):
        return self._chunks


def _patch_store(monkeypatch, store):
    monkeypatch.setattr(agent_graph, "get_vector_store", lambda: store)


def _patch_manager(monkeypatch, manager):
    """把 agent_graph 内的 manager 获取入口替换为替身（模块级单例不被触发）。"""
    monkeypatch.setattr(agent_graph, "get_mcp_client_manager", lambda: manager)


@pytest.fixture()
def conversation(db):
    """一个带一条 user 消息的会话（模拟 chat 路由落库用户消息后的状态）。"""
    conv = Conversation(title="外部工具测试", message_count=0)
    db.add(conv)
    db.flush()
    db.add(Message(conversation_id=conv.id, role="user", content="占位消息", citations=[]))
    db.commit()
    return conv


@pytest.fixture()
def chunk():
    """一条带完整引用字段的本地检索片段。"""
    return {
        "source": "p1_c0",
        "paper_id": 1,
        "title": "结直肠癌T分期研究",
        "authors": "张三",
        "year": 2024,
        "page_number": 3,
        "content": "多实例学习在病理图像上的应用……",
    }


class TestSignalTrigger:
    """信号词触发契约：命中信号 + manager 可用 + 有 arxiv.* 工具才调用。"""

    @pytest.mark.parametrize(
        "signal", ["arxiv", "论文检索", "最新研究", "未收录", "没有收录", "不在库中"]
    )
    def test_each_signal_triggers(self, db, conversation, monkeypatch, signal):
        manager = _FakeMCPManager(
            tools=[_FakeExternalTool("arxiv.search")], call_result="外部结果文本"
        )
        _patch_manager(monkeypatch, manager)
        _patch_store(monkeypatch, _FakeStore())
        state = run_pre_orchestration(
            db=db,
            conversation_id=conversation.id,
            user_message=f"这篇{signal}相关资料有哪些？",
        )
        assert manager.call_tool_calls, f"信号词 {signal} 应触发外部工具调用"
        assert "外部检索补充" in state["external_context"]
        assert "外部结果文本" in state["external_context"]

    def test_signal_case_insensitive(self, db, conversation, monkeypatch):
        """小写匹配：arXiv 大写形式同样命中。"""
        manager = _FakeMCPManager(
            tools=[_FakeExternalTool("arxiv.search")], call_result="R"
        )
        _patch_manager(monkeypatch, manager)
        _patch_store(monkeypatch, _FakeStore())
        run_pre_orchestration(
            db=db,
            conversation_id=conversation.id,
            user_message="arXiv 上有哪些 MIL 论文？",
        )
        assert manager.discover_calls == 1
        assert len(manager.call_tool_calls) == 1

    def test_no_signal_no_trigger(self, db, conversation, monkeypatch):
        """未命中信号词：连 manager 都不获取，上下文与现状一致。"""
        manager = _FakeMCPManager(
            tools=[_FakeExternalTool("arxiv.search")], call_result="R"
        )
        _patch_manager(monkeypatch, manager)
        _patch_store(monkeypatch, _FakeStore())
        state = run_pre_orchestration(
            db=db, conversation_id=conversation.id, user_message="什么是多实例学习？"
        )
        assert state["external_context"] == ""
        assert manager.available_calls == 0
        assert manager.discover_calls == 0
        assert manager.call_tool_calls == []
        assert all("外部检索补充" not in m["content"] for m in state["messages"])

    def test_unavailable_manager_no_trigger(self, db, conversation, monkeypatch):
        """未配置 mcp_servers（available()=False）：信号命中也不触发。"""
        manager = _FakeMCPManager(
            tools=[_FakeExternalTool("arxiv.search")],
            available=False,
            call_result="R",
        )
        _patch_manager(monkeypatch, manager)
        _patch_store(monkeypatch, _FakeStore())
        state = run_pre_orchestration(
            db=db,
            conversation_id=conversation.id,
            user_message="arxiv 上有哪些 MIL 论文？",
        )
        assert state["external_context"] == ""
        assert manager.available_calls == 1
        assert manager.discover_calls == 0
        assert manager.call_tool_calls == []


class TestCallContract:
    """调用契约：arxiv.* 工具、query 原样、limit=3。"""

    def test_call_args_query_verbatim_limit_3(self, db, conversation, monkeypatch):
        user_message = "帮我做 arxiv 论文检索：MIL 结直肠癌 T 分期"
        manager = _FakeMCPManager(
            tools=[_FakeExternalTool("arxiv.search")], call_result="R"
        )
        _patch_manager(monkeypatch, manager)
        _patch_store(monkeypatch, _FakeStore())
        run_pre_orchestration(
            db=db, conversation_id=conversation.id, user_message=user_message
        )
        assert manager.call_tool_calls == [
            {"tool_name": "arxiv.search", "args": {"query": user_message, "limit": 3}}
        ]

    def test_prefers_arxiv_search_tool(self, db, conversation, monkeypatch):
        """有多个 arxiv.* 工具时优先 arxiv.search。"""
        manager = _FakeMCPManager(
            tools=[
                _FakeExternalTool("arxiv.download"),
                _FakeExternalTool("arxiv.search"),
            ],
            call_result="R",
        )
        _patch_manager(monkeypatch, manager)
        _patch_store(monkeypatch, _FakeStore())
        run_pre_orchestration(
            db=db, conversation_id=conversation.id, user_message="arxiv 检索 MIL"
        )
        assert manager.call_tool_calls[0]["tool_name"] == "arxiv.search"


class TestContextInjection:
    """注入格式：「外部检索补充」段作为独立 system 消息进入上下文，不进 citations。"""

    def test_external_context_appended_after_rag(
        self, db, conversation, monkeypatch, chunk
    ):
        manager = _FakeMCPManager(
            tools=[_FakeExternalTool("arxiv.search")], call_result="外部结果文本"
        )
        _patch_manager(monkeypatch, manager)
        _patch_store(monkeypatch, _FakeStore(chunks=[chunk]))
        state = run_pre_orchestration(
            db=db,
            conversation_id=conversation.id,
            user_message="arxiv 上有哪些 MIL 论文？",
        )
        # 本地检索结果原样透出，外部结果不混入 context_chunks（citations 结构不变）
        assert state["context_chunks"] == [chunk]
        messages = state["messages"]
        ext_msgs = [
            m
            for m in messages
            if m["role"] == "system" and "外部检索补充" in m["content"]
        ]
        assert len(ext_msgs) == 1
        assert "外部结果文本" in ext_msgs[0]["content"]
        # 外部补充位于 RAG 上下文消息之后
        rag_idx = next(
            i for i, m in enumerate(messages) if "[1] 结直肠癌T分期研究" in m["content"]
        )
        assert messages.index(ext_msgs[0]) > rag_idx

    def test_external_context_without_local_chunks(self, db, conversation, monkeypatch):
        """本地零检索时外部补充仍注入；context_chunks 保持为空。"""
        manager = _FakeMCPManager(
            tools=[_FakeExternalTool("arxiv.search")], call_result="外部结果文本"
        )
        _patch_manager(monkeypatch, manager)
        _patch_store(monkeypatch, _FakeStore())
        state = run_pre_orchestration(
            db=db, conversation_id=conversation.id, user_message="arxiv 检索 MIL"
        )
        assert state["context_chunks"] == []
        assert state["external_context"] != ""
        ext_msgs = [
            m
            for m in state["messages"]
            if m["role"] == "system" and "外部检索补充" in m["content"]
        ]
        assert len(ext_msgs) == 1


class TestDegradation:
    """降级契约：任何异常/空结果/超时 → 纯本地路径，不阻断对话。"""

    def test_discover_raises_degrades(self, db, conversation, monkeypatch):
        manager = _FakeMCPManager(discover_exc=RuntimeError("连接失败"))
        _patch_manager(monkeypatch, manager)
        _patch_store(monkeypatch, _FakeStore())
        state = run_pre_orchestration(
            db=db, conversation_id=conversation.id, user_message="arxiv 检索 MIL"
        )
        assert state["external_context"] == ""
        assert manager.call_tool_calls == []
        assert all("外部检索补充" not in m["content"] for m in state["messages"])

    def test_call_tool_raises_degrades(self, db, conversation, monkeypatch):
        manager = _FakeMCPManager(
            tools=[_FakeExternalTool("arxiv.search")],
            call_exc=RuntimeError("调用超时"),
        )
        _patch_manager(monkeypatch, manager)
        _patch_store(monkeypatch, _FakeStore())
        state = run_pre_orchestration(
            db=db, conversation_id=conversation.id, user_message="arxiv 检索 MIL"
        )
        assert state["external_context"] == ""
        assert all("外部检索补充" not in m["content"] for m in state["messages"])

    def test_empty_result_no_injection(self, db, conversation, monkeypatch):
        manager = _FakeMCPManager(
            tools=[_FakeExternalTool("arxiv.search")], call_result=""
        )
        _patch_manager(monkeypatch, manager)
        _patch_store(monkeypatch, _FakeStore())
        state = run_pre_orchestration(
            db=db, conversation_id=conversation.id, user_message="arxiv 检索 MIL"
        )
        assert state["external_context"] == ""

    def test_no_arxiv_tool_only_skips(self, db, conversation, monkeypatch):
        """discover 有工具但无 arxiv.*：仅记日志跳过，不调用。"""
        manager = _FakeMCPManager(
            tools=[_FakeExternalTool("other.search", server="other")], call_result="R"
        )
        _patch_manager(monkeypatch, manager)
        _patch_store(monkeypatch, _FakeStore())
        state = run_pre_orchestration(
            db=db, conversation_id=conversation.id, user_message="arxiv 检索 MIL"
        )
        assert state["external_context"] == ""
        assert manager.discover_calls == 1
        assert manager.call_tool_calls == []

    def test_budget_timeout_degrades(self, db, conversation, monkeypatch):
        """节点总耗时预算：discover 超过预算即降级，不等满对方耗时。"""
        monkeypatch.setattr(agent_graph, "EXTERNAL_TOOL_BUDGET_SECONDS", 0.5)
        manager = _FakeMCPManager(
            tools=[_FakeExternalTool("arxiv.search")],
            call_result="R",
            discover_delay=5.0,
        )
        _patch_manager(monkeypatch, manager)
        _patch_store(monkeypatch, _FakeStore())
        start = time.monotonic()
        state = run_pre_orchestration(
            db=db, conversation_id=conversation.id, user_message="arxiv 检索 MIL"
        )
        elapsed = time.monotonic() - start
        assert state["external_context"] == ""
        assert elapsed < 4.0  # 预算生效：未等满 discover 的 5s


class TestManagerSingleton:
    """get_mcp_client_manager：懒加载单例 + mcp_servers 配置透传（假模块隔离真实实现）。"""

    def _fake_module(self, created):
        class _RecordingManager:
            def __init__(self, servers_config):
                created.append(servers_config)

            def available(self):
                return bool(created[0])

        fake_module = types.ModuleType("app.services.mcp_client")
        fake_module.MCPClientManager = _RecordingManager
        return fake_module

    def test_reads_config_and_is_singleton(self, monkeypatch):
        created = []
        monkeypatch.setitem(
            sys.modules, "app.services.mcp_client", self._fake_module(created)
        )
        monkeypatch.setattr(agent_graph, "_mcp_client_manager", None)
        monkeypatch.setattr(
            agent_graph,
            "config",
            types.SimpleNamespace(
                get=lambda key, default=None: (
                    [{"name": "arxiv"}] if key == "mcp_servers" else default
                )
            ),
        )
        m1 = agent_graph.get_mcp_client_manager()
        m2 = agent_graph.get_mcp_client_manager()
        assert m1 is m2  # 单例
        assert created == [[{"name": "arxiv"}]]  # 配置透传且只构造一次

    def test_missing_config_defaults_to_empty(self, monkeypatch):
        """config.yaml 无 mcp_servers 键 → 空列表（特性默认关闭）。"""
        created = []
        monkeypatch.setitem(
            sys.modules, "app.services.mcp_client", self._fake_module(created)
        )
        monkeypatch.setattr(agent_graph, "_mcp_client_manager", None)
        monkeypatch.setattr(
            agent_graph,
            "config",
            types.SimpleNamespace(get=lambda key, default=None: default),
        )
        agent_graph.get_mcp_client_manager()
        assert created == [[]]


class TestGraphTopology:
    """图拓扑：external_tools 位于 retrieve 之后、build_messages 之前。"""

    def test_external_tools_node_present(self):
        nodes = set(get_agent_graph().get_graph().nodes.keys())
        assert "external_tools" in nodes

    def test_external_tools_edge_position(self):
        edges = {(e.source, e.target) for e in get_agent_graph().get_graph().edges}
        # Phase G G2 后：retrieve → graph_expand → external_tools → build_messages
        assert ("graph_expand", "external_tools") in edges
        assert ("external_tools", "build_messages") in edges
        assert ("retrieve", "build_messages") not in edges
        assert ("build_messages", END) in edges
