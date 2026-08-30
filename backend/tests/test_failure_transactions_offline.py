"""Batch 23E 独立进程失败事务 Harness 契约。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


FIXTURE_V1 = Path("eval/fixtures/failure_transactions_public_v1.json")
FIXTURE = Path("eval/fixtures/failure_transactions_public_v2.json")


def test_public_fixture_is_synthetic_and_has_unique_scenarios():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert fixture["fixture_schema"] == "papermind-failure-transactions-fixture-v2"
    assert fixture["benchmark_id"] == "papermind-failure-transactions-public-v2"
    assert fixture["license"] == "CC0-1.0"
    assert fixture["synthetic"] is True
    ids = [item["scenario_id"] for item in fixture["scenarios"]]
    assert len(ids) == 11
    assert len(ids) == len(set(ids))
    assert ids[-4:] == [
        "regenerate-active-second-request",
        "regenerate-external-revision-conflict",
        "regenerate-external-delete",
        "regenerate-cancel-release-retry",
    ]
    assert all("expected_error_code" in item for item in fixture["scenarios"])


def test_v1_fixture_remains_frozen():
    fixture = json.loads(FIXTURE_V1.read_text(encoding="utf-8"))
    assert fixture["fixture_schema"] == "papermind-failure-transactions-fixture-v1"
    assert fixture["benchmark_id"] == "papermind-failure-transactions-public-v1"
    assert [item["scenario_id"] for item in fixture["scenarios"]] == [
        "chat-success-control",
        "chat-stream-failure",
        "chat-cancelled",
        "chat-assistant-commit-failure",
        "deep-review-plan-failure",
        "deep-review-commit-failure",
        "regenerate-commit-failure",
    ]


def test_gate_fails_closed_when_any_counter_is_nonzero():
    from eval.failure_transactions import build_failure_transaction_gate

    clean = {key: 0 for key in build_failure_transaction_gate({})["checks"]}
    assert build_failure_transaction_gate(clean)["passed"] is True
    for key in clean:
        dirty = {**clean, key: 1}
        assert build_failure_transaction_gate(dirty)["passed"] is False


def test_cli_runs_in_clean_subprocess_and_publishes_content_free_report(tmp_path):
    report_dir = Path("eval/reports") / f"failure-{tmp_path.name}"
    untrusted_runtime = tmp_path / "must-not-touch"
    env = {
        **os.environ,
        "PYTHONPATH": "",
        "OPENAI_API_KEY": "synthetic-secret-canary",
        "KIMI_API_KEY": "synthetic-secret-canary",
        "MOONSHOT_API_KEY": "synthetic-secret-canary",
        "LANGFUSE_PUBLIC_KEY": "synthetic-secret-canary",
        "LANGFUSE_SECRET_KEY": "synthetic-secret-canary",
        "PAPERMIND_DATA_DIR": str(untrusted_runtime),
    }
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "eval.failure_transactions",
            "--report-dir",
            str(report_dir),
        ],
        cwd=Path.cwd(),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert untrusted_runtime.exists() is False
    report_path = report_dir / "failure_transactions_public_v2.json"
    first_bytes = report_path.read_bytes()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["report_schema"] == "papermind-failure-transactions-report-v2"
    assert report["gate"]["passed"] is True
    assert len(report["scenarios"]) == 11
    assert report["offline_proof"] == {
        "fake_llm_calls": 10,
        "fake_retrieval_calls": 11,
        "fake_deep_review_calls": 4,
        "request_count": 14,
        "peer_request_count": 2,
        "retry_request_count": 1,
        "external_commit_count": 2,
        "coordinated_scenario_count": 3,
        "network_attempts": 0,
        "subprocess_attempts": 0,
        "private_path_attempts": 0,
        "real_service_module_count": 0,
    }
    by_id = {item["scenario_id"]: item for item in report["scenarios"]}
    active = by_id["regenerate-active-second-request"]
    assert active["terminal"] == "finished"
    assert active["peer_http_status"] == 409
    assert active["request_count"] == 2
    assert active["fake_llm_calls"] == active["fake_retrieval_calls"] == 1
    assert active["coordination_verified"] is True
    assert active["active_release_verified"] is True
    assert active["worker_join_verified"] is True

    revision = by_id["regenerate-external-revision-conflict"]
    assert revision["terminal"] == "error"
    assert revision["error_code"] == "regenerate_conflict"
    assert revision["external_commit_verified"] is True
    assert revision["target_state_verified"] is True

    deleted = by_id["regenerate-external-delete"]
    assert deleted["peer_http_status"] == 204
    assert deleted["error_code"] == "regenerate_target_missing"
    assert deleted["external_commit_verified"] is True
    assert deleted["target_state_verified"] is True

    cancelled = by_id["regenerate-cancel-release-retry"]
    assert cancelled["terminal"] == "none"
    assert cancelled["retry_http_status"] == 200
    assert cancelled["retry_finished_count"] == 1
    assert cancelled["fake_llm_calls"] == cancelled["fake_retrieval_calls"] == 2
    assert cancelled["active_release_verified"] is True
    assert cancelled["target_state_verified"] is True

    from eval.failure_transactions import validate_public_report

    invalid_proof = json.loads(json.dumps(report))
    invalid_proof["offline_proof"]["fake_llm_calls"] += 1
    try:
        validate_public_report(invalid_proof)
    except ValueError:
        pass
    else:
        raise AssertionError("报告 offline proof 与场景求和漂移时必须 fail closed")

    invalid_pass = json.loads(json.dumps(report))
    invalid_pass["scenarios"][-1]["retry_finished_count"] = 0
    try:
        validate_public_report(invalid_pass)
    except ValueError:
        pass
    else:
        raise AssertionError("场景 PASS 与冻结行为证据矛盾时必须 fail closed")
    rendered = report_path.read_text(encoding="utf-8")
    for forbidden in (
        "synthetic-question",
        "synthetic-answer",
        "synthetic-secret-canary",
        "Traceback",
        str(Path.cwd().resolve()),
    ):
        assert forbidden not in rendered

    second = subprocess.run(
        result.args,
        cwd=Path.cwd(),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert second.returncode == 0, second.stderr
    assert report_path.read_bytes() == first_bytes


def test_report_schema_rejects_unknown_or_content_fields():
    from eval.failure_transactions import validate_public_report

    invalid = {
        "report_schema": "papermind-failure-transactions-report-v2",
        "question": "synthetic-question",
    }
    try:
        validate_public_report(invalid)
    except ValueError:
        pass
    else:
        raise AssertionError("报告含未知正文键时必须 fail closed")
