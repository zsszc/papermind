from app.core.config import config


def test_get_settings_masks_api_key(client):
    """设置接口返回的 API Key 必须是脱敏值，绝不泄露原始 Key。"""
    r = client.get("/api/settings")
    assert r.status_code == 200
    data = r.json()

    masked = data["llm_api_key"]
    assert masked == "" or "*" in masked

    real_key = str(config.get("llm.api_key", "") or "")
    if real_key:
        assert masked != real_key
