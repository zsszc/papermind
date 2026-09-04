"""Batch 34：全语料 QA v3 覆盖与隔离契约。"""

import pytest

from eval.full_corpus_qa import (
    build_gap_plan,
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
        "relevant_evidence": [{"paper_uid": uid, "quote": "唯一证据文本长度必须至少达到二十个字符。"}],
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
        (lambda items: items.__setitem__(1, {**items[1], "question": " 训练问题一？ "}), "问题文本重复"),
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
