"""G1：参考文献解析与引用边（Phase G / G1）。

契约（specs/phases/phase-g-graphrag/spec.md §3.1）：
1. 新表 paper_citations（citing_id/cited_id FK papers.id + 唯一约束）；
   ensure_schema() 加 CREATE TABLE IF NOT EXISTS 迁移分支，幂等
2. References/参考文献 段启发式定位（全文最后一个独立标题行），
   按编号条目切分（[n] 与 n. 两种主流格式）
3. 标题候选提取：引号内文本优先，否则年份前的最长段
4. difflib.SequenceMatcher 相似度 ≥ 0.85 建边；自引跳过；重复边去重
5. processor 流水线尾部接入：解析异常仅记 [references] warning，不影响入库主流程
"""

import logging
from difflib import SequenceMatcher
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool

from app.models import Chunk, Paper, PaperCitation
from app.services import processor as processor_module
from app.services.processor import PaperProcessor
from app.services.reference_parser import (
    MATCH_THRESHOLD,
    best_match,
    extract_references_section,
    extract_title_candidate,
    rebuild_citation_edges,
    split_entries,
)


# ---------- 共用辅助 ----------

def _make_paper(db, title, file_path="papers/ref_lib.pdf", filename="ref_lib.pdf"):
    paper = Paper(
        title=title,
        authors="作者",
        year=2020,
        file_path=file_path,
        filename=filename,
    )
    db.add(paper)
    db.commit()
    return paper


def _edges_of(db, citing_id):
    return (
        db.query(PaperCitation)
        .filter(PaperCitation.citing_id == citing_id)
        .all()
    )


# ---------- 1. 表结构与 ensure_schema 迁移分支 ----------

def test_paper_citations_columns_and_unique_constraint(db):
    """paper_citations 表：可建边；重复 (citing_id, cited_id) 触发唯一约束。"""
    a = _make_paper(db, "论文甲", file_path="papers/a.pdf", filename="a.pdf")
    b = _make_paper(db, "论文乙", file_path="papers/b.pdf", filename="b.pdf")

    db.add(PaperCitation(citing_id=a.id, cited_id=b.id))
    db.commit()
    edge = db.query(PaperCitation).one()
    assert edge.citing_id == a.id
    assert edge.cited_id == b.id
    assert edge.created_at is not None

    db.add(PaperCitation(citing_id=a.id, cited_id=b.id))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_ensure_schema_paper_citations_branch_creates_table_on_old_db():
    """迁移分支：旧库（无 paper_citations 表）执行后建表，且列齐全。"""
    from app.database import ensure_paper_citations_table

    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # 模拟旧库：仅有 papers 表，无 paper_citations
    Paper.__table__.create(bind=eng)

    ensure_paper_citations_table(eng)

    with eng.connect() as conn:
        cols = {
            row[1]
            for row in conn.exec_driver_sql("PRAGMA table_info(paper_citations)").fetchall()
        }
    assert {"id", "citing_id", "cited_id", "created_at"} <= cols


def test_ensure_schema_paper_citations_branch_idempotent():
    """迁移分支幂等：二次执行不报错（CREATE TABLE IF NOT EXISTS 风格）。"""
    from app.database import ensure_paper_citations_table

    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Paper.__table__.create(bind=eng)

    ensure_paper_citations_table(eng)
    ensure_paper_citations_table(eng)  # 二次执行不得抛异常

    with eng.connect() as conn:
        tables = {
            row[0]
            for row in conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert "paper_citations" in tables


def test_ensure_schema_branch_noop_when_orm_already_created(db):
    """新库 create_all 已建表时，迁移分支为 no-op，不报错、不破坏既有数据。"""
    from app.database import ensure_paper_citations_table

    a = _make_paper(db, "论文甲", file_path="papers/a.pdf", filename="a.pdf")
    b = _make_paper(db, "论文乙", file_path="papers/b.pdf", filename="b.pdf")
    db.add(PaperCitation(citing_id=a.id, cited_id=b.id))
    db.commit()

    ensure_paper_citations_table(db.get_bind())

    assert db.query(PaperCitation).count() == 1


# ---------- 2. References 段定位 ----------

def test_extract_references_section_locates_last_standalone_heading():
    """定位全文最后一个独立标题行；正文中的 References 字样不算。"""
    text = (
        "References are important in academic writing.\n"
        "更多正文内容。\n"
        "\n"
        "References\n"
        "\n"
        "[1] 某文献条目"
    )
    section = extract_references_section(text)
    assert section is not None
    assert section.startswith("[1] 某文献条目")


def test_extract_references_section_chinese_heading():
    """中文「参考文献」独立标题行同样可定位。"""
    section = extract_references_section("正文。\n\n参考文献\n\n[1] 某文献")
    assert section == "[1] 某文献"


def test_extract_references_section_missing_returns_none():
    """无参考文献段 → None。"""
    assert extract_references_section("正文没有参考文献段。\n更多正文。") is None


# ---------- 3. 条目切分 ----------

def test_split_entries_bracket_format_with_line_wrap():
    """[n] 格式切分；换行续接的条目合并；段前杂行忽略。"""
    section = (
        "页眉残留杂行\n"
        '[1] A. Smith, J. Doe, "Deep residual learning for image recognition,"\n'
        "IEEE Trans. Pattern Anal., vol. 38, 2016.\n"
        "[2] B. Lee et al., Attention is all you need, NeurIPS, 2017.\n"
        "[3] C. Wang. Vision transformers for dense prediction. ICCV, 2021."
    )
    entries = split_entries(section)
    assert len(entries) == 3
    assert entries[0].startswith("A. Smith")
    assert "2016" in entries[0]  # 续行已合并进首条
    assert entries[2].startswith("C. Wang")


def test_split_entries_dot_format():
    """n. 格式切分。"""
    section = (
        "1. A. Smith. Deep residual learning for image recognition. TPAMI, 2016.\n"
        "2. B. Lee. Attention is all you need. NeurIPS, 2017."
    )
    entries = split_entries(section)
    assert len(entries) == 2
    assert entries[0].startswith("A. Smith")
    assert entries[1].startswith("B. Lee")


# ---------- 4. 标题候选提取 ----------

def test_extract_title_candidate_prefers_quoted_text():
    """引号内文本优先作为标题候选（尾随标点清洗）。"""
    entry = (
        '[1] A. Smith, J. Doe, "Deep residual learning for image recognition," '
        "TPAMI, vol. 38, 2016."
    )
    assert extract_title_candidate(entry) == "Deep residual learning for image recognition"


def test_extract_title_candidate_chinese_curly_quotes():
    """中文弯引号同样识别。"""
    entry = "[2] J. Devlin 等，“BERT：语言理解的深度双向预训练”，NAACL，2019。"
    assert extract_title_candidate(entry) == "BERT：语言理解的深度双向预训练"


def test_extract_title_candidate_unquoted_longest_segment_before_year():
    """无引号 → 取年份前的最长段。"""
    entry = "B. Lee et al., Attention is all you need, NeurIPS, 2017."
    assert extract_title_candidate(entry) == "Attention is all you need"


def test_extract_title_candidate_too_short_returns_none():
    """过短片段不可能是标题 → None。"""
    assert extract_title_candidate("Anon., Nature, 2020.") is None


# ---------- 5. 匹配阈值边界 ----------

def test_best_match_exact_title_matches():
    """规范化后完全一致的标题 → 命中。"""
    choices = [(1, "Deep Residual Learning for Image Recognition")]
    hit = best_match("Deep residual learning for image recognition", choices)
    assert hit is not None
    assert hit[0] == 1


def test_best_match_above_threshold_slight_variation():
    """轻微变体（样本相似度 0.88 ≥ 0.85）→ 命中（阈值上边界）。"""
    choices = [(1, "Deep Residual Learning for Image Recognition")]
    candidate = "Deep residual learning for large scale image recognition"
    ratio = SequenceMatcher(None, candidate.lower(), choices[0][1].lower()).ratio()
    assert MATCH_THRESHOLD <= ratio < 0.95  # 守卫：确为近阈值样本
    hit = best_match(candidate, choices)
    assert hit is not None and hit[0] == 1


def test_best_match_below_threshold_returns_none():
    """相似标题但相似度 0.766 < 0.85 → 不命中（阈值下边界）。"""
    choices = [(1, "Deep Residual Learning for Image Recognition")]
    candidate = "Deep residual learning for visual recognition and understanding"
    ratio = SequenceMatcher(None, candidate.lower(), choices[0][1].lower()).ratio()
    assert 0.7 < ratio < MATCH_THRESHOLD  # 守卫：确为近阈值样本
    assert best_match(candidate, choices) is None


# ---------- 6. 建边（DB 集成） ----------

_CITING_TEXT = (
    "正文内容。\n"
    "\n"
    "References\n"
    "\n"
    '[1] K. He et al., "Deep residual learning for image recognition," CVPR, 2016.\n'
    "[2] K. He et al., Deep residual learning for image recognition, TPAMI, 2016.\n"
    "[3] J. Smith, Totally unrelated work on protein folding, Nature, 2020."
)


def test_rebuild_citation_edges_builds_dedupes_and_skips_unmatched(db):
    """建边：模糊命中建边；同一边多条目去重；未命中条目不建边。"""
    cited = _make_paper(db, "Deep Residual Learning for Image Recognition")
    citing = _make_paper(db, "Some citing paper", file_path="papers/y.pdf", filename="y.pdf")

    n = rebuild_citation_edges(db, citing.id, _CITING_TEXT)

    assert n == 1
    edges = _edges_of(db, citing.id)
    assert len(edges) == 1
    assert edges[0].cited_id == cited.id


def test_rebuild_citation_edges_clears_out_edges_before_rebuild(db):
    """幂等：先清该 paper 的出边再重建——二次解析无参考文献段时旧边被清除。"""
    cited = _make_paper(db, "Deep Residual Learning for Image Recognition")
    citing = _make_paper(db, "Some citing paper", file_path="papers/y.pdf", filename="y.pdf")

    assert rebuild_citation_edges(db, citing.id, _CITING_TEXT) == 1
    assert rebuild_citation_edges(db, citing.id, _CITING_TEXT) == 1  # 重建不重复
    assert len(_edges_of(db, citing.id)) == 1

    # 二次解析文本无参考文献段 → 出边清零
    assert rebuild_citation_edges(db, citing.id, "正文，没有参考文献段。") == 0
    assert _edges_of(db, citing.id) == []


def test_rebuild_citation_edges_skips_self_citation(db):
    """自引跳过：条目标题与自身标题一致时不建边。"""
    paper = _make_paper(db, "Deep Residual Learning for Image Recognition")
    text = (
        "正文。\n\nReferences\n\n"
        "[1] K. He et al., Deep residual learning for image recognition, CVPR, 2016."
    )
    assert rebuild_citation_edges(db, paper.id, text) == 0
    assert db.query(PaperCitation).count() == 0


# ---------- 7. processor 流水线接入 ----------

class _FakeParser:
    """离线桩：不触碰真实 PDF，返回预置分页文本。"""

    def __init__(self, pages):
        self._pages = pages

    def extract_text(self, file_path):
        return self._pages


class _FakeChunker:
    """离线桩：固定返回一个段落 chunk，模拟分块完成。"""

    def chunk_pages(self, pages):
        return [
            {
                "content": "正文段落 chunk",
                "page_number": 1,
                "section_title": None,
                "chunk_type": "paragraph",
                "token_count": 5,
            }
        ]


def _make_processor(pages):
    """绕过重量级 __init__（真实 ChromaDB/Embedding），补齐 process() 触及的全部属性。"""
    proc = PaperProcessor.__new__(PaperProcessor)
    proc.parser = _FakeParser(pages)
    proc.chunker = _FakeChunker()
    proc.vector_store = MagicMock()
    return proc


@pytest.fixture()
def paper_pdf(tmp_path, monkeypatch):
    """把 processor 模块的 project_root 重定向到 tmp_path，并放置一个假 PDF 文件。"""
    papers_dir = tmp_path / "papers"
    papers_dir.mkdir()
    (papers_dir / "ref_test.pdf").write_bytes(b"%PDF-fake")
    # Path(__file__).resolve().parents[3] -> tmp_path
    monkeypatch.setattr(
        processor_module, "__file__", str(tmp_path / "a" / "b" / "c" / "processor.py")
    )
    return "papers/ref_test.pdf"


def test_processor_tail_builds_citation_edges(db, paper_pdf):
    """流水线尾部接入：入库后参考文献命中库内标题 → 建边。"""
    cited = _make_paper(
        db,
        "Deep Residual Learning for Image Recognition",
        file_path="papers/cited.pdf",
        filename="cited.pdf",
    )
    citing = _make_paper(db, "某引用论文", file_path=paper_pdf, filename="ref_test.pdf")
    pages = [
        {
            "page_number": 1,
            "text": (
                "正文。\n\nReferences\n\n"
                "[1] K. He et al., Deep residual learning for image recognition, CVPR, 2016."
            ),
            "width": 612,
            "height": 792,
        }
    ]
    proc = _make_processor(pages)

    result = proc.process(citing, db)

    assert result["status"] == "ok"
    edges = _edges_of(db, citing.id)
    assert len(edges) == 1
    assert edges[0].cited_id == cited.id


def test_processor_reference_failure_isolated(db, paper_pdf, monkeypatch, caplog):
    """失败隔离：引用解析抛异常仅记 [references] warning，入库主流程不受影响。"""

    def _boom(*args, **kwargs):
        raise RuntimeError("解析爆炸")

    monkeypatch.setattr(processor_module, "rebuild_citation_edges", _boom)

    paper = _make_paper(db, "某论文", file_path=paper_pdf, filename="ref_test.pdf")
    pages = [{"page_number": 1, "text": "正文文本", "width": 612, "height": 792}]
    proc = _make_processor(pages)

    with caplog.at_level(logging.WARNING):
        result = proc.process(paper, db)

    assert result["status"] == "ok"
    # 主流程产物不受影响：段落 chunk + 摘要 chunk 均已入库
    assert db.query(Chunk).filter(Chunk.paper_id == paper.id).count() >= 1
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("[references]" in r.getMessage() for r in warnings)
