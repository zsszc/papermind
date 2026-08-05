"""文献处理失败状态与同篇互斥契约测试。"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.models import Paper
from app.routers import papers as papers_router
from app.services.processor import PaperProcessor


def _paper(db, file_path="papers/missing.pdf") -> Paper:
    paper = Paper(
        title="处理状态测试",
        file_path=file_path,
        filename="missing.pdf",
        processed="pending",
    )
    db.add(paper)
    db.commit()
    return paper


def test_processor_raises_when_source_pdf_is_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPERMIND_DATA_DIR", str(tmp_path))
    processor = PaperProcessor.__new__(PaperProcessor)
    paper = SimpleNamespace(file_path="papers/missing.pdf")

    with pytest.raises(FileNotFoundError, match="PDF"):
        processor.process(paper, MagicMock())


def test_manual_process_rejects_error_result_and_marks_paper_error(
    client, db, monkeypatch
):
    paper = _paper(db)
    fake_processor = MagicMock()
    fake_processor.process.return_value = {
        "status": "error",
        "message": "模拟处理器错误结果",
    }
    monkeypatch.setattr(papers_router, "PaperProcessor", lambda: fake_processor)

    response = client.post(f"/api/papers/{paper.id}/process")

    assert response.status_code == 500
    assert response.json()["detail"] == "文献处理失败，请稍后再试"
    db.refresh(paper)
    assert paper.processed == "error"


def test_manual_process_returns_409_when_same_paper_is_locked(client, db, monkeypatch):
    paper = _paper(db)
    fake_processor = MagicMock()
    fake_processor.process.return_value = {"status": "ok", "pages": 1, "chunks": 1}
    monkeypatch.setattr(papers_router, "PaperProcessor", lambda: fake_processor)

    lock = papers_router._get_paper_lock(paper.id)
    assert lock.acquire(blocking=False)
    try:
        response = client.post(f"/api/papers/{paper.id}/process")
    finally:
        lock.release()
        with papers_router._paper_locks_lock:
            papers_router._paper_locks.pop(paper.id, None)

    assert response.status_code == 409
    fake_processor.process.assert_not_called()
