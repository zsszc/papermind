"""Batch 25：发布 E2E 必须由具备后端依赖的 CI job 显式调度。"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
RELEASE_TESTS = (
    ROOT / "electron" / "test" / "release-flow.test.js",
    ROOT / "electron" / "test" / "data-dir-migration.test.js",
)


def test_release_e2e_uses_explicit_gate_and_configurable_python():
    for path in RELEASE_TESTS:
        source = path.read_text(encoding="utf-8")
        assert "PAPERMIND_RELEASE_E2E" in source
        assert "PAPERMIND_PYTHON" in source


def test_backend_ci_job_dispatches_real_release_e2e():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "运行发布候选真实 E2E" in workflow
    assert "PAPERMIND_RELEASE_E2E: \"1\"" in workflow
    assert "PAPERMIND_PYTHON: python" in workflow
    assert "test/release-flow.test.js" in workflow
    assert "test/data-dir-migration.test.js" in workflow


def test_frontend_ci_enforces_chunk_budget_after_build():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    build_position = workflow.index("run: npm run build")
    budget_position = workflow.index("run: npm run check:chunks")
    assert budget_position > build_position
