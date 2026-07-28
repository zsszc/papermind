"""上传接口测试：大小限制（413）、扩展名白名单（400）、正常小文件上传成功。

说明：
- 通过 monkeypatch 把 MAX_UPLOAD_SIZE 改小来测试 413，无需真的生成 50MB 文件；
- papers 导入触发的后台处理线程（embedding/ChromaDB/LLM）全部 mock 掉；
- thesis 上传的 DocxParser.parse 也 mock 掉，保证测试快速、离线、稳定。
"""

import shutil
import uuid
from pathlib import Path

import pytest

from app.routers import papers as papers_router
from app.routers import thesis as thesis_router

# 项目根目录（路由内用 Path(__file__).resolve().parents[3] 定位，上传目录必须在其下，
# 否则路由里的 relative_to 会抛 ValueError）
PROJECT_ROOT = Path(papers_router.__file__).resolve().parents[3]


def _make_temp_dir(name: str) -> Path:
    """在项目根下创建独立临时目录（隐藏命名，不污染真实数据目录）。"""
    path = PROJECT_ROOT / f".pytest_tmp_{name}_{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=True)
    return path

# 最小 PDF 字节流：即使解析失败，导入接口也会容错（metadata 记 parse_error）继续
MINIMAL_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)

FAKE_DOCX = b"PK\x03\x04 fake docx content"


@pytest.fixture()
def papers_env(client, monkeypatch):
    """papers 导入环境：数据目录重定向到临时目录，后台处理 mock 掉。"""
    papers_dir = _make_temp_dir("papers")
    notes_dir = _make_temp_dir("notes")
    monkeypatch.setattr(papers_router, "get_papers_dir", lambda: papers_dir)
    monkeypatch.setattr(papers_router, "get_notes_dir", lambda: notes_dir)
    # mock 后台处理入口，避免触发 embedding/ChromaDB/LLM
    monkeypatch.setattr(papers_router, "_process_paper_background", lambda paper_id: None)
    try:
        yield client, papers_dir, notes_dir
    finally:
        shutil.rmtree(papers_dir, ignore_errors=True)
        shutil.rmtree(notes_dir, ignore_errors=True)


@pytest.fixture()
def thesis_env(client, monkeypatch):
    """thesis 上传环境：数据目录重定向到临时目录，DocxParser.parse mock 掉。"""
    thesis_dir = _make_temp_dir("my-thesis")
    monkeypatch.setattr(thesis_router, "get_thesis_dir", lambda: thesis_dir)

    def fake_parse(self, path):
        return {
            "title": "测试毕业论文",
            "chapters": [],
            "word_count": 100,
            "citations": [],
            "paragraphs": [],
        }

    monkeypatch.setattr(thesis_router.DocxParser, "parse", fake_parse)
    try:
        yield client, thesis_dir
    finally:
        shutil.rmtree(thesis_dir, ignore_errors=True)


def test_import_oversized_pdf_returns_413(papers_env, monkeypatch):
    """超过单文件大小上限的 PDF 应返回 413，且不落地残留文件。"""
    client, papers_dir, _ = papers_env
    # 把上限改小到 10 字节，用小文件即可触发 413，无需生成 50MB
    monkeypatch.setattr(papers_router, "MAX_UPLOAD_SIZE", 10)

    r = client.post(
        "/api/papers/import",
        files=[("files", ("big.pdf", MINIMAL_PDF, "application/pdf"))],
    )
    assert r.status_code == 413
    assert list(papers_dir.iterdir()) == []


def test_import_invalid_extension_returns_400(papers_env):
    """非 .pdf 扩展名应返回 400。"""
    client, _, _ = papers_env
    r = client.post(
        "/api/papers/import",
        files=[("files", ("notes.txt", b"hello", "text/plain"))],
    )
    assert r.status_code == 400


def test_import_small_pdf_success(papers_env):
    """正常小 PDF 上传成功：落盘、建库记录、生成空笔记文件。"""
    client, papers_dir, notes_dir = papers_env
    r = client.post(
        "/api/papers/import",
        files=[("files", ("paper.pdf", MINIMAL_PDF, "application/pdf"))],
    )
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 1
    item = data["items"][0]
    assert item["filename"] == "paper.pdf"

    # PDF 与笔记文件均已写入（重定向后的 tmp 目录）
    assert (papers_dir / "paper.pdf").exists()
    assert (notes_dir / f"{item['id']}.md").exists()


def test_thesis_upload_oversized_returns_413(thesis_env, monkeypatch):
    """超过单文件大小上限的 docx 应返回 413，且不落地残留文件。"""
    client, thesis_dir = thesis_env
    monkeypatch.setattr(thesis_router, "MAX_UPLOAD_SIZE", 10)

    r = client.post(
        "/api/thesis/upload",
        files={"file": ("thesis.docx", FAKE_DOCX, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    )
    assert r.status_code == 413
    assert list(thesis_dir.iterdir()) == []


def test_thesis_upload_invalid_extension_returns_400(thesis_env):
    """非 .docx 扩展名应返回 400。"""
    client, _ = thesis_env
    r = client.post(
        "/api/thesis/upload",
        files={"file": ("thesis.pdf", MINIMAL_PDF, "application/pdf")},
    )
    assert r.status_code == 400


def test_thesis_upload_small_docx_success(thesis_env):
    """正常小 docx 上传成功（DocxParser.parse 已 mock）。"""
    client, thesis_dir = thesis_env
    r = client.post(
        "/api/thesis/upload",
        files={"file": ("thesis.docx", FAKE_DOCX, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["filename"] == "thesis.docx"
    assert data["title"] == "测试毕业论文"
    assert (thesis_dir / "thesis.docx").exists()
