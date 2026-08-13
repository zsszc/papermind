"""Batch 13：公开 fixture CLI 与报告双次可复现契约。"""

import json
from pathlib import Path

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
