"""chat 路由核心契约测试。

首批覆盖 regenerate 端点（规格反推实证：自初始提交起引用未赋值的
enable_web_search，首帧即 NameError，端点完全不可用）。
"""

from types import SimpleNamespace

import pytest

from app.models import Conversation, Message


@pytest.fixture()
def conversation_with_pair(db):
    """构造含一对 user/assistant 消息的会话。"""
    conv = Conversation(title="t", message_count=2)
    db.add(conv)
    db.flush()
    db.add(Message(conversation_id=conv.id, role="user", content="q", citations=[]))
    db.flush()
    am = Message(conversation_id=conv.id, role="assistant", content="old", citations=[])
    db.add(am)
    db.commit()
    db.refresh(am)
    return conv, am


class TestRegenerate:
    def test_streams_replacement_answer(self, client, conversation_with_pair, monkeypatch):
        """重新生成：SSE 流式返回新答案（修复前首帧即 NameError）。"""
        conv, am = conversation_with_pair

        async def fake_stream(messages, enable_web_search=False):
            yield "new-answer"

        monkeypatch.setattr("app.routers.chat.llm_service.chat_stream", fake_stream)
        monkeypatch.setattr(
            "app.routers.chat.get_vector_store",
            lambda: SimpleNamespace(available=lambda: False),
        )

        resp = client.post(f"/api/chat/conversations/{conv.id}/messages/{am.id}/regenerate")
        assert resp.status_code == 200
        assert "new-answer" in resp.text

    def test_404_when_conversation_missing(self, client):
        resp = client.post("/api/chat/conversations/999/messages/1/regenerate")
        assert resp.status_code == 404

    def test_404_when_target_not_assistant(self, client, conversation_with_pair):
        conv, _ = conversation_with_pair
        user_msg = [m for m in conv.messages if m.role == "user"][0]
        resp = client.post(f"/api/chat/conversations/{conv.id}/messages/{user_msg.id}/regenerate")
        assert resp.status_code == 404


class TestDeleteMessagesFrom:
    """delete_messages_from 截断删除后回溯 message_count（Batch7b-F11）。

    行为契约（specs/phases/batch-7b-fixes/spec.md 3.4）：
    删除后会话 message_count == 实际剩余消息数；删全部消息归 0；会话不存在仍 404。
    """

    @pytest.fixture()
    def conversation_with_four(self, db):
        """构造 4 条消息的会话；message_count=5 模拟流式计数领先 1 的现状语义。"""
        import datetime

        conv = Conversation(title="t", message_count=5)
        db.add(conv)
        db.flush()
        base = datetime.datetime(2026, 1, 1, 0, 0, 0)
        for i in range(4):
            db.add(Message(
                conversation_id=conv.id,
                role="user" if i % 2 == 0 else "assistant",
                content=f"m{i}",
                citations=[],
                # 显式递增时间戳，保证 created_at 升序与插入序一致（消除同秒并列的不确定性）
                created_at=base + datetime.timedelta(seconds=i),
            ))
        db.commit()
        db.refresh(conv)
        return conv

    def test_truncates_and_backfills_message_count(self, client, db, conversation_with_four):
        """从第 3 条起删除：剩 2 条，message_count 回溯为 2（修复前保持旧值 5）。"""
        conv = conversation_with_four
        target = sorted(conv.messages, key=lambda m: m.created_at)[2]

        resp = client.delete(f"/api/chat/conversations/{conv.id}/messages/{target.id}")
        assert resp.status_code == 204

        db.expire_all()
        remaining = (
            db.query(Message).filter(Message.conversation_id == conv.id)
            .order_by(Message.created_at.asc()).all()
        )
        assert [m.content for m in remaining] == ["m0", "m1"]
        assert conv.message_count == len(remaining)

    def test_delete_all_messages_zeroes_count(self, client, db, conversation_with_four):
        """边界：从首条起全部删除，message_count 归 0。"""
        conv = conversation_with_four
        first = sorted(conv.messages, key=lambda m: m.created_at)[0]

        resp = client.delete(f"/api/chat/conversations/{conv.id}/messages/{first.id}")
        assert resp.status_code == 204

        db.expire_all()
        remaining = db.query(Message).filter(Message.conversation_id == conv.id).count()
        assert remaining == 0
        assert conv.message_count == 0

    def test_404_when_conversation_missing(self, client):
        """不回归：会话不存在仍 404。"""
        resp = client.delete("/api/chat/conversations/999/messages/1")
        assert resp.status_code == 404
