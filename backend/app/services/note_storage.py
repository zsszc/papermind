"""个人笔记的大小校验与原子落盘。"""

import os
import tempfile
from pathlib import Path


MAX_NOTE_BYTES = 1024 * 1024


def validate_note_content(content: str) -> bytes:
    """编码笔记并执行 UTF-8 字节上限。"""
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_NOTE_BYTES:
        raise ValueError("笔记内容超过 1MiB 上限")
    return encoded


def atomic_write_note(target: Path, content: str) -> None:
    """同目录写临时文件并原子替换；失败时保留旧文件。"""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = validate_note_content(content)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as temporary_file:
            temporary_file.write(encoded)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, target)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
