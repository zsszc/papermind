"""Phase D（D2）：llm.py Langfuse 观测包裹契约测试。

对应 specs/phases/phase-d-langfuse/spec.md 3.2 行为契约：
- 未配置 PAPERMIND_LANGFUSE_PUBLIC_KEY / SECRET_KEY（或只配一个）→ 三方法零侵入
- 配置后 __init__ 惰性初始化 langfuse.openai wrapper client
- 初始化异常 → 记 [langfuse] warning 并降级为未启用态，主链路行为不变
- 观测 kwargs（name/metadata/stream/message_count）经可选参数 trace_metadata 传入，缺省不报错
- 观测路径任何异常一律吞掉，绝不影响 LLM 主链路返回值
"""

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from openai import AsyncOpenAI, OpenAI

from app.services.llm import LLMService

_ENV_VARS = (
    "PAPERMIND_LANGFUSE_PUBLIC_KEY",
    "PAPERMIND_LANGFUSE_SECRET_KEY",
    "PAPERMIND_LANGFUSE_HOST",
)


def _make_service(monkeypatch, public_key=None, secret_key=None):
    """按给定环境变量构造 LLMService；缺省清除全部 PAPERMIND_LANGFUSE_* 变量。"""
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    if public_key is not None:
        monkeypatch.setenv("PAPERMIND_LANGFUSE_PUBLIC_KEY", public_key)
    if secret_key is not None:
        monkeypatch.setenv("PAPERMIND_LANGFUSE_SECRET_KEY", secret_key)
    return LLMService()


def _fake_response(content="你好"):
    """构造 openai 非流式响应的鸭子类型。"""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


class _FakeStream:
    """异步迭代器鸭子类型：模拟 openai 流式响应。"""

    def __init__(self, texts):
        self._texts = texts

    def __aiter__(self):
        async def _gen():
            for t in self._texts:
                yield SimpleNamespace(
                    choices=[SimpleNamespace(delta=SimpleNamespace(content=t))]
                )

        return _gen()


_MESSAGES = [
    {"role": "system", "content": "你是助手"},
    {"role": "user", "content": "你好"},
]
_TRACE_META = {"conversation_id": 7, "skill": "translator", "chunk_count": 3}


class TestSwitchZeroIntrusion:
    """开关契约：未配置（含只配一个 key）时三方法与现状逐字节一致。"""

    def test_unconfigured_not_enabled(self, monkeypatch):
        svc = _make_service(monkeypatch)
        assert svc._langfuse_enabled is False
        assert type(svc.client) is AsyncOpenAI
        assert type(svc.sync_client) is OpenAI

    def test_partial_key_treated_as_unconfigured(self, monkeypatch):
        svc = _make_service(monkeypatch, public_key="pk-lf-test")
        assert svc._langfuse_enabled is False
        assert type(svc.client) is AsyncOpenAI

    def test_unconfigured_module_has_no_langfuse_import(self, monkeypatch):
        """未配置时构造不得引入 langfuse 模块（零 import 副作用）。"""
        for mod in [m for m in sys.modules if m.startswith("langfuse")]:
            monkeypatch.delitem(sys.modules, mod, raising=False)
        _make_service(monkeypatch)
        assert not any(m.startswith("langfuse") for m in sys.modules)

    @pytest.mark.asyncio
    async def test_unconfigured_create_kwargs_clean(self, monkeypatch):
        """未配置时 create 调用参数不含任何观测字段。"""
        svc = _make_service(monkeypatch)
        svc.client.chat.completions.create = AsyncMock(return_value=_fake_response())
        result = await svc.chat_completion(_MESSAGES, trace_metadata=_TRACE_META)
        assert result == "你好"
        kwargs = svc.client.chat.completions.create.call_args.kwargs
        assert "name" not in kwargs
        assert "metadata" not in kwargs


class TestEnabledWrapper:
    """配置即启用：client 切换为 langfuse.openai wrapper，trace 字段正确。"""

    def test_enabled_uses_langfuse_wrapper(self, monkeypatch):
        """启用态：配置经 wrapper 官方注入点（langfuse.openai 模块属性）生效。"""
        import langfuse.openai as lf_module

        svc = _make_service(monkeypatch, public_key="pk-lf-test", secret_key="sk-lf-test")
        assert svc._langfuse_enabled is True
        # langfuse 2.x wrapper 对 openai 资源方法做 wrapt 全局包装，client 类不变；
        # 启用证据 = 模块属性已注入（host 缺省指向自托管 3001 端口）
        assert lf_module.openai.langfuse_public_key == "pk-lf-test"
        assert lf_module.openai.langfuse_secret_key == "sk-lf-test"
        assert lf_module.openai.langfuse_host == "http://localhost:3001"

    @pytest.mark.asyncio
    async def test_chat_completion_observation_kwargs(self, monkeypatch):
        svc = _make_service(monkeypatch, public_key="pk-lf-test", secret_key="sk-lf-test")
        svc.client.chat.completions.create = AsyncMock(return_value=_fake_response())
        result = await svc.chat_completion(_MESSAGES, trace_metadata=_TRACE_META)
        assert result == "你好"
        kwargs = svc.client.chat.completions.create.call_args.kwargs
        assert kwargs["name"] == "chat_completion"
        assert kwargs["model"] == svc.model
        metadata = kwargs["metadata"]
        assert metadata["conversation_id"] == 7
        assert metadata["skill"] == "translator"
        assert metadata["chunk_count"] == 3
        assert metadata["message_count"] == 2
        assert metadata["stream"] is False

    @pytest.mark.asyncio
    async def test_chat_completion_default_metadata_ok(self, monkeypatch):
        """trace_metadata 缺省不报错，metadata 仅含基础字段。"""
        svc = _make_service(monkeypatch, public_key="pk-lf-test", secret_key="sk-lf-test")
        svc.client.chat.completions.create = AsyncMock(return_value=_fake_response())
        result = await svc.chat_completion(_MESSAGES)
        assert result == "你好"
        metadata = svc.client.chat.completions.create.call_args.kwargs["metadata"]
        assert metadata == {"message_count": 2, "stream": False}

    def test_chat_completion_sync_observation_kwargs(self, monkeypatch):
        svc = _make_service(monkeypatch, public_key="pk-lf-test", secret_key="sk-lf-test")
        svc.sync_client.chat.completions.create = MagicMock(return_value=_fake_response())
        result = svc.chat_completion_sync(_MESSAGES, trace_metadata=_TRACE_META)
        assert result == "你好"
        kwargs = svc.sync_client.chat.completions.create.call_args.kwargs
        assert kwargs["name"] == "chat_completion_sync"
        assert kwargs["metadata"]["conversation_id"] == 7
        assert kwargs["metadata"]["stream"] is False

    def test_chat_completion_sync_allows_bounded_max_tokens(self, monkeypatch):
        svc = _make_service(monkeypatch, public_key="pk-lf-test", secret_key="sk-lf-test")
        svc.sync_client.chat.completions.create = MagicMock(return_value=_fake_response())

        assert svc.chat_completion_sync(_MESSAGES, max_tokens=512) == "你好"
        kwargs = svc.sync_client.chat.completions.create.call_args.kwargs
        assert kwargs["max_tokens"] == 512

    @pytest.mark.asyncio
    async def test_chat_stream_observation_kwargs_and_passthrough(self, monkeypatch):
        """流式：观测字段正确，且 yield 的增量与现状逐字节一致。"""
        svc = _make_service(monkeypatch, public_key="pk-lf-test", secret_key="sk-lf-test")
        svc.client.chat.completions.create = AsyncMock(return_value=_FakeStream(["你", "好"]))
        deltas = [
            d
            async for d in svc.chat_stream(_MESSAGES, trace_metadata=_TRACE_META)
        ]
        assert deltas == ["你", "好"]
        kwargs = svc.client.chat.completions.create.call_args.kwargs
        assert kwargs["name"] == "chat_stream"
        assert kwargs["metadata"]["stream"] is True
        assert kwargs["metadata"]["conversation_id"] == 7
        assert kwargs["metadata"]["message_count"] == 2
        assert kwargs["stream"] is True


class TestDegradationContract:
    """降级契约：观测路径任何异常一律吞掉记 [langfuse] warning，主链路不受影响。"""

    def test_init_failure_degrades_to_disabled(self, monkeypatch):
        """langfuse client 初始化抛异常 → 降级为未启用态 + warning，client 回退标准实现。"""
        import langfuse.openai as lf_module

        monkeypatch.setattr(
            lf_module.openai,
            "AsyncOpenAI",
            MagicMock(side_effect=RuntimeError("langfuse boom")),
        )
        logger_mock = MagicMock()
        monkeypatch.setattr("app.services.llm.logger", logger_mock)

        svc = _make_service(monkeypatch, public_key="pk-lf-test", secret_key="sk-lf-test")
        assert svc._langfuse_enabled is False
        assert type(svc.client) is AsyncOpenAI
        assert type(svc.sync_client) is OpenAI
        warning_texts = [c.args[0] for c in logger_mock.warning.call_args_list]
        assert any("[langfuse]" in t for t in warning_texts)

    @pytest.mark.asyncio
    async def test_init_failure_main_chain_unchanged(self, monkeypatch):
        """初始化降级后，三方法主链路行为与现状一致。"""
        import langfuse.openai as lf_module

        monkeypatch.setattr(
            lf_module.openai,
            "AsyncOpenAI",
            MagicMock(side_effect=RuntimeError("langfuse boom")),
        )
        svc = _make_service(monkeypatch, public_key="pk-lf-test", secret_key="sk-lf-test")
        svc.client.chat.completions.create = AsyncMock(return_value=_fake_response())
        result = await svc.chat_completion(_MESSAGES, trace_metadata=_TRACE_META)
        assert result == "你好"
        kwargs = svc.client.chat.completions.create.call_args.kwargs
        assert "name" not in kwargs

    @pytest.mark.asyncio
    async def test_bad_trace_metadata_swallowed(self, monkeypatch):
        """观测参数组装异常（如 trace_metadata 非 dict）→ 吞掉，主链路照常返回。"""
        svc = _make_service(monkeypatch, public_key="pk-lf-test", secret_key="sk-lf-test")
        svc.client.chat.completions.create = AsyncMock(return_value=_fake_response())
        result = await svc.chat_completion(_MESSAGES, trace_metadata=12345)
        assert result == "你好"

    @pytest.mark.asyncio
    async def test_llm_error_path_unchanged_when_enabled(self, monkeypatch):
        """启用态下 LLM 调用失败仍返回带内错误串（非预期异常不重试、不上抛）。"""
        svc = _make_service(monkeypatch, public_key="pk-lf-test", secret_key="sk-lf-test")
        svc.client.chat.completions.create = AsyncMock(side_effect=Exception("boom"))
        result = await svc.chat_completion(_MESSAGES, trace_metadata=_TRACE_META)
        assert result.startswith("[调用 LLM 出错:")
