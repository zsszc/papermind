"""Batch 32 Gate 的公开合成报告夹具。"""

from eval.run import within_paper_semantic_rerank_contract_metadata


def report(profile: str, *, coverage: float = 0.46) -> dict:
    candidate = profile == "within-paper-semantic-rerank-v1"
    contract = within_paper_semantic_rerank_contract_metadata()
    return {
        "report_schema": "2.0",
        "run": {"git_sha": "a" * 40, "git_tracked_clean": True},
        "benchmark": {
            "dataset_sha256": "1" * 64, "qrels_sha256": "2" * 64,
            "corpus_manifest_sha256": "3" * 64,
            "database_logical_manifest_sha256": "3" * 64,
            "page_text_manifest_sha256": "4" * 64,
            "vector_manifest_sha256": "5" * 64,
            "hnsw_config_sha256": "6" * 64,
            "hnsw_binary_manifest_sha256": "7" * 64,
            **({"within_paper_semantic_formula_sha256": contract["formula_sha256"]}
               if candidate else {}),
        },
        "pipeline": {
            "profile": profile, "effective_profile": profile,
            "lexical_profile": "bm25-bilingual", "semantic_rerank": None,
            "split": "train", "evidence_resolver": "page-span-v2", "top_k": 5,
            **({"within_paper_semantic": contract} if candidate else {}),
        },
        "diagnostics": {"runtime_degraded_count": 0}, "with_llm": False,
        "overall": {"n_positive": 13, "n_negative": 0, "recall@5": 0.4,
                    "mrr": 0.3, "ndcg@5": 0.3, "span_coverage@5": coverage},
        "by_question_type": [
            {"question_type": "factoid", "n": 8, "recall": 0.4, "mrr": 0.3,
             "ndcg": 0.3, "span_coverage": coverage},
            {"question_type": "method_detail", "n": 4, "recall": 0.4, "mrr": 0.3,
             "ndcg": 0.3, "span_coverage": coverage},
            {"question_type": "summary", "n": 1, "recall": 0.0, "mrr": 0.0,
             "ndcg": 0.0, "span_coverage": 0.0},
        ],
        "latency": {"p95": 900.0, "count": 13},
        "items": [{"qa_id": f"private-{index}", "degraded": False}
                  for index in range(13)],
    }
