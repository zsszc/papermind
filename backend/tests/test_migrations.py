"""database.py 的 ensure_schema 轻量迁移特征化测试（只测不改）。

锁定 SCHEMA_MIGRATIONS 登记的 9 个迁移分支的现有行为：
- 旧 schema 库（缺迁移目标列）执行 ensure_schema 后新列补齐、默认值/NULL 语义正确；
- 存量数据在迁移后完整保留；
- 幂等（重复执行不报错、不重复加列）；
- 登记表中的表不存在时整张表静默跳过；
- 迁移过程任何异常写 WARNING 后向上抛出，阻断不完整 schema 启动。

隔离手段：用 monkeypatch 把 ``app.database.engine`` 替换为指向 tmp_path 的
临时库引擎，绝不触碰真实的 data/papers.db。
"""

import logging
import sqlite3

import pytest
from sqlalchemy import create_engine

import app.database as database_module
from app.database import SCHEMA_MIGRATIONS, ensure_schema

# 9 个迁移分支的期望默认值（None 表示 ADD COLUMN 不带 DEFAULT，存量行应为 NULL）。
# 新增迁移分支时需同步维护本表。
MIGRATION_EXPECTATIONS = [
    ("chunks", "page_start", None),
    ("chunks", "page_end", None),
    ("papers", "last_read_page", 1),
    ("papers", "metadata_json", "{}"),
    ("conversations", "paper_ids", "[]"),
    ("conversations", "summary", None),
    ("messages", "citations", "[]"),
    ("messages", "skill_used", None),
    ("messages", "token_usage", None),
]

# 旧 schema：四张表都只含迁移前就存在的基础列，缺全部 9 个迁移目标列
_OLD_SCHEMA_DDL = {
    "chunks": (
        "CREATE TABLE chunks (id INTEGER PRIMARY KEY, paper_id INTEGER, "
        "content TEXT, page_number INTEGER, chunk_index INTEGER)"
    ),
    "papers": "CREATE TABLE papers (id INTEGER PRIMARY KEY, title VARCHAR(500))",
    "conversations": "CREATE TABLE conversations (id INTEGER PRIMARY KEY, title VARCHAR(200))",
    "messages": (
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, "
        "conversation_id INTEGER, role VARCHAR(20), content TEXT)"
    ),
}

_OLD_ROWS = {
    "chunks": (1, 1, "旧分块", 1, 0),
    "papers": (1, "旧文献"),
    "conversations": (1, "旧会话"),
    "messages": (1, 1, "user", "旧消息"),
}


def _build_old_db(
    db_path, tables=("papers", "chunks", "conversations", "messages")
):
    """用 sqlite3 手工建旧 schema 库并插入存量数据。"""
    conn = sqlite3.connect(db_path)
    for table in tables:
        conn.execute(_OLD_SCHEMA_DDL[table])
        placeholders = ", ".join("?" * len(_OLD_ROWS[table]))
        conn.execute(f"INSERT INTO {table} VALUES ({placeholders})", _OLD_ROWS[table])
    conn.commit()
    conn.close()


def _make_engine(db_path):
    return create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})


def _columns(db_path, table):
    """读取指定表的全部列名。"""
    conn = sqlite3.connect(db_path)
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    conn.close()
    return cols


def _cell(db_path, table, column, row_id=1):
    """读取存量行（id=1）在指定列上的值。"""
    conn = sqlite3.connect(db_path)
    val = conn.execute(f"SELECT {column} FROM {table} WHERE id=?", (row_id,)).fetchone()[0]
    conn.close()
    return val


@pytest.fixture()
def old_db(tmp_path, monkeypatch):
    """旧 schema 库 + 被替换的 app.database.engine，返回库文件路径。"""
    db_path = tmp_path / "old.db"
    _build_old_db(db_path)
    monkeypatch.setattr(database_module, "engine", _make_engine(db_path))
    return db_path


def test_migration_registry_has_exactly_seven_branches():
    """锁定：登记表当前恰好 9 个迁移分支（新增字段登记时应同步更新本测试）。"""
    total = sum(len(cols) for cols in SCHEMA_MIGRATIONS.values())
    assert total == 9
    assert set(SCHEMA_MIGRATIONS) == {
        "papers", "chunks", "conversations", "messages"
    }


class TestSevenMigrationBranches:
    """逐一锁定 9 个 ADD COLUMN 分支：列被补齐且存量行获得预期默认值/NULL。"""

    @pytest.mark.parametrize(
        ("table", "column", "expected_default"),
        MIGRATION_EXPECTATIONS,
        ids=[f"{t}.{c}" for t, c, _ in MIGRATION_EXPECTATIONS],
    )
    def test_branch_adds_column_with_expected_default(
        self, old_db, table, column, expected_default
    ):
        """锁定：缺失列经 ALTER TABLE ADD COLUMN 补齐；带 DEFAULT 的分支存量行取默认值，
        不带 DEFAULT 的分支存量行为 NULL。"""
        assert column not in _columns(old_db, table)  # 前置：迁移前确实缺列

        ensure_schema()

        assert column in _columns(old_db, table)
        assert _cell(old_db, table, column) == expected_default

    def test_all_seven_columns_present_after_migration(self, old_db):
        """锁定：旧库执行一次 ensure_schema 后 9 个登记列全部存在。"""
        ensure_schema()

        for table, column, _ in MIGRATION_EXPECTATIONS:
            assert column in _columns(old_db, table), f"{table}.{column} 未被迁移补齐"


class TestMigrationSemantics:
    """迁移的整体语义：数据保留、幂等、跳表、空库、降级。"""

    def test_existing_data_preserved(self, old_db):
        """锁定：迁移不破坏存量数据，基础列的值原样保留。"""
        ensure_schema()

        conn = sqlite3.connect(old_db)
        papers = conn.execute("SELECT id, title FROM papers").fetchall()
        conversations = conn.execute("SELECT id, title FROM conversations").fetchall()
        messages = conn.execute(
            "SELECT id, conversation_id, role, content FROM messages"
        ).fetchall()
        conn.close()

        assert papers == [(1, "旧文献")]
        assert conversations == [(1, "旧会话")]
        assert messages == [(1, 1, "user", "旧消息")]

    def test_ensure_schema_idempotent(self, old_db):
        """锁定：重复执行 ensure_schema 幂等——无异常、列不重复、数据不变。"""
        ensure_schema()
        cols_after_first = {t: _columns(old_db, t) for t in _OLD_SCHEMA_DDL}

        ensure_schema()  # 第二次执行不应抛异常

        for t in _OLD_SCHEMA_DDL:
            assert _columns(old_db, t) == cols_after_first[t]
        assert _cell(old_db, "papers", "last_read_page") == 1

    def test_missing_table_silently_skipped(self, tmp_path, monkeypatch):
        """锁定：登记表中的表在库里不存在时整张表跳过，其余表正常迁移。"""
        db_path = tmp_path / "partial.db"
        _build_old_db(
            db_path, tables=("papers", "chunks", "messages")
        )  # 无 conversations
        monkeypatch.setattr(database_module, "engine", _make_engine(db_path))

        ensure_schema()  # 不应抛异常

        # 其余两张表正常补齐
        for table, column, _ in MIGRATION_EXPECTATIONS:
            if table == "conversations":
                continue
            assert column in _columns(db_path, table)
        # conversations 表仍然是「不存在」，迁移不会替它建表
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='conversations'"
        ).fetchone()
        conn.close()
        assert row is None

    def test_empty_database_is_noop(self, tmp_path, monkeypatch):
        """锁定：全新空库（无任何表）执行 ensure_schema 整体无操作、不报错、不建表。"""
        db_path = tmp_path / "empty.db"
        sqlite3.connect(db_path).close()  # 建空库文件
        monkeypatch.setattr(database_module, "engine", _make_engine(db_path))

        ensure_schema()

        conn = sqlite3.connect(db_path)
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        conn.close()
        assert tables == []

    def test_migration_failure_propagates_and_logs(self, tmp_path, monkeypatch, caplog):
        """迁移失败必须 fail-close，不得伪装成启动成功。"""
        db_path = tmp_path / "corrupt.db"
        db_path.write_bytes(b"this is not a sqlite database file")
        monkeypatch.setattr(database_module, "engine", _make_engine(db_path))

        with caplog.at_level(logging.WARNING, logger="papermind"):
            with pytest.raises(Exception):
                ensure_schema()

        assert any(
            "[schema] 轻量级迁移失败" in r.message and r.levelno == logging.WARNING
            for r in caplog.records
        )


class TestPragmaListener:
    """连接事件监听器的 PRAGMA 行为（直接以原生连接调用，规避内存库 WAL 无操作）。"""

    def test_pragma_listener_sets_wal_and_synchronous_normal(self, tmp_path):
        """锁定：_set_sqlite_pragma 对连接执行 journal_mode=WAL 与 synchronous=NORMAL。"""
        db_path = tmp_path / "pragma.db"
        conn = sqlite3.connect(db_path)
        try:
            database_module._set_sqlite_pragma(conn, None)
            journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            synchronous = conn.execute("PRAGMA synchronous").fetchone()[0]
        finally:
            conn.close()

        assert journal_mode.lower() == "wal"
        assert synchronous == 1  # NORMAL
