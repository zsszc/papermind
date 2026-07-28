"""MemoryManager 单元测试：统一 API、容量淘汰、类型隔离、旧接口兼容层与 LLM 降级。

使用内存 SQLite + 真实 ORM；LLM 调用一律 mock，绝不触发真实 API。
"""

from unittest.mock import AsyncMock

import pytest

from app.models import Conversation, MemorySummary, Message
from app.services.llm import llm_service
from app.services.memory_manager import (
    DEFAULT_CAPACITY_LIMITS,
    MemoryManager,
)


# ---------- 工具函数 ----------


def _make_conversation(db) -> Conversation:
    conv = Conversation(title="测试会话")
    db.add(conv)
    db.commit()
    return conv


def _make_messages(db, conversation_id: int, n: int) -> None:
    """为会话批量插入 n 条消息（user/assistant 交替）。"""
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        db.add(Message(conversation_id=conversation_id, role=role, content=f"消息{i}"))
    db.commit()


# ---------- 统一读写 API ----------


class TestUnifiedAPI:
    def test_add_and_get_by_type(self, db):
        mgr = MemoryManager(db)
        mem = mgr.add_memory("fact", "用户研究结直肠癌T分期", importance=8)
        assert mem is not None and mem.id is not None

        items = mgr.get_memory("fact")
        assert len(items) == 1
        assert items[0].content == "用户研究结直肠癌T分期"
        assert items[0].importance == 8

    def test_get_memory_none_returns_all_types(self, db):
        mgr = MemoryManager(db)
        for t in ("short_term", "long_term", "preference", "fact"):
            assert mgr.add_memory(t, f"{t}内容") is not None
        all_items = mgr.get_memory()
        assert len(all_items) == 4
        # 全部类型混合时按时间/ id 倒序，最新写入的排在最前
        assert all_items[0].content == "fact内容"

    def test_get_memory_short_term_newest_first(self, db):
        mgr = MemoryManager(db)
        mgr.add_memory("short_term", "第一条")
        mgr.add_memory("short_term", "第二条")
        items = mgr.get_memory("short_term")
        assert [m.content for m in items] == ["第二条", "第一条"]

    def test_get_memory_long_term_ordered_by_importance(self, db):
        mgr = MemoryManager(db)
        mgr.add_memory("fact", "低重要性", importance=2)
        mgr.add_memory("fact", "高重要性", importance=9)
        items = mgr.get_memory("fact")
        assert [m.content for m in items] == ["高重要性", "低重要性"]

    def test_get_memory_limit(self, db):
        mgr = MemoryManager(db)
        for i in range(5):
            mgr.add_memory("fact", f"事实{i}")
        assert len(mgr.get_memory("fact", limit=3)) == 3

    def test_invalid_type_raises(self, db):
        mgr = MemoryManager(db)
        with pytest.raises(ValueError):
            mgr.get_memory("bogus")
        with pytest.raises(ValueError):
            mgr.add_memory("bogus", "内容")
        with pytest.raises(ValueError):
            mgr.clear_memory("bogus")

    def test_empty_content_raises(self, db):
        mgr = MemoryManager(db)
        with pytest.raises(ValueError):
            mgr.add_memory("fact", "   ")

    def test_clear_memory_returns_deleted_count(self, db):
        mgr = MemoryManager(db)
        for i in range(3):
            mgr.add_memory("preference", f"偏好{i}")
        assert mgr.clear_memory("preference") == 3
        assert mgr.get_memory("preference") == []
        # 重复清空返回 0
        assert mgr.clear_memory("preference") == 0

    def test_delete_memory(self, db):
        mgr = MemoryManager(db)
        mem = mgr.add_memory("fact", "待删除")
        assert mgr.delete_memory(mem.id) is True
        assert mgr.get_memory("fact") == []
        # 不存在时返回 False
        assert mgr.delete_memory(99999) is False


# ---------- 容量淘汰 ----------


class TestCapacityEviction:
    def test_short_term_evicts_oldest(self, db):
        mgr = MemoryManager(db, capacity_limits={"short_term": 3})
        kept = []
        for i in range(5):
            m = mgr.add_memory("short_term", f"短期{i}")
            kept.append(m.content)
        items = mgr.get_memory("short_term")
        assert len(items) == 3
        # 只保留最新 3 条，最旧两条被淘汰
        contents = {m.content for m in items}
        assert contents == {"短期2", "短期3", "短期4"}

    def test_long_term_evicts_lowest_importance(self, db):
        mgr = MemoryManager(db, capacity_limits={"fact": 2})
        mgr.add_memory("fact", "重要", importance=9)
        mgr.add_memory("fact", "次要", importance=3)
        mgr.add_memory("fact", "中等", importance=5)
        items = mgr.get_memory("fact")
        assert len(items) == 2
        # 重要性最低的「次要」被淘汰
        assert {m.content for m in items} == {"重要", "中等"}

    def test_eviction_only_within_same_type(self, db):
        mgr = MemoryManager(db, capacity_limits={"short_term": 2})
        for i in range(4):
            mgr.add_memory("short_term", f"短期{i}")
        mgr.add_memory("fact", "事实不受影响")
        assert len(mgr.get_memory("short_term")) == 2
        assert len(mgr.get_memory("fact")) == 1

    def test_default_capacity_limits_exist_for_all_types(self):
        for t in ("short_term", "long_term", "preference", "fact"):
            assert DEFAULT_CAPACITY_LIMITS[t] > 0


# ---------- 四类记忆类型隔离 ----------


class TestTypeIsolation:
    def test_read_write_isolated_per_type(self, db):
        mgr = MemoryManager(db)
        for t in ("short_term", "long_term", "preference", "fact"):
            mgr.add_memory(t, f"仅属于{t}")
        for t in ("short_term", "long_term", "preference", "fact"):
            items = mgr.get_memory(t)
            assert len(items) == 1
            assert items[0].content == f"仅属于{t}"
            assert items[0].memory_type == t

    def test_clear_one_type_keeps_others(self, db):
        mgr = MemoryManager(db)
        for t in ("short_term", "long_term", "preference", "fact"):
            mgr.add_memory(t, f"{t}内容")
        mgr.clear_memory("short_term")
        assert mgr.get_memory("short_term") == []
        for t in ("long_term", "preference", "fact"):
            assert len(mgr.get_memory(t)) == 1


# ---------- 旧接口兼容层 ----------


class TestLegacyCompat:
    def test_get_recent_short_term_memories(self, db):
        mgr = MemoryManager(db)
        for i in range(7):
            mgr.add_memory("short_term", f"短期{i}")
        items = mgr.get_recent_short_term_memories(limit=5)
        assert len(items) == 5
        # 与旧行为一致：按时间倒序（最新在前）
        assert items[0].content == "短期6"

    def test_get_long_term_memories_aggregates_three_types(self, db):
        mgr = MemoryManager(db)
        mgr.add_memory("short_term", "短期不应出现", importance=10)
        mgr.add_memory("long_term", "长期", importance=5)
        mgr.add_memory("preference", "偏好", importance=7)
        mgr.add_memory("fact", "事实", importance=9)
        items = mgr.get_long_term_memories(limit=10)
        # 聚合 long_term/preference/fact 三类，按重要性降序，不含 short_term
        assert [m.content for m in items] == ["事实", "偏好", "长期"]

    def test_add_long_term_memory_default_type_fact(self, db):
        mgr = MemoryManager(db)
        mem = mgr.add_long_term_memory("默认类型内容")
        assert mem is not None
        assert mem.memory_type == "fact"
        assert mgr.get_memory("fact")[0].content == "默认类型内容"

    def test_add_long_term_memory_custom_type_and_importance(self, db):
        mgr = MemoryManager(db)
        mem = mgr.add_long_term_memory("偏好内容", memory_type="preference", importance=8)
        assert mem.memory_type == "preference"
        assert mem.importance == 8


# ---------- Prompt 上下文 ----------


class TestBuildMemoryContext:
    def test_context_contains_both_sections(self, db):
        mgr = MemoryManager(db)
        mgr.add_memory("short_term", "最近在讨论MIL方法")
        mgr.add_memory("preference", "偏好中文回答")
        ctx = mgr.build_memory_context()
        assert "用户背景与偏好" in ctx
        assert "偏好中文回答" in ctx
        assert "近期讨论主题" in ctx
        assert "最近在讨论MIL方法" in ctx

    def test_empty_memory_returns_empty_string(self, db):
        mgr = MemoryManager(db)
        assert mgr.build_memory_context() == ""


# ---------- LLM 路径（mock，验证降级） ----------


class TestLLMDegradation:
    @pytest.mark.asyncio
    async def test_summarize_returns_empty_when_few_messages(self, db):
        conv = _make_conversation(db)
        _make_messages(db, conv.id, 3)
        mgr = MemoryManager(db)
        assert await mgr.summarize_conversation(conv.id) == ""

    @pytest.mark.asyncio
    async def test_summarize_success(self, db, monkeypatch):
        conv = _make_conversation(db)
        _make_messages(db, conv.id, 6)
        monkeypatch.setattr(
            llm_service, "chat_completion", AsyncMock(return_value="用户关注MIL方法")
        )
        mgr = MemoryManager(db)
        assert await mgr.summarize_conversation(conv.id) == "用户关注MIL方法"

    @pytest.mark.asyncio
    async def test_summarize_llm_failure_degrades_to_empty(self, db, monkeypatch):
        conv = _make_conversation(db)
        _make_messages(db, conv.id, 6)
        monkeypatch.setattr(
            llm_service, "chat_completion", AsyncMock(side_effect=RuntimeError("LLM挂了"))
        )
        mgr = MemoryManager(db)
        # 异常不抛出到调用方，降级返回空串
        assert await mgr.summarize_conversation(conv.id) == ""

    @pytest.mark.asyncio
    async def test_update_short_term_skips_when_not_multiple_of_5(self, db, monkeypatch):
        conv = _make_conversation(db)
        _make_messages(db, conv.id, 4)
        mock_llm = AsyncMock(return_value="摘要")
        monkeypatch.setattr(llm_service, "chat_completion", mock_llm)
        mgr = MemoryManager(db)
        await mgr.update_short_term_memory(conv.id)
        mock_llm.assert_not_called()
        assert mgr.get_memory("short_term") == []

    @pytest.mark.asyncio
    async def test_update_short_term_writes_summary(self, db, monkeypatch):
        conv = _make_conversation(db)
        _make_messages(db, conv.id, 5)
        monkeypatch.setattr(
            llm_service, "chat_completion", AsyncMock(return_value="第一轮摘要")
        )
        mgr = MemoryManager(db)
        await mgr.update_short_term_memory(conv.id)
        items = mgr.get_memory("short_term")
        assert len(items) == 1
        assert items[0].content == "第一轮摘要"
        assert items[0].source_conversation_id == conv.id

        # 再次达到 5 的倍数时更新已有记录而非新增
        _make_messages(db, conv.id, 5)
        monkeypatch.setattr(
            llm_service, "chat_completion", AsyncMock(return_value="第二轮摘要")
        )
        await mgr.update_short_term_memory(conv.id)
        items = mgr.get_memory("short_term")
        assert len(items) == 1
        assert items[0].content == "第二轮摘要"

    @pytest.mark.asyncio
    async def test_update_short_term_llm_failure_not_raised(self, db, monkeypatch):
        conv = _make_conversation(db)
        _make_messages(db, conv.id, 5)
        monkeypatch.setattr(
            llm_service, "chat_completion", AsyncMock(side_effect=RuntimeError("LLM挂了"))
        )
        mgr = MemoryManager(db)
        # 不抛出、不写入脏数据
        await mgr.update_short_term_memory(conv.id)
        assert mgr.get_memory("short_term") == []

    @pytest.mark.asyncio
    async def test_update_short_term_llm_returns_empty_writes_nothing(self, db, monkeypatch):
        conv = _make_conversation(db)
        _make_messages(db, conv.id, 5)
        monkeypatch.setattr(
            llm_service, "chat_completion", AsyncMock(return_value="")
        )
        mgr = MemoryManager(db)
        await mgr.update_short_term_memory(conv.id)
        assert mgr.get_memory("short_term") == []
