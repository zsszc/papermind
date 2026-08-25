"""Batch 23E 独立进程失败事务 Harness 契约。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


FIXTURE = Path("eval/fixtures/failure_transactions_public_v1.json")


def test_public_fixture_is_synthetic_and_has_unique_scenarios():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert fixture["fixture_schema"] == "papermind-failure-transactions-fixture-v1"
    assert fixture["benchmark_id"] == "papermind-failure-transactions-public-v1"
    assert fixture["license"] == "CC0-1.0"
    assert fixture["synthetic"] is True
    ids = [item["scenario_id"] for item in fixture["scenarios"]]
    assert len(ids) == 7
    assert len(ids) == len(set(ids))


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
    report_path = report_dir / "failure_transactions_public_v1.json"
    first_bytes = report_path.read_bytes()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["report_schema"] == "papermind-failure-transactions-report-v1"
    assert report["gate"]["passed"] is True
    assert len(report["scenarios"]) == 7
    assert report["offline_proof"] == {
        "network_attempts": 0,
        "subprocess_attempts": 0,
        "private_path_attempts": 0,
        "real_service_module_count": 0,
    }
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
        "report_schema": "papermind-failure-transactions-report-v1",
        "question": "synthetic-question",
    }
    try:
        validate_public_report(invalid)
    except ValueError:
        pass
    else:
        raise AssertionError("报告含未知正文键时必须 fail closed")
