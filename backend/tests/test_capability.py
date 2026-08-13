"""Electron 生产模式的进程能力令牌边界。"""

import secrets

from app.core.capability import has_valid_capability


TOKEN_ENV = "PAPERMIND_API_TOKEN"
INSTANCE_ENV = "PAPERMIND_INSTANCE_ID"
HEADER = "X-PaperMind-Token"


def test_non_ascii_raw_header_is_rejected_without_exception():
    scope = {"headers": [(b"x-papermind-token", b"\xff\xfe")]}
    assert has_valid_capability(scope, "expected-token") is False


def test_development_without_token_remains_compatible(client, monkeypatch):
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    monkeypatch.delenv(INSTANCE_ENV, raising=False)

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["instance_id"] is None


def test_capability_accepts_only_constant_time_matching_token(client, monkeypatch):
    token = secrets.token_urlsafe(32)
    monkeypatch.setenv(TOKEN_ENV, token)
    monkeypatch.setenv(INSTANCE_ENV, "instance-test-1")

    accepted = client.get("/api/health", headers={HEADER: token})
    missing = client.get("/api/health")
    wrong = client.get("/api/health", headers={HEADER: "wrong-token"})

    assert accepted.status_code == 200
    assert accepted.json()["instance_id"] == "instance-test-1"
    assert token not in accepted.text
    for rejected in (missing, wrong):
        assert rejected.status_code == 401
        assert rejected.json() == {
            "detail": "未授权的本地请求",
            "error_code": "invalid_capability",
        }
        assert token not in rejected.text


def test_unauthorized_response_keeps_electron_cors_headers(client, monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, secrets.token_urlsafe(32))

    response = client.get("/api/health", headers={"Origin": "null"})

    assert response.status_code == 401
    assert response.headers["access-control-allow-origin"] == "null"


def test_options_preflight_does_not_require_capability(client, monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, secrets.token_urlsafe(32))

    response = client.options(
        "/api/health",
        headers={
            "Origin": "null",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": HEADER,
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "null"


def test_static_and_docs_share_the_same_capability_boundary(client, monkeypatch, tmp_path):
    token = secrets.token_urlsafe(32)
    monkeypatch.setenv(TOKEN_ENV, token)
    monkeypatch.setenv("PAPERMIND_DATA_DIR", str(tmp_path))
    papers = tmp_path / "papers"
    papers.mkdir()
    (papers / "sample.txt").write_text("private-paper", encoding="utf-8")

    assert client.get("/static/papers/sample.txt").status_code == 401
    assert client.get("/docs").status_code == 401
    assert client.get("/mcp/sse").status_code == 401
    response = client.get("/static/papers/sample.txt", headers={HEADER: token})
    assert response.status_code == 200
    assert response.text == "private-paper"
