"""SQLite 外键和删除关联数据的一致性测试。"""

import sqlite3
from types import SimpleNamespace

from app.database import _set_sqlite_pragma
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
