"""Batch 34：全语料 QA v3 覆盖与隔离契约。"""

import json

import pytest

from eval.full_corpus_qa import (
    _load_assignments_artifact,
    assemble_full_corpus_dataset,
    build_parser,
    build_gap_plan,
    merge_supplements,
    public_gap_summary,
    validate_full_coverage,
)


def _assignment(uid: str, split: str) -> dict:
    return {"paper_uid": uid, "pdf_sha256": uid[-64:], "split": split}


def _item(qa_id: str, uid: str, split: str, question: str) -> dict:
    return {
        "qa_id": qa_id,
        "question": question,
        "ground_truth": "参考答案",
        "relevant_evidence": [
            {
                "paper_uid": uid,
                "quote": "唯一证据文本长度必须至少达到二十个字符。",
            }
        ],
        "question_type": "method_detail",
        "source": "imported_paper",
        "has_answer": True,
        "split": split,
    }


def test_gap_plan_only_returns_undercovered_papers_and_public_summary_is_anonymous():
    train_uid = "sha256:" + "a" * 64
    dev_uid = "sha256:" + "b" * 64
    holdout_uid = "sha256:" + "c" * 64
    assignments = [
        _assignment(train_uid, "train"),
        _assignment(dev_uid, "dev"),
        _assignment(holdout_uid, "holdout"),
    ]
    items = [
        _item("a1", train_uid, "train", "训练问题一？"),
        _item("a2", train_uid, "train", "训练问题二？"),
        _item("b1", dev_uid, "dev", "开发问题一？"),
    ]

    plan = build_gap_plan(assignments, items, minimum_per_paper=2)

    assert [(row["split"], row["current"], row["needed"]) for row in plan] == [
        ("dev", 1, 1),
        ("holdout", 0, 2),
    ]
    summary = public_gap_summary(plan)
    assert summary == {
        "gap_papers": 2,
        "required_new_qa": 3,
        "by_split": {
            "dev": {"gap_papers": 1, "required_new_qa": 1},
            "holdout": {"gap_papers": 1, "required_new_qa": 2},
        },
    }
    assert train_uid not in str(summary)
    assert dev_uid not in str(summary)
    assert holdout_uid not in str(summary)


def test_full_coverage_accepts_two_unique_questions_per_frozen_paper():
    train_uid = "sha256:" + "a" * 64
    dev_uid = "sha256:" + "b" * 64
    assignments = [_assignment(train_uid, "train"), _assignment(dev_uid, "dev")]
    items = [
        _item("a1", train_uid, "train", "训练问题一？"),
        _item("a2", train_uid, "train", "训练问题二？"),
        _item("b1", dev_uid, "dev", "开发问题一？"),
        _item("b2", dev_uid, "dev", "开发问题二？"),
    ]

    assert validate_full_coverage(items, assignments, minimum_per_paper=2) == {
        "items": 4,
        "papers": 2,
        "minimum_per_paper": 2,
        "split_items": {"dev": 2, "train": 2},
        "split_papers": {"dev": 1, "train": 1},
    }


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda items: items.__setitem__(1, {**items[1], "split": "dev"}), "split"),
        (
            lambda items: items.__setitem__(
                1, {**items[1], "question": " 训练问题一？ "}
            ),
            "问题文本重复",
        ),
        (lambda items: items.pop(), "覆盖不足"),
    ],
)
def test_full_coverage_rejects_split_leak_duplicate_question_or_gap(mutate, message):
    uid = "sha256:" + "a" * 64
    assignments = [_assignment(uid, "train")]
    items = [
        _item("a1", uid, "train", "训练问题一？"),
        _item("a2", uid, "train", "训练问题二？"),
    ]
    mutate(items)

    with pytest.raises(ValueError, match=message):
        validate_full_coverage(items, assignments, minimum_per_paper=2)


def test_merge_supplements_requires_the_exact_frozen_gap():
    train_uid = "sha256:" + "a" * 64
    dev_uid = "sha256:" + "b" * 64
    assignments = [
        _assignment(train_uid, "train"),
        _assignment(dev_uid, "dev"),
    ]
    existing = [
        _item("a1", train_uid, "train", "训练既有问题？"),
        _item("b1", dev_uid, "dev", "开发既有问题？"),
    ]
    supplements = [
        _item("a2", train_uid, "train", "训练补充问题？"),
        _item("b2", dev_uid, "dev", "开发补充问题？"),
    ]

    merged = merge_supplements(existing, supplements, assignments)

    assert [item["qa_id"] for item in merged] == ["a1", "b1", "a2", "b2"]
    with pytest.raises(ValueError, match="补题数量"):
        merge_supplements(existing, supplements[:-1], assignments)
    with pytest.raises(ValueError, match="补题目标"):
        merge_supplements(
            existing,
            supplements + [_item("a3", train_uid, "train", "不应超额的问题？")],
            assignments,
        )


def test_assemble_full_corpus_dataset_moves_consumed_legacy_items_to_train():
    legacy_uid = "sha256:" + "c" * 64
    v2_uid = "sha256:" + "d" * 64
    legacy_items = [
        _item("legacy-1", legacy_uid, "dev", "历史问题一？"),
        _item("legacy-2", legacy_uid, "holdout", "历史问题二？"),
    ]
    legacy_manifest = {
        "documents": [
            {
                "paper_uid": legacy_uid,
                "pdf_sha256": "c" * 64,
            }
        ]
    }
    v2_assignments = [_assignment(v2_uid, "dev")]
    v2_items = [
        _item("v2-1", v2_uid, "dev", "新问题一？"),
        _item("v2-2", v2_uid, "dev", "新问题二？"),
    ]

    combined, assignments = assemble_full_corpus_dataset(
        legacy_items,
        legacy_manifest,
        v2_items,
        v2_assignments,
    )

    assert [item["split"] for item in combined[:2]] == ["train", "train"]
    assert {row["split"] for row in assignments if row["paper_uid"] == legacy_uid} == {
        "train"
    }
    assert validate_full_coverage(combined, assignments)["papers"] == 2


def test_v3_assignment_artifact_round_trip(tmp_path):
    uid = "sha256:" + "e" * 64
    path = tmp_path / "assignments.json"
    path.write_text(
        json.dumps(
            {
                "split_schema": "full-corpus-qa-v3-paper-splits-v1",
                "assignments": [_assignment(uid, "train")],
            }
        ),
        encoding="utf-8",
    )

    assert _load_assignments_artifact(path) == [_assignment(uid, "train")]


def test_validate_cli_supports_explicit_readonly_candidate_database():
    args = build_parser().parse_args(
        [
            "validate",
            "--splits",
            "eval/private/assignments.json",
            "--dataset",
            "eval/private/qa.jsonl",
            "--resolve-evidence",
            "--database",
            "eval/private/candidate.db",
            "--corpus-root",
            "..",
        ]
    )

    assert args.database.endswith("candidate.db")
    assert args.corpus_root == ".."
