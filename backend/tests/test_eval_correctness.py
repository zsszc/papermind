"""Batch 12：RAG 评测数学正确性与报告诊断契约。"""

import hashlib

import pytest

from app.models import Chunk, Paper
from eval.metrics import (
    citation_f1,
    citation_precision,
    citation_recall,
    ndcg_at_k,
)
from eval.run import (
    _build_benchmark_metadata,
    _extract_citations,
    _resolve_qrels_or_raise,
)


class TestCitationExtraction:
    def test_supports_abstract_chunk_and_preserves_order(self):
        answer = "摘要见 [p1_c-1]，正文见 [p1_c2]，再次引用 [p1_c-1]。"
        assert _extract_citations(answer) == ["p1_c-1", "p1_c2"]

    def test_ignores_bare_chunk_ids(self):
        answer = "内部编号 p1_c2 不是引用，只有 [p2_c3] 是引用。"
        assert _extract_citations(answer) == ["p2_c3"]


class TestMetricCorrectness:
    def test_ndcg_deduplicates_retrieved_ids(self):
        score = ndcg_at_k(["a", "a"], ["a"], 2)
        assert score == pytest.approx(1.0)
        assert 0.0 <= score <= 1.0

    def test_citation_precision_recall_f1(self):
        citations = ["good", "bad"]
        relevant = ["good"]
        assert citation_precision(citations, relevant) == pytest.approx(0.5)
        assert citation_recall(citations, relevant) == pytest.approx(1.0)
        assert citation_f1(citations, relevant) == pytest.approx(2 / 3)

    @pytest.mark.parametrize(
        ("citations", "relevant"),
        [([], []), ([], ["good"]), (["bad"], [])],
    )
    def test_citation_metrics_empty_boundaries(self, citations, relevant):
        assert citation_precision(citations, relevant) == 0.0
        assert citation_recall(citations, relevant) == 0.0
        assert citation_f1(citations, relevant) == 0.0


class TestEvaluationPreflightAndFingerprint:
    @staticmethod
    def _entry(*, keywords):
        return {
            "qa_id": "qa-1",
            "question": "问题",
            "ground_truth": "答案",
            "relevant_chunks": [{"paper_id": 1, "keywords": keywords}],
            "question_type": "factoid",
            "source": "synthetic",
            "has_answer": True,
        }

    def test_positive_with_unresolved_qrels_fails_before_scoring(self, db):
        with pytest.raises(ValueError, match="qa-1"):
            _resolve_qrels_or_raise(db, [self._entry(keywords=["不存在"])])

    def test_fingerprint_is_stable_and_contains_no_corpus_text(self, db, tmp_path):
        db.add(Paper(
            id=1,
            title="论文一",
            filename="one.pdf",
            file_path="papers/one.pdf",
        ))
        db.commit()
        secret_content = "不得写入报告的论文正文"
        db.add(Chunk(paper_id=1, chunk_index=0, content=secret_content))
        db.commit()
        dataset_path = tmp_path / "qa.jsonl"
        dataset_path.write_text('{"qa_id":"qa-1"}\n', encoding="utf-8")

        first = _build_benchmark_metadata(db, dataset_path)
        second = _build_benchmark_metadata(db, dataset_path)

        assert first == second
        assert first["dataset_sha256"] == hashlib.sha256(
            dataset_path.read_bytes()).hexdigest()
        assert first["n_papers"] == 1
        assert first["n_chunks"] == 1
        assert len(first["corpus_manifest_sha256"]) == 64
        assert secret_content not in repr(first)
