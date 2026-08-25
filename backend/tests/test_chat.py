"""chat 路由核心契约测试。

首批覆盖 regenerate 端点（规格反推实证：自初始提交起引用未赋值的
enable_web_search，首帧即 NameError，端点完全不可用）。
"""

import dataclasses
import json
import sys
import types
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

        resp = client.post(
            f"/api/chat/conversations/{conv.id}/messages/{am.id}/regenerate",
            json={"expected_revision": 0},
        )
        assert resp.status_code == 200
        assert "new-answer" in resp.text

    def test_regenerate_uses_same_atomic_citation_finalization(
        self, client, db, conversation_with_pair, monkeypatch
    ):
        conv, am = conversation_with_pair
        chunk = {
            "source": "p1_c0", "paper_id": 1, "title": "合成文献",
            "authors": "", "year": 2026, "page_number": 1, "content": "合成证据",
        }

        class FakePipeline:
            def __init__(self, *args, **kwargs):
                pass

            def search(self, *args, **kwargs):
                return [chunk]

        async def fake_stream(messages, enable_web_search=False):
            yield "新答案[^1^]越界[^9^]"

        from .conftest import TestingSessionLocal

        monkeypatch.setattr("app.routers.chat.RetrievalPipeline", FakePipeline)
        monkeypatch.setattr("app.routers.chat.llm_service.chat_stream", fake_stream)
        monkeypatch.setattr("app.database.SessionLocal", TestingSessionLocal)

        resp = client.post(
            f"/api/chat/conversations/{conv.id}/messages/{am.id}/regenerate",
            json={"expected_revision": 0},
        )
        frames = [
            json.loads(line[len("data: "):])
            for line in resp.text.splitlines() if line.startswith("data: ")
        ]

        assert frames[-1]["content"] == "新答案[^1^]越界"
        assert [item["source"] for item in frames[-1]["citations"]] == ["p1_c0"]
        db.expire_all()
        saved = db.query(Message).filter(Message.id == am.id).one()
        assert saved.content == frames[-1]["content"]
        assert saved.citations[0]["removed"] == 1
        assert saved.revision == 1
        assert frames[-1]["revision"] == 1

    def test_404_when_conversation_missing(self, client):
        resp = client.post(
            "/api/chat/conversations/999/messages/1/regenerate",
            json={"expected_revision": 0},
        )
        assert resp.status_code == 404

    def test_404_when_target_not_assistant(self, client, conversation_with_pair):
        conv, _ = conversation_with_pair
        user_msg = [m for m in conv.messages if m.role == "user"][0]
        resp = client.post(
            f"/api/chat/conversations/{conv.id}/messages/{user_msg.id}/regenerate",
            json={"expected_revision": 0},
        )
        assert resp.status_code == 404

    def test_llm_error_sentinel_rolls_back_original_message(
        self, client, db, conversation_with_pair, monkeypatch
    ):
        """RED：重生成失败只能发脱敏 error，原正文与引用不可被错误串覆盖。"""
        conv, am = conversation_with_pair
        am.citations = [{"source": "p1_c0", "paper_id": 1, "title": "原引用"}]
        db.commit()

        async def fake_stream(messages, enable_web_search=False):
            yield "不应提交的半条回答"
            yield "\n[调用 LLM 出错: private-stack-canary]"

        from .conftest import TestingSessionLocal

        monkeypatch.setattr("app.routers.chat.llm_service.chat_stream", fake_stream)
        monkeypatch.setattr(
            "app.routers.chat.get_vector_store",
            lambda: SimpleNamespace(available=lambda: False),
        )
        monkeypatch.setattr("app.database.SessionLocal", TestingSessionLocal)

        resp = client.post(
            f"/api/chat/conversations/{conv.id}/messages/{am.id}/regenerate",
            json={"expected_revision": 0},
        )
        frames = [
            json.loads(line[len("data: "):])
            for line in resp.text.splitlines() if line.startswith("data: ")
        ]

        assert frames[-1] == {
            "error": "AI 服务暂时不可用，请稍后重试",
            "error_code": "llm_generation_failed",
            "conversation_id": conv.id,
        }
        assert not any(frame.get("finished") is True for frame in frames)
        assert "private-stack-canary" not in resp.text
        db.expire_all()
        saved = db.query(Message).filter(Message.id == am.id).one()
        assert saved.content == "old"
        assert saved.citations == [{"source": "p1_c0", "paper_id": 1, "title": "原引用"}]

    def test_stale_revision_returns_409_before_llm(
        self, client, conversation_with_pair, monkeypatch
    ):
        """RED：过期客户端不得进入检索或 LLM，更不能覆盖较新消息。"""
        conv, am = conversation_with_pair
        calls = {"llm": 0}

        async def fake_stream(messages, enable_web_search=False):
            calls["llm"] += 1
            yield "不应调用"

        monkeypatch.setattr("app.routers.chat.llm_service.chat_stream", fake_stream)
        resp = client.post(
            f"/api/chat/conversations/{conv.id}/messages/{am.id}/regenerate",
            json={"expected_revision": 99},
        )

        assert resp.status_code == 409
        assert calls["llm"] == 0

    def test_concurrent_mutation_is_not_overwritten(
        self, client, db, conversation_with_pair, monkeypatch
    ):
        """RED：生成期间外部更新 revision 后，终态条件更新失败且保留外部状态。"""
        conv, am = conversation_with_pair
        from .conftest import TestingSessionLocal

        async def fake_stream(messages, enable_web_search=False):
            with TestingSessionLocal() as other_db:
                other = other_db.query(Message).filter(Message.id == am.id).one()
                other.content = "外部较新答案"
                other.citations = [{"source": "p9_c9", "paper_id": 9, "title": "外部引用"}]
                if hasattr(other, "revision"):
                    other.revision = 1
                other_db.commit()
            yield "本请求旧答案"

        monkeypatch.setattr("app.routers.chat.llm_service.chat_stream", fake_stream)
        monkeypatch.setattr(
            "app.routers.chat.get_vector_store",
            lambda: SimpleNamespace(available=lambda: False),
        )
        monkeypatch.setattr("app.database.SessionLocal", TestingSessionLocal)

        resp = client.post(
            f"/api/chat/conversations/{conv.id}/messages/{am.id}/regenerate",
            json={"expected_revision": 0},
        )
        frames = self._frames(resp.text)

        assert frames[-1]["error_code"] == "regenerate_conflict"
        assert not any(frame.get("finished") is True for frame in frames)
        db.expire_all()
        saved = db.query(Message).filter(Message.id == am.id).one()
        assert saved.content == "外部较新答案"
        assert saved.citations[0]["source"] == "p9_c9"

    @staticmethod
    def _frames(resp_text):
        return [
            json.loads(line[len("data: "):])
            for line in resp_text.splitlines()
            if line.startswith("data: ")
        ]


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


class TestGuardrailsIntegration:
    """C1 路由集成（Phase C）：流式完成后、落库前执行 verify_citations。

    行为契约（specs/phases/phase-c-guardrails/spec.md 3.1 / AC2）：
    mock LLM 输出含越界引用 → SSE 完成后落库 citations 带 verified=false、
    文本中越界标记已剔除；全部有效时 verified=true 且文本不被篡改。
    """

    @staticmethod
    def _chunk() -> dict:
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

    def _patch_deps(self, monkeypatch, answer_text, retrieved):
        """替换 LLM 流式输出、向量库与流式落库会话工厂，全程离线。"""
        from app.services import agent_graph

        from .conftest import TestingSessionLocal

        async def fake_stream(messages, enable_web_search=False):
            yield answer_text

        monkeypatch.setattr("app.routers.chat.llm_service.chat_stream", fake_stream)
        store = SimpleNamespace(
            available=lambda: True,
            search=lambda query, top_k, filters: retrieved,
        )
        monkeypatch.setattr(agent_graph, "get_vector_store", lambda: store)
        # 流式落库内部 `from app.database import SessionLocal`，指向内存库
        monkeypatch.setattr("app.database.SessionLocal", TestingSessionLocal)

    def _assistant_message(self, db, conversation_id):
        db.expire_all()
        return (
            db.query(Message)
            .filter(Message.conversation_id == conversation_id, Message.role == "assistant")
            .one()
        )

    def test_out_of_range_citation_marked_unverified(self, client, db, monkeypatch):
        """越界引用：落库文本剔除 [^9^]，citations 带 verified=false 与 removed 计数。"""
        chunk = self._chunk()
        self._patch_deps(monkeypatch, "根据文献[^1^]，结论成立[^9^]。", [chunk])

        resp = client.post("/api/chat", json={"message": "MIL 是什么"})
        assert resp.status_code == 200
        assert '"finished": true' in resp.text
        frames = [
            json.loads(line[len("data: "):])
            for line in resp.text.splitlines()
            if line.startswith("data: ")
        ]
        assert frames[-1]["content"] == "根据文献[^1^]，结论成立。"
        assert frames[-1]["verification"] == {
            "total": 2, "valid": 1, "removed": 1, "verified": False,
        }
        assert [item["source"] for item in frames[-1]["citations"]] == ["p1_c0"]

        msg = self._assistant_message(db, 1)
        assert msg.content == "根据文献[^1^]，结论成立。"
        assert len(msg.citations) == 1
        saved = msg.citations[0]
        assert saved["verified"] is False
        assert saved["removed"] == 1
        # 原 7 键不丢失
        assert saved["source"] == "p1_c0"
        assert saved["title"] == "结直肠癌T分期研究"

    def test_finished_does_not_publish_retrieved_but_uncited_chunks(
        self, client, db, monkeypatch
    ):
        chunk = self._chunk()
        self._patch_deps(monkeypatch, "没有引用标记的回答。", [chunk])

        resp = client.post("/api/chat", json={"message": "MIL 是什么"})
        frames = [
            json.loads(line[len("data: "):])
            for line in resp.text.splitlines()
            if line.startswith("data: ")
        ]

        assert frames[-1]["finished"] is True
        assert frames[-1]["content"] == "没有引用标记的回答。"
        assert frames[-1]["citations"] == []

    def test_all_valid_citations_verified(self, client, db, monkeypatch):
        """全部有效：文本不被篡改，citations 带 verified=true、removed=0。"""
        chunk = self._chunk()
        answer = "根据文献[^1^]，结论成立。"
        self._patch_deps(monkeypatch, answer, [chunk])

        resp = client.post("/api/chat", json={"message": "MIL 是什么"})
        assert resp.status_code == 200

        msg = self._assistant_message(db, 1)
        assert msg.content == answer
        assert msg.citations[0]["verified"] is True
        assert msg.citations[0]["removed"] == 0

    def test_non_stream_uses_same_citation_finalization(self, client, monkeypatch):
        chunk = self._chunk()
        from app.services import agent_graph

        async def fake_completion(messages):
            return "同步答案[^1^]越界[^7^]"

        store = SimpleNamespace(
            available=lambda: True,
            search=lambda query, top_k, filters: [chunk],
        )
        monkeypatch.setattr(agent_graph, "get_vector_store", lambda: store)
        monkeypatch.setattr(
            "app.routers.chat.llm_service.chat_completion", fake_completion
        )

        resp = client.post("/api/chat", json={"message": "MIL", "stream": False})

        assert resp.status_code == 200
        assert resp.json()["content"] == "同步答案[^1^]越界"
        assert [item["source"] for item in resp.json()["citations"]] == ["p1_c0"]
        assert resp.json()["verification"]["removed"] == 1


class TestGenerationFailureTransactions:
    """Batch 23C：LLM 失败不是正文，assistant 与计数必须 fail-close。"""

    @staticmethod
    def _patch_orchestration(monkeypatch):
        monkeypatch.setattr(
            "app.routers.chat.run_pre_orchestration",
            lambda **kwargs: {
                "messages": [{"role": "user", "content": kwargs["user_message"]}],
                "context_chunks": [],
                "web_search_enabled": False,
                "history_total": 1,
            },
        )

    @staticmethod
    def _frames(response):
        return [
            json.loads(line[len("data: "):])
            for line in response.text.splitlines() if line.startswith("data: ")
        ]

    def test_stream_error_sentinel_is_sanitized_and_not_persisted(
        self, client, db, monkeypatch
    ):
        self._patch_orchestration(monkeypatch)

        async def fake_stream(messages, enable_web_search=False):
            yield "不应提交的半条回答"
            yield "\n[调用 LLM 出错: private-stack-canary]"

        from .conftest import TestingSessionLocal

        monkeypatch.setattr("app.routers.chat.llm_service.chat_stream", fake_stream)
        monkeypatch.setattr("app.database.SessionLocal", TestingSessionLocal)

        response = client.post("/api/chat", json={"message": "失败问题"})
        frames = self._frames(response)

        assert frames[-1] == {
            "error": "AI 服务暂时不可用，请稍后重试",
            "error_code": "llm_generation_failed",
            "conversation_id": 1,
        }
        assert not any(frame.get("finished") is True for frame in frames)
        assert "private-stack-canary" not in response.text
        db.expire_all()
        assert db.query(Message).filter(Message.role == "assistant").count() == 0
        conv = db.query(Conversation).one()
        assert conv.message_count == db.query(Message).filter(
            Message.conversation_id == conv.id
        ).count() == 1

    def test_empty_stream_is_failure_and_not_success(self, client, db, monkeypatch):
        self._patch_orchestration(monkeypatch)

        async def fake_stream(messages, enable_web_search=False):
            if False:
                yield ""

        from .conftest import TestingSessionLocal

        monkeypatch.setattr("app.routers.chat.llm_service.chat_stream", fake_stream)
        monkeypatch.setattr("app.database.SessionLocal", TestingSessionLocal)

        response = client.post("/api/chat", json={"message": "空结果"})
        frames = self._frames(response)

        assert frames[-1]["error_code"] == "empty_generation"
        assert not any(frame.get("finished") is True for frame in frames)
        assert db.query(Message).filter(Message.role == "assistant").count() == 0

    def test_stream_exception_after_delta_has_one_error_terminal(
        self, client, db, monkeypatch
    ):
        """真实严格流会抛异常：半条 delta 后只许 error，异常正文不得泄漏。"""
        self._patch_orchestration(monkeypatch)

        async def fake_stream(messages, enable_web_search=False):
            yield "不应落库的 provisional"
            raise RuntimeError("raised-private-canary")

        from .conftest import TestingSessionLocal

        monkeypatch.setattr("app.routers.chat.llm_service.chat_stream", fake_stream)
        monkeypatch.setattr("app.database.SessionLocal", TestingSessionLocal)

        response = client.post("/api/chat", json={"message": "中途失败"})
        frames = self._frames(response)

        assert sum("error" in frame for frame in frames) == 1
        assert frames[-1]["error_code"] == "llm_generation_failed"
        assert not any(frame.get("finished") is True for frame in frames)
        assert "raised-private-canary" not in response.text
        assert db.query(Message).filter(Message.role == "assistant").count() == 0

    def test_non_stream_error_sentinel_returns_sanitized_503(
        self, client, db, monkeypatch
    ):
        self._patch_orchestration(monkeypatch)

        async def fake_completion(messages):
            return "[调用 LLM 出错: private-stack-canary]"

        monkeypatch.setattr(
            "app.routers.chat.llm_service.chat_completion", fake_completion
        )

        response = client.post(
            "/api/chat", json={"message": "失败问题", "stream": False}
        )

        assert response.status_code == 503
        assert response.json()["detail"] == "AI 服务暂时不可用，请稍后重试"
        assert "private-stack-canary" not in response.text
        db.expire_all()
        assert db.query(Message).filter(Message.role == "assistant").count() == 0
        conv = db.query(Conversation).one()
        assert conv.message_count == db.query(Message).filter(
            Message.conversation_id == conv.id
        ).count() == 1


class TestDeepReview:
    """F2：POST /api/chat/deep-review（Phase F，spec 3.2）。

    行为契约：
    - 事件序列 {type:"plan", questions:[...]} → 多个 {delta} → {finished, citations}；
    - 复用 chat.py 的 SSE 帧格式与落库前 Guardrails 校验（citations 仅本地 chunk）；
    - plan / synthesize 失败 → {error} 帧；单个子问题失败降级、不阻塞整体；
    - 服务层 services/deep_review.py（F1 并行开发）全程以假模块注入 mock，
      仅依赖契约接口 plan(topic) / execute(sub_question, db=...) /
      synthesize(topic, sub_answers)。
    """

    # ---------- 测试工具 ----------

    @staticmethod
    def _frames(resp_text) -> list:
        """解析 SSE 响应文本为帧 dict 列表。"""
        frames = []
        for line in resp_text.splitlines():
            if line.startswith("data: "):
                frames.append(json.loads(line[len("data: "):]))
        return frames

    @staticmethod
    def _chunk(source="p1_c0", paper_id=1) -> dict:
        """一条带完整引用字段的本地检索片段。"""
        return {
            "source": source,
            "paper_id": paper_id,
            "title": "结直肠癌T分期研究",
            "authors": "张三",
            "year": 2024,
            "page_number": 3,
            "content": "多实例学习在病理图像上的应用……",
        }

    @staticmethod
    def _install_fake_service(monkeypatch, plan, execute, synthesize, use_singleton=False):
        """向 sys.modules 注入假的 app.services.deep_review（F1 尚未落地）。

        use_singleton=True 时以 deep_review_service 单例形式暴露接口，
        否则以模块级函数形式暴露（两种暴露方式路由都应兼容）。
        """
        fake = types.ModuleType("app.services.deep_review")
        if use_singleton:
            setattr(fake, "deep_review_service", SimpleNamespace(
                plan=plan, execute=execute, synthesize=synthesize
            ))
        else:
            setattr(fake, "plan", plan)
            setattr(fake, "execute", execute)
            setattr(fake, "synthesize", synthesize)
        monkeypatch.setitem(sys.modules, "app.services.deep_review", fake)
        # 同步挂到包属性上，兼容 `from app.services import deep_review` 写法
        monkeypatch.setattr("app.services.deep_review", fake, raising=False)
        return fake

    def _patch_sessionlocal(self, monkeypatch):
        """流式落库内部 `from app.database import SessionLocal`，指向内存库。"""
        from .conftest import TestingSessionLocal

        monkeypatch.setattr("app.database.SessionLocal", TestingSessionLocal)

    def _happy_fakes(self, content="综述正文。", citations=None, questions=None):
        """构造一组正常路径的 plan/execute/synthesize 假实现与调用记录。"""
        calls = {"plan": [], "execute": [], "synthesize": []}
        qs = questions if questions is not None else [
            {"id": 1, "question": "MIL 方法有哪些"},
            {"id": 2, "question": "ViT 在 WSI 的应用"},
        ]
        cits = citations if citations is not None else [self._chunk()]

        async def fake_plan(topic):
            calls["plan"].append(topic)
            return qs

        async def fake_execute(q, db=None):
            calls["execute"].append(q)
            return {"question": q, "answer": "子答案", "citations": cits}

        async def fake_synthesize(topic, sub_answers):
            calls["synthesize"].append((topic, sub_answers))
            return {"content": content, "citations": cits}

        return calls, fake_plan, fake_execute, fake_synthesize

    # ---------- 用例 ----------

    def test_event_sequence_plan_deltas_finished(self, client, db, monkeypatch):
        """帧序列：plan 在先 → 多个 delta（拼接==全文）→ finished 含 citations。"""
        content = "综述正文。" * 200  # 1000 字符，保证多个 delta 帧
        citations = [self._chunk("p1_c0", 1), self._chunk("p2_c1", 2)]
        questions = [
            {"id": 1, "question": "MIL 方法有哪些"},
            {"id": 2, "question": "ViT 在 WSI 的应用"},
            {"id": 3, "question": "T 分期评价指标"},
        ]
        calls, fake_plan, fake_execute, fake_synthesize = self._happy_fakes(
            content=content, citations=citations, questions=questions
        )
        self._install_fake_service(monkeypatch, fake_plan, fake_execute, fake_synthesize)
        self._patch_sessionlocal(monkeypatch)

        resp = client.post("/api/chat/deep-review", json={"topic": "结直肠癌 T 分期综述"})
        assert resp.status_code == 200
        frames = self._frames(resp.text)

        # plan 帧在先，携带子问题列表
        assert frames[0]["type"] == "plan"
        assert frames[0]["questions"] == questions
        assert frames[0]["conversation_id"] is None
        # 尾帧 finished + citations + conversation_id
        assert frames[-1]["finished"] is True
        assert frames[-1]["delta"] == ""
        assert frames[-1]["citations"] == citations
        assert frames[-1]["conversation_id"] == 1
        # 中间为多个 delta 帧，拼接 == 综述全文
        delta_frames = frames[1:-1]
        assert len(delta_frames) >= 2
        assert all(f["finished"] is False for f in delta_frames)
        assert all(f["conversation_id"] is None for f in delta_frames)
        assert "".join(f["delta"] for f in delta_frames) == content
        # 服务层契约调用：plan(topic) / 逐子问题 execute / synthesize(topic, sub_answers)
        assert calls["plan"] == ["结直肠癌 T 分期综述"]
        assert [q["id"] for q in calls["execute"]] == [1, 2, 3]
        assert calls["synthesize"][0][0] == "结直肠癌 T 分期综述"
        assert len(calls["synthesize"][0][1]) == 3

    def test_persists_messages_with_guardrails(self, client, db, monkeypatch):
        """落库：用户消息(topic)+助手消息(清洗后综述)，citations 带 verified/removed。"""
        content = "根据文献[^1^]，结论成立[^9^]。"
        calls, fake_plan, fake_execute, fake_synthesize = self._happy_fakes(content=content)
        self._install_fake_service(monkeypatch, fake_plan, fake_execute, fake_synthesize)
        self._patch_sessionlocal(monkeypatch)

        resp = client.post("/api/chat/deep-review", json={"topic": "MIL 综述"})
        assert resp.status_code == 200

        db.expire_all()
        msgs = (
            db.query(Message)
            .filter(Message.conversation_id == 1)
            .order_by(Message.id.asc())
            .all()
        )
        assert [m.role for m in msgs] == ["user", "assistant"]
        assert msgs[0].content == "MIL 综述"
        # Guardrails：越界 [^9^] 标记落库前剔除
        assert msgs[1].content == "根据文献[^1^]，结论成立。"
        saved = msgs[1].citations[0]
        assert saved["verified"] is False
        assert saved["removed"] == 1
        assert saved["source"] == "p1_c0"
        # 自动建会话：标题取 topic[:30]，计数 = 实际消息数
        conv = db.query(Conversation).filter(Conversation.id == 1).first()
        assert conv.title == "MIL 综述"
        assert conv.message_count == 2

    def test_citations_local_chunks_only(self, client, db, monkeypatch):
        """citations 仅本地 chunk：外部来源（无 paper_id）被过滤，不进帧也不落库。"""
        local = self._chunk()
        external = {"url": "https://example.com/x", "title": "外部网页"}
        calls, fake_plan, fake_execute, fake_synthesize = self._happy_fakes(
            citations=[local, external]
        )
        self._install_fake_service(monkeypatch, fake_plan, fake_execute, fake_synthesize)
        self._patch_sessionlocal(monkeypatch)

        resp = client.post("/api/chat/deep-review", json={"topic": "MIL 综述"})
        assert resp.status_code == 200
        frames = self._frames(resp.text)
        assert frames[-1]["citations"] == [local]

        db.expire_all()
        msg = (
            db.query(Message)
            .filter(Message.conversation_id == 1, Message.role == "assistant")
            .one()
        )
        assert len(msg.citations) == 1
        assert msg.citations[0]["source"] == "p1_c0"

    def test_plan_failure_emits_error_frame(self, client, db, monkeypatch):
        """plan 失败 → 单个 {error} 帧（脱敏文案），不落库任何消息。"""

        async def bad_plan(topic):
            raise RuntimeError("LLM 不可用")

        calls, _, fake_execute, fake_synthesize = self._happy_fakes()
        self._install_fake_service(monkeypatch, bad_plan, fake_execute, fake_synthesize)
        self._patch_sessionlocal(monkeypatch)

        resp = client.post("/api/chat/deep-review", json={"topic": "MIL 综述"})
        assert resp.status_code == 200
        frames = self._frames(resp.text)
        assert len(frames) == 1
        assert "error" in frames[0]
        assert "LLM 不可用" not in frames[0]["error"]  # 异常原文不透出（宪法第 13 条）
        assert db.query(Message).count() == 0
        assert db.query(Conversation).count() == 0

    def test_single_subquestion_failure_degrades(self, client, db, monkeypatch):
        """单个子问题失败降级为「该子问题检索不足」占位，不阻塞整体流程。"""
        captured = {}

        async def fake_plan(topic):
            return ["q1", "q2"]

        async def fake_execute(q, db=None):
            if q == "q2":
                raise RuntimeError("检索超时")
            return {"question": q, "answer": "答案1", "citations": []}

        async def fake_synthesize(topic, sub_answers):
            captured["sub_answers"] = sub_answers
            return {"content": "综述全文", "citations": []}

        self._install_fake_service(monkeypatch, fake_plan, fake_execute, fake_synthesize)
        self._patch_sessionlocal(monkeypatch)

        resp = client.post("/api/chat/deep-review", json={"topic": "MIL 综述"})
        assert resp.status_code == 200
        frames = self._frames(resp.text)
        assert frames[0]["type"] == "plan"
        assert frames[0]["questions"] == ["q1", "q2"]
        assert frames[-1]["finished"] is True
        # 失败的子问题以降级占位进入 synthesize
        assert len(captured["sub_answers"]) == 2
        assert captured["sub_answers"][1]["answer"] == "该子问题检索不足"

    def test_synthesize_failure_emits_error_frame(self, client, db, monkeypatch):
        """synthesize 失败 → {error} 帧收尾，无 finished 帧，不落库。"""

        async def bad_synthesize(topic, sub_answers):
            raise RuntimeError("LLM 超时")

        calls, fake_plan, fake_execute, _ = self._happy_fakes()
        self._install_fake_service(monkeypatch, fake_plan, fake_execute, bad_synthesize)
        self._patch_sessionlocal(monkeypatch)

        resp = client.post("/api/chat/deep-review", json={"topic": "MIL 综述"})
        assert resp.status_code == 200
        frames = self._frames(resp.text)
        assert "error" in frames[-1]
        assert all(not f.get("finished") for f in frames)
        assert db.query(Message).count() == 0
        assert db.query(Conversation).count() == 0

    def test_appends_to_existing_conversation(self, client, db, monkeypatch):
        """携带 conversation_id：消息追加到已有会话，计数在原有基础上 +2。"""
        conv = Conversation(title="已有会话", message_count=99)
        db.add(conv)
        db.flush()
        db.add(Message(
            conversation_id=conv.id, role="user", content="旧问题", citations=[]
        ))
        db.add(Message(
            conversation_id=conv.id, role="assistant", content="旧回答", citations=[]
        ))
        db.commit()
        db.refresh(conv)

        calls, fake_plan, fake_execute, fake_synthesize = self._happy_fakes()
        self._install_fake_service(monkeypatch, fake_plan, fake_execute, fake_synthesize)
        self._patch_sessionlocal(monkeypatch)

        resp = client.post(
            "/api/chat/deep-review",
            json={"topic": "MIL 综述", "conversation_id": conv.id},
        )
        assert resp.status_code == 200
        frames = self._frames(resp.text)
        assert frames[-1]["conversation_id"] == conv.id

        db.expire_all()
        conv = db.query(Conversation).filter(Conversation.id == conv.id).first()
        assert conv.message_count == 4
        assert db.query(Message).filter(Message.conversation_id == conv.id).count() == 4

    @pytest.mark.parametrize("review", ["", "   ", {"content": None, "citations": []}])
    def test_empty_review_emits_error_without_orphan(
        self, client, db, monkeypatch, review
    ):
        """RED：空/非字符串汇总不得创建会话或发送 finished。"""
        calls, fake_plan, fake_execute, _ = self._happy_fakes()

        async def empty_synthesize(topic, sub_answers):
            return review

        self._install_fake_service(monkeypatch, fake_plan, fake_execute, empty_synthesize)
        self._patch_sessionlocal(monkeypatch)

        resp = client.post("/api/chat/deep-review", json={"topic": "MIL 综述"})
        frames = self._frames(resp.text)

        assert frames[-1]["error_code"] == "empty_generation"
        assert not any(frame.get("finished") is True for frame in frames)
        assert db.query(Conversation).count() == 0
        assert db.query(Message).count() == 0

    def test_guardrail_empty_review_emits_error_without_orphan(
        self, client, db, monkeypatch
    ):
        """RED：正文只含越界引用，清洗为空时不得落库或假成功。"""
        calls, fake_plan, fake_execute, fake_synthesize = self._happy_fakes(
            content="[^9^]", citations=[self._chunk()]
        )
        self._install_fake_service(monkeypatch, fake_plan, fake_execute, fake_synthesize)
        self._patch_sessionlocal(monkeypatch)

        resp = client.post("/api/chat/deep-review", json={"topic": "MIL 综述"})
        frames = self._frames(resp.text)

        assert frames[-1]["error_code"] == "empty_generation"
        assert not any(frame.get("finished") is True for frame in frames)
        assert db.query(Conversation).count() == 0
        assert db.query(Message).count() == 0

    def test_service_singleton_entrypoint(self, client, db, monkeypatch):
        """F1 若以 deep_review_service 单例暴露接口，路由同样可用。"""
        calls, fake_plan, fake_execute, fake_synthesize = self._happy_fakes()
        self._install_fake_service(
            monkeypatch, fake_plan, fake_execute, fake_synthesize, use_singleton=True
        )
        self._patch_sessionlocal(monkeypatch)

        resp = client.post("/api/chat/deep-review", json={"topic": "MIL 综述"})
        assert resp.status_code == 200
        frames = self._frames(resp.text)
        assert frames[0]["type"] == "plan"
        assert frames[-1]["finished"] is True

    def test_plan_frame_serializes_dataclass_questions(self, client, db, monkeypatch):
        """plan 帧对 F1 自定义 SubQuestion 类型（dataclass）做 JSON 序列化。"""

        @dataclasses.dataclass
        class SubQuestion:
            question: str

        async def fake_plan(topic):
            return [SubQuestion(question="子问题A")]

        calls, _, fake_execute, fake_synthesize = self._happy_fakes()
        self._install_fake_service(monkeypatch, fake_plan, fake_execute, fake_synthesize)
        self._patch_sessionlocal(monkeypatch)

        resp = client.post("/api/chat/deep-review", json={"topic": "MIL 综述"})
        assert resp.status_code == 200
        frames = self._frames(resp.text)
        assert frames[0]["questions"] == [{"question": "子问题A"}]

    def test_citations_fallback_aggregates_sub_answer_chunks(self, client, db, monkeypatch):
        """Review 缺省 citations 时，回退聚合各子答案引用。

        兼容 F1 SubAnswer 的 chunks 字段命名（spec 3.1：execute 带本地引用）。
        """
        chunk = self._chunk()

        async def fake_plan(topic):
            return ["q1"]

        async def fake_execute(q, db=None):
            # F1 真实 SubAnswer 的引用字段名为 chunks
            return {"question": q, "answer": "子答案", "chunks": [chunk]}

        async def fake_synthesize(topic, sub_answers):
            return {"content": "综述全文", "citations": None}

        self._install_fake_service(monkeypatch, fake_plan, fake_execute, fake_synthesize)
        self._patch_sessionlocal(monkeypatch)

        resp = client.post("/api/chat/deep-review", json={"topic": "MIL 综述"})
        assert resp.status_code == 200
        frames = self._frames(resp.text)
        assert frames[-1]["finished"] is True
        assert frames[-1]["citations"] == [chunk]

    def test_404_when_conversation_missing(self, client):
        """conversation_id 指向不存在会话 → 404（与 /api/chat 一致）。"""
        resp = client.post(
            "/api/chat/deep-review", json={"topic": "x", "conversation_id": 999}
        )
        assert resp.status_code == 404

    def test_zero_conversation_id_is_not_treated_as_missing_input(self, client, db):
        """RED：显式 conversation_id=0 必须按指定会话校验，不得新建会话。"""
        resp = client.post(
            "/api/chat/deep-review", json={"topic": "x", "conversation_id": 0}
        )
        assert resp.status_code == 404
        assert db.query(Conversation).count() == 0

    def test_422_when_topic_blank(self, client, db):
        """RED：纯空白主题在进入流式任务和数据库前拒绝。"""
        resp = client.post("/api/chat/deep-review", json={"topic": "   "})
        assert resp.status_code == 422
        assert db.query(Conversation).count() == 0

    def test_422_when_topic_missing(self, client):
        """缺 topic → 422（topic 必填）。"""
        resp = client.post("/api/chat/deep-review", json={})
        assert resp.status_code == 422
