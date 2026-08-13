"""Electron 生产模式的本地进程能力边界。"""

import hmac
import os

from fastapi.responses import JSONResponse


TOKEN_ENV = "PAPERMIND_API_TOKEN"
TOKEN_HEADER = b"x-papermind-token"


def _request_token(scope: dict) -> str:
    """从原始 ASGI header 中提取能力令牌，避免引入请求体缓冲。"""
    for name, value in scope.get("headers", []):
        if name.lower() == TOKEN_HEADER:
            return value.decode("utf-8", errors="ignore")
    return ""


class CapabilityMiddleware:
    """仅在环境变量配置令牌时保护全部 HTTP 路径。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") == "OPTIONS":
            await self.app(scope, receive, send)
            return

        expected = os.environ.get(TOKEN_ENV, "")
        supplied = _request_token(scope)
        if expected and not hmac.compare_digest(supplied, expected):
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
