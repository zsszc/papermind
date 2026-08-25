"""Batch 23A 公开离线生成 Guardrail Harness RED。"""

from __future__ import annotations

import json

import pytest

from app.services.generation_guardrails import (
    verify_citations,
    verify_citations_detailed,
)
from eval.generation_guardrails import (
    build_generation_guardrail_gate,
    evaluate_generation_cases,
)


def _chunk(chunk_id: str) -> dict:
    return {
        "chunk_id": chunk_id,
        "source": chunk_id,
        "paper_id": int(chunk_id.split("_")[0][1:]),
        "content": "公开合成证据",
    }


def test_detailed_verifier_maps_production_markers_and_preserves_compatibility():
    chunks = [_chunk("p1_c0"), _chunk("p2_c0")]
    answer = "甲[^1^]，重复[^1^]，越界[^9^]，格式错误[^abc^]。"

    cleaned, report, cited_ids = verify_citations_detailed(answer, chunks)

    assert cleaned == "甲[^1^]，重复[^1^]，越界，格式错误。"
    assert cited_ids == ["p1_c0"]
    assert report == {
        "total": 4,
        "valid": 2,
        "removed": 2,
        "verified": False,
        "unique_valid": 1,
        "duplicate_valid": 1,
        "out_of_range": 1,
        "malformed": 1,
    }
    compat_cleaned, compat_report = verify_citations(answer, chunks)
    assert compat_cleaned == cleaned
    assert compat_report == {
        "total": 4, "valid": 2, "removed": 2, "verified": False,
    }


def test_offline_metrics_are_hand_calculable_and_negative_citations_fail_safe_refusal():
    cases = [
        {
            "case_id": "positive-mixed",
            "has_answer": True,
            "retrieved_chunks": [_chunk("p1_c0"), _chunk("p2_c0")],
            "relevant_chunk_ids": ["p1_c0"],
            "answer": "正确[^1^]，但又引用无关证据[^2^]，并越界[^9^]。",
        },
        {
            "case_id": "positive-partial",
            "has_answer": True,
            "retrieved_chunks": [_chunk("p3_c0"), _chunk("p4_c0")],
            "relevant_chunk_ids": ["p3_c0", "p4_c0"],
            "answer": "只覆盖一半[^1^]。",
        },
        {
            "case_id": "negative-safe",
            "has_answer": False,
            "retrieved_chunks": [],
            "relevant_chunk_ids": [],
            "answer": "文献库中没有相关内容。",
        },
        {
            "case_id": "negative-unsafe",
            "has_answer": False,
            "retrieved_chunks": [_chunk("p5_c0")],
            "relevant_chunk_ids": [],
            "answer": "不知道，但可能如此[^1^]。",
        },
    ]

    report = evaluate_generation_cases(cases)

    overall = report["overall"]
    # 每个引用声明都进入 precision 分母：mixed 为 1/3，partial 为 1，宏平均 2/3。
    assert overall["citation_precision"] == pytest.approx(2 / 3)
    assert overall["citation_recall"] == pytest.approx(0.75)
    assert overall["citation_f1"] == pytest.approx(7 / 12)
    assert overall["out_of_bounds_citation_count"] == 1
    assert overall["negative_refusal_rate"] == pytest.approx(0.5)
    assert overall["negative_citation_count"] == 1
    unsafe = next(item for item in report["cases"] if item["case_id"] == "negative-unsafe")
    assert unsafe["refused"] is True
    assert unsafe["safe_refusal"] is False
    rendered = json.dumps(report, ensure_ascii=False)
    assert "正确" not in rendered
    assert "公开合成证据" not in rendered


def test_generation_gate_requires_no_boundary_violations_and_safe_refusal():
    report = {
        "overall": {
            "citation_precision": 1.0,
            "citation_recall": 1.0,
            "citation_f1": 1.0,
            "out_of_bounds_citation_count": 1,
            "malformed_citation_count": 0,
            "duplicate_citation_count": 0,
            "negative_refusal_rate": 1.0,
            "negative_citation_count": 0,
        }
    }

    gate = build_generation_guardrail_gate(report)

    assert gate["passed"] is False
    assert gate["checks"]["out_of_bounds_citation_count"]["passed"] is False
