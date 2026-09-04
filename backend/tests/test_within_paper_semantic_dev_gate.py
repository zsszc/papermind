"""Batch 32：一次性 dev 配对 Gate。"""

from eval.run import within_paper_semantic_rerank_contract_metadata
from eval.within_paper_semantic_dev_gate import evaluate_dev
from eval.paper_first_train_gate import _report_sha256


def _report(profile, *, p95=900.0, gain=0.0):
    candidate = profile != "hybrid"
    contract = within_paper_semantic_rerank_contract_metadata()
    report = {
        "report_schema": "2.0", "run": {"git_sha": "a" * 40, "git_tracked_clean": True},
        "benchmark": {"dataset_sha256": "1" * 64, "qrels_sha256": "2" * 64,
                      "corpus_manifest_sha256": "3" * 64, "database_logical_manifest_sha256": "3" * 64,
                      "page_text_manifest_sha256": "4" * 64, "vector_manifest_sha256": "5" * 64,
                      "hnsw_config_sha256": "6" * 64, "hnsw_binary_manifest_sha256": "7" * 64},
        "pipeline": {"profile": profile, "effective_profile": profile, "lexical_profile": "bm25-bilingual",
                     "semantic_rerank": None, "split": "dev", "evidence_resolver": "page-span-v2", "top_k": 5},
        "diagnostics": {"runtime_degraded_count": 0},
        "overall": {"n_positive": 12, "n_negative": 0, "recall@5": 0.5 + gain, "mrr": 0.4 + gain,
                    "ndcg@5": 0.4 + gain, "span_coverage@5": 0.5 + gain},
        "by_question_type": [
            {"question_type": kind, "n": count, "recall": 0.5 + gain, "span_coverage": 0.5 + gain}
            for kind, count in (("factoid", 6), ("method_detail", 3), ("summary", 3))],
        "latency": {"count": 12, "p95": p95},
    }
    if candidate:
        report["benchmark"]["within_paper_semantic_formula_sha256"] = contract["formula_sha256"]
        report["pipeline"]["within_paper_semantic"] = contract
    return report


def _inputs(*, p95=900.0):
    contract = within_paper_semantic_rerank_contract_metadata()
    train = {"schema": "within-paper-semantic-train-gate-v1", "passed": True}
    train_sha = _report_sha256(train)
    base, cand = _report("hybrid"), _report("within-paper-semantic-rerank-v1", p95=p95, gain=0.1)
    cand["benchmark"]["semantic_dev_train_gate_sha256"] = train_sha
    claim = {"schema": "within-paper-semantic-dev-claim-v1", "train_gate_sha256": train_sha,
             "git_sha": "a" * 40, "formula_sha256": contract["formula_sha256"]}
    return base, cand, train, claim


def test_dev_gate_passes_quality_non_regression_and_latency():
    result = evaluate_dev(*_inputs())
    assert result["passed"] is True
    assert "qa_id" not in str(result)


def test_dev_gate_rejects_absolute_p95_even_when_quality_improves():
    result = evaluate_dev(*_inputs(p95=1081.0))
    assert result["passed"] is False
    assert result["checks"]["candidate_p95_ms"]["passed"] is False
