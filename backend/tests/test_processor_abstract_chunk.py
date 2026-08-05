"""B2：入库流水线摘要级 chunk（Phase B / T2）。

契约（specs/phases/phase-b-retrieval/spec.md §3.2）：
1. 入库处理后存在 chunk_type="abstract" 的 chunk；paper.abstract 非空时优先用之
2. abstract 为空时取首页文本前 1500 字符启发式
3. ChromaDB 中 chunk id 形如 p{paper_id}_abstract；重复入库先删后写不重复
4. 首页文本也为空 → 跳过摘要 chunk，不阻塞入库
"""

from unittest.mock import MagicMock

import pytest

from app.models import Paper, Chunk
from app.services import processor as processor_module
from app.services.processor import PaperProcessor


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
def paper_file(tmp_path, monkeypatch):
    """把 processor 模块的 project_root 重定向到 tmp_path，并放置一个假 PDF 文件。"""
    papers_dir = tmp_path / "papers"
    papers_dir.mkdir()
    (papers_dir / "test_abstract.pdf").write_bytes(b"%PDF-fake")
    # Path(__file__).resolve().parents[3] -> tmp_path
    monkeypatch.setattr(
        processor_module, "__file__", str(tmp_path / "a" / "b" / "c" / "processor.py")
    )
    return "papers/test_abstract.pdf"


def _make_paper(db, file_path, abstract=None):
    paper = Paper(
        title="测试论文",
        authors="张三",
        year=2024,
        abstract=abstract,
        file_path=file_path,
        filename="test_abstract.pdf",
    )
    db.add(paper)
    db.commit()
    return paper


def _abstract_chunks(db, paper_id):
    return (
        db.query(Chunk)
        .filter(Chunk.paper_id == paper_id, Chunk.chunk_type == "abstract")
        .all()
    )


def test_abstract_chunk_created_from_abstract_field(db, paper_file):
    """abstract 字段非空 → 生成 chunk_type=abstract 的 chunk，内容取自 abstract。"""
    paper = _make_paper(db, paper_file, abstract="本研究提出了一种结直肠癌分期方法。")
    pages = [{"page_number": 1, "text": "首页正文文本", "width": 612, "height": 792}]
    proc = _make_processor(pages)

    result = proc.process(paper, db)

    assert result["status"] == "ok"
    abstracts = _abstract_chunks(db, paper.id)
    assert len(abstracts) == 1
    assert abstracts[0].content == "本研究提出了一种结直肠癌分期方法。"
    # ChromaDB 写入的 chunk 列表中也包含摘要 chunk，id 对齐不变式 p{paper_id}_c-1
    add_call = proc.vector_store.add_chunks.call_args
    assert add_call is not None
    written = add_call.args[1] if add_call.args else add_call.kwargs["chunks"]
    abstract_entries = [c for c in written if c.get("chunk_type") == "abstract"]
    assert len(abstract_entries) == 1
    assert abstract_entries[0]["id"] == f"p{paper.id}_c-1"
    assert abstract_entries[0]["chunk_index"] == -1


def test_add_chunks_honors_explicit_chunk_id():
    """VectorStore.add_chunks 遇带 id/chunk_index 的 chunk 时两者都用其值（摘要 chunk 不变式契约）。"""
    from app.services.retrieval import VectorStore

    store = VectorStore.__new__(VectorStore)
    store.collection = MagicMock()
    store.embedding_service = MagicMock()
    store.embedding_service.embed.return_value = [[0.1, 0.2, 0.3]]

    store.add_chunks(
        7,
        [{"id": "p7_c-1", "content": "摘要内容", "chunk_type": "abstract", "chunk_index": -1, "page_number": 1}],
        {"title": "t", "authors": "a", "year": 2024},
    )

    add_kwargs = store.collection.add.call_args.kwargs
    assert add_kwargs["ids"] == ["p7_c-1"]
    assert add_kwargs["metadatas"][0]["chunk_type"] == "abstract"
    assert add_kwargs["metadatas"][0]["chunk_index"] == -1


def test_abstract_chunk_falls_back_to_first_page_1500_chars(db, paper_file):
    """abstract 为空 → 取首页文本前 1500 字符作为摘要 chunk。"""
    paper = _make_paper(db, paper_file, abstract=None)
    first_page_text = "摘" * 2000  # 超过 1500 字符
    pages = [
        {"page_number": 1, "text": first_page_text, "width": 612, "height": 792},
        {"page_number": 2, "text": "第二页内容", "width": 612, "height": 792},
    ]
    proc = _make_processor(pages)

    result = proc.process(paper, db)

    assert result["status"] == "ok"
    abstracts = _abstract_chunks(db, paper.id)
    assert len(abstracts) == 1
    assert abstracts[0].content == "摘" * 1500
    assert abstracts[0].page_number == 1


def test_empty_abstract_and_empty_first_page_skips_abstract_chunk(db, paper_file):
    """abstract 与首页文本均为空 → 跳过摘要 chunk，入库照常完成（不阻塞）。"""
    paper = _make_paper(db, paper_file, abstract="  ")
    pages = [{"page_number": 1, "text": "", "width": 612, "height": 792}]
    proc = _make_processor(pages)

    result = proc.process(paper, db)

    assert result["status"] == "ok"
    assert result["chunks"] == 1  # 仅段落 chunk
    assert _abstract_chunks(db, paper.id) == []


def test_reprocess_no_duplicate_abstract_chunks(db, paper_file):
    """重复入库：先删后写，abstract chunk 不重复。"""
    paper = _make_paper(db, paper_file, abstract="摘要内容")
    pages = [{"page_number": 1, "text": "首页文本", "width": 612, "height": 792}]
    proc = _make_processor(pages)

    proc.process(paper, db)
    proc.process(paper, db)

    assert len(_abstract_chunks(db, paper.id)) == 1
    # 每次处理都先删该 paper 的全部向量（含旧摘要 chunk）再写
    assert proc.vector_store.delete_by_paper_id.call_count == 2
    assert proc.vector_store.add_chunks.call_count == 2
