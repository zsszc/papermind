import io
import shutil
import zipfile
from datetime import datetime
from pathlib import Path
from typing import List

import yaml

from app.core.config import config
from app.core.logger import logger


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
) -> bytes:
    """创建项目全量备份（返回 zip 字节）。"""
    project_root = get_project_root()
    default_dirs = ["data", "papers", "notes", "summaries", "my-thesis", "skills", "logs"]
    if include_vector:
        default_dirs.append("vector_db")
    dirs_to_backup = dirs or default_dirs

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        written_names = set()
        for dirname in dirs_to_backup:
            src_dir = project_root / dirname
            if not src_dir.exists():
                continue
            for file_path in src_dir.rglob("*"):
                if file_path.is_file():
                    arcname = str(file_path.relative_to(project_root))
                    zf.write(file_path, arcname)
                    written_names.add(arcname)

        # Electron 历史版本把 SQLite 放在 PAPERMIND_DATA_DIR 根目录；
        # 开发模式通常位于 data/。统一以 data/papers.db 入包且避免重复。
        if include_db:
            db_path = config.data_dir / "papers.db"
            db_arcname = "data/papers.db"
            if db_path.is_file() and db_arcname not in written_names:
                zf.write(db_path, db_arcname)

        # 同时备份配置文件（去掉 API Key）
        config_path = project_root / "config.yaml"
        if config_path.exists():
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

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"papermind_auto_backup_{timestamp}.zip"
    backup_path = backup_dir / filename

    try:
        data = create_backup()
        backup_path.write_bytes(data)
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
