"""路由层 LLM 失败异常脱敏契约测试（Batch 7b / F8，宪法第 13 条）。

覆盖 papers /summarize、thesis /analyze、thesis /suggest-citations 三处，
每处两条失败路径：
- chat_completion 自身抛异常（兜底分支）
- chat_completion 返回 `[调用 LLM 出错: {原文}]` 错误串（llm_service 主失败路径，
  `_format_error` 兜底会透传异常原文）

期望：HTTP 5xx + 通用文案，detail 不含异常原文；原文仅入日志。
另含成功路径与前置 4xx 文案的不回归守卫。
"""

import shutil
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models import Paper, Chunk, ThesisFile
from app.routers import papers as papers_router
from app.routers import thesis as thesis_router

# 项目根目录（路由内用 Path(__file__).resolve().parents[3] 定位）
PROJECT_ROOT = Path(papers_router.__file__).resolve().parents[3]

# 各测试用独立的异常特征串，断言响应中绝不出现
SECRET_RAISE = "boom-secret-raise-path"
SECRET_STRING = "boom-secret-error-string"

# 足够长的章节正文（/analyze 有 30 字符下限）
LONG_CHAPTER_TEXT = "这是一段足够长的毕业论文章节正文，用于通过三十字符下限校验。" * 3


def _make_temp_dir(name: str) -> Path:
    """在项目根下创建独立临时目录（隐藏命名，不污染真实数据目录）。"""
    path = PROJECT_ROOT / f".pytest_tmp_{name}_{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=True)
    return path


async def _fake_chat_raise(messages, **kwargs):
    """模拟 LLM 调用自身抛异常。"""
    raise Exception(SECRET_RAISE)


async def _fake_chat_error_string(messages, **kwargs):
    """模拟 llm_service 捕获异常后返回的错误串（可能携带异常原文）。"""
    return f"[调用 LLM 出错: {SECRET_STRING}]"


async def _fake_chat_ok(messages, **kwargs):
    """模拟 LLM 正常返回 Markdown 文本。"""
    return "## 评审/概括\n\n这是一段正常的 AI 输出。"


@pytest.fixture()
def summaries_env(client, monkeypatch):
    """AI 概括落盘目录重定向到临时目录。"""
    summaries_dir = _make_temp_dir("summaries")
    monkeypatch.setattr(papers_router, "get_summaries_dir", lambda: summaries_dir)
    try:
        yield client, summaries_dir
    finally:
        shutil.rmtree(summaries_dir, ignore_errors=True)


def _create_done_paper(db) -> Paper:
    """建档一篇 processed=done 且带 chunk 的论文，满足 /summarize 前置条件。"""
    paper = Paper(
        title="脱敏测试文献",
        file_path="papers/nonexistent.pdf",
        filename="nonexistent.pdf",
        status="unread",
        source="local",
        processed="done",
    )
    db.add(paper)
    db.flush()
    db.add(Chunk(paper_id=paper.id, content="论文正文片段", chunk_index=0))
    db.commit()
    return paper


@pytest.fixture()
def thesis_env(client, db, monkeypatch):
    """thesis 分析环境：docx 文件落临时目录，DocxParser 解析 mock 掉。"""
    thesis_dir = _make_temp_dir("my-thesis")
    docx_path = thesis_dir / "test.docx"
    docx_path.write_bytes(b"PK\x03\x04 fake docx")

    def fake_parse(self, path):
        return {
            "title": "测试毕业论文",
            "chapters": [{"title": "第一章 绪论", "level": 1, "start_paragraph": 0, "end_paragraph": 0}],
            "paragraphs": [{"text": LONG_CHAPTER_TEXT}],
            "citations": [],
            "word_count": 100,
        }

    def fake_extract(self, paragraphs, chapter):
        return LONG_CHAPTER_TEXT

    monkeypatch.setattr(thesis_router.DocxParser, "parse", fake_parse)
    monkeypatch.setattr(thesis_router.DocxParser, "extract_chapter_text", fake_extract)

    thesis = ThesisFile(
        title="测试毕业论文",
        file_path=str(docx_path.relative_to(PROJECT_ROOT)),
        filename="test.docx",
        chapter_structure=[{"title": "第一章 绪论", "level": 1, "start_paragraph": 0, "end_paragraph": 0}],
        word_count=100,
    )
    db.add(thesis)
    db.commit()

    # 向量库桩掉（/suggest-citations 内部惰性 import，patch 模块属性即可）
    fake_store = SimpleNamespace(available=lambda: False, search=lambda **kwargs: [])
    monkeypatch.setattr("app.services.retrieval.get_vector_store", lambda: fake_store)
    try:
        yield client, thesis
    finally:
        shutil.rmtree(thesis_dir, ignore_errors=True)


class TestSummarizeSanitize:
    """papers /summarize 的 LLM 失败脱敏。"""

    def test_llm_raise_detail_sanitized(self, client, db, summaries_env, monkeypatch):
        """chat_completion 抛异常 → 504 通用文案，detail 不含异常原文。"""
        paper = _create_done_paper(db)
        monkeypatch.setattr(papers_router.llm_service, "chat_completion", _fake_chat_raise)

        r = client.post(f"/api/papers/{paper.id}/summarize")
        assert r.status_code == 504
        detail = r.json()["detail"]
        assert SECRET_RAISE not in detail
        assert "概括失败" in detail

    def test_llm_error_string_detail_sanitized(self, client, db, summaries_env, monkeypatch):
        """LLM 返回 `[调用 LLM 出错...]` 错误串 → 504 通用文案，不透传错误串原文。"""
        paper = _create_done_paper(db)
        monkeypatch.setattr(papers_router.llm_service, "chat_completion", _fake_chat_error_string)

        r = client.post(f"/api/papers/{paper.id}/summarize")
        assert r.status_code == 504
        detail = r.json()["detail"]
        assert SECRET_STRING not in detail
        assert "概括失败" in detail

    def test_not_done_still_400(self, client, db, summaries_env):
        """不回归：processed != done 仍为 400 原文案。"""
        paper = Paper(
            title="未处理文献",
            file_path="papers/x.pdf",
            filename="x.pdf",
            status="unread",
            source="local",
            processed="pending",
        )
        db.add(paper)
        db.commit()
        r = client.post(f"/api/papers/{paper.id}/summarize")
        assert r.status_code == 400
        assert r.json()["detail"] == "论文尚未处理完成，请稍后再试"

    def test_success_path_unchanged(self, client, db, summaries_env, monkeypatch):
        """不回归：LLM 成功时仍写概括文件并返回正文，GET /summary 剥离标题行。"""
        client_, summaries_dir = summaries_env
        paper = _create_done_paper(db)
        monkeypatch.setattr(papers_router.llm_service, "chat_completion", _fake_chat_ok)

        r = client.post(f"/api/papers/{paper.id}/summarize")
        assert r.status_code == 200
        assert r.json()["summary"].startswith("## 评审/概括")
        summary_file = summaries_dir / f"{paper.id}.md"
        assert summary_file.exists()
        assert summary_file.read_text(encoding="utf-8").startswith("# 脱敏测试文献")

        r2 = client.get(f"/api/papers/{paper.id}/summary")
        assert r2.status_code == 200
        assert not r2.json()["summary"].startswith("# ")


class TestProcessSanitize:
    """papers /process 的内部异常不得透传。"""

    def test_processor_exception_detail_sanitized(self, client, db, monkeypatch):
        paper = Paper(
            title="处理失败测试",
            file_path="papers/x.pdf",
            filename="x.pdf",
        )
        db.add(paper)
        db.commit()

        def fail_process(self, paper, db):
            raise RuntimeError(SECRET_RAISE)

        monkeypatch.setattr(papers_router.PaperProcessor, "process", fail_process)
        response = client.post(f"/api/papers/{paper.id}/process")

        assert response.status_code == 500
        assert SECRET_RAISE not in response.text
        assert response.json()["detail"] == "文献处理失败，请稍后再试"


class TestAnalyzeSanitize:
    """thesis /analyze 的 LLM 失败脱敏。"""

    def test_llm_raise_detail_sanitized(self, thesis_env, monkeypatch):
        """chat_completion 抛异常 → 500 通用文案，detail 不含异常原文。"""
        client, thesis = thesis_env
        monkeypatch.setattr(thesis_router.llm_service, "chat_completion", _fake_chat_raise)

        r = client.post(f"/api/thesis/{thesis.id}/analyze", json={})
        assert r.status_code == 500
        detail = r.json()["detail"]
        assert SECRET_RAISE not in detail
        assert "评审失败" in detail

    def test_llm_error_string_detail_sanitized(self, thesis_env, monkeypatch):
        """LLM 返回错误串 → 500 通用文案（现状是 200 且把错误串带进 suggestions）。"""
        client, thesis = thesis_env
        monkeypatch.setattr(thesis_router.llm_service, "chat_completion", _fake_chat_error_string)

        r = client.post(f"/api/thesis/{thesis.id}/analyze", json={})
        assert r.status_code == 500
        body = r.text
        assert SECRET_STRING not in body
        assert "评审失败" in r.json()["detail"]

    def test_short_chapter_still_400(self, thesis_env, monkeypatch):
        """不回归：章节文本过短仍为 400 原文案。"""
        client, thesis = thesis_env
        monkeypatch.setattr(
            thesis_router.DocxParser, "extract_chapter_text", lambda self, p, c: "过短"
        )
        r = client.post(f"/api/thesis/{thesis.id}/analyze", json={})
        assert r.status_code == 400
        assert r.json()["detail"] == "章节内容为空或过短，无法生成评审意见"

    def test_success_path_unchanged(self, thesis_env, monkeypatch):
        """不回归：LLM 成功时 200 返回评审意见。"""
        client, thesis = thesis_env
        monkeypatch.setattr(thesis_router.llm_service, "chat_completion", _fake_chat_ok)

        r = client.post(f"/api/thesis/{thesis.id}/analyze", json={})
        assert r.status_code == 200
        assert r.json()["suggestions"].startswith("## 评审/概括")
        assert r.json()["chapter_title"] == "第一章 绪论"

    def test_response_serialization_exception_sanitized(self, thesis_env, monkeypatch):
        """响应模型构造失败时也只返回通用文案。"""
        client, thesis = thesis_env
        monkeypatch.setattr(thesis_router.llm_service, "chat_completion", _fake_chat_ok)

        def fail_response(**kwargs):
            raise RuntimeError(SECRET_RAISE)

        monkeypatch.setattr(thesis_router, "ThesisAnalyzeResponse", fail_response)
        response = client.post(f"/api/thesis/{thesis.id}/analyze", json={})

        assert response.status_code == 500
        assert SECRET_RAISE not in response.text
        assert response.json()["detail"] == "响应序列化失败，请稍后再试"


class TestSuggestCitationsSanitize:
    """thesis /suggest-citations 的 LLM 失败脱敏。"""

    def test_llm_raise_detail_sanitized(self, thesis_env, monkeypatch):
        """chat_completion 抛异常 → 500 通用文案（现状靠全局兜底，文案不含路由语义）。"""
        client, thesis = thesis_env
        monkeypatch.setattr(thesis_router.llm_service, "chat_completion", _fake_chat_raise)

        r = client.post(
            f"/api/thesis/{thesis.id}/suggest-citations",
            json={"paragraph": "多实例学习在病理图像上的应用段落。"},
        )
        assert r.status_code == 500
        detail = r.json()["detail"]
        assert SECRET_RAISE not in detail
        assert "引用推荐失败" in detail

    def test_llm_error_string_detail_sanitized(self, thesis_env, monkeypatch):
        """LLM 返回错误串 → 500 通用文案（现状是 200 且把错误串带进 suggestions）。"""
        client, thesis = thesis_env
        monkeypatch.setattr(thesis_router.llm_service, "chat_completion", _fake_chat_error_string)

        r = client.post(
            f"/api/thesis/{thesis.id}/suggest-citations",
            json={"paragraph": "多实例学习在病理图像上的应用段落。"},
        )
        assert r.status_code == 500
        assert SECRET_STRING not in r.text
        assert "引用推荐失败" in r.json()["detail"]

    def test_zero_evidence_skips_llm_and_returns_local_notice(
        self, thesis_env, monkeypatch
    ):
        """零本地证据时不得让 LLM 猜测推荐文献。"""
        client, thesis = thesis_env
        completion = AsyncMock(return_value="不应生成")
        monkeypatch.setattr(thesis_router.llm_service, "chat_completion", completion)

        r = client.post(
            f"/api/thesis/{thesis.id}/suggest-citations",
            json={"paragraph": "  多实例学习在病理图像上的应用段落。  "},
        )
        assert r.status_code == 200
        data = r.json()
        assert "未找到" in data["suggestions"]
        assert data["citations"] == []
        assert "paragraph" not in data
        completion.assert_not_awaited()

    def test_healthy_retrieval_uses_shared_hybrid_candidate_pool(
        self, thesis_env, monkeypatch
    ):
        """引用推荐与聊天使用相同 hybrid top-10 语义候选池，最终仍返回 top-5。"""
        client, thesis = thesis_env
        calls = []
        chunk = {
            "chunk_id": "p8_c2",
            "paper_id": 8,
            "title": "本地证据",
            "authors": "测试作者",
            "year": 2024,
            "content": "targetanchor evidence",
            "page_number": 3,
            "chunk_type": "result",
            "score": 0.9,
            "source": "semantic",
        }

        class Store:
            def available(self):
                return True

            def search(self, **kwargs):
                calls.append(kwargs)
                return [chunk]

        monkeypatch.setattr(
            "app.services.retrieval.get_vector_store", lambda: Store()
        )
        monkeypatch.setattr(
            thesis_router.llm_service, "chat_completion", _fake_chat_ok
        )

        response = client.post(
            f"/api/thesis/{thesis.id}/suggest-citations",
            json={"paragraph": "targetanchor"},
        )

        assert response.status_code == 200
        assert calls == [{"query": "targetanchor", "top_k": 10, "filters": {}}]
        assert [item["chunk_id"] for item in response.json()["citations"]] == [
            "p8_c2"
        ]

    @pytest.mark.parametrize("paragraph", ["", "   \n\t"])
    def test_blank_paragraph_rejected(self, thesis_env, paragraph):
        client, thesis = thesis_env

        response = client.post(
            f"/api/thesis/{thesis.id}/suggest-citations",
            json={"paragraph": paragraph},
        )

        assert response.status_code == 422

    def test_paragraph_over_20000_characters_rejected(self, thesis_env):
        client, thesis = thesis_env

        response = client.post(
            f"/api/thesis/{thesis.id}/suggest-citations",
            json={"paragraph": "x" * 20001},
        )

        assert response.status_code == 422

    def test_paragraph_at_20000_characters_is_accepted(self, thesis_env, monkeypatch):
        client, thesis = thesis_env
        monkeypatch.setattr(thesis_router.llm_service, "chat_completion", _fake_chat_ok)

        response = client.post(
            f"/api/thesis/{thesis.id}/suggest-citations",
            json={"paragraph": "x" * 20000},
        )

        assert response.status_code == 200

    def test_query_string_paragraph_is_not_accepted(self, thesis_env):
        client, thesis = thesis_env

        response = client.post(
            f"/api/thesis/{thesis.id}/suggest-citations",
            params={"paragraph": "不应进入 URL"},
        )

        assert response.status_code == 422
