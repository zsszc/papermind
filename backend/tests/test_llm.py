"""LLMService 错误格式化契约测试（Batch 7 / F2）。

背景：429 配额/冻结类错误（exceeded_current_quota_error）此前被笼统归入
"负载过高"文案，排障时被严重误导。本测试钉死四类错误的文案分支。
"""

import pytest

from app.services.llm import LLMService


@pytest.fixture()
def svc():
    """不经 __init__ 构造（避免创建真实 OpenAI client），_format_error 不依赖实例状态。"""
    return LLMService.__new__(LLMService)


class TestFormatError:
    # 真实错误体样本（2026-08-04 排障时抓取，org/key 已脱敏）
    QUOTA_ERR = (
        "Error code: 429 - {'error': {'message': 'Your account org-[REDACTED] "
        "is suspended due to insufficient balance, please recharge your account "
        "or check your plan and billing details', 'type': 'exceeded_current_quota_error'}}"
    )
    OVERLOAD_ERR = (
        "Error code: 429 - {'error': {'message': 'The engine is currently "
        "overloaded, please try again later', 'type': 'engine_overloaded_error'}}"
    )

    def test_quota_error_has_dedicated_message(self, svc):
        """配额/冻结类错误必须有专属文案，不得误报为负载过高。"""
        msg = svc._format_error(Exception(self.QUOTA_ERR))
        assert "额度" in msg or "冻结" in msg
        assert "负载过高" not in msg

    def test_overloaded_keeps_busy_message(self, svc):
        """引擎过载仍是负载过高文案（不回归）。"""
        assert "负载过高" in svc._format_error(Exception(self.OVERLOAD_ERR))

    def test_401_message(self, svc):
        assert "API Key" in svc._format_error(Exception("Error code: 401 - Authentication failed"))

    def test_timeout_message(self, svc):
        assert "超时" in svc._format_error(Exception("Request timed out"))

    def test_unknown_error_passthrough(self, svc):
        assert svc._format_error(Exception("some other error")) == "some other error"


class TestObservabilityZeroIntrusion:
    """Phase D（D2）：未配置 PAPERMIND_LANGFUSE_* 时三方法零侵入（开关关闭态）。"""

    def test_unconfigured_service_not_enabled(self, monkeypatch):
        """两个 key 均未设置 → 未启用态，client 保持标准 openai 实现。"""
        from openai import AsyncOpenAI, OpenAI

        monkeypatch.delenv("PAPERMIND_LANGFUSE_PUBLIC_KEY", raising=False)
        monkeypatch.delenv("PAPERMIND_LANGFUSE_SECRET_KEY", raising=False)
        service = LLMService()
        assert service._langfuse_enabled is False
        assert type(service.client) is AsyncOpenAI
        assert type(service.sync_client) is OpenAI
