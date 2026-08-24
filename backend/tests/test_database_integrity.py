"""SQLite 外键和删除关联数据的一致性测试。"""

import sqlite3
from types import SimpleNamespace

import pytest

from app.database import _set_sqlite_pragma
from app.main import _preflight_database
from app.models import Conversation, MemorySummary, Paper, PaperCitation
from app.routers import papers as papers_router


def _paper(db, title: str) -> Paper:
    paper = Paper(
        title=title,
        file_path=f"papers/{title}.pdf",
        filename=f"{title}.pdf",
    )
    db.add(paper)
    db.flush()
    return paper


def test_sqlite_connect_pragma_enables_foreign_keys():
    connection = sqlite3.connect(":memory:")
    try:
        _set_sqlite_pragma(connection, None)
        enabled = connection.execute("PRAGMA foreign_keys").fetchone()[0]
    finally:
        connection.close()

    assert enabled == 1


def test_startup_preflight_skips_new_database(tmp_path):
    report = _preflight_database(tmp_path / "missing.db")

    assert report == {
        "exists": False,
        "quick_check_ok": True,
        "foreign_key_violation_count": 0,
        "orphan_paper_tags_count": 0,
    }


def test_startup_preflight_rejects_corrupt_database(tmp_path):
    database = tmp_path / "corrupt.db"
    database.write_bytes(b"not a sqlite database")

    with pytest.raises(sqlite3.DatabaseError):
        _preflight_database(database)


def test_startup_preflight_reports_foreign_keys_without_blocking(tmp_path):
    database = tmp_path / "library.db"
    with sqlite3.connect(database) as conn:
        conn.executescript(
            """
            PRAGMA foreign_keys=OFF;
            CREATE TABLE papers (id INTEGER PRIMARY KEY);
            CREATE TABLE tags (id INTEGER PRIMARY KEY);
            CREATE TABLE paper_tags (
                paper_id INTEGER REFERENCES papers(id),
                tag_id INTEGER REFERENCES tags(id)
            );
            INSERT INTO tags VALUES (1);
            INSERT INTO paper_tags VALUES (999, 1);
            """
        )

    report = _preflight_database(database)

    assert report["exists"] is True
    assert report["quick_check_ok"] is True
    assert report["foreign_key_violation_count"] == 1
    assert report["orphan_paper_tags_count"] == 1


def test_explicit_readonly_sqlalchemy_session_rejects_writes(tmp_path):
    from sqlalchemy.exc import OperationalError

    from app.services.data_integrity import open_readonly_sqlalchemy_database

    database = tmp_path / "readonly.db"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE sample (value INTEGER)")
        conn.execute("INSERT INTO sample VALUES (1)")
    engine, session_factory = open_readonly_sqlalchemy_database(database)
    try:
        with session_factory() as db:
            connection = db.connection()
            assert connection.exec_driver_sql(
                "SELECT value FROM sample"
            ).scalar() == 1
            with pytest.raises(OperationalError):
                connection.exec_driver_sql("INSERT INTO sample VALUES (2)")
                db.commit()
    finally:
        engine.dispose()


def test_delete_paper_removes_incoming_and_outgoing_citation_edges(
    client, db, monkeypatch
):
    center = _paper(db, "中心")
    incoming = _paper(db, "入边")
    outgoing = _paper(db, "出边")
    unrelated_a = _paper(db, "无关甲")
    unrelated_b = _paper(db, "无关乙")
    db.add_all(
        [
            PaperCitation(citing_id=incoming.id, cited_id=center.id),
            PaperCitation(citing_id=center.id, cited_id=outgoing.id),
            PaperCitation(citing_id=unrelated_a.id, cited_id=unrelated_b.id),
        ]
    )
    db.commit()
    monkeypatch.setattr(
        papers_router,
        "get_vector_store",
        lambda: SimpleNamespace(delete_by_paper_id=lambda paper_id: None),
    )

    response = client.delete(f"/api/papers/{center.id}")

    assert response.status_code == 204
    remaining = db.query(PaperCitation).all()
    assert [(edge.citing_id, edge.cited_id) for edge in remaining] == [
        (unrelated_a.id, unrelated_b.id)
    ]


def test_delete_conversation_detaches_source_memory(client, db):
    conversation = Conversation(title="待删除会话", message_count=0)
    db.add(conversation)
    db.flush()
    memory = MemorySummary(
        memory_type="fact",
        content="仍需保留的研究事实",
        source_conversation_id=conversation.id,
    )
    db.add(memory)
    db.commit()

    response = client.delete(f"/api/chat/conversations/{conversation.id}")

    assert response.status_code == 204
    db.refresh(memory)
    assert memory.content == "仍需保留的研究事实"
    assert memory.source_conversation_id is None
