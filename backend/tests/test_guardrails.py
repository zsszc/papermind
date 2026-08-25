"""Guardrails 防幻觉护栏测试（Phase C）。

C1：verify_citations 纯函数——校验答案中的 [^n^] 引用标记是否落在本次检索
片段编号范围内（1-based），越界/零检索时剔除标记并输出 verified 报告。
C2：零检索时 build_messages 组装的 system prompt 追加拒答硬约束段。

规格：specs/phases/phase-c-guardrails/spec.md 3.1 / 3.2。
全程不调真实 LLM / embedding / 网络。
"""

import logging
from types import SimpleNamespace

import pytest

from app.models import Conversation, Message
from app.services import agent_graph
from app.services.agent_graph import run_pre_orchestration, verify_citations


def _chunk(i: int = 1) -> dict:
    """一条带完整引用字段的检索片段。"""
    return {
        "source": f"p{i}_c0",
        "paper_id": i,
        "title": f"文献{i}",
        "authors": "张三",
        "year": 2024,
        "page_number": 3,
        "content": "多实例学习在病理图像上的应用……",
    }


class TestVerifyCitations:
    """verify_citations 纯函数边界用例（spec 3.1 / 第 4 节边界表）。"""

    def test_all_valid_markers_kept(self):
        """全部有效：文本原样保留，verified=true。"""
        text = "结论A[^1^]，结论B[^2^]。"
        cleaned, report = verify_citations(text, [_chunk(1), _chunk(2), _chunk(3)])
        assert cleaned == text
        assert report == {"total": 2, "valid": 2, "removed": 0, "verified": True}

    def test_out_of_range_marker_removed(self):
        """越界：剔除标记但保留语句本身，verified=false。"""
        cleaned, report = verify_citations("结论成立[^5^]。", [_chunk(1), _chunk(2)])
        assert cleaned == "结论成立。"
        assert report == {"total": 1, "valid": 0, "removed": 1, "verified": False}

    def test_marker_removed_when_no_retrieval(self):
        """零检索：任何 [^n^] 均越界，剔除 + verified=false。"""
        cleaned, report = verify_citations("答案[^1^]。", [])
        assert cleaned == "答案。"
        assert report == {"total": 1, "valid": 0, "removed": 1, "verified": False}

    def test_no_markers_with_retrieval(self):
        """无引用标记且有检索：不篡改文本，verified=true（total=0）。"""
        text = "没有任何引用标注的回答。"
        cleaned, report = verify_citations(text, [_chunk(1)])
        assert cleaned == text
        assert report == {"total": 0, "valid": 0, "removed": 0, "verified": True}

    def test_no_markers_no_retrieval(self):
        """零检索且无引用：原样返回，verified=true。"""
        cleaned, report = verify_citations("文献库中没有相关内容。", [])
        assert cleaned == "文献库中没有相关内容。"
        assert report["verified"] is True
        assert report["total"] == 0

    def test_mixed_valid_and_invalid(self):
        """混合：有效保留、越界剔除，计数正确。"""
        cleaned, report = verify_citations(
            "见[^1^]与[^9^]及[^2^]。", [_chunk(1), _chunk(2)]
        )
        assert cleaned == "见[^1^]与及[^2^]。"
        assert report == {"total": 3, "valid": 2, "removed": 1, "verified": False}

    def test_zero_and_negative_index_removed(self):
        "[^0^] 与负数编号均越界（编号 1-based），剔除。"""
        cleaned, report = verify_citations("甲[^0^]乙[^-1^]丙", [_chunk(1)])
        assert cleaned == "甲乙丙"
        assert report == {"total": 2, "valid": 0, "removed": 2, "verified": False}

    def test_duplicate_valid_markers_counted(self):
        """同一编号重复出现：每次出现独立计数。"""
        cleaned, report = verify_citations("[^1^]再引[^1^]", [_chunk(1)])
        assert cleaned == "[^1^]再引[^1^]"
        assert report == {"total": 2, "valid": 2, "removed": 0, "verified": True}

    def test_removal_logs_warning_without_answer_text(self, caplog):
        """有剔除时只记聚合计数，不记答案全文或原始引用 token。"""
        secret = "患者隐私段落XYZ"
        with caplog.at_level(logging.WARNING, logger="papermind"):
            verify_citations(f"{secret}[^7^]", [_chunk(1)])
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("[guardrails]" in r.message for r in warnings)
        assert any("out_of_range=1" in r.message for r in warnings)
        assert all("[^7^]" not in r.message and "tokens=" not in r.message for r in warnings)
        assert all(secret not in r.message for r in warnings)

    def test_no_removal_no_warning(self, caplog):
        """无剔除时不记 warning。"""
        with caplog.at_level(logging.WARNING, logger="papermind"):
            verify_citations("正常[^1^]", [_chunk(1)])
        assert not [r for r in caplog.records if "[guardrails]" in r.message]


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


def _patch_store(monkeypatch, chunks):
    """把 agent_graph 内的向量库替换为返回固定片段的假实现。"""
    store = SimpleNamespace(
        available=lambda: True,
        search=lambda query, top_k, filters: chunks,
    )
    monkeypatch.setattr(agent_graph, "get_vector_store", lambda: store)


class TestNoRetrievalGuard:
    """C2 拒答强化（spec 3.2 / AC3）：零检索时 system prompt 追加硬约束段。"""

    def test_guard_appended_when_no_retrieval(self, db, conversation, monkeypatch):
        """检索为空：发往 LLM 的 system prompt 含拒答硬约束段。"""
        _patch_store(monkeypatch, [])
        state = run_pre_orchestration(
            db=db, conversation_id=conversation.id, user_message="什么是MIL？"
        )
        system_content = state["messages"][0]["content"]
        assert "未检索到相关文献片段" in system_content
        assert "文献库中没有相关内容" in system_content
        assert "禁止编造任何引用标记" in system_content

    def test_guard_absent_when_retrieved(self, db, conversation, monkeypatch):
        """不回归：有检索结果时 system prompt 与现状一致（不含硬约束段）。"""
        _patch_store(monkeypatch, [_chunk(1)])
        state = run_pre_orchestration(
            db=db, conversation_id=conversation.id, user_message="什么是MIL？"
        )
        system_content = state["messages"][0]["content"]
        assert system_content == state["system_prompt"]
        assert "未检索到相关文献片段" not in system_content
