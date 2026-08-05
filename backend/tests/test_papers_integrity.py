"""papers 数据完整性契约测试（Batch 7b / F9 + F10）。

F9：批量导入中途失败时，清理本次为失败篇目已落盘的 PDF/笔记文件（不留孤儿），
    该篇无 DB 记录、返回错误标记，其余篇目不受影响。
F10：删除 paper 时级联删除 thesis_citations 中 paper_id 关联行（不留悬空引用）。
"""

import shutil
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models import Paper, ThesisFile, ThesisCitation
from app.routers import papers as papers_router

# 项目根目录（路由内用 Path(__file__).resolve().parents[3] 定位）
PROJECT_ROOT = Path(papers_router.__file__).resolve().parents[3]

# 最小 PDF 字节流：即使解析失败，导入接口也会容错继续
MINIMAL_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)


def _make_temp_dir(name: str) -> Path:
    """在项目根下创建独立临时目录（隐藏命名，不污染真实数据目录）。"""
    path = PROJECT_ROOT / f".pytest_tmp_{name}_{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture()
def papers_env(client, db, monkeypatch):
    """papers 导入环境：数据目录重定向到临时目录，后台处理 mock 掉。"""
    papers_dir = _make_temp_dir("papers")
    notes_dir = _make_temp_dir("notes")
    monkeypatch.setattr(papers_router, "get_papers_dir", lambda: papers_dir)
    monkeypatch.setattr(papers_router, "get_notes_dir", lambda: notes_dir)
    # mock 后台处理入口，避免触发 embedding/ChromaDB/LLM
    monkeypatch.setattr(papers_router, "_process_paper_background", lambda paper_id: None)
    try:
        yield client, db, papers_dir, notes_dir
    finally:
        shutil.rmtree(papers_dir, ignore_errors=True)
        shutil.rmtree(notes_dir, ignore_errors=True)


class TestImportOrphanCleanup:
    """F9：批量导入中途失败的孤儿文件清理。"""

    def test_second_file_save_fails_first_kept_no_residue(self, papers_env, monkeypatch):
        """AC2：第二篇写盘注入失败 → 第一篇成功保留，第二篇无文件残留，返回错误标记。"""
        client, db, papers_dir, notes_dir = papers_env
        real_save = papers_router._save_upload_file
        calls = {"n": 0}

        async def flaky_save(file, target_path, max_size=None):
            calls["n"] += 1
            if calls["n"] == 2:
                # 模拟写了半成品后失败（验证清理真的发生，而非本来就没文件）
                target_path.write_bytes(b"partial-bytes")
                raise IOError("模拟写盘失败")
            return await real_save(file, target_path, max_size)

        monkeypatch.setattr(papers_router, "_save_upload_file", flaky_save)

        r = client.post(
            "/api/papers/import",
            files=[
                ("files", ("a.pdf", MINIMAL_PDF, "application/pdf")),
                ("files", ("b.pdf", MINIMAL_PDF, "application/pdf")),
            ],
        )
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 1
        assert data["items"][0]["filename"] == "a.pdf"

        # 错误标记：指出失败文件名，不带异常原文
        assert len(data["errors"]) == 1
        assert data["errors"][0]["filename"] == "b.pdf"
        assert "模拟写盘失败" not in str(data["errors"])

        # 第一篇的 PDF 与笔记保留；第二篇无任何残留
        assert (papers_dir / "a.pdf").exists()
        assert not (papers_dir / "b.pdf").exists()
        first_id = data["items"][0]["id"]
        assert (notes_dir / f"{first_id}.md").exists()
        assert len(list(notes_dir.iterdir())) == 1

        # DB 只有第一篇
        assert db.query(Paper).count() == 1
        assert db.query(Paper).first().filename == "a.pdf"

    def test_note_write_fails_cleans_pdf_and_continues(self, papers_env, monkeypatch):
        """第一篇在笔记写入阶段失败 → 其 PDF 一并清理，第二篇继续成功导入。"""
        client, db, papers_dir, notes_dir = papers_env
        real_run = papers_router.run_in_threadpool
        failed = {"done": False}

        async def flaky_run(func, *args):
            # 仅让第一次笔记写入（Path.write_text）失败
            if not failed["done"] and getattr(func, "__name__", "") == "write_text":
                failed["done"] = True
                raise IOError("模拟笔记写入失败")
            return await real_run(func, *args)

        monkeypatch.setattr(papers_router, "run_in_threadpool", flaky_run)

        r = client.post(
            "/api/papers/import",
            files=[
                ("files", ("a.pdf", MINIMAL_PDF, "application/pdf")),
                ("files", ("b.pdf", MINIMAL_PDF, "application/pdf")),
            ],
        )
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 1
        assert data["items"][0]["filename"] == "b.pdf"
        assert [e["filename"] for e in data["errors"]] == ["a.pdf"]

        # a 篇 PDF 已落盘但随失败清理；b 篇 PDF/笔记正常
        assert not (papers_dir / "a.pdf").exists()
        assert (papers_dir / "b.pdf").exists()
        second_id = data["items"][0]["id"]
        assert (notes_dir / f"{second_id}.md").exists()
        assert len(list(notes_dir.iterdir())) == 1

        # DB 只有 b 篇（a 篇 flush 过的记录随失败回滚）
        papers = db.query(Paper).all()
        assert len(papers) == 1
        assert papers[0].filename == "b.pdf"

    def test_all_files_fail_no_residue_anywhere(self, papers_env, monkeypatch):
        """全部篇目失败 → 所有落盘文件清理完毕，DB 无记录，逐篇返回错误标记。

        写盘直接抛错（不落半成品），同时覆盖「文件已不存在时清理容错」边界。
        """
        client, db, papers_dir, notes_dir = papers_env

        async def always_fail(file, target_path, max_size=None):
            raise IOError("模拟写盘失败")

        monkeypatch.setattr(papers_router, "_save_upload_file", always_fail)

        r = client.post(
            "/api/papers/import",
            files=[
                ("files", ("a.pdf", MINIMAL_PDF, "application/pdf")),
                ("files", ("b.pdf", MINIMAL_PDF, "application/pdf")),
            ],
        )
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 0
        assert data["items"] == []
        assert [e["filename"] for e in data["errors"]] == ["a.pdf", "b.pdf"]
        assert list(papers_dir.iterdir()) == []
        assert list(notes_dir.iterdir()) == []
        assert db.query(Paper).count() == 0


class TestDeletePaperCascadesCitations:
    """F10：删除 paper 级联清理 thesis_citations 关联行。"""

    @pytest.fixture(autouse=True)
    def _stub_vector_store(self, monkeypatch):
        """删除时会清理向量库，桩掉避免初始化 ChromaDB。"""
        fake_store = SimpleNamespace(delete_by_paper_id=lambda paper_id: None)
        monkeypatch.setattr(papers_router, "get_vector_store", lambda: fake_store)

    def _create_paper(self, db, title: str) -> Paper:
        paper = Paper(
            title=title,
            file_path="papers/nonexistent.pdf",
            filename="nonexistent.pdf",
            status="unread",
            source="local",
            processed="done",
        )
        db.add(paper)
        db.flush()
        return paper

    def _create_thesis(self, db) -> ThesisFile:
        thesis = ThesisFile(
            title="毕业论文",
            file_path="my-thesis/nonexistent.docx",
            filename="nonexistent.docx",
            chapter_structure=[],
            word_count=100,
        )
        db.add(thesis)
        db.flush()
        return thesis

    def test_delete_paper_cascades_thesis_citations(self, client, db):
        """AC3：删除有引用的 paper 后，thesis_citations 无对应行；他篇引用与大论文不受影响。"""
        paper_a = self._create_paper(db, "文献A")
        paper_b = self._create_paper(db, "文献B")
        thesis = self._create_thesis(db)
        db.add_all([
            ThesisCitation(thesis_id=thesis.id, paper_id=paper_a.id, chapter_index=0,
                           citation_text="(Zhou, 2024)", detected_auto=True),
            ThesisCitation(thesis_id=thesis.id, paper_id=paper_b.id, chapter_index=1,
                           citation_text="(Li, 2023)", detected_auto=True),
        ])
        db.commit()

        r = client.delete(f"/api/papers/{paper_a.id}")
        assert r.status_code == 204

        # A 的引用行被级联删除；B 的引用行与大论文保留
        remaining = db.query(ThesisCitation).all()
        assert len(remaining) == 1
        assert remaining[0].paper_id == paper_b.id
        assert db.query(ThesisFile).count() == 1
        assert db.query(Paper).filter(Paper.id == paper_b.id).first() is not None

    def test_delete_paper_without_citations_no_impact(self, client, db):
        """边界：paper 无任何引用时正常删除；paper_id 为空的引用行不受影响。"""
        paper = self._create_paper(db, "无引用文献")
        thesis = self._create_thesis(db)
        db.add(ThesisCitation(thesis_id=thesis.id, paper_id=None, chapter_index=0,
                              citation_text="[1]", detected_auto=True))
        db.commit()

        r = client.delete(f"/api/papers/{paper.id}")
        assert r.status_code == 204
        assert db.query(Paper).count() == 0
        assert db.query(ThesisCitation).count() == 1
