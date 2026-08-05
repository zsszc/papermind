"""Batch 12：IDF/BM25 词法检索观察实验的纯离线契约。"""

from app.models import Chunk, Paper
from eval.run import _bm25_chunk_search, _tokenize_technical_terms


def _add_paper_with_chunks(db, contents):
    db.add(Paper(
        id=1,
        title="词法测试论文",
        filename="lexical.pdf",
        file_path="papers/lexical.pdf",
    ))
    db.commit()
    db.add_all([
        Chunk(paper_id=1, chunk_index=index, content=content)
        for index, content in enumerate(contents)
    ])
    db.commit()


def test_tokenizer_preserves_academic_technical_anchors():
    query = "ReCo-MIL 与 F1-score，学习率 1e-4，准确率 87.3%"
    assert _tokenize_technical_terms(query) == [
        "reco-mil",
        "f1-score",
        "1e-4",
        "87.3%",
    ]


def test_bm25_rare_term_outranks_repeated_common_term(db):
    _add_paper_with_chunks(db, [
        "MIL MIL MIL common baseline",
        "rareanchor precise evidence",
        "common background material",
    ])

    results = _bm25_chunk_search(db, "common rareanchor", limit=3)

    assert results[0]["chunk_id"] == "p1_c1"
    assert results[0]["score"] > results[1]["score"]


def test_bm25_length_normalization_prefers_concise_evidence(db):
    _add_paper_with_chunks(db, [
        "CAFR " * 4 + "background " * 200,
        "CAFR precise evidence",
    ])

    results = _bm25_chunk_search(db, "CAFR", limit=2)

    assert [item["chunk_id"] for item in results] == ["p1_c1", "p1_c0"]


def test_bm25_pure_chinese_query_degrades_to_empty_without_guessing(db):
    _add_paper_with_chunks(db, ["English technical evidence"])
    assert _bm25_chunk_search(db, "纯中文问题", limit=5) == []
