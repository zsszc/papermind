"""Batch 26：v2 train 失败归因必须只输出去标识化聚合。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval import train_failure_diagnostics as diagnostics


def _item(
    qa_id: str,
    question_type: str,
    relevant: list[str],
    retrieved: list[str],
    span_coverage: float,
) -> dict:
    return {
        "qa_id": qa_id,
        "question_type": question_type,
        "has_answer": True,
        "relevant_ids": relevant,
        "retrieved_ids": retrieved,
        "latency_ms": 10.0,
        "mode_used": "hybrid",
        "degraded": False,
        "recall": float(bool(set(relevant) & set(retrieved))),
        "mrr": 1.0 if retrieved and retrieved[0] in relevant else 0.0,
        "ndcg": 1.0 if retrieved and retrieved[0] in relevant else 0.0,
        "relevant_span_count": 1,
        "relevant_chunk_count": len(relevant),
        "any_hit": float(span_coverage > 0),
        "span_coverage": span_coverage,
    }


def _report() -> dict:
    items = [
        _item("private-full", "factoid", ["p1_c1"], ["p1_c1"], 1.0),
        _item(
            "private-partial", "method_detail", ["p2_c1", "p2_c2"],
            ["p2_c1", "p9_c1"], 0.5,
        ),
        _item("private-same", "summary", ["p3_c1"], ["p3_c9"], 0.0),
        _item("private-cross", "factoid", ["p4_c1"], ["p8_c1"], 0.0),
        _item("private-empty", "method_detail", ["p5_c1"], [], 0.0),
    ]
    return {
        "report_schema": "2.0",
        "run": {"git_sha": "a" * 40, "git_tracked_clean": True},
        "benchmark": {
            "dataset_sha256": "b" * 64,
            "qrels_sha256": "c" * 64,
            "corpus_manifest_sha256": "d" * 64,
        },
        "pipeline": {
            "profile": "hybrid",
            "effective_profile": "hybrid",
            "lexical_profile": "bm25-bilingual",
            "split": "train",
            "evidence_resolver": "page-span-v2",
            "top_k": 5,
        },
        "diagnostics": {"runtime_degraded_count": 0},
        "with_llm": False,
        "overall": {
            "n_positive": len(items),
            "n_negative": 0,
            "recall@5": 0.4,
            "mrr": 0.2,
            "ndcg@5": 0.2,
            "span_coverage@5": 0.3,
        },
        "latency": {"p50": 10.0, "p95": 20.0, "mean": 12.0, "count": 5},
        "items": items,
    }


def test_analyzer_classifies_every_item_and_emits_no_identifiers():
    result = diagnostics.analyze_train_report(_report())

    assert result["schema"] == "train-failure-diagnostics-v1"
    assert result["total_items"] == 5
    assert result["failure_items"] == 4
    assert {row["category"]: row["count"] for row in result["categories"]} == {
        "cross_paper_miss": 1,
        "empty_retrieval": 1,
        "full_coverage": 1,
        "partial_coverage": 1,
        "same_paper_miss": 1,
    }
    rendered = diagnostics.render_report(result)
    assert "private-" not in rendered
    assert "p1_c1" not in rendered
    assert "qa_id" not in rendered
    assert "chunk_id" not in rendered


def test_analyzer_is_deterministic_and_uses_fixed_tie_priority():
    first = diagnostics.analyze_train_report(_report())
    second = diagnostics.analyze_train_report(_report())

    assert diagnostics.render_report(first) == diagnostics.render_report(second)
    assert first["recommendation"]["candidate"] == "query-document-expansion-v1"
    assert first["recommendation"]["support_count"] == 1
    gate = first["recommendation"]["train_gate"]
    assert gate["minimum_span_coverage_gain"] == pytest.approx(1 / 5)
    assert gate["dev_policy"] == "run-once-only-after-train-pass"
    assert gate["holdout_policy"] == "forbidden"


def test_dirty_report_is_rejected_by_default_and_requires_verified_override():
    report = _report()
    report["run"]["git_tracked_clean"] = False

    with pytest.raises(ValueError, match="clean Git"):
        diagnostics.analyze_train_report(report)
    with pytest.raises(ValueError, match="祖先"):
        diagnostics.analyze_train_report(
            report,
            allow_historical_dirty=True,
            historical_commit_verified=False,
        )


def test_verified_historical_report_is_selection_only_and_keeps_original_sha():
    report = _report()
    report["run"]["git_tracked_clean"] = False
    report["benchmark"].update({
        "database_logical_manifest_sha256": "d" * 64,
        "page_text_manifest_sha256": "e" * 64,
        "vector_manifest_sha256": "f" * 64,
        "hnsw_config_sha256": "1" * 64,
        "hnsw_binary_manifest_sha256": "2" * 64,
    })
    report["diagnostics"]["vector_snapshot"] = {
        "database_chunk_count": 5,
        "vector_count": 5,
        "missing_vector_ids": 0,
        "extra_vector_ids": 0,
        "embedding_dimension": 1024,
        "hnsw_space": "cosine",
        "hnsw_num_threads": 1,
        "hnsw_search_ef": 5,
        "vector_manifest_sha256": "f" * 64,
        "hnsw_config_sha256": "1" * 64,
        "hnsw_binary_manifest_sha256": "2" * 64,
    }

    result = diagnostics.analyze_train_report(
        report,
        allow_historical_dirty=True,
        historical_commit_verified=True,
    )

    assert result["provenance"] == {
        "source_git_tracked_clean": False,
        "historical_dirty_override": True,
        "historical_commit_verified": True,
        "usage": "candidate-selection-only",
        "promotion_eligible": False,
    }
    assert result["recommendation"]["train_gate"][
        "requires_fresh_clean_baseline"
    ] is True
    assert result["input_report_sha256"] == diagnostics.report_sha256(report)


def test_historical_override_rejects_incomplete_vector_contract():
    report = _report()
    report["run"]["git_tracked_clean"] = False
    report["benchmark"].update({
        "database_logical_manifest_sha256": "d" * 64,
        "page_text_manifest_sha256": "e" * 64,
        "vector_manifest_sha256": "f" * 64,
        "hnsw_config_sha256": "1" * 64,
        "hnsw_binary_manifest_sha256": "2" * 64,
    })
    report["diagnostics"]["vector_snapshot"] = {
        "database_chunk_count": 5,
        "vector_count": 4,
    }

    with pytest.raises(ValueError, match="向量快照"):
        diagnostics.analyze_train_report(
            report,
            allow_historical_dirty=True,
            historical_commit_verified=True,
        )


def test_commit_ancestor_verification_is_fail_closed(monkeypatch, tmp_path):
    class Completed:
        returncode = 0

    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return Completed()

    monkeypatch.setattr(diagnostics.subprocess, "run", fake_run)
    assert diagnostics.verify_commit_ancestor("a" * 40, tmp_path) is True
    assert calls[0][0] == [
        "git", "merge-base", "--is-ancestor", "a" * 40, "HEAD",
    ]

    class Missing:
        returncode = 128

    monkeypatch.setattr(
        diagnostics.subprocess, "run", lambda *args, **kwargs: Missing()
    )
    assert diagnostics.verify_commit_ancestor("b" * 40, tmp_path) is False


def test_by_type_aggregation_is_conservative_and_complete():
    result = diagnostics.analyze_train_report(_report())
    rows = {row["question_type"]: row for row in result["by_question_type"]}

    assert rows["factoid"]["n"] == 2
    assert rows["factoid"]["mean_span_coverage"] == pytest.approx(0.5)
    assert sum(rows["factoid"]["categories"].values()) == 2
    assert set(rows) == {"factoid", "method_detail", "summary"}


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda report: report["pipeline"].update(split="dev"), "train"),
        (lambda report: report["pipeline"].update(split="holdout"), "train"),
        (lambda report: report["pipeline"].update(top_k=10), "top_k"),
        (
            lambda report: report["diagnostics"].update(
                runtime_degraded_count=1
            ),
            "降级",
        ),
        (lambda report: report.update(with_llm=True), "LLM"),
        (
            lambda report: report["items"][-1].update(
                qa_id=report["items"][0]["qa_id"]
            ),
            "qa_id",
        ),
        (
            lambda report: report["items"][0].update(
                retrieved_ids=["not-a-chunk"]
            ),
            "chunk ID",
        ),
        (
            lambda report: report["items"][0].update(span_coverage=1.1),
            "span_coverage",
        ),
    ],
)
def test_analyzer_fails_closed_for_invalid_or_unblinded_reports(mutate, match):
    report = _report()
    mutate(report)
    with pytest.raises(ValueError, match=match):
        diagnostics.analyze_train_report(report)


def test_safe_cli_paths_reject_escape_and_symlink(tmp_path: Path):
    private_root = tmp_path / "private"
    private_root.mkdir()
    report = private_root / "train.json"
    report.write_text("{}", encoding="utf-8")
    symlink = private_root / "link.json"
    symlink.symlink_to(report)

    assert diagnostics.validate_cli_path(
        report, private_root=private_root, must_exist=True
    ) == report.resolve()
    with pytest.raises(ValueError, match="私有目录"):
        diagnostics.validate_cli_path(
            tmp_path / "outside.json", private_root=private_root,
            must_exist=False,
        )
    with pytest.raises(ValueError, match="symlink"):
        diagnostics.validate_cli_path(
            symlink, private_root=private_root, must_exist=True
        )


def test_exclusive_report_write_uses_0600_and_refuses_overwrite(tmp_path: Path):
    output = tmp_path / "diagnostics.json"
    payload = diagnostics.analyze_train_report(_report())

    diagnostics.write_report_exclusive(output, payload)

    assert output.stat().st_mode & 0o777 == 0o600
    assert json.loads(output.read_text(encoding="utf-8")) == payload
    with pytest.raises(FileExistsError):
        diagnostics.write_report_exclusive(output, payload)
