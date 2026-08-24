"""Batch 13：公开 fixture CLI 与报告双次可复现契约。"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from eval import run


FIXTURE = "eval/fixtures/rag_public_v1.json"
DATASET = "eval/dataset/qa_public_v1.jsonl"


def _run_report(tmp_path, profile="bm25"):
    report_dir = tmp_path / f"reports-{profile}"
    rc = run.main([
        "--fixture", FIXTURE,
        "--dataset", DATASET,
        "--keyword-only",
        "--lexical-profile", profile,
        "--threshold", "0",
        "--report-dir", str(report_dir),
    ])
    assert rc == 0
    reports = sorted(report_dir.glob("*.json"))
    assert len(reports) == 1
    return json.loads(reports[0].read_text(encoding="utf-8"))


def test_fixture_cli_does_not_use_real_session_local(tmp_path, monkeypatch):
    def forbidden_real_database():
        raise AssertionError("fixture 评测不得连接真实 SessionLocal")

    monkeypatch.setattr("app.database.SessionLocal", forbidden_real_database)
    report = _run_report(tmp_path)
    assert report["benchmark"]["benchmark_id"] == "papermind-rag-public-v1"


def test_two_runs_have_identical_comparison_key_and_quality_metrics(tmp_path):
    first = _run_report(tmp_path / "first")
    second = _run_report(tmp_path / "second")

    assert first["benchmark"]["comparison_key"] == second["benchmark"]["comparison_key"]
    assert first["overall"] == second["overall"]
    assert first["by_question_type"] == second["by_question_type"]
    assert [item["relevant_ids"] for item in first["items"]] == [
        item["relevant_ids"] for item in second["items"]
    ]


def test_report_has_qrels_hash_and_no_absolute_dataset_path(tmp_path):
    report = _run_report(tmp_path)
    benchmark = report["benchmark"]

    assert len(benchmark["qrels_sha256"]) == 64
    assert benchmark["fixture_license"] == "CC0-1.0"
    assert report["dataset"] == "qa_public_v1.jsonl"
    assert not report["dataset"].startswith("/")
    assert "/Users/" not in json.dumps(report, ensure_ascii=False)


def test_parser_rejects_hybrid_fixture_mode():
    parser = run.build_parser()
    args = parser.parse_args(["--fixture", FIXTURE, "--dataset", DATASET])
    assert run._validate_fixture_args(args) == "fixture 评测必须显式使用 --keyword-only"


def test_private_split_filter_keeps_only_requested_partition():
    items = [
        {"qa_id": "train-1", "split": "train"},
        {"qa_id": "dev-1", "split": "dev"},
        {"qa_id": "holdout-1", "split": "holdout"},
    ]

    assert run._select_split(items, "all") == items
    assert run._select_split(items, "dev") == [items[1]]
    with pytest.raises(ValueError, match="没有可评测条目"):
        run._select_split(items[:1], "holdout")


def test_parser_accepts_private_split():
    args = run.build_parser().parse_args(["--split", "dev"])
    assert args.split == "dev"


def test_private_hybrid_requires_explicit_vector_snapshot():
    parser = run.build_parser()
    args = parser.parse_args(["--dataset", "private.jsonl"])

    assert run._validate_fixture_args(args) == (
        "hybrid 评测必须显式指定 --vector-dir 隔离向量快照"
    )


def test_parser_accepts_explicit_vector_snapshot(tmp_path):
    args = run.build_parser().parse_args(["--vector-dir", str(tmp_path)])
    assert args.vector_dir == str(tmp_path)


def test_parser_accepts_explicit_candidate_database_and_corpus_root(tmp_path):
    args = run.build_parser().parse_args([
        "--database", str(tmp_path / "candidate.db"),
        "--corpus-root", str(tmp_path / "corpus"),
    ])

    assert args.database == str(tmp_path / "candidate.db")
    assert args.corpus_root == str(tmp_path / "corpus")


def test_page_span_v2_requires_isolated_database_split_and_corpus(tmp_path):
    parser = run.build_parser()
    args = parser.parse_args(["--evidence-resolver", "page-span-v2"])
    assert "--database/--corpus-root" in run._validate_fixture_args(args)

    args = parser.parse_args([
        "--evidence-resolver", "page-span-v2",
        "--database", str(tmp_path / "candidate.db"),
        "--corpus-root", str(tmp_path / "corpus"),
    ])
    assert "train/dev/holdout" in run._validate_fixture_args(args)


def test_page_span_v2_report_uses_character_coverage_gate(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.database import Base
    from app.models import Chunk, Paper

    database = tmp_path / "candidate.db"
    engine = create_engine(f"sqlite:///{database}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        db.add(Paper(
            id=1, title="fixture", doi="10.1/span", filename="x.pdf",
            file_path="papers/x.pdf", processed="done",
        ))
        db.add(Chunk(
            paper_id=1, chunk_index=0, page_number=1,
            page_start=0, page_end=30, content="target evidence span",
        ))
        db.commit()
    engine.dispose()

    entry = {
        "qa_id": "span-1", "question": "target evidence",
        "ground_truth": "target", "question_type": "factoid",
        "source": "synthetic", "has_answer": True, "split": "train",
        "relevant_evidence": [{
            "paper_uid": "doi:10.1/span",
            "quote": "target evidence span long enough",
        }],
    }
    dataset = tmp_path / "qa.jsonl"
    dataset.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    report_dir = tmp_path / "reports"
    group = {
        "paper_id": 1, "page_number": 1, "page_start": 0, "page_end": 20,
        "chunks": [{
            "chunk_id": "p1_c0", "page_start": 0, "page_end": 20,
        }],
    }
    monkeypatch.setattr(
        run,
        "_resolve_span_qrels_or_raise",
        lambda *args, **kwargs: (
            {"span-1": ["p1_c0"]}, {"span-1": [group]}, "a" * 64
        ),
    )

    assert run.main([
        "--database", str(database), "--corpus-root", str(corpus),
        "--dataset", str(dataset), "--split", "train",
        "--evidence-resolver", "page-span-v2", "--keyword-only",
        "--threshold", "0", "--report-dir", str(report_dir),
    ]) == 0
    report = json.loads(next(report_dir.glob("*.json")).read_text())

    assert report["benchmark"]["resolver_version"] == "page-span-v2"
    assert report["benchmark"]["page_text_manifest_sha256"] == "a" * 64
    assert report["pipeline"]["evidence_resolver"] == "page-span-v2"
    assert report["overall"]["any_hit@5"] == pytest.approx(1.0)
    assert report["overall"]["span_coverage@5"] == pytest.approx(1.0)
    assert report["gate"]["metric"] == "span_coverage@5"
    assert "chunks" not in report["items"][0]


def test_explicit_database_does_not_use_production_session(
    tmp_path, monkeypatch
):
    from sqlalchemy import create_engine

    from app.database import Base

    database = tmp_path / "candidate.db"
    engine = create_engine(f"sqlite:///{database}")
    Base.metadata.create_all(engine)
    engine.dispose()
    corpus_root = tmp_path / "corpus"
    corpus_root.mkdir()
    dataset = tmp_path / "empty.jsonl"
    dataset.write_text("", encoding="utf-8")
    report_dir = tmp_path / "reports"

    def forbidden_real_database():
        raise AssertionError("显式候选评测不得连接生产 SessionLocal")

    monkeypatch.setattr("app.database.SessionLocal", forbidden_real_database)
    monkeypatch.setattr(run, "_prepare_eval_items", lambda args: ([], None))
    args = run.build_parser().parse_args([
        "--database", str(database),
        "--corpus-root", str(corpus_root),
        "--dataset", str(dataset),
        "--keyword-only",
        "--threshold", "0",
        "--report-dir", str(report_dir),
    ])

    assert run.run_eval(args) == 0


def test_qrels_hash_changes_when_evidence_changes():
    original = [{
        "qa_id": "qa",
        "has_answer": True,
        "relevant_evidence": [{"paper_uid": "doi:10.1/a", "quote": "a" * 20}],
    }]
    changed = [{
        "qa_id": "qa",
        "has_answer": True,
        "relevant_evidence": [{"paper_uid": "doi:10.1/a", "quote": "b" * 20}],
    }]
    assert run._qrels_sha256(original) != run._qrels_sha256(changed)


def test_corpus_fingerprint_does_not_depend_on_database_paper_id(db, tmp_path):
    from app.models import Chunk, Paper

    dataset = tmp_path / "qa.jsonl"
    dataset.write_text("{}\n", encoding="utf-8")
    paper = Paper(
        id=9, title="stable", doi="10.1/stable", filename="x.pdf",
        file_path="papers/x.pdf", processed="done",
    )
    db.add(paper)
    db.add(Chunk(paper_id=9, chunk_index=0, content="same content"))
    db.commit()
    first = run._build_benchmark_metadata(db, dataset)["corpus_manifest_sha256"]

    db.query(Chunk).delete()
    db.query(Paper).delete()
    db.add(Paper(
        id=99, title="stable", doi="https://doi.org/10.1/STABLE.", filename="y.pdf",
        file_path="papers/y.pdf", processed="done",
    ))
    db.add(Chunk(paper_id=99, chunk_index=0, content="same content"))
    db.commit()
    second = run._build_benchmark_metadata(db, dataset)["corpus_manifest_sha256"]

    assert first == second


def test_corpus_fingerprint_handles_orphan_fixture_chunks_without_dynamic_id(
    tmp_path
):
    first = SimpleNamespace(
        paper_id=9, chunk_index=0, content="orphan fixture content"
    )
    second = SimpleNamespace(
        paper_id=99, chunk_index=0, content="orphan fixture content"
    )

    assert run._manifest_chunk_paper_uid(first, {}) == (
        run._manifest_chunk_paper_uid(second, {})
    )


def test_eval_workflow_uses_public_fixture_and_frozen_gates():
    workflow = (
        Path(__file__).resolve().parents[2] / ".github" / "workflows" / "eval.yml"
    ).read_text(encoding="utf-8")

    assert "--fixture eval/fixtures/rag_public_v1.json" in workflow
    assert "--dataset eval/dataset/qa_public_v1.jsonl" in workflow
    assert "--lexical-profile count" in workflow
    assert "--lexical-profile bm25" in workflow
    assert workflow.count("--threshold 0.85") == 2
    assert "HF_ENDPOINT" not in workflow
