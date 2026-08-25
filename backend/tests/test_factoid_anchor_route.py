"""Batch 22I：Factoid 稀有实体/数值锚点路由 RED。"""

from copy import deepcopy

import pytest

from app.services import retrieval_pipeline as pipeline_module
from eval import run


def _item(chunk_id, source="semantic"):
    return {
        "chunk_id": chunk_id,
        "paper_id": int(chunk_id.split("_", 1)[0][1:]),
        "content": chunk_id,
        "source": source,
    }


class _Store:
    def __init__(self, results, *, available=True):
        self.results = results
        self._available = available
        self.calls = []

    def available(self):
        return self._available

    def search(self, **kwargs):
        self.calls.append(dict(kwargs))
        return deepcopy(self.results)


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        (
            "ReCo-MIL 在 TCGA-CRC 上达到 87.3%，距离为 5 mm",
            ["reco-mil", "tcga-crc", "87.3%", "5", "mm"],
        ),
        ("比较 ABMIL 与 TransMIL 的方法", ["abmil", "transmil"]),
        ("What method improves accuracy?", []),
        ("样本量为 100，重复 100 次", ["100"]),
        ("普通中文问题，没有 ASCII 实体", []),
    ],
)
def test_extract_factoid_anchors_is_frozen_and_deduplicated(query, expected):
    assert pipeline_module.extract_factoid_anchors(query) == expected


def test_anchor_fusion_without_anchor_route_is_deep_equal_to_production_rrf():
    semantic = [_item("p2_c0"), _item("p1_c0")]
    lexical = [_item("p1_c0", "keyword"), _item("p3_c0", "keyword")]

    expected = pipeline_module.rrf_fuse_chunks(semantic, lexical, 3)
    actual = pipeline_module.anchor_rrf_fuse_chunks(
        semantic, lexical, [], 3
    )

    assert actual == expected
    assert semantic == [_item("p2_c0"), _item("p1_c0")]
    assert lexical == [
        _item("p1_c0", "keyword"), _item("p3_c0", "keyword")
    ]


def test_anchor_fusion_adds_equal_weight_third_legacy_route():
    semantic = [_item("p2_c0"), _item("p1_c0")]
    lexical = [_item("p3_c0", "keyword"), _item("p1_c0", "keyword")]
    anchor = [_item("p1_c0", "anchor"), _item("p4_c0", "anchor")]

    results = pipeline_module.anchor_rrf_fuse_chunks(
        semantic, lexical, anchor, 4
    )

    assert [item["chunk_id"] for item in results] == [
        "p1_c0", "p2_c0", "p3_c0", "p4_c0"
    ]
    assert results[0]["source"] == "semantic"


def test_anchor_profile_without_anchors_matches_hybrid(db, monkeypatch):
    semantic = [_item("p2_c0"), _item("p1_c0")]
    lexical = [_item("p1_c0", "keyword"), _item("p3_c0", "keyword")]
    monkeypatch.setattr(
        pipeline_module,
        "keyword_chunk_search",
        lambda *args, **kwargs: deepcopy(lexical),
    )
    anchor_calls = []
    monkeypatch.setattr(
        pipeline_module,
        "anchor_chunk_search",
        lambda *args, **kwargs: anchor_calls.append((args, kwargs)),
    )
    store = _Store(semantic)
    pipeline = pipeline_module.RetrievalPipeline(db, vector_store=store)

    baseline = pipeline.search(
        "普通中文问题", top_k=3, profile="hybrid",
        lexical_profile="bm25-bilingual", diagnostics={},
    )
    diagnostics = {}
    candidate = pipeline.search(
        "普通中文问题", top_k=3, profile="hybrid-anchor-v1",
        lexical_profile="bm25-bilingual", diagnostics=diagnostics,
    )

    assert candidate == baseline
    assert anchor_calls == []
    assert diagnostics == {
        "requested_profile": "hybrid-anchor-v1",
        "effective_profile": "hybrid-anchor-v1",
        "degraded": False,
        "reason": None,
    }


def test_anchor_profile_uses_filtered_third_route(db, monkeypatch):
    semantic = [_item("p2_c0"), _item("p1_c0")]
    lexical = [_item("p3_c0", "keyword"), _item("p1_c0", "keyword")]
    anchor = [_item("p1_c0", "anchor")]
    monkeypatch.setattr(
        pipeline_module,
        "keyword_chunk_search",
        lambda *args, **kwargs: deepcopy(lexical),
    )
    calls = []

    def anchor_search(*args, **kwargs):
        calls.append((args, kwargs))
        return deepcopy(anchor)

    monkeypatch.setattr(pipeline_module, "anchor_chunk_search", anchor_search)
    diagnostics = {}
    results = pipeline_module.RetrievalPipeline(
        db, vector_store=_Store(semantic)
    ).search(
        "ABMIL 在 100 cases 上的结果",
        top_k=3,
        filters={"paper_id": 1},
        profile="hybrid-anchor-v1",
        lexical_profile="bm25-bilingual",
        diagnostics=diagnostics,
    )

    assert [item["chunk_id"] for item in results] == [
        "p1_c0", "p2_c0", "p3_c0"
    ]
    assert calls[0][1]["filters"] == {"paper_id": 1}
    assert calls[0][1]["limit"] == 6
    assert diagnostics["degraded"] is False


def test_anchor_route_failure_is_explicit_runtime_degradation(db, monkeypatch):
    monkeypatch.setattr(
        pipeline_module,
        "keyword_chunk_search",
        lambda *args, **kwargs: [_item("p1_c0", "keyword")],
    )
    monkeypatch.setattr(
        pipeline_module,
        "anchor_chunk_search",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("broken")),
    )
    diagnostics = {}
    results = pipeline_module.RetrievalPipeline(
        db, vector_store=_Store([_item("p2_c0")])
    ).search(
        "ABMIL 100 cases", top_k=2, profile="hybrid-anchor-v1",
        lexical_profile="bm25-bilingual", diagnostics=diagnostics,
    )

    assert results == [_item("p2_c0"), _item("p1_c0", "keyword")]
    assert diagnostics == {
        "requested_profile": "hybrid-anchor-v1",
        "effective_profile": "hybrid",
        "degraded": True,
        "reason": "anchor_search_failed",
    }


def test_eval_anchor_profile_is_train_dev_only_and_requires_snapshot(tmp_path):
    parser = run.build_parser()
    safe = parser.parse_args([
        "--dataset", "eval/private/qa_private_v1.jsonl",
        "--database", "papers.db",
        "--corpus-root", ".",
        "--vector-dir", str(tmp_path / "vector"),
        "--retrieval-profile", "hybrid-anchor-v1",
        "--lexical-profile", "bm25-bilingual",
        "--evidence-resolver", "page-span-v2",
        "--split", "train",
    ])
    assert run._validate_cli_args(safe) is None

    safe.split = "holdout"
    assert "禁止 holdout" in run._validate_cli_args(safe)
    safe.split = "train"
    safe.vector_dir = None
    assert "vector-dir" in run._validate_cli_args(safe)
