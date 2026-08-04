"""图片分析上传安全契约测试（Batch 7 / F4，宪法第 13 条）。

- 超过 10MB → HTTP 413
- 分析异常 → 通用文案，不透传异常原文（原文只进日志）
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.image_analyzer import ImageAnalyzerService


class TestAnalyzeImageUpload:
    @staticmethod
    def _stub_analyzer(monkeypatch):
        """桩掉分析服务：大小校验在调服务之前发生，避免 RED 阶段打真实 LLM。"""

        async def fake_stream(**kwargs):
            yield "ok"

        monkeypatch.setattr(
            "app.routers.chat.image_analyzer_service.analyze_stream", fake_stream
        )

    def test_oversized_image_returns_413(self, client, monkeypatch):
        """超过 10MB 的图片被拒绝。"""
        self._stub_analyzer(monkeypatch)
        big = b"x" * (10 * 1024 * 1024 + 1)
        resp = client.post(
            "/api/chat/analyze-image",
            files={"file": ("big.png", big, "image/png")},
        )
        assert resp.status_code == 413

    def test_boundary_10mb_accepted(self, client, monkeypatch):
        """恰好 10MB 边界放行（≤10MB 合法）。"""
        self._stub_analyzer(monkeypatch)
        exactly = b"x" * (10 * 1024 * 1024)
        resp = client.post(
            "/api/chat/analyze-image",
            files={"file": ("edge.png", exactly, "image/png")},
        )
        assert resp.status_code == 200

    def test_empty_image_400(self, client):
        """空文件 400（既有行为特征化）。"""
        resp = client.post(
            "/api/chat/analyze-image",
            files={"file": ("empty.png", b"", "image/png")},
        )
        assert resp.status_code == 400


class TestAnalyzeExceptionSanitization:
    @staticmethod
    def _svc_with_raising_client():
        svc = ImageAnalyzerService.__new__(ImageAnalyzerService)
        svc.model = "kimi-k2.6"  # __new__ 绕过 __init__，需补齐 analyze 用到的属性
        svc.client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=AsyncMock(side_effect=Exception("boom-secret-detail"))
                )
            )
        )
        return svc

    def test_analyze_error_text_sanitized(self):
        """analyze() 异常时返回通用文案，不含异常原文。"""
        svc = self._svc_with_raising_client()
        result = asyncio.run(svc.analyze(b"img", "a.png", "q"))
        assert "boom-secret-detail" not in result
        assert result.startswith("[图片分析失败")

    def test_analyze_stream_error_text_sanitized(self):
        """analyze_stream() 尾帧错误文本同样脱敏。"""
        svc = self._svc_with_raising_client()

        async def collect():
            return [c async for c in svc.analyze_stream(b"img", "a.png", "q")]

        chunks = asyncio.run(collect())
        joined = "".join(chunks)
        assert "boom-secret-detail" not in joined
        assert "图片分析失败" in joined
