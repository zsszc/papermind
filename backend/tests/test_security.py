"""安全加固测试：/static 路径穿越防护、CORS 白名单、统一异常响应不泄露详情。"""

import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.main import global_exception_handler

# 用于验证异常详情不泄露到响应体的哨兵字符串
SECRET = "SECRET_EXCEPTION_DETAIL_9f3a"


class TestStaticTraversal:
    """路径穿越与非白名单路径必须被拒绝（403 或 404 均视为已阻止）。"""

    @pytest.mark.parametrize("path", [
        "/static/../config.yaml",
        "/static/config.yaml",
        "/static/backend/app/main.py",
        "/static/data/papers.db",
        "/static/logs/app.log",
        "/static/papers/../../config.yaml",
        "/static/%2e%2e/config.yaml",
    ])
    def test_forbidden_paths(self, client, path):
        resp = client.get(path)
        assert resp.status_code in (403, 404)
        # 响应体不应包含项目根敏感文件内容
        assert "api_key" not in resp.text.lower()


class TestStaticWhitelist:
    """白名单目录行为：用 tmp_path 伪造项目根，不依赖真实 papers/ 数据。"""

    @pytest.fixture()
    def fake_root(self, tmp_path, monkeypatch):
        papers = tmp_path / "papers"
        papers.mkdir()
        (papers / "ok.txt").write_text("白名单内容", encoding="utf-8")
        (tmp_path / "secret.yaml").write_text("top-secret", encoding="utf-8")
        monkeypatch.setattr("app.routers.static.PROJECT_ROOT", tmp_path)
        return tmp_path

    def test_whitelisted_file_ok(self, client, fake_root):
        resp = client.get("/static/papers/ok.txt")
        assert resp.status_code == 200
        assert resp.text == "白名单内容"

    def test_missing_file_404(self, client, fake_root):
        assert client.get("/static/papers/not-exist.txt").status_code == 404

    def test_traversal_inside_whitelist_blocked(self, client, fake_root):
        resp = client.get("/static/papers/../secret.yaml")
        assert resp.status_code in (403, 404)
        assert "top-secret" not in resp.text

    def test_symlink_escape_blocked(self, client, fake_root):
        link = fake_root / "papers" / "evil.txt"
        try:
            link.symlink_to(fake_root / "secret.yaml")
        except OSError:
            pytest.skip("当前环境不支持创建软链接")
        resp = client.get("/static/papers/evil.txt")
        assert resp.status_code in (403, 404)
        assert "top-secret" not in resp.text


class TestCors:
    """CORS：仅白名单 Origin 放行，且不允许携带凭证。"""

    def test_allowed_origin(self, client):
        resp = client.get("/api/health", headers={"Origin": "http://localhost:5173"})
        assert resp.headers.get("access-control-allow-origin") == "http://localhost:5173"

    def test_disallowed_origin(self, client):
        resp = client.get("/api/health", headers={"Origin": "http://evil.example.com"})
        assert "access-control-allow-origin" not in resp.headers

    def test_credentials_disabled(self, client):
        resp = client.options("/api/health", headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        })
        assert resp.headers.get("access-control-allow-credentials") != "true"


class TestGlobalExceptionHandler:
    """500 响应不得包含异常原文，仅返回通用文案 + error_code + path。"""

    def test_handler_response_hides_details(self):
        scope = {
            "type": "http", "method": "GET", "path": "/api/x",
            "headers": [], "query_string": b"",
            "server": ("testserver", 80), "client": ("testclient", 50000),
            "scheme": "http",
        }
        request = Request(scope)
        resp = asyncio.run(global_exception_handler(request, RuntimeError(SECRET)))
        assert resp.status_code == 500
        body_bytes = bytes(resp.body)
        body = json.loads(body_bytes)
        assert body["error_code"] == "internal_error"
        assert body["path"] == "/api/x"
        assert SECRET not in body_bytes.decode("utf-8")

    def test_500_via_temp_route(self):
        """在独立 FastAPI 实例上挂一个必抛异常的路由，端到端验证响应体。"""
        test_app = FastAPI()
        test_app.add_exception_handler(Exception, global_exception_handler)

        @test_app.get("/boom")
        async def boom():
            raise RuntimeError(SECRET)

        c = TestClient(test_app, raise_server_exceptions=False)
        resp = c.get("/boom")
        assert resp.status_code == 500
        assert SECRET not in resp.text
        assert resp.json()["error_code"] == "internal_error"
