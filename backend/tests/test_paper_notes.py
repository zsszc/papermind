"""PaperDetail 笔记存在性、大小上限与原子写入契约。"""

from pathlib import Path

import pytest

from app.models import Paper


def _paper(db) -> Paper:
    paper = Paper(
        title="笔记测试论文",
        filename="note.pdf",
        file_path="papers/note.pdf",
    )
    db.add(paper)
    db.commit()
    db.refresh(paper)
    return paper


def test_note_endpoints_reject_missing_paper(client, monkeypatch, tmp_path):
    monkeypatch.setattr("app.routers.papers.get_notes_dir", lambda: tmp_path)

    assert client.get("/api/papers/999/note").status_code == 404
    response = client.post("/api/papers/999/note", data={"content": "孤立内容"})

    assert response.status_code == 404
    assert not (tmp_path / "999.md").exists()


def test_note_rejects_utf8_body_larger_than_one_mib(client, db, monkeypatch, tmp_path):
    paper = _paper(db)
    monkeypatch.setattr("app.routers.papers.get_notes_dir", lambda: tmp_path)

    response = client.post(
        f"/api/papers/{paper.id}/note",
        data={"content": "界" * (1024 * 1024 // 3 + 1)},
    )

    assert response.status_code == 413
    assert not (tmp_path / f"{paper.id}.md").exists()


def test_note_valid_content_is_written_atomically(client, db, monkeypatch, tmp_path):
    paper = _paper(db)
    monkeypatch.setattr("app.routers.papers.get_notes_dir", lambda: tmp_path)

    response = client.post(f"/api/papers/{paper.id}/note", data={"content": "最新笔记"})

    assert response.status_code == 200
    assert (tmp_path / f"{paper.id}.md").read_text(encoding="utf-8") == "最新笔记"
    assert list(tmp_path.glob(".*.tmp")) == []


def test_atomic_note_failure_preserves_old_file(monkeypatch, tmp_path):
    from app.services.note_storage import atomic_write_note

    target = tmp_path / "1.md"
    target.write_text("旧笔记", encoding="utf-8")

    def fail_replace(_source: Path, _target: Path):
        raise OSError("replace failed")

    monkeypatch.setattr("app.services.note_storage.os.replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        atomic_write_note(target, "新笔记")

    assert target.read_text(encoding="utf-8") == "旧笔记"
    assert list(tmp_path.glob(".*.tmp")) == []
