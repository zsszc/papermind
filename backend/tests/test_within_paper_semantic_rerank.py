"""Batch 32：论文内语义选块唯一候选契约。"""

from copy import deepcopy

import pytest

from app.services import retrieval_pipeline as retrieval
from eval import run


def _item(chunk_id, *, rank=None, source="semantic"):
    paper_id = int(chunk_id[1:].split("_c", 1)[0])
    row = {"chunk_id": chunk_id, "paper_id": paper_id,
           "content": chunk_id, "score": 0.9, "source": source}
    if rank is not None:
        row["within_paper_rank"] = rank
    return row


def test_selection_locks_top5_incumbent_and_replaces_only_same_paper():
    semantic = [_item("p1_c0"), _item("p2_c9")]
    lexical = [_item("p1_c0", source="keyword"), _item("p2_c9", source="keyword")]
    local = [
        _item("p1_c0", rank=2), _item("p1_c1", rank=1),
        _item("p2_c1", rank=1), _item("p2_c2", rank=2),
    ]
    original = deepcopy((semantic, lexical, local))

    result = retrieval.within_paper_semantic_fuse_chunks(
        semantic, lexical, local, 2
    )

    assert [row["chunk_id"] for row in result] == ["p1_c0", "p2_c1"]
    assert [row["paper_id"] for row in result] == [1, 2]
    assert (semantic, lexical, local) == original


@pytest.mark.parametrize("local", [
    [_item("p1_c1", rank=1), _item("p1_c2", rank=1)],
    [{**_item("p1_c1", rank=1), "paper_id": 2}],
    [_item("p9_c0", rank=1)],
])
def test_selection_fails_closed_for_rank_metadata_or_scope(local):
    with pytest.raises(retrieval.WithinPaperSemanticContractError):
        retrieval.within_paper_semantic_fuse_chunks(
            [_item("p1_c0")], [], local, 1
        )


def test_pipeline_reuses_one_embedding_and_queries_selected_papers(monkeypatch):
    calls = []

    class Embedding:
        def embed_query(self, query):
            calls.append(("embed", query))
            return [0.1, 0.2]

    class Store:
        embedding_service = Embedding()

        def available(self):
            return True

        def search_by_embedding(self, embedding, **kwargs):
            calls.append(("global", embedding, kwargs))
            return [_item("p1_c0"), _item("p2_c9")]

    monkeypatch.setattr(
        retrieval, "keyword_chunk_search", lambda *a, **k: []
    )

    def local_search(db, store, embedding, *, paper_ids, filters):
        calls.append(("local", embedding, tuple(paper_ids), filters))
        return [_item("p1_c0", rank=1), _item("p2_c1", rank=1)]

    monkeypatch.setattr(retrieval, "within_paper_semantic_search", local_search)
    diagnostics = {}
    result = retrieval.RetrievalPipeline(None, vector_store=Store()).search(
        "private", top_k=2, profile="within-paper-semantic-rerank-v1",
        lexical_profile="bm25-bilingual", diagnostics=diagnostics,
    )

    assert [row["chunk_id"] for row in result] == ["p1_c0", "p2_c1"]
    assert sum(call[0] == "embed" for call in calls) == 1
    assert calls[1][1] is calls[2][1]
    assert calls[2][2] == (1, 2)
    assert diagnostics["effective_profile"] == "within-paper-semantic-rerank-v1"


def test_eval_profile_is_train_only_and_contract_is_stable(tmp_path):
    parser = run.build_parser()
    args = parser.parse_args([
        "--dataset", "eval/private/qa_private_v2.jsonl",
        "--database", str(tmp_path / "papers.db"),
        "--corpus-root", str(tmp_path / "corpus"),
        "--vector-dir", str(tmp_path / "vectors"),
        "--retrieval-profile", "within-paper-semantic-rerank-v1",
        "--lexical-profile", "bm25-bilingual",
        "--evidence-resolver", "page-span-v2", "--split", "train",
    ])
    assert run._validate_cli_args(args) is None
    args.split = "dev"
    assert "完整 train" in run._validate_cli_args(args)
    contract = run.within_paper_semantic_rerank_contract_metadata()
    assert contract["algorithm"] == "within-paper-semantic-rerank-v1"
    assert contract["within_paper_depth"] == 5
    assert contract["embedding_calls_per_query"] == 1
    assert len(contract["formula_sha256"]) == 64
