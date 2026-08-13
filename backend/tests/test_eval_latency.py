"""eval 延迟指标测试（Phase B / T3 / B4）。

契约（specs/phases/phase-b-retrieval/spec.md §3.4、tasks.md T3）：
- ``latency_stats([])`` == ``{"p50": 0.0, "p95": 0.0, "mean": 0.0, "count": 0}``
- ``latency_stats([100.0])`` → p50 == p95 == mean == 100.0，count == 1
- 多条样本 P50 / P95 / mean 正确（线性插值法，与 numpy 默认一致）
- ``eval/run.py`` 报告 JSON 顶层含 ``latency`` 字段，每条 item 记录含 ``latency_ms``
- ``eval/trend.py`` 读取缺 ``latency`` 字段的旧报告不崩（向后兼容）；
  含 ``latency`` 字段的新报告同样不崩（未知字段忽略）

run.py 端到端用例使用内存 SQLite + ``--keyword-only``（不加载语义模型、不调 LLM）。
"""

import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import Chunk
from eval import run, trend
from eval.metrics import latency_stats


# ---------------------------------------------------------------------------
# latency_stats 纯函数
# ---------------------------------------------------------------------------

class TestLatencyStats:
    def test_empty_list(self):
        assert latency_stats([]) == {"p50": 0.0, "p95": 0.0, "mean": 0.0, "count": 0}

    def test_none_input(self):
        # 与 metrics 模块其余函数一致的 None 归一（or []）
        assert latency_stats(None) == {"p50": 0.0, "p95": 0.0, "mean": 0.0, "count": 0}

    def test_single_sample(self):
        stats = latency_stats([100.0])
        assert stats["p50"] == pytest.approx(100.0)
        assert stats["p95"] == pytest.approx(100.0)
        assert stats["mean"] == pytest.approx(100.0)
        assert stats["count"] == 1

    def test_multiple_samples_hand_calculated(self):
        # [10, 20, 30, 40, 50]，线性插值（numpy method="linear"）：
        # p50 → rank = 0.5*4 = 2.0     → 30
        # p95 → rank = 0.95*4 = 3.8    → 40 + 0.8*(50-40) = 48
        # mean → 150/5 = 30
        stats = latency_stats([10.0, 20.0, 30.0, 40.0, 50.0])
        assert stats["p50"] == pytest.approx(30.0)
        assert stats["p95"] == pytest.approx(48.0)
        assert stats["mean"] == pytest.approx(30.0)
        assert stats["count"] == 5

    def test_unsorted_input_equivalent(self):
        sorted_stats = latency_stats([10.0, 20.0, 30.0, 40.0, 50.0])
        shuffled_stats = latency_stats([50.0, 10.0, 30.0, 20.0, 40.0])
        assert shuffled_stats == sorted_stats

    def test_two_samples_interpolation(self):
        # [100, 200]：p50 → rank 0.5 → 150；p95 → rank 0.95 → 195
        stats = latency_stats([100.0, 200.0])
        assert stats["p50"] == pytest.approx(150.0)
        assert stats["p95"] == pytest.approx(195.0)
        assert stats["mean"] == pytest.approx(150.0)
        assert stats["count"] == 2

    def test_result_types(self):
        stats = latency_stats([1.0, 2.0, 3.0])
        assert isinstance(stats["p50"], float)
        assert isinstance(stats["p95"], float)
        assert isinstance(stats["mean"], float)
        assert isinstance(stats["count"], int)

    def test_accepts_int_samples(self):
        stats = latency_stats([100])
        assert stats["p50"] == pytest.approx(100.0)
        assert isinstance(stats["p50"], float)


# ---------------------------------------------------------------------------
# eval.run 报告 latency 字段（端到端，内存 SQLite + keyword-only）
# ---------------------------------------------------------------------------

def _make_eval_session():
    """独立的内存 SQLite Session（不触碰真实 data/papers.db）。"""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    return engine, Session


@pytest.fixture
def eval_env(tmp_path, monkeypatch):
    """构造最小评测环境：2 个 chunk + 2 条 QA（1 正例 1 负例）。"""
    engine, Session = _make_eval_session()
    session = Session()
    session.add(Chunk(paper_id=1, content="肿瘤 背景抑制 BiGRU 多实例学习", chunk_index=0))
    session.add(Chunk(paper_id=1, content="实验结果 准确率 对比", chunk_index=1))
    session.commit()
    session.close()

    # run_eval 内部 `from app.database import SessionLocal`，patch 之
    monkeypatch.setattr("app.database.SessionLocal", Session)

    entries = [
        {
            "qa_id": "pos1",
            "question": "肿瘤 背景抑制",
            "ground_truth": "肿瘤、背景抑制",
            "relevant_chunks": [{"paper_id": 1, "keywords": ["肿瘤"]}],
            "question_type": "factoid",
            "source": "synthetic",
            "has_answer": True,
        },
        {
            "qa_id": "neg1",
            "question": "库中不存在的问题",
            "ground_truth": "不知道",
            "relevant_chunks": [],
            "question_type": "out_of_scope",
            "source": "synthetic",
            "has_answer": False,
        },
    ]
    dataset_path = tmp_path / "ds.jsonl"
    dataset_path.write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in entries),
        encoding="utf-8",
    )
    report_dir = tmp_path / "reports"
    yield dataset_path, report_dir
    engine.dispose()


class TestRunReportLatency:
    def _run_and_load_report(self, eval_env):
        dataset_path, report_dir = eval_env
        rc = run.main([
            "--dataset", str(dataset_path),
            "--keyword-only",
            "--report-dir", str(report_dir),
            "--threshold", "0",
        ])
        assert rc == 0
        files = sorted(report_dir.glob("*.json"))
        assert len(files) == 1, "应恰好写入一份报告"
        return json.loads(files[0].read_text(encoding="utf-8"))

    def test_report_top_level_latency_field(self, eval_env):
        report = self._run_and_load_report(eval_env)
        assert "latency" in report, "报告顶层缺 latency 字段"
        latency = report["latency"]
        assert {"p50", "p95", "mean", "count"} <= set(latency.keys())
        # 2 条 QA 各检索一次 → 2 个延迟样本
        assert latency["count"] == 2
        for key in ("p50", "p95", "mean"):
            assert isinstance(latency[key], float)
            assert latency[key] >= 0.0

    def test_per_item_latency_ms(self, eval_env):
        report = self._run_and_load_report(eval_env)
        items = report["items"]
        assert len(items) == 2
        for item in items:
            assert "latency_ms" in item, f"item {item.get('qa_id')} 缺 latency_ms"
            assert isinstance(item["latency_ms"], (int, float))
            assert item["latency_ms"] >= 0.0

    def test_latency_block_consistent_with_per_item(self, eval_env):
        # 不变量：latency 块 == 对 per-item latency_ms 重新计算 latency_stats
        report = self._run_and_load_report(eval_env)
        per_item_ms = [item["latency_ms"] for item in report["items"]]
        expected = latency_stats(per_item_ms)
        latency = report["latency"]
        assert latency["p50"] == pytest.approx(expected["p50"])
        assert latency["p95"] == pytest.approx(expected["p95"])
        assert latency["mean"] == pytest.approx(expected["mean"])
        assert latency["count"] == expected["count"]

    def test_report_v2_contains_comparison_evidence(self, eval_env):
        """Batch 12：质量报告必须携带可比性指纹与 gate 结果。"""
        report = self._run_and_load_report(eval_env)

        assert report["report_schema"] == "2.0"
        assert len(report["benchmark"]["dataset_sha256"]) == 64
        assert len(report["benchmark"]["corpus_manifest_sha256"]) == 64
        assert report["benchmark"]["n_chunks"] == 2
        assert report["pipeline"]["top_k"] == 5
        assert report["diagnostics"]["unresolved_qrels"] == []
        assert report["diagnostics"]["runtime_degraded_count"] == 0
        assert report["gate"]["passed"] is True
        assert all("mode_used" in item for item in report["items"])


def test_hybrid_runtime_degradation_invalidates_gate(tmp_path, monkeypatch):
    """语义检索任一题异常时，不得继续以 hybrid 名义通过门禁。"""
    engine, Session = _make_eval_session()
    session = Session()
    session.add(Chunk(paper_id=1, content="target evidence", chunk_index=0))
    session.commit()
    session.close()
    monkeypatch.setattr("app.database.SessionLocal", Session)

    fake_store = type("Store", (), {
        "available": lambda self: True,
        "search": lambda self, **kwargs: (_ for _ in ()).throw(RuntimeError("broken")),
    })()
    monkeypatch.setattr("app.services.retrieval.get_vector_store", lambda: fake_store)
    dataset = tmp_path / "ds.jsonl"
    dataset.write_text(json.dumps({
        "qa_id": "q1", "question": "target evidence", "ground_truth": "target",
        "relevant_chunks": [{"paper_id": 1, "keywords": ["target"]}],
        "question_type": "factoid", "source": "synthetic", "has_answer": True,
    }) + "\n", encoding="utf-8")
    report_dir = tmp_path / "reports"

    assert run.main([
        "--dataset", str(dataset), "--threshold", "0",
        "--report-dir", str(report_dir),
    ]) == 1
    report = json.loads(next(report_dir.glob("*.json")).read_text(encoding="utf-8"))
    assert report["diagnostics"]["runtime_degraded_count"] == 1
    assert report["pipeline"]["effective_profile"] == "runtime-degraded"
    assert report["gate"]["passed"] is False
    engine.dispose()


# ---------------------------------------------------------------------------
# trend.py 兼容：缺 latency 字段的旧报告不崩；含 latency 的新报告同样不崩
# ---------------------------------------------------------------------------

def _write_fake_report(report_dir: Path, stem: str, *, with_latency: bool) -> Path:
    """写一份结构对齐 eval.run 输出的假报告（可选 latency 字段）。"""
    report = {
        "timestamp": "2026-08-05T10:00:00",
        "top_k": 5,
        "retrieval_mode": "hybrid",
        "overall": {
            "recall@5": 0.5,
            "mrr": 0.5,
            "ndcg@5": 0.5,
            "n_positive": 4,
            "n_negative": 1,
        },
        "by_question_type": [
            {"question_type": "factoid", "n": 4, "recall": 0.5, "mrr": 0.5, "ndcg": 0.5},
        ],
        "items": [],
    }
    if with_latency:
        report["latency"] = {"p50": 12.3, "p95": 45.6, "mean": 20.1, "count": 5}
    path = report_dir / f"{stem}.json"
    path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    return path


class TestTrendLatencyCompat:
    def test_old_report_without_latency_loads(self, tmp_path):
        """旧报告（无 latency 字段）→ trend 读取不崩（向后兼容契约）。"""
        report_dir = tmp_path / "reports"
        report_dir.mkdir()
        path = _write_fake_report(report_dir, "20260728_100000", with_latency=False)

        summary = trend.summarize_report(path)  # 不抛异常
        assert summary.recall == pytest.approx(0.5)
        summaries = trend.load_summaries(report_dir)
        assert len(summaries) == 1
        assert trend.main(["--report-dir", str(report_dir)]) == 0

    def test_new_report_with_latency_loads(self, tmp_path):
        """新报告（含 latency 字段）→ trend 读取不崩（未知字段忽略）。"""
        report_dir = tmp_path / "reports"
        report_dir.mkdir()
        path = _write_fake_report(report_dir, "20260805_100000", with_latency=True)

        summary = trend.summarize_report(path)  # 不抛异常
        assert summary.recall == pytest.approx(0.5)
        summaries = trend.load_summaries(report_dir)
        assert len(summaries) == 1
        assert trend.main(["--report-dir", str(report_dir)]) == 0

    def test_mixed_old_and_new_reports(self, tmp_path):
        """新旧报告混排（趋势对比场景）→ trend 主流程正常完成。"""
        report_dir = tmp_path / "reports"
        report_dir.mkdir()
        _write_fake_report(report_dir, "20260728_100000", with_latency=False)
        _write_fake_report(report_dir, "20260805_100000", with_latency=True)

        summaries = trend.load_summaries(report_dir)
        assert len(summaries) == 2
        assert trend.main(["--report-dir", str(report_dir)]) == 0
