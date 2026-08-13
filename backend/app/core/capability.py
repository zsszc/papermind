"""Electron 生产模式的本地进程能力边界。"""

import hmac
import os

from fastapi.responses import JSONResponse


TOKEN_ENV = "PAPERMIND_API_TOKEN"
TOKEN_HEADER = b"x-papermind-token"


def _request_token(scope: dict) -> bytes:
    """从原始 ASGI header 中提取能力令牌，避免引入请求体缓冲。"""
    for name, value in scope.get("headers", []):
        if name.lower() == TOKEN_HEADER:
            return value
    return b""


def has_valid_capability(scope: dict, expected: str) -> bool:
    """直接比较原始字节，畸形或非 ASCII header 也只能得到拒绝结果。"""
    return hmac.compare_digest(_request_token(scope), expected.encode("utf-8"))


class CapabilityMiddleware:
    """仅在环境变量配置令牌时保护全部 HTTP 路径。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") == "OPTIONS":
            await self.app(scope, receive, send)
            return

        expected = os.environ.get(TOKEN_ENV, "")
        if expected and not has_valid_capability(scope, expected):
            response = JSONResponse(
                status_code=401,
                content={
                    "detail": "未授权的本地请求",
                    "error_code": "invalid_capability",
                },
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)
