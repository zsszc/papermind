import io
import os
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import List

import yaml

from app.core.config import config
from app.core.logger import logger
from app.services.data_integrity import create_sqlite_snapshot


def get_project_root() -> Path:
    """兼容旧调用名：实际返回当前运行时数据根目录。"""
    return config.runtime_root


def _redacted_config_bytes(config_path: Path) -> bytes:
    """读取 config.yaml 并剥离 llm.api_key，返回脱敏后的序列化字节。

    仅用于备份入包；磁盘上的真实 config.yaml 不做任何修改。
    """
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if isinstance(cfg.get("llm"), dict) and cfg["llm"].get("api_key"):
        cfg["llm"]["api_key"] = "[REDACTED]"
    return yaml.safe_dump(cfg, allow_unicode=True).encode("utf-8")


def create_backup(
    dirs: List[str] = None,
    include_db: bool = True,
    include_vector: bool = True,
    include_config: bool = True,
) -> bytes:
    """创建项目全量备份（返回 zip 字节）。"""
    project_root = get_project_root()
    default_dirs = ["data", "papers", "notes", "summaries", "my-thesis", "skills", "logs"]
    if include_vector:
        default_dirs.append("vector_db")
    dirs_to_backup = dirs or default_dirs

    db_path = config.data_dir / "papers.db"
    excluded_database_paths = {
        db_path.resolve(),
        Path(f"{db_path}-wal").resolve(),
        Path(f"{db_path}-shm").resolve(),
    }

    buffer = io.BytesIO()
    with tempfile.TemporaryDirectory(prefix="papermind-backup-") as temp_dir:
        snapshot_path = Path(temp_dir) / "papers.db"
        if include_db and db_path.is_file():
            create_sqlite_snapshot(db_path, snapshot_path)

        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for dirname in dirs_to_backup:
                src_dir = project_root / dirname
                if not src_dir.exists():
                    continue
                for file_path in src_dir.rglob("*"):
                    if file_path.is_file() and file_path.resolve() not in excluded_database_paths:
                        arcname = str(file_path.relative_to(project_root))
                        zf.write(file_path, arcname)

            # Electron 与开发模式统一使用经校验的独立快照。
            if snapshot_path.is_file():
                zf.write(snapshot_path, "data/papers.db")

            # 同时备份配置文件（去掉 API Key）
            config_path = project_root / "config.yaml"
            if include_config and config_path.exists():
                try:
                    zf.writestr("config.yaml", _redacted_config_bytes(config_path))
                except Exception:
                    logger.warning("[backup] config.yaml 脱敏失败，跳过该文件", exc_info=True)

    buffer.seek(0)
    return buffer.read()


def auto_backup(backup_dir: Path = None) -> Path:
    """执行一次自动备份，返回备份文件路径。"""
    project_root = get_project_root()
    if backup_dir is None:
        backup_dir = project_root / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    filename = f"papermind_auto_backup_{timestamp}.zip"
    backup_path = backup_dir / filename

    try:
        data = create_backup()
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{filename}.", suffix=".tmp", dir=backup_dir
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as backup_file:
                backup_file.write(data)
                backup_file.flush()
                os.fsync(backup_file.fileno())
            os.replace(temporary_path, backup_path)
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise
        logger.info(f"[backup] 自动备份完成: {backup_path}")
        return backup_path
    except Exception as e:
        logger.error(f"[backup] 自动备份失败: {e}", exc_info=True)
        raise


def cleanup_old_backups(backup_dir: Path = None, keep: int = 10):
    """保留最近 N 个自动备份，删除更早的。"""
    project_root = get_project_root()
    if backup_dir is None:
        backup_dir = project_root / "backups"
    if not backup_dir.exists():
        return

    backups = sorted(
        [p for p in backup_dir.glob("papermind_auto_backup_*.zip")],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old in backups[keep:]:
        try:
            old.unlink()
            logger.info(f"[backup] 清理旧备份: {old}")
        except Exception as e:
            logger.warning(f"[backup] 删除旧备份失败 {old}: {e}")
