"""Batch 22J 真实语料 Benchmark v2 盲化 Harness RED。"""

from __future__ import annotations

import json

import pytest

from eval.benchmark_v2 import (
    audit_v2_coverage,
    audit_v2_evidence,
    build_v2_freeze_artifact,
    consume_split_once,
    evaluate_v2_readiness,
    freeze_paper_splits,
    public_coverage_summary,
    validate_v2_dataset,
)
from eval import run


def _item(qa_id: str, uid: str, *, split: str = "train") -> dict:
    return {
        "qa_id": qa_id,
        "question": "私有问题原文",
        "ground_truth": "私有参考答案",
        "relevant_evidence": [{
            "paper_uid": uid,
            "quote": "This is one uniquely identifying evidence sentence.",
        }],
        "question_type": "factoid",
        "source": "imported_paper",
        "has_answer": True,
        "reviewed": True,
        "split": split,
    }


def _manifest(documents: list[dict]) -> dict:
    import hashlib
    payload = json.dumps(
        sorted(documents, key=lambda item: item.get("paper_uid") or ""),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "manifest_sha256": hashlib.sha256(payload).hexdigest(),
        "documents": documents,
    }


def test_coverage_audit_maps_every_copy_without_names_or_content(tmp_path):
    papers = tmp_path / "papers"
    papers.mkdir()
    covered = b"%PDF-1.7\ncovered"
    eligible = b"%PDF-1.7\neligible"
    unimported = b"%PDF-1.7\nunimported"
    (papers / "secret-a.pdf").write_bytes(covered)
    (papers / "secret-a-copy.pdf").write_bytes(covered)
    (papers / "secret-b.pdf").write_bytes(eligible)
    (papers / "secret-c.pdf").write_bytes(unimported)

    import hashlib
    covered_sha = hashlib.sha256(covered).hexdigest()
    eligible_sha = hashlib.sha256(eligible).hexdigest()
    manifest = _manifest([
        {"paper_uid": "doi:10.1/covered", "pdf_sha256": covered_sha},
        {"paper_uid": f"sha256:{eligible_sha}", "pdf_sha256": eligible_sha},
    ])
    audit = audit_v2_coverage(
        papers, manifest, [_item("old", "doi:10.1/covered")]
    )

    assert audit["physical_pdf_files"] == 4
    assert audit["unique_pdf_contents"] == 3
    assert audit["duplicate_pdf_files"] == 1
    assert audit["covered_unique_contents"] == 1
    assert audit["eligible_imported_papers"] == 1
    assert audit["unimported_unique_contents"] == 1
    assert len(audit["files"]) == 4
    rendered = json.dumps(audit, ensure_ascii=False)
    assert "secret-" not in rendered
    assert "%PDF-1.7" not in rendered
    assert all(len(row["copy_token_sha256"]) == 64 for row in audit["files"])
    public = json.dumps(public_coverage_summary(audit), ensure_ascii=False)
    assert "paper_uid" not in public
    assert "pdf_sha256" not in public


def test_coverage_fails_closed_on_uid_or_content_identity_ambiguity(tmp_path):
    papers = tmp_path / "papers"
    papers.mkdir()
    payload = b"%PDF-1.7\nsame"
    (papers / "a.pdf").write_bytes(payload)
    import hashlib
    digest = hashlib.sha256(payload).hexdigest()

    with pytest.raises(ValueError, match="多个 paper_uid"):
        audit_v2_coverage(papers, _manifest([
            {"paper_uid": "doi:10.1/a", "pdf_sha256": digest},
            {"paper_uid": "doi:10.1/b", "pdf_sha256": digest},
        ]), [])

    with pytest.raises(ValueError, match="paper_uid.*多个 PDF SHA"):
        audit_v2_coverage(papers, _manifest([
            {"paper_uid": "doi:10.1/a", "pdf_sha256": digest},
            {"paper_uid": "doi:10.1/a", "pdf_sha256": "f" * 64},
        ]), [])

    with pytest.raises(ValueError, match="v1.*未命中"):
        audit_v2_coverage(
            papers,
            _manifest([{"paper_uid": "doi:10.1/a", "pdf_sha256": digest}]),
            [_item("old", "doi:10.1/missing")],
        )


def test_coverage_rejects_pdf_symlink(tmp_path):
    papers = tmp_path / "papers"
    papers.mkdir()
    source = tmp_path / "outside.pdf"
    source.write_bytes(b"%PDF-1.7\noutside")
    (papers / "linked.pdf").symlink_to(source)

    with pytest.raises(ValueError, match="软链接"):
        audit_v2_coverage(papers, _manifest([]), [])


def test_readiness_gate_requires_twelve_new_unique_imported_papers(tmp_path):
    papers = tmp_path / "papers"
    papers.mkdir()
    documents = []
    import hashlib
    for index in range(3):
        payload = f"%PDF-1.7\n{index}".encode()
        (papers / f"paper-{index}.pdf").write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        documents.append({
            "paper_uid": f"sha256:{digest}", "pdf_sha256": digest,
        })
    audit = audit_v2_coverage(papers, _manifest(documents), [])

    gate = evaluate_v2_readiness(audit, min_new_papers=12)

    assert gate["passed"] is False
    assert gate["checks"]["new_unique_imported_papers"] == {
        "actual": 3, "threshold": 12, "operator": ">=", "passed": False,
    }


def test_v2_dataset_rejects_v1_overlap_and_noneligible_paper():
    old_uid = "sha256:" + "1" * 64
    new_uid = "sha256:" + "2" * 64
    other_uid = "sha256:" + "3" * 64

    coverage = {
        "eligible_documents": [{
            "paper_uid": new_uid, "pdf_sha256": "2" * 64,
        }],
        "v1_paper_uids": [old_uid],
        "v1_pdf_sha256s": ["1" * 64],
    }
    with pytest.raises(ValueError, match="v1.*重叠"):
        validate_v2_dataset(
            [_item("new", old_uid)], [_item("old", old_uid)], coverage,
            min_items=1, min_papers=1,
        )
    with pytest.raises(ValueError, match="未覆盖且已导入"):
        validate_v2_dataset(
            [_item("new", other_uid)], [_item("old", old_uid)], coverage,
            min_items=1, min_papers=1,
        )

    summary = validate_v2_dataset(
        [_item("new", new_uid)], [_item("old", old_uid)], coverage,
        min_items=1, min_papers=1,
    )
    assert summary["papers"] == 1


def test_evidence_audit_requires_every_positive_to_resolve_uniquely():
    items = [_item("q1", "doi:10.1/a"), _item("q2", "doi:10.1/b")]
    calls = []

    def resolver(_db, item, *, runtime_root):
        calls.append((item["qa_id"], runtime_root))
        if item["qa_id"] == "q2":
            raise ValueError("证据多处命中")
        return [{
            "paper_uid": "doi:10.1/a",
            "chunks": [{"chunk_id": "p1_c0"}],
        }]

    with pytest.raises(ValueError, match="q2"):
        audit_v2_evidence(object(), items, "/private/root", resolver=resolver)
    assert [qa_id for qa_id, _root in calls] == ["q1", "q2"]


def test_freeze_artifact_hashes_private_fields_and_binds_split_identity():
    uid = "sha256:" + "2" * 64
    items = [_item("new", uid)]
    coverage = {
        "coverage_manifest_sha256": "b" * 64,
        "eligible_paper_uids_sha256": "c" * 64,
    }
    artifact = build_v2_freeze_artifact(
        items,
        coverage,
        dataset_bytes=b"private-jsonl-bytes",
        corpus_manifest_sha256="d" * 64,
        database_logical_manifest_sha256="d" * 64,
        page_text_manifest_sha256="e" * 64,
        vector_manifest_sha256="f" * 64,
    )

    rendered = json.dumps(artifact, ensure_ascii=False)
    assert "私有问题原文" not in rendered
    assert "uniquely identifying" not in rendered
    assert artifact["splits"] == {"train": 1}
    assert len(artifact["dataset_sha256"]) == 64
    assert len(artifact["qrels_sha256"]) == 64
    assert len(artifact["paper_split_sha256"]) == 64
    import hashlib
    assert artifact["dataset_sha256"] == hashlib.sha256(
        b"private-jsonl-bytes"
    ).hexdigest()
    assert artifact["question_type_counts"] == {"factoid": 1}
    assert artifact["paper_split_counts"] == {"train": 1}


def test_paper_split_is_frozen_before_qa_and_is_write_once(tmp_path):
    documents = [
        {"paper_uid": f"doi:10.1/{split}", "pdf_sha256": str(index) * 64}
        for index, split in enumerate(("train", "dev", "holdout"), start=1)
    ]
    coverage = {"eligible_documents": documents}
    assignments = [
        {"paper_uid": row["paper_uid"], "pdf_sha256": row["pdf_sha256"],
         "split": row["paper_uid"].rsplit("/", 1)[-1]}
        for row in documents
    ]
    output = tmp_path / "paper-splits.json"

    frozen = freeze_paper_splits(
        coverage, assignments, output, min_papers_per_split=1
    )

    assert frozen["paper_counts"] == {"dev": 1, "holdout": 1, "train": 1}
    assert len(frozen["paper_split_sha256"]) == 64
    with pytest.raises(FileExistsError):
        freeze_paper_splits(
            coverage, assignments, output, min_papers_per_split=1
        )


def test_split_ledger_is_write_once_and_holdout_needs_preregistered_gate(tmp_path):
    freeze = {
        "freeze_schema": "private-benchmark-v2-freeze-v1",
        "freeze_sha256": "a" * 64,
        "dataset_sha256": "b" * 64,
        "holdout_gate_sha256": "c" * 64,
    }
    ledger = consume_split_once(freeze, "train", "production-baseline", tmp_path)
    assert ledger["split"] == "train"
    with pytest.raises(FileExistsError):
        consume_split_once(freeze, "train", "production-baseline", tmp_path)

    with pytest.raises(ValueError, match="holdout.*预注册"):
        consume_split_once(
            freeze, "holdout", "candidate-gate", tmp_path
        )
    allowed = consume_split_once(
        freeze, "holdout", "candidate-gate", tmp_path,
        preregistered_gate_sha256="c" * 64,
    )
    assert allowed["preregistered_gate_sha256"] == "c" * 64


def test_generic_eval_cli_can_never_consume_holdout():
    args = run.build_parser().parse_args([
        "--split", "holdout", "--keyword-only",
    ])
    assert "通用 CLI 禁止 holdout" in run._validate_cli_args(args)
