"""Batch 12：IDF/BM25 词法检索观察实验的纯离线契约。"""

from app.models import Chunk, Paper
from eval.run import (
    _bm25_chunk_search,
    _keyword_chunk_search,
    _query_technical_terms,
    _rerank_bm25_neighbors,
    _tokenize_technical_terms,
    build_parser,
)


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


def test_bilingual_profile_expands_only_explicit_domain_terms():
    query = "PAMIL 如何利用原型提升 WSI 分类推理的可解释性？"

    assert _query_technical_terms(query, bilingual=False) == ["pamil", "wsi"]
    expanded = _query_technical_terms(query, bilingual=True)
    assert expanded[:2] == ["pamil", "wsi"]
    assert {
        "prototype", "classification", "inference", "interpretability"
    }.issubset(expanded)


def test_bilingual_bm25_can_retrieve_english_evidence_for_chinese_query(db):
    _add_paper_with_chunks(db, [
        "unrelated background",
        "prototype classification provides interpretable inference",
    ])

    assert _bm25_chunk_search(db, "原型分类的可解释推理", limit=2) == []
    results = _bm25_chunk_search(
        db, "原型分类的可解释推理", limit=2, bilingual=True
    )
    assert results[0]["chunk_id"] == "p1_c1"


def test_neighbor_profile_uses_same_paper_adjacent_scores_only():
    scored = [
        {"chunk_id": "p1_c-1", "paper_id": 1, "score": 80.0},
        {"chunk_id": "p1_c0", "paper_id": 1, "score": 2.0},
        {"chunk_id": "p1_c1", "paper_id": 1, "score": 4.0},
        {"chunk_id": "p2_c0", "paper_id": 2, "score": 100.0},
    ]

    results = _rerank_bm25_neighbors(scored)
    by_id = {item["chunk_id"]: item for item in results}

    # 严格按同论文 chunk_index±1 累加，不从其他论文借分。
    assert by_id["p1_c0"]["score"] == 2.0 + 0.5 * (80.0 + 4.0)
    assert by_id["p1_c1"]["score"] == 4.0 + 0.5 * 2.0
    assert by_id["p1_c-1"]["score"] == 80.0 + 0.5 * 2.0
    assert by_id["p2_c0"]["score"] == 100.0


def test_neighbor_profile_uses_chunk_id_as_deterministic_tie_break():
    scored = [
        {"chunk_id": "p2_c0", "paper_id": 2, "score": 1.0},
        {"chunk_id": "p1_c0", "paper_id": 1, "score": 1.0},
    ]

    results = _rerank_bm25_neighbors(scored)

    assert [item["chunk_id"] for item in results] == ["p1_c0", "p2_c0"]


def test_neighbor_profile_can_promote_contiguous_evidence_into_top_k():
    scored = [
        {"chunk_id": "p1_c0", "paper_id": 1, "score": 8.0},
        {"chunk_id": "p2_c0", "paper_id": 2, "score": 4.0},
        {"chunk_id": "p1_c1", "paper_id": 1, "score": 1.0},
    ]

    baseline_top_two = [item["chunk_id"] for item in scored[:2]]
    neighbor_top_two = [
        item["chunk_id"] for item in _rerank_bm25_neighbors(scored)[:2]
    ]

    assert "p1_c1" not in baseline_top_two
    assert "p1_c1" in neighbor_top_two


def test_neighbor_profile_is_opt_in_and_keeps_old_profile_unchanged(db):
    _add_paper_with_chunks(db, [
        "prototype prototype prototype",
        "prototype classification evidence",
        "classification",
    ])

    baseline = _keyword_chunk_search(
        db, "原型分类", limit=3, lexical_profile="bm25-bilingual"
    )
    neighbor = _keyword_chunk_search(
        db,
        "原型分类",
        limit=3,
        lexical_profile="bm25-bilingual-neighbor",
    )

    assert baseline == _bm25_chunk_search(
        db, "原型分类", limit=3, bilingual=True
    )
    assert neighbor != baseline
    assert {item["source"] for item in neighbor} == {
        "keyword-bm25-bilingual-neighbor"
    }


def test_parser_accepts_neighbor_profile():
    args = build_parser().parse_args([
        "--keyword-only",
        "--lexical-profile",
        "bm25-bilingual-neighbor",
    ])

    assert args.lexical_profile == "bm25-bilingual-neighbor"
