"""Batch 33：前端生产 chunk 预算 Gate。"""

import importlib.util
from pathlib import Path


_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_frontend_chunks.py"
_SPEC = importlib.util.spec_from_file_location("check_frontend_chunks", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
JS_BUDGET_BYTES = _MODULE.JS_BUDGET_BYTES
PDF_WORKER_BUDGET_BYTES = _MODULE.PDF_WORKER_BUDGET_BYTES
scan_chunks = _MODULE.scan_chunks


def _write(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"0" * size)


def test_chunk_budget_accepts_boundary_and_pdf_worker_exception(tmp_path):
    _write(tmp_path / "assets" / "app.js", JS_BUDGET_BYTES)
    _write(tmp_path / "assets" / "pdf.worker.min-test.mjs", PDF_WORKER_BUDGET_BYTES)
    rows, errors = scan_chunks(tmp_path)
    assert len(rows) == 2
    assert errors == []


def test_chunk_budget_rejects_oversize_or_missing_build(tmp_path):
    assert scan_chunks(tmp_path)[1] == ["缺少 dist/assets，请先执行生产构建"]
    _write(tmp_path / "assets" / "ui.js", JS_BUDGET_BYTES + 1)
    assert "ui.js" in scan_chunks(tmp_path)[1][0]
