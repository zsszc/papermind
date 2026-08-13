"""Batch 13：公开评测 fixture 与稳定 evidence qrels 契约。"""

import json

import pytest

from app.models import Chunk, Paper
from eval.dataset import resolve_relevant_chunks, validate_dataset
from eval.fixture import load_fixture, open_fixture_database


PUBLIC_FIXTURE = "eval/fixtures/rag_public_v1.json"
PUBLIC_DATASET = "eval/dataset/qa_public_v1.jsonl"


def test_public_fixture_seeds_isolated_memory_database():
    fixture = load_fixture(PUBLIC_FIXTURE)
    database = open_fixture_database(PUBLIC_FIXTURE)
    try:
        db = database.session_factory()
        assert str(database.engine.url) == "sqlite:///:memory:"
        assert db.query(Paper).count() == len(fixture["papers"])
        assert db.query(Chunk).count() == sum(
            len(paper["chunks"]) for paper in fixture["papers"]
        )
        assert {paper.doi for paper in db.query(Paper).all()} == {
            paper["paper_uid"].removeprefix("doi:")
            for paper in fixture["papers"]
        }
    finally:
        db.close()
        database.close()


def test_fixture_rejects_duplicate_stable_paper_uid(tmp_path):
    fixture = {
        "benchmark_id": "duplicate-test",
        "license": "CC0-1.0",
        "papers": [
            {
                "paper_uid": "doi:10.5555/duplicate",
                "title": f"论文 {index}",
                "chunks": [{
                    "chunk_index": 0,
                    "content": "这是一段长度超过二十个字符的原创合成测试正文内容。",
                }],
            }
            for index in range(2)
        ],
    }
    path = tmp_path / "duplicate.json"
    path.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="paper_uid 重复"):
        load_fixture(path)


def _evidence_item(**overrides):
    item = {
        "qa_id": "evidence-001",
        "question": "Alpha-MIL 使用什么特征提取器？",
        "ground_truth": "使用预训练 ResNet-34。",
        "relevant_evidence": [{
            "paper_uid": "doi:10.5555/papermind.alpha-mil",
            "quote": (
                "Alpha-MIL encodes every tile with a pretrained ResNet-34 "
                "feature extractor."
            ),
        }],
        "question_type": "factoid",
        "source": "synthetic",
        "has_answer": True,
    }
    item.update(overrides)
    return item


def test_evidence_dataset_validates_without_legacy_relevant_chunks():
    validate_dataset([_evidence_item()])


def test_evidence_quote_must_be_long_enough():
    item = _evidence_item(relevant_evidence=[{
        "paper_uid": "doi:10.5555/papermind.alpha-mil",
        "quote": "too short",
    }])
    with pytest.raises(ValueError, match="至少 20"):
        validate_dataset([item])


def test_evidence_resolves_by_doi_and_unique_quote():
    database = open_fixture_database(PUBLIC_FIXTURE)
    db = database.session_factory()
    try:
        assert resolve_relevant_chunks(db, _evidence_item()) == ["p1_c0"]
    finally:
        db.close()
        database.close()


def test_evidence_zero_match_is_a_label_error():
    database = open_fixture_database(PUBLIC_FIXTURE)
    db = database.session_factory()
    try:
        item = _evidence_item(relevant_evidence=[{
            "paper_uid": "doi:10.5555/papermind.alpha-mil",
            "quote": "This evidence sentence deliberately does not exist anywhere.",
        }])
        with pytest.raises(ValueError, match="未命中"):
            resolve_relevant_chunks(db, item)
    finally:
        db.close()
        database.close()


def test_evidence_multiple_matches_is_a_label_error():
    database = open_fixture_database(PUBLIC_FIXTURE)
    db = database.session_factory()
    try:
        paper = db.query(Paper).filter(Paper.doi == "10.5555/papermind.alpha-mil").one()
        duplicated = "This deliberately duplicated evidence sentence is long enough."
        db.add_all([
            Chunk(paper_id=paper.id, chunk_index=98, content=duplicated),
            Chunk(paper_id=paper.id, chunk_index=99, content=duplicated),
        ])
        db.commit()
        item = _evidence_item(relevant_evidence=[{
            "paper_uid": "doi:10.5555/papermind.alpha-mil",
            "quote": duplicated,
        }])
        with pytest.raises(ValueError, match="多处命中"):
            resolve_relevant_chunks(db, item)
    finally:
        db.close()
        database.close()


def test_public_dataset_is_valid_and_covers_required_types():
    from eval.dataset import load_dataset

    items = load_dataset(PUBLIC_DATASET)
    validate_dataset(items)
    positive_types = {item["question_type"] for item in items if item["has_answer"]}
    assert positive_types == {
        "factoid", "summary", "comparison", "method_detail", "experiment_data"
    }
    assert sum(not item["has_answer"] for item in items) >= 2
