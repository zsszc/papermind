"""SQLite 一致快照、完整性审计与仅副本修复。"""

import os
import sqlite3
import tempfile
from pathlib import Path


def _readonly_uri(path: Path) -> str:
    """生成 SQLite 只读 URI；不使用 immutable，以便正确读取 WAL。"""
    return f"{path.resolve().as_uri()}?mode=ro"


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
        is not None
    )


def create_sqlite_snapshot(source: Path, destination: Path) -> Path:
    """通过 SQLite backup API 创建包含已提交 WAL 的原子快照。"""
    source = Path(source)
    destination = Path(destination)
    if source.resolve() == destination.resolve():
        raise ValueError("快照目标不得覆盖源数据库")
    if not source.is_file():
        raise FileNotFoundError(source)

    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(fd)
    temporary_path = Path(temporary_name)
    source_conn = None
    destination_conn = None
    try:
        source_conn = sqlite3.connect(_readonly_uri(source), uri=True)
        source_conn.execute("PRAGMA query_only=ON")
        destination_conn = sqlite3.connect(temporary_path)
        source_conn.backup(destination_conn)
        destination_conn.commit()
        rows = [row[0] for row in destination_conn.execute("PRAGMA quick_check")]
        if rows != ["ok"]:
            raise sqlite3.DatabaseError("SQLite 快照完整性校验失败")
        destination_conn.close()
        destination_conn = None
        source_conn.close()
        source_conn = None

        with temporary_path.open("rb") as snapshot_file:
            os.fsync(snapshot_file.fileno())
        os.replace(temporary_path, destination)
        return destination
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    finally:
        if destination_conn is not None:
            destination_conn.close()
        if source_conn is not None:
            source_conn.close()


def audit_database(database_path: Path) -> dict:
    """只读返回完整性计数，不暴露具体用户数据。"""
    database_path = Path(database_path)
    with sqlite3.connect(_readonly_uri(database_path), uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        quick_check_rows = [row[0] for row in conn.execute("PRAGMA quick_check")]
        foreign_key_violation_count = sum(
            1 for _ in conn.execute("PRAGMA foreign_key_check")
        )
        orphan_paper_tags_count = 0
        if _table_exists(conn, "paper_tags") and _table_exists(conn, "papers"):
            orphan_paper_tags_count = conn.execute(
                """
                SELECT COUNT(*)
                FROM paper_tags AS pt
                WHERE NOT EXISTS (
                    SELECT 1 FROM papers AS p WHERE p.id = pt.paper_id
                )
                """
            ).fetchone()[0]

    return {
        "quick_check_ok": quick_check_rows == ["ok"],
        "foreign_key_violation_count": foreign_key_violation_count,
        "orphan_paper_tags_count": orphan_paper_tags_count,
    }


def repair_database_copy(source: Path, destination: Path, *, dry_run: bool = True) -> dict:
    """仅在新快照上清理 paper_tags 孤儿，永不修改源库。"""
    destination = create_sqlite_snapshot(source, destination)
    before = audit_database(destination)
    would_delete = before["orphan_paper_tags_count"]
    deleted = 0

    if not dry_run and would_delete:
        with sqlite3.connect(destination) as conn:
            cursor = conn.execute(
                """
                DELETE FROM paper_tags
                WHERE NOT EXISTS (
                    SELECT 1 FROM papers WHERE papers.id = paper_tags.paper_id
                )
                """
            )
            deleted = cursor.rowcount
            conn.commit()

    after = audit_database(destination)
    if not after["quick_check_ok"]:
        destination.unlink(missing_ok=True)
        raise sqlite3.DatabaseError("修复副本未通过完整性校验")
    if not dry_run and after["foreign_key_violation_count"]:
        destination.unlink(missing_ok=True)
        raise sqlite3.IntegrityError("修复副本仍存在外键违规")

    return {
        "dry_run": dry_run,
        "would_delete_orphan_paper_tags": would_delete,
        "deleted_orphan_paper_tags": deleted,
        "before": before,
        "after": after,
        "output_path": str(destination),
    }


def open_readonly_sqlalchemy_database(database_path: Path):
    """以 SQLite URI 只读模式创建显式 SQLAlchemy engine/session factory。

    用于候选评测和向量构建，避免导入期绑定的生产 ``SessionLocal``。调用方
    负责关闭 Session 并 ``engine.dispose()``。
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    database_path = Path(database_path).resolve()
    if not database_path.is_file():
        raise FileNotFoundError(database_path)
    uri = _readonly_uri(database_path)

    def _connect():
        connection = sqlite3.connect(
            uri, uri=True, check_same_thread=False
        )
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    engine = create_engine("sqlite://", creator=_connect)
    return engine, sessionmaker(
        autocommit=False, autoflush=False, bind=engine
    )
