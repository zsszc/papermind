"""Batch 28：完整 train route-depth 聚合与隐私契约。"""

from __future__ import annotations

from copy import deepcopy

import pytest

from eval.route_depth_diagnostics import (
    analyze_route_depth,
    validate_route_depth_records,
)


def _binding() -> dict:
    return {
        "git_sha": "a" * 40,
        "git_tracked_clean": True,
        "dataset_sha256": "1" * 64,
        "qrels_sha256": "2" * 64,
        "corpus_manifest_sha256": "3" * 64,
        "database_logical_manifest_sha256": "3" * 64,
        "page_text_manifest_sha256": "4" * 64,
        "vector_manifest_sha256": "5" * 64,
        "hnsw_config_sha256": "6" * 64,
        "hnsw_binary_manifest_sha256": "7" * 64,
        "split": "train",
        "evidence_resolver": "page-span-v2",
        "lexical_profile": "bm25-bilingual",
        "top_k": 5,
        "production_route_limit": 10,
        "diagnostic_route_limit": 20,
    }


def _group(chunk_id: str) -> dict:
    return {
        "page_start": 10,
        "page_end": 20,
        "chunks": [{
            "chunk_id": chunk_id,
            "page_start": 0,
            "page_end": 30,
        }],
    }


def _record(index: int, category: str) -> dict:
    qtype = "factoid" if index < 8 else "method_detail" if index < 12 else "summary"
    relevant = f"p{index + 1}_c1"
    semantic = [f"p{100 + index}_c{rank}" for rank in range(20)]
    keyword = [f"p{200 + index}_c{rank}" for rank in range(20)]
    baseline = semantic[:3] + keyword[:2]
    if category == "baseline_full":
        semantic[0] = relevant
        baseline[0] = relevant
    elif category == "deep_route_recoverable":
        semantic[7] = relevant
    elif category == "correct_paper_only":
        semantic[0] = f"p{index + 1}_c9"
        baseline[0] = semantic[0]
    elif category != "paper_absent":
        raise AssertionError(category)
    return {
        "question_type": qtype,
        "evidence_groups": [_group(relevant)],
        "semantic_ids": semantic,
        "lexical_ids": keyword,
        "baseline_ids": baseline,
    }


def _records() -> list[dict]:
    categories = (
        ["baseline_full"] * 5
        + ["deep_route_recoverable"] * 4
        + ["correct_paper_only"] * 3
        + ["paper_absent"]
    )
    return [_record(index, category) for index, category in enumerate(categories)]


def test_aggregate_classifies_depth_and_selects_single_candidate():
    report = analyze_route_depth(_records(), _binding())

    assert report["schema"] == "route-depth-diagnostics-v1"
    assert report["total_items"] == 13
    assert report["categories"] == [
        {"category": "baseline_full", "count": 5, "share": 5 / 13},
        {"category": "deep_route_recoverable", "count": 4, "share": 4 / 13},
        {"category": "correct_paper_only", "count": 3, "share": 3 / 13},
        {"category": "paper_absent", "count": 1, "share": 1 / 13},
    ]
    assert report["routes"]["semantic"]["first_hit_depth"] == {
        "1-5": 5,
        "6-10": 4,
        "11-20": 0,
        "not_found": 4,
    }
    assert report["recommendation"]["candidate"] == (
        "paper-preserving-deep-route-v1"
    )
    assert report["recommendation"]["support_count"] == 4


def test_span_metrics_distinguish_top5_top10_and_top20():
    records = _records()
    report = analyze_route_depth(records, _binding())

    semantic = report["routes"]["semantic"]
    assert semantic["any_hit@5"] == 5 / 13
    assert semantic["any_hit@10"] == 9 / 13
    assert semantic["any_hit@20"] == 9 / 13
    assert semantic["span_coverage@5"] == 5 / 13
    assert semantic["span_coverage@10"] == 9 / 13
    assert semantic["span_coverage@20"] == 9 / 13


def test_analysis_is_deterministic_does_not_mutate_and_emits_no_identity():
    records = _records()
    original = deepcopy(records)

    first = analyze_route_depth(records, _binding())
    second = analyze_route_depth(records, _binding())

    assert first == second
    assert records == original
    rendered = str(first).lower()
    for forbidden in (
        "qa_id", "chunk_id", "question", "content", "paper_uid",
        "doi:", "p1_c1", "/users/",
    ):
        assert forbidden not in rendered


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        (lambda rows, binding: rows.pop(), "13"),
        (
            lambda rows, binding: rows[0]["semantic_ids"].append(
                rows[0]["semantic_ids"][0]
            ),
            "重复",
        ),
        (
            lambda rows, binding: rows[0]["semantic_ids"].__setitem__(0, "bad"),
            "畸形",
        ),
        (
            lambda rows, binding: rows[0]["baseline_ids"].__setitem__(0, "p999_c0"),
            "基线",
        ),
        (lambda rows, binding: binding.update(git_tracked_clean=False), "clean"),
        (lambda rows, binding: binding.update(split="dev"), "train"),
        (
            lambda rows, binding: binding.update(lexical_profile="count"),
            "bm25-bilingual",
        ),
    ],
)
def test_invalid_or_incomplete_inputs_fail_closed(mutation, match):
    records = _records()
    binding = _binding()
    mutation(records, binding)

    with pytest.raises(ValueError, match=match):
        validate_route_depth_records(records, binding)


def test_tied_failure_categories_follow_preregistered_priority():
    records = _records()
    # 将一个 deep 改成 paper-only，使 deep/paper-only 同为 3，仍选 deep。
    records[5] = _record(5, "correct_paper_only")

    report = analyze_route_depth(records, _binding())

    assert report["recommendation"]["dominant_failure"] == (
        "deep_route_recoverable"
    )
    assert report["recommendation"]["candidate"] == (
        "paper-preserving-deep-route-v1"
    )
