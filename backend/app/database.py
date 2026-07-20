from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, declarative_base

from app.core.config import config
from app.core.logger import logger

DATABASE_URL = f"sqlite:///{config.data_dir / 'papers.db'}"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    echo=False,
)

# 版本化 Schema 迁移记录：映射 {表名: {列名: (SQL 类型, 默认值)}}
# 新增模型字段时，只需在这里登记，启动时会自动 ALTER TABLE ADD COLUMN
SCHEMA_MIGRATIONS = {
    "papers": {
        "last_read_page": ("INTEGER", 1),
        "metadata_json": ("JSON", "'{}'"),
    },
    "conversations": {
        "paper_ids": ("JSON", "'[]'"),
        "summary": ("TEXT", None),
    },
    "messages": {
        "citations": ("JSON", "'[]'"),
        "skill_used": ("VARCHAR(100)", None),
        "token_usage": ("INTEGER", None),
    },
}


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_conn, connection_record):
    """启用 WAL 模式，提升并发写入性能，减少 database locked。"""
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("PRAGMA synchronous=NORMAL;")
    cursor.close()


def _apply_schema_migrations():
    """执行轻量级 Schema 迁移：自动为旧表补齐模型中新增的列。"""
    try:
        with engine.connect() as conn:
            for table_name, columns in SCHEMA_MIGRATIONS.items():
                # 查询表是否存在
                table_exists = conn.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (table_name,),
                ).fetchone()
                if not table_exists:
                    continue

                # 获取当前已有列
                existing = {
                    row[1]
                    for row in conn.exec_driver_sql(
                        f"PRAGMA table_info({table_name})"
                    ).fetchall()
                }

                for col_name, (col_type, default_val) in columns.items():
                    if col_name in existing:
                        continue
                    if default_val is not None:
                        sql = f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_type} DEFAULT {default_val}"
                    else:
                        sql = f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_type}"
                    conn.exec_driver_sql(sql)
                    logger.info(f"[schema] 已迁移 {table_name}.{col_name}")
            conn.commit()
    except Exception as e:
        logger.warning(f"[schema] 轻量级迁移失败: {e}", exc_info=True)


def ensure_schema():
    """在 Base.metadata.create_all 之后调用，保证旧数据库也能对齐新列。"""
    _apply_schema_migrations()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
