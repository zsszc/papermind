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
