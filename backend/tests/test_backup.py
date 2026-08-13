"""备份服务契约测试（Batch 7 / F3）。

核心契约：备份包内的 config.yaml 必须剥离 llm.api_key（宪法第 14 条），
磁盘原文件不受影响；config.yaml 缺失时跳过不中断备份。
"""

import io
import sqlite3
import zipfile

import pytest
import yaml

from app.services import backup


@pytest.fixture()
def fake_project(tmp_path, monkeypatch):
    """构造假项目根：data/ 目录 + 含明文 key 的 config.yaml。"""
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "x.txt").write_text("data-payload", encoding="utf-8")
    (tmp_path / "config.yaml").write_text(
        "llm:\n  api_key: sk-realsecretkey123456\n  model: kimi-k2.6\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(backup, "get_project_root", lambda: tmp_path)
    monkeypatch.setattr(
        type(backup.config), "data_dir", property(lambda self: tmp_path / "data")
    )
    return tmp_path


def _read_packed_config(zip_bytes: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        return yaml.safe_load(zf.read("config.yaml").decode("utf-8"))


class TestConfigRedaction:
    def test_backup_config_api_key_redacted(self, fake_project):
        """备份包内 api_key 必须是 [REDACTED]，其余配置原样保留。"""
        data = backup.create_backup(dirs=["data"], include_vector=False)
        packed = _read_packed_config(data)
        assert packed["llm"]["api_key"] == "[REDACTED]"
        assert packed["llm"]["model"] == "kimi-k2.6"

    def test_disk_config_untouched(self, fake_project):
        """脱敏只影响包内副本，磁盘原文件保持明文（运行需要）。"""
        backup.create_backup(dirs=["data"], include_vector=False)
        disk = (fake_project / "config.yaml").read_text(encoding="utf-8")
        assert "sk-realsecretkey123456" in disk

    def test_missing_config_skipped(self, tmp_path, monkeypatch):
        """config.yaml 不存在时跳过，备份不中断。"""
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "x.txt").write_text("d", encoding="utf-8")
        monkeypatch.setattr(backup, "get_project_root", lambda: tmp_path)
        monkeypatch.setattr(
            type(backup.config), "data_dir", property(lambda self: tmp_path / "data")
        )
        data = backup.create_backup(dirs=["data"], include_vector=False)
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            assert "config.yaml" not in zf.namelist()
            assert "data/x.txt" in zf.namelist()


def test_include_db_backs_up_database_outside_data_subdir(tmp_path, monkeypatch):
    """Electron 兼容：数据库位于运行时根时仍以 data/papers.db 入包。"""
    database = tmp_path / "papers.db"
    conn = sqlite3.connect(database)
    conn.execute("CREATE TABLE sample (value TEXT)")
    conn.execute("INSERT INTO sample VALUES ('sqlite-test')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(backup, "get_project_root", lambda: tmp_path)
    monkeypatch.setattr(type(backup.config), "data_dir", property(lambda self: tmp_path))

    data = backup.create_backup(dirs=["missing"], include_db=True, include_vector=False)

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        extracted = tmp_path / "extracted.db"
        extracted.write_bytes(zf.read("data/papers.db"))
        conn = sqlite3.connect(extracted)
        assert conn.execute("SELECT value FROM sample").fetchone() == ("sqlite-test",)
        conn.close()


def test_backup_uses_consistent_snapshot_for_uncheckpointed_wal(tmp_path, monkeypatch):
    """WAL 中已提交的数据必须进入独立且可校验的主库快照。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    database = data_dir / "papers.db"
    conn = sqlite3.connect(database)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("CREATE TABLE sample (value TEXT)")
    conn.execute("INSERT INTO sample VALUES ('latest')")
    conn.commit()

    monkeypatch.setattr(backup, "get_project_root", lambda: tmp_path)
    monkeypatch.setattr(type(backup.config), "data_dir", property(lambda self: data_dir))
    packed = backup.create_backup(dirs=["data"], include_vector=False)
    conn.close()

    with zipfile.ZipFile(io.BytesIO(packed)) as zf:
        assert "data/papers.db" in zf.namelist()
        assert "data/papers.db-wal" not in zf.namelist()
        assert "data/papers.db-shm" not in zf.namelist()
        extracted = tmp_path / "wal-extracted.db"
        extracted.write_bytes(zf.read("data/papers.db"))

    restored = sqlite3.connect(extracted)
    assert restored.execute("PRAGMA quick_check").fetchone() == ("ok",)
    assert restored.execute("SELECT value FROM sample").fetchone() == ("latest",)
    restored.close()


def test_include_db_false_excludes_database_and_sidecars(tmp_path, monkeypatch):
    """include_db=False 在 data 目录入包时也必须排除 DB/WAL/SHM。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "papers.db").write_bytes(b"db")
    (data_dir / "papers.db-wal").write_bytes(b"wal")
    (data_dir / "papers.db-shm").write_bytes(b"shm")
    (data_dir / "keep.txt").write_text("keep", encoding="utf-8")
    monkeypatch.setattr(backup, "get_project_root", lambda: tmp_path)
    monkeypatch.setattr(type(backup.config), "data_dir", property(lambda self: data_dir))

    packed = backup.create_backup(
        dirs=["data"], include_db=False, include_vector=False
    )

    with zipfile.ZipFile(io.BytesIO(packed)) as zf:
        assert zf.namelist() == ["data/keep.txt"]


def test_auto_backup_does_not_leave_final_file_when_atomic_write_fails(
    tmp_path, monkeypatch
):
    """原子写失败时不得留下看似完整的最终 ZIP。"""
    monkeypatch.setattr(backup, "get_project_root", lambda: tmp_path)
    monkeypatch.setattr(backup, "create_backup", lambda: b"zip")

    def fail_replace(source, target):
        raise OSError("replace failed")

    monkeypatch.setattr(backup.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        backup.auto_backup(tmp_path / "backups")

    assert list((tmp_path / "backups").iterdir()) == []
