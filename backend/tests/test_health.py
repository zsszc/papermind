def test_health(client):
    """健康检查接口应返回 ok 与基本字段。"""
    r = client.get("/api/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert "version" in data
    assert "llm_ready" in data
