"""LLMService 错误格式化契约测试（Batch 7 / F2）。

背景：429 配额/冻结类错误（exceeded_current_quota_error）此前被笼统归入
"负载过高"文案，排障时被严重误导。本测试钉死错误分类与流式失败边界。
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.llm import LLMGenerationError, LLMService


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

    def test_unknown_error_is_sanitized(self, svc):
        """未知异常不得把 SDK、路径或请求正文原样暴露给客户端。"""
        canary = "some-other-error-private-canary"
        formatted = svc._format_error(Exception(canary))
        assert formatted == "Kimi API 调用失败，请稍后重试。"
        assert canary not in formatted


class TestStrictStreamFailure:
    @pytest.mark.asyncio
    async def test_partial_stream_failure_raises_without_retry_or_error_delta(self):
        """首 token 后失败不可从头重试，也不可把错误包装成普通正文。"""
        service = LLMService.__new__(LLMService)
        service._langfuse_enabled = False
        service.model = "test-model"
        service.max_tokens = 32
        service.temperature = 0.3
        service.max_total_chars = 1000

        class FailingStream:
            def __aiter__(self):
                async def generate():
                    yield SimpleNamespace(
                        choices=[SimpleNamespace(delta=SimpleNamespace(content="partial"))]
                    )
                    raise RuntimeError("private-stream-canary")

                return generate()

        create = AsyncMock(return_value=FailingStream())
        service.client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )

        deltas = []
        with pytest.raises(LLMGenerationError):
            async for delta in service.chat_stream([{"role": "user", "content": "q"}]):
                deltas.append(delta)

        assert deltas == ["partial"]
        assert create.await_count == 1


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
