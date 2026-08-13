"""Batch 17：私有真实语料 manifest、稳定 UID 与隐私边界。"""

import hashlib
from pathlib import Path

import pytest

from app.models import Chunk, Paper
from eval.dataset import resolve_relevant_chunks, validate_dataset
from eval.private_benchmark import (
    audit_corpus,
    normalize_doi,
    public_summary,
    validate_private_dataset,
)


def _add_paper(db, tmp_path, *, title, content, doi=None, duplicate=False):
    papers_dir = tmp_path / "papers"
    papers_dir.mkdir(exist_ok=True)
    filename = f"{title}.pdf"
    payload = b"%PDF-1.7\n" + content.encode("utf-8")
    (papers_dir / filename).write_bytes(payload)
    if duplicate:
        (papers_dir / f"{title}_copy.pdf").write_bytes(payload)
    paper = Paper(
        title=title,
        filename=filename,
        file_path=f"papers/{filename}",
        doi=doi,
        processed="done",
    )
    db.add(paper)
    db.flush()
    db.add(Chunk(paper_id=paper.id, chunk_index=0, content=content))
    db.commit()
    return paper, hashlib.sha256(payload).hexdigest()


def test_corpus_audit_deduplicates_without_modifying_files(db, tmp_path):
    _add_paper(db, tmp_path, title="one", content="unique evidence one", duplicate=True)
    _add_paper(db, tmp_path, title="two", content="unique evidence two")
    before = sorted((p.name, p.read_bytes()) for p in (tmp_path / "papers").iterdir())

    manifest = audit_corpus(db, tmp_path)

    after = sorted((p.name, p.read_bytes()) for p in (tmp_path / "papers").iterdir())
    assert before == after
    assert manifest["physical_pdf_files"] == 3
    assert manifest["unique_pdf_contents"] == 2
    assert manifest["duplicate_pdf_files"] == 1
    assert manifest["database_papers"] == 2
    assert manifest["chunks"] == 2
    assert manifest["missing_source_files"] == []
    assert len(manifest["manifest_sha256"]) == 64


def test_public_summary_contains_no_titles_or_paths(db, tmp_path):
    _add_paper(db, tmp_path, title="secret-title", content="private evidence text")
    summary = public_summary(audit_corpus(db, tmp_path))
    rendered = str(summary)
    assert "secret-title" not in rendered
    assert str(tmp_path) not in rendered
    assert set(summary) == {
        "manifest_sha256", "physical_pdf_files", "unique_pdf_contents",
        "duplicate_pdf_files", "database_papers", "processed_done",
        "chunks", "missing_source_file_count",
    }


def test_manifest_fingerprint_changes_when_chunk_content_changes(db, tmp_path):
    paper, _ = _add_paper(
        db, tmp_path, title="fingerprint", content="original unique chunk content"
    )
    before = audit_corpus(db, tmp_path)["manifest_sha256"]
    paper.chunks[0].content = "changed unique chunk content"
    db.commit()
    after = audit_corpus(db, tmp_path)["manifest_sha256"]
    assert before != after


def test_doi_is_canonicalized_and_sha256_uid_resolves_unique_evidence(db, tmp_path):
    doi_paper, _ = _add_paper(
        db,
        tmp_path,
        title="doi-paper",
        doi=" HTTPS://DOI.ORG/10.1109/TMI.2022.3202759. ",
        content="This DOI evidence sentence is uniquely present in this paper.",
    )
    hash_paper, digest = _add_paper(
        db,
        tmp_path,
        title="hash-paper",
        content="This hash evidence sentence is uniquely present in this paper.",
    )
    assert normalize_doi(doi_paper.doi) == "10.1109/tmi.2022.3202759"

    items = [
        {
            "qa_id": "doi-1", "question": "q", "ground_truth": "a",
            "relevant_evidence": [{
                "paper_uid": "doi:10.1109/tmi.2022.3202759",
                "quote": "DOI evidence sentence is uniquely present",
            }],
            "question_type": "factoid", "source": "imported_paper", "has_answer": True,
        },
        {
            "qa_id": "sha-1", "question": "q", "ground_truth": "a",
            "relevant_evidence": [{
                "paper_uid": f"sha256:{digest}",
                "quote": "hash evidence sentence is uniquely present",
            }],
            "question_type": "factoid", "source": "imported_paper", "has_answer": True,
        },
    ]
    validate_dataset(items)
    assert resolve_relevant_chunks(db, items[0], runtime_root=tmp_path) == [
        f"p{doi_paper.id}_c0"
    ]
    assert resolve_relevant_chunks(db, items[1], runtime_root=tmp_path) == [
        f"p{hash_paper.id}_c0"
    ]


def test_sha_uid_and_evidence_fail_closed(db, tmp_path):
    _, digest = _add_paper(
        db,
        tmp_path,
        title="ambiguous",
        content="Repeated evidence phrase long enough. Repeated evidence phrase long enough.",
    )
    invalid = [{
        "qa_id": "bad", "question": "q", "ground_truth": "a",
        "relevant_evidence": [{"paper_uid": "sha256:not-a-hash", "quote": "x" * 20}],
        "question_type": "factoid", "source": "imported_paper", "has_answer": True,
    }]
    with pytest.raises(ValueError, match="sha256"):
        validate_dataset(invalid)

    ambiguous = {
        **invalid[0],
        "relevant_evidence": [{
            "paper_uid": f"sha256:{digest}",
            "quote": "Repeated evidence phrase long enough.",
        }],
    }
    with pytest.raises(ValueError, match="多处命中"):
        resolve_relevant_chunks(db, ambiguous, runtime_root=tmp_path)


def test_private_eval_paths_are_gitignored():
    root = Path(__file__).resolve().parents[2]
    ignore = (root / ".gitignore").read_text(encoding="utf-8")
    assert "backend/eval/private/" in ignore
    assert "backend/eval/dataset/qa_candidates.jsonl" in ignore


def _private_item(qa_id, uid, split="train", reviewed=True):
    return {
        "qa_id": qa_id,
        "question": "问题",
        "ground_truth": "答案",
        "relevant_evidence": [{
            "paper_uid": uid,
            "quote": "This is a uniquely identifying evidence quote.",
        }],
        "question_type": "factoid",
        "source": "imported_paper",
        "has_answer": True,
        "reviewed": reviewed,
        "split": split,
    }


def test_private_dataset_requires_review_stable_uid_coverage_and_paper_split():
    valid = [
        _private_item("q1", "sha256:" + "a" * 64, "train"),
        _private_item("q2", "doi:10.1/example", "dev"),
    ]
    summary = validate_private_dataset(valid, min_items=2, min_papers=2)
    assert summary["items"] == 2
    assert summary["papers"] == 2
    assert summary["splits"] == {"dev": 1, "train": 1}

    with pytest.raises(ValueError, match="人工审稿"):
        validate_private_dataset(
            [_private_item("q1", "sha256:" + "a" * 64, reviewed=False)],
            min_items=1,
            min_papers=1,
        )
    with pytest.raises(ValueError, match="跨 split"):
        validate_private_dataset([
            _private_item("q1", "sha256:" + "a" * 64, "train"),
            _private_item("q2", "sha256:" + "a" * 64, "holdout"),
        ], min_items=2, min_papers=1)
    with pytest.raises(ValueError, match="覆盖论文"):
        validate_private_dataset(valid, min_items=2, min_papers=3)
