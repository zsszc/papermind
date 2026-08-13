"""备份下载端点必须复用服务层的一致快照实现。"""

from app.routers import export


def test_manual_backup_reuses_backup_service(client, monkeypatch):
    calls = []

    def fake_create_backup(**kwargs):
        calls.append(kwargs)
        return b"PK\x05\x06" + b"\x00" * 18

    monkeypatch.setattr(export, "create_backup", fake_create_backup)

    response = client.post("/api/export/backup")

    assert response.status_code == 200
    assert calls == [{"include_config": False}]
    assert response.content.startswith(b"PK")
