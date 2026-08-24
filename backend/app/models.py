import datetime
from sqlalchemy import (
    Column,
    Integer,
    String,
    Text,
    DateTime,
    Boolean,
    ForeignKey,
    JSON,
    Table,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import relationship

from app.database import Base


paper_tags = Table(
    "paper_tags",
    Base.metadata,
    Column("paper_id", Integer, ForeignKey("papers.id"), primary_key=True),
    Column("tag_id", Integer, ForeignKey("tags.id"), primary_key=True),
)


class Paper(Base):
    __tablename__ = "papers"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(500), nullable=True)
    authors = Column(Text, nullable=True)
    year = Column(Integer, nullable=True)
    journal = Column(String(500), nullable=True)
    abstract = Column(Text, nullable=True)
    doi = Column(String(200), nullable=True, index=True)
    pages = Column(Integer, nullable=True)
    file_path = Column(String(1000), nullable=False)
    filename = Column(String(500), nullable=False)
    status = Column(String(50), default="unread")  # unread / read / important / todo
    source = Column(String(50), default="local")  # local / arxiv / crossref
    processed = Column(String(50), default="pending")  # pending / processing / done / error
    last_read_page = Column(Integer, nullable=True, default=1)
    metadata_json = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    tags = relationship("Tag", secondary=paper_tags, back_populates="papers")
    chunks = relationship("Chunk", back_populates="paper", cascade="all, delete-orphan")


class Chunk(Base):
    __tablename__ = "chunks"

    id = Column(Integer, primary_key=True, index=True)
    paper_id = Column(Integer, ForeignKey("papers.id"), nullable=False)
    content = Column(Text, nullable=False)
    page_number = Column(Integer, nullable=True)
    # 相对 PDFParser 页文本的 0-based 半开字符区间；旧库/摘要允许为空。
    page_start = Column(Integer, nullable=True)
    page_end = Column(Integer, nullable=True)
    chunk_index = Column(Integer, nullable=False, default=0)
    section_title = Column(String(500), nullable=True)
    chunk_type = Column(String(50), default="paragraph")  # abstract / intro / method / result / conclusion
    token_count = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    paper = relationship("Paper", back_populates="chunks")


class Tag(Base):
    __tablename__ = "tags"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), unique=True, nullable=False)
    color = Column(String(20), default="#1890ff")
    description = Column(Text, nullable=True)

    papers = relationship("Paper", secondary=paper_tags, back_populates="tags")


class Conversation(Base):
    __tablename__ = "conversations"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(500), nullable=True)
    summary = Column(Text, nullable=True)
    paper_ids = Column(JSON, default=list)
    message_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    messages = relationship("Message", back_populates="conversation", cascade="all, delete-orphan")


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, index=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id"), nullable=False)
    role = Column(String(50), nullable=False)  # user / assistant / system
    content = Column(Text, nullable=False)
    citations = Column(JSON, default=list)
    skill_used = Column(String(100), nullable=True)
    token_usage = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    conversation = relationship("Conversation", back_populates="messages")


class Skill(Base):
    __tablename__ = "skills"

    id = Column(Integer, primary_key=True, index=True)
    skill_id = Column(String(100), unique=True, nullable=False)
    display_name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    trigger_words = Column(JSON, default=list)
    parameters = Column(JSON, default=dict)
    prompt_template = Column(Text, nullable=True)
    icon = Column(String(100), nullable=True)
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class ThesisFile(Base):
    __tablename__ = "thesis_files"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(500), nullable=True)
    file_path = Column(String(1000), nullable=False)
    filename = Column(String(500), nullable=False)
    chapter_structure = Column(JSON, default=list)  # [{title, level, start_paragraph, end_paragraph}]
    word_count = Column(Integer, nullable=True)
    metadata_json = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    citations = relationship("ThesisCitation", back_populates="thesis", cascade="all, delete-orphan")


class ThesisCitation(Base):
    __tablename__ = "thesis_citations"

    id = Column(Integer, primary_key=True, index=True)
    thesis_id = Column(Integer, ForeignKey("thesis_files.id"), nullable=False)
    paper_id = Column(Integer, ForeignKey("papers.id"), nullable=True)
    chapter_index = Column(Integer, nullable=True)
    section_index = Column(Integer, nullable=True)
    context = Column(Text, nullable=True)
    citation_text = Column(String(500), nullable=True)  # [1] 或 (Zhou et al., 2024)
    detected_auto = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    thesis = relationship("ThesisFile", back_populates="citations")
    paper = relationship("Paper")


class MemorySummary(Base):
    __tablename__ = "memory_summaries"

    id = Column(Integer, primary_key=True, index=True)
    memory_type = Column(String(50), default="short_term")  # short_term / long_term / preference / fact
    content = Column(Text, nullable=False)
    source_conversation_id = Column(Integer, ForeignKey("conversations.id"), nullable=True)
    importance = Column(Integer, default=5)  # 1-10
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    conversation = relationship("Conversation")


# ---------- FTS5 全文检索虚拟表 ----------

_PAPERS_FTS_DDL = """
    CREATE VIRTUAL TABLE IF NOT EXISTS papers_fts USING fts5(
        title, authors, abstract,
        content='papers',
        content_rowid='id'
    )
"""

_PAPERS_FTS_TRIGGERS = [
    """
    CREATE TRIGGER IF NOT EXISTS papers_fts_insert AFTER INSERT ON papers BEGIN
        INSERT INTO papers_fts(rowid, title, authors, abstract)
        VALUES (new.id, new.title, new.authors, new.abstract);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS papers_fts_update AFTER UPDATE ON papers BEGIN
        INSERT INTO papers_fts(papers_fts, rowid, title, authors, abstract)
        VALUES ('delete', old.id, old.title, old.authors, old.abstract);
        INSERT INTO papers_fts(rowid, title, authors, abstract)
        VALUES (new.id, new.title, new.authors, new.abstract);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS papers_fts_delete AFTER DELETE ON papers BEGIN
        INSERT INTO papers_fts(papers_fts, rowid, title, authors, abstract)
        VALUES ('delete', old.id, old.title, old.authors, old.abstract);
    END
    """,
]


def ensure_papers_fts(engine):
    """确保 papers_fts 虚拟表、触发器存在，并重建索引。"""
    from app.core.logger import logger
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql(_PAPERS_FTS_DDL)
            for trigger in _PAPERS_FTS_TRIGGERS:
                conn.exec_driver_sql(trigger)
            conn.exec_driver_sql("INSERT INTO papers_fts(papers_fts) VALUES ('rebuild')")
            conn.commit()
        logger.info("[fts] papers_fts 虚拟表检查/重建完成")
    except Exception as e:
        logger.warning(f"[fts] papers_fts 初始化失败: {e}", exc_info=True)
        raise


@event.listens_for(Paper.__table__, "after_create")
def _create_papers_fts_table(target, connection, **kw):
    """papers 表创建后，创建 FTS5 虚拟表及同步触发器。"""
    connection.exec_driver_sql(_PAPERS_FTS_DDL)
    for trigger in _PAPERS_FTS_TRIGGERS:
        connection.exec_driver_sql(trigger)
    connection.exec_driver_sql("INSERT INTO papers_fts(papers_fts) VALUES ('rebuild')")


@event.listens_for(Paper.__table__, "after_drop")
def _drop_papers_fts_table(target, connection, **kw):
    connection.exec_driver_sql("DROP TABLE IF EXISTS papers_fts")

class PaperAnnotation(Base):
    __tablename__ = "paper_annotations"

    id = Column(Integer, primary_key=True, index=True)
    paper_id = Column(Integer, ForeignKey("papers.id"), nullable=False)
    page_number = Column(Integer, nullable=False)
    selected_text = Column(Text, nullable=False)
    note = Column(Text, nullable=True)
    color = Column(String(20), default="yellow")
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    paper = relationship("Paper", back_populates="annotations")


Paper.annotations = relationship("PaperAnnotation", back_populates="paper", cascade="all, delete-orphan")


class PaperCitation(Base):
    """文献间引用边（Phase G / G1）：citing_id 引用了 cited_id。"""

    __tablename__ = "paper_citations"

    id = Column(Integer, primary_key=True, index=True)
    citing_id = Column(Integer, ForeignKey("papers.id"), nullable=False)
    cited_id = Column(Integer, ForeignKey("papers.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("citing_id", "cited_id", name="uq_paper_citations_pair"),
    )

    citing_paper = relationship("Paper", foreign_keys=[citing_id])
    cited_paper = relationship("Paper", foreign_keys=[cited_id])
