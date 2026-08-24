"""Batch 22F：Weighted-RRF CLI、快照和自动选择器 RED。"""

from copy import deepcopy
import importlib
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Chunk, Paper
from eval import run


def _selector():
    return importlib.import_module("eval.weighted_rrf_selector")


def test_weighted_cli_locks_profile_weight_and_private_protocol(tmp_path):
    parser = run.build_parser()
    common = [
        "--retrieval-profile", "weighted-rrf-v1",
        "--database", str(tmp_path / "papers.db"),
        "--corpus-root", str(tmp_path / "corpus"),
        "--vector-dir", str(tmp_path / "vectors"),
        "--split", "train", "--evidence-resolver", "page-span-v2",
        "--lexical-profile", "bm25-bilingual",
    ]
    args = parser.parse_args(common)
    assert "--rrf-lexical-weight" in run._validate_cli_args(args)

    args = parser.parse_args(common + ["--rrf-lexical-weight", "1.25"])
    assert run._validate_cli_args(args) is None

    args = parser.parse_args([
        "--vector-dir", str(tmp_path / "vectors"),
        "--rrf-lexical-weight", "1.25",
    ])
    assert "仅适用于 weighted-rrf-v1" in run._validate_cli_args(args)

    args = parser.parse_args(common[:-4] + [
        "--split", "holdout", "--evidence-resolver", "page-span-v2",
        "--lexical-profile", "bm25-bilingual",
        "--rrf-lexical-weight", "1.25",
    ])
    assert "holdout" in run._validate_cli_args(args)


def test_weighted_contract_separates_formula_and_configuration_hashes():
    first = run.weighted_rrf_contract_metadata(1.0)
    second = run.weighted_rrf_contract_metadata(1.5)
    assert first["formula_sha256"] == second["formula_sha256"]
    assert first["configuration_sha256"] != second["configuration_sha256"]
    assert first["semantic_weight"] == 1.0
    assert second["lexical_weight"] == 1.5


class _Collection:
    def __init__(self, ids, dimension=1024):
        self.ids = ids
        self.dimension = dimension

    def get(self, include):
        assert include == ["embeddings"]
        return {
            "ids": list(self.ids),
            "embeddings": [[float(index)] * self.dimension
                           for index, _ in enumerate(self.ids)],
        }


def test_vector_snapshot_audit_hashes_ids_and_embeddings(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'papers.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    db.add(Paper(
        id=1, title="fixture", filename="x.pdf", file_path="papers/x.pdf"
    ))
    db.add(Chunk(paper_id=1, chunk_index=0, content="content"))
    db.commit()
    try:
        audit = run._audit_vector_snapshot(
            db, SimpleNamespace(collection=_Collection(["p1_c0"]))
        )
        assert audit["database_chunk_count"] == 1
        assert audit["vector_count"] == 1
        assert audit["embedding_dimension"] == 1024
        assert len(audit["vector_manifest_sha256"]) == 64
        with pytest.raises(ValueError, match="ID 不一致"):
            run._audit_vector_snapshot(
                db, SimpleNamespace(collection=_Collection(["p1_c9"]))
            )
    finally:
        db.close()
        engine.dispose()


def _report(
    weight, *, recall=0.6, any_hit=0.6, factoid=1 / 3,
    mrr=0.4, ndcg=0.45, split="train", degraded=0,
    profile="weighted-rrf-v1",
):
    items = [
        {
            "qa_id": f"q{index:02d}",
            "retrieved_ids": [f"p1_c{index}"],
            "degraded": False,
        }
        for index in range(24)
    ]
    return {
        "run": {"git_sha": "a" * 40},
        "benchmark": {
            "dataset_sha256": "a" * 64,
            "qrels_sha256": "b" * 64,
            "corpus_manifest_sha256": "c" * 64,
            "page_text_manifest_sha256": "d" * 64,
            "resolver_version": "page-span-v2",
            "vector_manifest_sha256": "e" * 64,
        },
        "pipeline": {
            "profile": profile,
            "effective_profile": profile,
            "lexical_profile": "bm25-bilingual",
            "semantic_rerank": None,
            "split": split,
            "top_k": 5,
            "evidence_resolver": "page-span-v2",
            "weighted_rrf": {
                "semantic_weight": 1.0,
                "lexical_weight": weight,
                "rrf_k": 60,
                "formula_sha256": "f" * 64,
                "configuration_sha256": f"{int(weight * 100):064x}",
            },
        },
        "diagnostics": {
            "runtime_degraded_count": degraded,
            "vector_snapshot": {
                "database_chunk_count": 464,
                "vector_count": 464,
                "missing_vector_ids": 0,
                "extra_vector_ids": 0,
                "embedding_dimension": 1024,
                "vector_manifest_sha256": "e" * 64,
            },
        },
        "overall": {
            "recall@5": recall, "any_hit@5": any_hit,
            "mrr": mrr, "ndcg@5": ndcg,
            "n_positive": 24, "n_negative": 0,
        },
        "by_question_type": [
            {"question_type": "factoid", "n": 6, "recall": factoid},
            {"question_type": "method_detail", "n": 18, "recall": recall},
        ],
        "latency": {"p95": 300.0, "count": 24},
        "items": items,
        "with_llm": False,
    }


def _production_report():
    report = _report(1.0, profile="hybrid")
    report["pipeline"].pop("weighted_rrf")
    return report


def test_train_selector_requires_baseline_parity_and_selects_lexicographically():
    module = _selector()
    baseline = _report(1.0)
    candidates = [
        _report(1.25, recall=0.62, factoid=0.5, mrr=0.39, ndcg=0.44),
        _report(1.5, recall=0.64, factoid=0.5, mrr=0.40, ndcg=0.45),
        _report(2.0, recall=0.61, factoid=0.5, mrr=0.39, ndcg=0.44),
    ]
    result = module.select_weighted_rrf_train(
        _production_report(), baseline, list(reversed(candidates))
    )
    assert result["baseline_parity"]["matched_queries"] == 24
    assert result["winner"]["lexical_weight"] == 1.5
    assert result["passed"] is True


def test_train_selector_fails_closed_on_parity_grid_and_degradation():
    module = _selector()
    production = _production_report()
    production["items"][0]["retrieved_ids"] = ["p9_c9"]
    candidates = [_report(1.25), _report(1.5), _report(2.0)]
    with pytest.raises(ValueError, match="parity"):
        module.select_weighted_rrf_train(production, _report(1.0), candidates)

    with pytest.raises(ValueError, match="冻结权重网格"):
        module.select_weighted_rrf_train(
            _production_report(), _report(1.0),
            [_report(1.25), _report(1.25), _report(2.0)],
        )

    degraded = _report(1.5, degraded=1)
    degraded["items"][0]["degraded"] = True
    with pytest.raises(ValueError, match="降级"):
        module.select_weighted_rrf_train(
            _production_report(), _report(1.0),
            [_report(1.25), degraded, _report(2.0)],
        )


def test_dev_gate_only_accepts_train_winner_and_requires_strict_gain():
    module = _selector()
    selection = {"passed": True, "winner": {"lexical_weight": 1.5}}
    baseline = _report(1.0, split="dev")
    candidate = _report(
        1.5, split="dev", recall=0.61, factoid=1 / 3,
        mrr=0.4, ndcg=0.45,
    )
    gate = module.evaluate_weighted_rrf_dev(baseline, candidate, selection)
    assert gate["passed"] is True

    wrong = deepcopy(candidate)
    wrong["pipeline"]["weighted_rrf"]["lexical_weight"] = 2.0
    with pytest.raises(ValueError, match="train winner"):
        module.evaluate_weighted_rrf_dev(baseline, wrong, selection)

    equal = _report(1.5, split="dev")
    assert module.evaluate_weighted_rrf_dev(
        baseline, equal, selection
    )["passed"] is False
