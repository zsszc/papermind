"""Batch 22K 语料就绪度只读 API 与隐私契约。"""

from __future__ import annotations

import hashlib
import json

import pytest

from app.services.corpus_readiness import (
    PUBLIC_READINESS_FIELDS,
    calculate_benchmark_v2_readiness,
)


def _document(payload: bytes, uid: str) -> tuple[dict, str]:
    digest = hashlib.sha256(payload).hexdigest()
    return {"paper_uid": uid, "pdf_sha256": digest}, digest


def test_calculate_readiness_returns_only_aggregate_whitelist(tmp_path):
    papers = tmp_path / "papers"
    papers.mkdir()
    covered = b"%PDF-1.7\ncovered"
    eligible = b"%PDF-1.7\neligible"
    unimported = b"%PDF-1.7\nunimported"
    (papers / "private-covered.pdf").write_bytes(covered)
    (papers / "private-covered-copy.pdf").write_bytes(covered)
    (papers / "private-eligible.pdf").write_bytes(eligible)
    (papers / "private-unimported.pdf").write_bytes(unimported)
    covered_document, _covered_sha = _document(covered, "doi:10.1/private")
    eligible_document, _eligible_sha = _document(
        eligible, "sha256:" + hashlib.sha256(eligible).hexdigest()
    )
    manifest = {
        "manifest_sha256": "a" * 64,
        "documents": [covered_document, eligible_document],
    }

    result = calculate_benchmark_v2_readiness(
        papers,
        manifest,
        ["doi:10.1/private"],
        minimum_new_papers=12,
    )

    assert set(result) == PUBLIC_READINESS_FIELDS
    assert result == {
        "status": "WAIT",
        "ready": False,
        "minimum_new_papers": 12,
        "missing_new_papers": 11,
        "physical_pdf_files": 4,
        "unique_pdf_contents": 3,
        "duplicate_pdf_files": 1,
        "covered_unique_contents": 1,
        "eligible_imported_papers": 1,
        "unimported_unique_contents": 1,
        "error_code": None,
    }
    rendered = json.dumps(result, ensure_ascii=False)
    assert "private-covered" not in rendered
    assert "paper_uid" not in rendered
    assert "pdf_sha256" not in rendered
    assert "10.1/private" not in rendered


def test_calculate_readiness_rejects_missing_manifest_content(tmp_path):
    papers = tmp_path / "papers"
    papers.mkdir()
    document, _digest = _document(b"%PDF-1.7\nmissing", "doi:10.1/missing")
    manifest = {"manifest_sha256": "a" * 64, "documents": [document]}

    with pytest.raises(ValueError, match="不存在"):
        calculate_benchmark_v2_readiness(
            papers, manifest, [], minimum_new_papers=12
        )


def test_calculate_readiness_cannot_lower_minimum(tmp_path):
    papers = tmp_path / "papers"
    papers.mkdir()

    with pytest.raises(ValueError, match="至少需要 12"):
        calculate_benchmark_v2_readiness(
            papers,
            {"manifest_sha256": "a" * 64, "documents": []},
            [],
            minimum_new_papers=1,
        )


def test_readiness_endpoint_strips_internal_identity_fields(client, monkeypatch):
    payload = {
        "status": "WAIT",
        "ready": False,
        "minimum_new_papers": 12,
        "missing_new_papers": 11,
        "physical_pdf_files": 36,
        "unique_pdf_contents": 19,
        "duplicate_pdf_files": 17,
        "covered_unique_contents": 18,
        "eligible_imported_papers": 1,
        "unimported_unique_contents": 0,
        "error_code": None,
        "paper_uid": "doi:10.1/secret",
        "pdf_sha256": "f" * 64,
        "path": "/private/papers/secret.pdf",
    }
    monkeypatch.setattr(
        "app.routers.readiness.get_benchmark_v2_readiness",
        lambda _db: payload,
    )

    response = client.get("/api/readiness/benchmark-v2")

    assert response.status_code == 200
    assert response.json() == {key: payload[key] for key in PUBLIC_READINESS_FIELDS}
    rendered = response.text
    assert "paper_uid" not in rendered
    assert "pdf_sha256" not in rendered
    assert "secret.pdf" not in rendered


def test_readiness_endpoint_fails_closed_without_leaking_exception(client, monkeypatch):
    def broken(_db):
        raise ValueError("/private/papers/secret-title.pdf sha256=deadbeef")

    monkeypatch.setattr(
        "app.routers.readiness.get_benchmark_v2_readiness", broken
    )

    response = client.get("/api/readiness/benchmark-v2")

    assert response.status_code == 200
    assert response.json() == {
        "status": "UNAVAILABLE",
        "ready": False,
        "minimum_new_papers": 12,
        "missing_new_papers": None,
        "physical_pdf_files": None,
        "unique_pdf_contents": None,
        "duplicate_pdf_files": None,
        "covered_unique_contents": None,
        "eligible_imported_papers": None,
        "unimported_unique_contents": None,
        "error_code": "benchmark_data_unavailable",
    }
    assert "secret-title" not in response.text
    assert "deadbeef" not in response.text
