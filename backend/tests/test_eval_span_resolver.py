"""Batch 22D：Benchmark v2 跨块证据解析的合成 RED 契约。"""

import pytest

from app.models import Chunk, Paper
from eval.dataset import resolve_relevant_spans_v2
from eval.metrics import evidence_any_hit_at_k, evidence_span_coverage_at_k


def _entry(quote):
    return {
        "qa_id": "span-q1",
        "relevant_evidence": [{
            "paper_uid": "doi:10.1/span-test",
            "quote": quote,
        }],
    }


def _seed(db, text, spans):
    paper = Paper(
        id=1,
        title="span fixture",
        doi="10.1/span-test",
        filename="span.pdf",
        file_path="papers/span.pdf",
    )
    db.add(paper)
    db.flush()
    for index, (start, end) in enumerate(spans):
        db.add(Chunk(
            paper_id=paper.id,
            chunk_index=index,
            page_number=1,
            page_start=start,
            page_end=end,
            content=text[start:end],
        ))
    db.commit()
    return paper


def test_single_chunk_evidence_resolves_one_group(db):
    text = "prefix A uniquely identifying evidence sentence suffix"
    quote = "uniquely identifying evidence sentence"
    paper = _seed(db, text, [(0, len(text))])

    groups = resolve_relevant_spans_v2(
        db, _entry(quote), page_loader=lambda row: (
            [{"page_number": 1, "text": text}] if row.id == paper.id else []
        ),
    )

    assert groups == [["p1_c0"]]


def test_cross_chunk_evidence_maps_to_both_chunks(db):
    text = "0123456789 evidence crosses a chunk boundary 9876543210"
    quote = "evidence crosses a chunk boundary"
    start = text.index(quote)
    paper = _seed(db, text, [(0, start + 12), (start + 12, len(text))])

    groups = resolve_relevant_spans_v2(
        db, _entry(quote),
        page_loader=lambda row: [{"page_number": 1, "text": text}],
    )

    assert paper.id == 1
    assert groups == [["p1_c0", "p1_c1"]]


def test_overlap_can_make_multiple_chunks_relevant_without_duplicate_error(db):
    text = "prefix overlap evidence remains uniquely located in page suffix"
    quote = "overlap evidence remains uniquely located"
    start = text.index(quote)
    end = start + len(quote)
    _seed(db, text, [(0, end - 5), (start + 5, len(text))])

    groups = resolve_relevant_spans_v2(
        db, _entry(quote),
        page_loader=lambda row: [{"page_number": 1, "text": text}],
    )

    assert groups == [["p1_c0", "p1_c1"]]


def test_quote_repeated_in_original_pages_fails_closed(db):
    quote = "repeated evidence phrase long enough"
    text = f"{quote} gap {quote}"
    _seed(db, text, [(0, len(text))])

    with pytest.raises(ValueError, match="原文多处命中"):
        resolve_relevant_spans_v2(
            db, _entry(quote),
            page_loader=lambda row: [{"page_number": 1, "text": text}],
        )


def test_cross_page_quote_is_rejected(db):
    quote = "evidence starts here and finishes there"
    first = "prefix " + quote[:20]
    second = quote[20:] + " suffix"
    paper = _seed(db, first, [(0, len(first))])
    db.add(Chunk(
        paper_id=paper.id,
        chunk_index=1,
        page_number=2,
        page_start=0,
        page_end=len(second),
        content=second,
    ))
    db.commit()

    with pytest.raises(ValueError, match="跨页"):
        resolve_relevant_spans_v2(
            db, _entry(quote),
            page_loader=lambda row: [
                {"page_number": 1, "text": first},
                {"page_number": 2, "text": second},
            ],
        )


def test_missing_offsets_are_not_treated_as_v2_qrels(db):
    text = "prefix uniquely identifying evidence sentence suffix"
    quote = "uniquely identifying evidence sentence"
    paper = _seed(db, text, [])
    db.add(Chunk(
        paper_id=paper.id,
        chunk_index=0,
        page_number=1,
        content=text,
    ))
    db.commit()

    with pytest.raises(ValueError, match="坐标缺失"):
        resolve_relevant_spans_v2(
            db, _entry(quote),
            page_loader=lambda row: [{"page_number": 1, "text": text}],
        )


def test_span_metrics_keep_any_hit_separate_from_chunk_coverage():
    groups = [["p1_c0", "p1_c1"], ["p2_c0"]]
    retrieved = ["p1_c1", "noise"]

    assert evidence_any_hit_at_k(retrieved, groups, 5) == pytest.approx(0.5)
    assert evidence_span_coverage_at_k(retrieved, groups, 5) == pytest.approx(1 / 3)
