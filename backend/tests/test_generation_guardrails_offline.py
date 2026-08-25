"""Batch 23A 公开离线生成 Guardrail Harness RED。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.generation_guardrails import (
    build_rag_prompt,
    verify_citations,
    verify_citations_detailed,
)
from eval.generation_guardrails import (
    build_generation_guardrail_gate,
    evaluate_generation_cases,
    run_public_generation_gate,
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


@pytest.mark.parametrize(
    ("answer", "expected_cleaned", "out_of_range", "malformed"),
    [
        ("a[^0^]b", "ab", 1, 0),
        ("a[^-1^]b", "ab", 1, 0),
        ("a[^2^]b", "ab", 1, 0),
        ("a[^abc^]b", "ab", 0, 1),
    ],
)
def test_verifier_fails_closed_on_marker_boundaries(
    answer, expected_cleaned, out_of_range, malformed
):
    cleaned, report, cited_ids = verify_citations_detailed(answer, [_chunk("p1_c0")])

    assert cleaned == expected_cleaned
    assert cited_ids == []
    assert report["out_of_range"] == out_of_range
    assert report["malformed"] == malformed


def test_verifier_rejects_legacy_marker_and_ambiguous_chunk_identity():
    duplicate_chunks = [_chunk("p1_c0"), _chunk("p1_c0")]

    cleaned, report, cited_ids = verify_citations_detailed(
        "旧协议[p9_c9]，歧义[^1^]。", duplicate_chunks
    )

    assert cleaned == "旧协议，歧义。"
    assert cited_ids == []
    assert report["malformed"] == 2
    assert report["total"] == 2


@pytest.mark.parametrize(
    ("rate", "passed"),
    [(0.90, True), (0.899, False)],
)
def test_negative_refusal_threshold_is_inclusive(rate, passed):
    report = {
        "overall": {
            "citation_precision": 1.0,
            "citation_recall": 1.0,
            "citation_f1": 1.0,
            "out_of_bounds_citation_count": 0,
            "malformed_citation_count": 0,
            "duplicate_citation_count": 0,
            "negative_refusal_rate": rate,
            "negative_citation_count": 0,
        }
    }

    assert build_generation_guardrail_gate(report)["passed"] is passed


def test_generation_error_cannot_masquerade_as_safe_refusal():
    report = evaluate_generation_cases([{
        "case_id": "negative-error",
        "has_answer": False,
        "retrieved_chunks": [],
        "relevant_chunk_ids": [],
        "answer": "文献库中没有相关内容。",
        "generation_error": "sanitized-error",
    }])

    assert report["cases"][0]["refused"] is False
    assert report["cases"][0]["safe_refusal"] is False
    assert report["overall"]["negative_refusal_rate"] == 0.0


def test_public_generation_report_is_content_free_and_gate_passes(tmp_path):
    report_dir = Path("eval/reports") / tmp_path.name
    report, report_path = run_public_generation_gate(report_dir)

    assert report_path.exists()
    assert report["gate"]["passed"] is True
    assert report["overall"]["citation_precision"] == 1.0
    assert report["overall"]["negative_refusal_rate"] == 1.0
    assert report["offline_proof"]["forbidden_modules_loaded"] == []
    assert len(report["message_contract_sha256"]) == 64
    forbidden_keys = {"answer", "retrieved_chunks", "messages", "prompt", "content", "path"}
    assert not any(forbidden_keys & set(case) for case in report["cases"])
    rendered = report_path.read_text(encoding="utf-8")
    assert "合成材料甲" not in rendered
    assert "文献库中没有相关内容" not in rendered


def test_production_agent_reexports_the_shared_pure_message_builder():
    from app.services import agent_graph

    assert agent_graph.build_rag_prompt is build_rag_prompt
