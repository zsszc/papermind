"""Batch 20：生产聊天与 eval 必须消费同一 RetrievalPipeline 排序。"""

from pathlib import Path

import pytest

from app.models import Chunk, Conversation, Message, Paper
from app.services import agent_graph
from app.services.agent_graph import run_pre_orchestration
from eval import run


class _Store:
    def __init__(self, results):
        self.results = results
        self.calls = []

    def available(self):
        return True

    def search(self, **kwargs):
        self.calls.append(dict(kwargs))
        diagnostics = kwargs.get("rerank_diagnostics")
        if diagnostics is not None:
            diagnostics.update({
                "requested": bool(kwargs.get("rerank")),
                "effective": False,
                "error": None,
            })
        return [dict(item) for item in self.results]


def _add_corpus(db):
    for paper_id, content in (
        (1, "targetanchor precise evidence"),
        (2, "unrelated semantic background"),
    ):
        db.add(Paper(
            id=paper_id,
            title=f"paper-{paper_id}",
            filename=f"paper-{paper_id}.pdf",
            file_path=f"papers/paper-{paper_id}.pdf",
            year=2024,
        ))
        db.add(Chunk(
            paper_id=paper_id,
            chunk_index=0,
            content=content,
            chunk_type="result",
        ))
    conversation = Conversation(title="parity", message_count=0)
    db.add(conversation)
    db.flush()
    db.add(Message(
        conversation_id=conversation.id,
        role="user",
        content="targetanchor",
        citations=[],
    ))
    db.commit()
    return conversation


@pytest.mark.parametrize(
    "lexical_profile", ["bm25-bilingual", "bm25-bilingual-v2"]
)
def test_chat_and_eval_hybrid_return_identical_chunk_order(
    db, monkeypatch, lexical_profile
):
    conversation = _add_corpus(db)
    store = _Store([
        {
            "chunk_id": "p2_c0", "paper_id": 2, "title": "paper-2",
            "authors": None, "year": 2024,
            "content": "unrelated semantic background", "page_number": None,
            "chunk_type": "result", "score": 0.9, "source": "semantic",
        },
        {
            "chunk_id": "p1_c0", "paper_id": 1, "title": "paper-1",
            "authors": None, "year": 2024,
            "content": "targetanchor precise evidence", "page_number": None,
            "chunk_type": "result", "score": 0.8, "source": "semantic",
        },
    ])
    monkeypatch.setattr(agent_graph, "get_vector_store", lambda: store)
    original_get = agent_graph.config.get

    def fake_config_get(key, default=None):
        if key == "retrieval.chat_profile":
            return "hybrid"
        if key == "retrieval.lexical_profile":
            return lexical_profile
        return original_get(key, default)

    monkeypatch.setattr(agent_graph.config, "get", fake_config_get)
    monkeypatch.setattr(run, "_open_eval_vector_store", lambda _: store)

    chat_state = run_pre_orchestration(
        db=db,
        conversation_id=conversation.id,
        user_message="targetanchor",
    )
    retriever = run.Retriever(
        db=db,
        top_k=5,
        vector_dir=Path("explicit-snapshot"),
        retrieval_profile="hybrid",
        lexical_profile=lexical_profile,
        semantic_rerank=False,
    )
    eval_results = retriever.search("targetanchor")

    assert [item["chunk_id"] for item in chat_state["context_chunks"]] == [
        item["chunk_id"] for item in eval_results
    ]


def test_chat_and_eval_local_neighbor_use_top20_and_identical_order(
    db, monkeypatch
):
    paper = Paper(
        id=1,
        title="paper-1",
        filename="paper-1.pdf",
        file_path="papers/paper-1.pdf",
        year=2024,
    )
    db.add(paper)
    for index in range(4):
        db.add(Chunk(
            paper_id=1,
            chunk_index=index,
            content=f"local evidence {index}",
            chunk_type="result",
        ))
    conversation = Conversation(title="neighbor parity", message_count=0)
    db.add(conversation)
    db.flush()
    db.add(Message(
        conversation_id=conversation.id,
        role="user",
        content="local evidence",
        citations=[],
    ))
    db.commit()

    store = _Store([{
        "chunk_id": "p1_c2",
        "paper_id": 1,
        "title": "paper-1",
        "authors": None,
        "year": 2024,
        "content": "local evidence 2",
        "page_number": None,
        "chunk_type": "result",
        "score": 0.9,
        "source": "semantic",
    }])
    monkeypatch.setattr(agent_graph, "get_vector_store", lambda: store)
    original_get = agent_graph.config.get

    def fake_config_get(key, default=None):
        if key == "retrieval.chat_profile":
            return "hybrid-local-neighbor"
        if key == "retrieval.lexical_profile":
            return "bm25-bilingual"
        return original_get(key, default)

    monkeypatch.setattr(agent_graph.config, "get", fake_config_get)
    monkeypatch.setattr(run, "_open_eval_vector_store", lambda _: store)

    chat_state = run_pre_orchestration(
        db=db,
        conversation_id=conversation.id,
        user_message="local evidence",
    )
    eval_results = run.Retriever(
        db=db,
        top_k=5,
        vector_dir=Path("explicit-snapshot"),
        retrieval_profile="hybrid-local-neighbor",
        lexical_profile="bm25-bilingual",
        semantic_rerank=False,
    ).search("local evidence")

    assert [call["top_k"] for call in store.calls] == [20, 20]
    assert [item["chunk_id"] for item in chat_state["context_chunks"]] == [
        item["chunk_id"] for item in eval_results
    ]


def test_eval_parser_accepts_local_neighbor_candidate():
    args = run.build_parser().parse_args([
        "--retrieval-profile", "hybrid-local-neighbor",
        "--vector-dir", "explicit-snapshot",
    ])

    assert args.retrieval_profile == "hybrid-local-neighbor"
    assert run._validate_cli_args(args) is None
