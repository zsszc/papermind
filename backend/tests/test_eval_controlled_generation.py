"""Batch 19：受控真实生成评测与生产语义 profile 契约。"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from eval import run


def _args(*extra):
    return run.build_parser().parse_args(list(extra))


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (("--with-llm", "--qa-id", "q1"), "--split dev"),
        (("--with-llm", "--split", "dev"), "--qa-id"),
        (
            (
                "--with-llm", "--split", "dev", "--qa-id", "q1",
                "--max-llm-calls", "0",
            ),
            "--max-llm-calls",
        ),
    ],
)
def test_with_llm_requires_dev_whitelist_and_positive_budget(argv, message):
    assert message in run._validate_cli_args(_args(*argv))


def test_generation_budget_rejects_selection_before_health_or_calls(monkeypatch):
    calls = {"health": 0, "generate": 0}

    async def health():
        calls["health"] += 1
        return {"ok": True, "model": "fake"}

    monkeypatch.setattr("app.services.llm.llm_service.health_check", health)
    monkeypatch.setattr(
        run,
        "_generate_answer",
        lambda *args, **kwargs: calls.__setitem__("generate", calls["generate"] + 1),
    )
    monkeypatch.setattr(
        run,
        "load_dataset",
        lambda _: [
            {"qa_id": "q1", "split": "dev"},
            {"qa_id": "q2", "split": "dev"},
        ],
    )
    monkeypatch.setattr(run, "validate_dataset", lambda _: None)

    rc = run.run_eval(_args(
        "--with-llm", "--split", "dev", "--qa-id", "q1", "--qa-id", "q2",
        "--max-llm-calls", "1", "--keyword-only",
    ))

    assert rc == 2
    assert calls == {"health": 0, "generate": 0}


def test_llm_health_preflight_failure_stops_before_generation(monkeypatch):
    calls = {"generate": 0}

    async def unhealthy():
        return {"ok": False, "error": "quota"}

    monkeypatch.setattr("app.services.llm.llm_service.health_check", unhealthy)
    monkeypatch.setattr(
        run,
        "_generate_answer",
        lambda *args, **kwargs: calls.__setitem__("generate", calls["generate"] + 1),
    )
    monkeypatch.setattr(run, "_prepare_eval_items", lambda args: ([{"qa_id": "q1"}], None))

    rc = run.run_eval(_args(
        "--with-llm", "--split", "dev", "--qa-id", "q1",
        "--max-llm-calls", "1", "--keyword-only",
    ))

    assert rc == 2
    assert calls["generate"] == 0


def test_llm_health_preflight_exception_stops_cleanly(monkeypatch):
    async def broken_health():
        raise RuntimeError("health boom")

    monkeypatch.setattr(
        "app.services.llm.llm_service.health_check", broken_health
    )
    monkeypatch.setattr(
        run, "_prepare_eval_items", lambda args: ([{"qa_id": "q1"}], None)
    )

    rc = run.run_eval(_args(
        "--with-llm", "--split", "dev", "--qa-id", "q1",
        "--max-llm-calls", "1", "--keyword-only",
    ))

    assert rc == 2


def test_empty_answer_is_a_generation_error():
    assert run._generation_error_kind("") == "empty_response"
    assert run._generation_error_kind("  \n") == "empty_response"
    assert run._generation_error_kind("[调用 LLM 出错: timeout]") == (
        "llm_error_response"
    )
    assert run._generation_error_kind("有效答案 [p1_c0]") is None


def test_with_llm_report_dir_must_be_inside_private_root(tmp_path, monkeypatch):
    private_root = tmp_path / "eval" / "private"
    dataset = private_root / "qa.jsonl"
    monkeypatch.setattr(run, "PRIVATE_EVAL_ROOT", private_root)

    unsafe = _args(
        "--with-llm", "--split", "dev", "--qa-id", "q1",
        "--max-llm-calls", "1", "--keyword-only",
        "--dataset", str(dataset),
        "--report-dir", str(tmp_path / "public"),
    )
    safe = _args(
        "--with-llm", "--split", "dev", "--qa-id", "q1",
        "--max-llm-calls", "1", "--keyword-only",
        "--dataset", str(dataset),
        "--report-dir", str(private_root / "smoke"),
    )

    assert "eval/private" in run._validate_cli_args(unsafe)
    assert run._validate_cli_args(safe) is None


def test_with_llm_dataset_must_be_inside_private_root(tmp_path, monkeypatch):
    private_root = tmp_path / "eval" / "private"
    monkeypatch.setattr(run, "PRIVATE_EVAL_ROOT", private_root)
    args = _args(
        "--with-llm", "--split", "dev", "--qa-id", "q1",
        "--max-llm-calls", "1", "--keyword-only",
        "--dataset", str(tmp_path / "public.jsonl"),
        "--report-dir", str(private_root / "smoke"),
    )

    assert "dataset" in run._validate_cli_args(args)


def test_generate_answer_caps_output_at_512_tokens(monkeypatch):
    captured = {}

    def fake(messages, *, max_tokens=None):
        captured["max_tokens"] = max_tokens
        return "ok"

    monkeypatch.setattr(
        "app.services.llm.llm_service.chat_completion_sync", fake
    )

    assert run._generate_answer("q", []) == "ok"
    assert captured["max_tokens"] == 512


def test_semantic_production_calls_only_vector_store_top5(monkeypatch):
    calls = []
    store = SimpleNamespace(
        available=lambda: True,
        search=lambda **kwargs: calls.append(kwargs) or [
            {"chunk_id": "p1_c0", "score": 1.0}
        ],
    )
    monkeypatch.setattr(run, "_open_eval_vector_store", lambda _: store)
    monkeypatch.setattr(
        run,
        "_keyword_chunk_search",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("semantic-production 不得运行词法检索")
        ),
    )
    monkeypatch.setattr(
        run,
        "_rrf_fuse_chunks",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("semantic-production 不得运行 RRF")
        ),
    )

    retriever = run.Retriever(
        db=object(), top_k=9, vector_dir=Path("snapshot"),
        retrieval_profile="semantic-production",
    )
    result = retriever.search("question")

    assert result[0]["chunk_id"] == "p1_c0"
    assert calls == [{"query": "question", "top_k": 5}]
    assert retriever.mode == "semantic-production"


def test_semantic_production_requires_top5_and_vector_snapshot(tmp_path):
    missing_vector = _args("--retrieval-profile", "semantic-production")
    wrong_topk = _args(
        "--retrieval-profile", "semantic-production", "--vector-dir", str(tmp_path),
        "--top-k", "10",
    )

    assert "--vector-dir" in run._validate_cli_args(missing_vector)
    assert "top-k=5" in run._validate_cli_args(wrong_topk)
