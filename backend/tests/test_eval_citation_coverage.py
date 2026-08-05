"""eval --with-llm 的 citation_coverage 接入测试（Phase C / C3）。

契约（specs/phases/phase-c-guardrails/spec.md §3.3、AC4）：
- ``eval.run --with-llm``（mock LLM）对每条正例计算 ``citation_coverage``
  （既有 per-item 行为），并将其**汇总均值写入报告 ``overall.citation_coverage``**；
- ``overall.citation_coverage`` 与 ``generation.citation_coverage`` 同源一致；
- 负例答案只参与拒答判定，不进 citation_coverage 均值；
- 非 ``--with-llm`` 运行的报告 ``overall`` 不含 ``citation_coverage``
  （该字段是 --with-llm 路径的增量字段）；
- LLM 带内错误串（``[调用 LLM 出错: ...]``）被当 answer 记录时，
  该条 citation_coverage 按 0 计（metrics 契约：无引用 → 0.0）；
- ``eval/trend.py`` 对缺该字段的旧报告不崩，对含该字段的新报告同样不崩
  （延续 B4 兼容模式：未知字段忽略，trend.py 本体无需改动）。

绝不触发真实 LLM 调用：``chat_completion_sync`` 一律 monkeypatch 为 fake。
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


# ---------------------------------------------------------------------------
# 公共测试环境（内存 SQLite + keyword-only，模式同 test_eval_latency.py）
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
    """最小评测环境：2 个 chunk + 3 条 QA（2 正例 1 负例）。

    - pos1 期望 chunk 为 p1_c0（含「肿瘤」）；
    - pos2 期望 chunk 为 p1_c1（含「实验结果」）；
    - neg1 为负例（无期望 chunk）。
    """
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
            "qa_id": "pos2",
            "question": "实验结果 准确率",
            "ground_truth": "实验结果、准确率",
            "relevant_chunks": [{"paper_id": 1, "keywords": ["实验结果"]}],
            "question_type": "experiment_data",
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


def _question_of(messages) -> str:
    """从 _generate_answer 组装的用户消息中取出问题原文（末段「问题：xxx」）。"""
    return messages[-1]["content"].rsplit("问题：", 1)[-1]


def _fake_llm_answer(messages) -> str:
    """fake LLM：pos1 引对 chunk、p1_c0；pos2 引错 chunk、p1_c0；负例拒答。"""
    question = _question_of(messages)
    if "肿瘤" in question:
        return "答：涉及肿瘤与背景抑制 [p1_c0]。"
    if "实验结果" in question:
        return "答：实验结果与准确率详见 [p1_c0]。"  # 期望 p1_c1，引错 → 0
    return "不知道"


def _run_and_load_report(eval_env, extra_args):
    dataset_path, report_dir = eval_env
    rc = run.main([
        "--dataset", str(dataset_path),
        "--keyword-only",
        "--report-dir", str(report_dir),
        "--threshold", "0",
        *extra_args,
    ])
    assert rc == 0
    files = sorted(report_dir.glob("*.json"))
    assert len(files) == 1, "应恰好写入一份报告"
    return json.loads(files[0].read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# --with-llm 主流程：overall.citation_coverage 入报告
# ---------------------------------------------------------------------------

class TestWithLlmCitationCoverage:
    def test_overall_citation_coverage_present_and_correct(
            self, eval_env, monkeypatch):
        """C3 核心契约：报告 overall 含 citation_coverage 且为正例均值。"""
        monkeypatch.setattr(
            "app.services.llm.llm_service.chat_completion_sync", _fake_llm_answer)
        report = _run_and_load_report(eval_env, ["--with-llm"])

        overall = report["overall"]
        assert "citation_coverage" in overall, \
            "C3：--with-llm 报告 overall 应含 citation_coverage 汇总均值"
        # 手算：pos1 引对 (1/1=1.0)，pos2 引错 (0/1=0.0) → 均值 0.5
        assert overall["citation_coverage"] == pytest.approx(0.5)
        # 负例不进均值（若计入则均值为 (1+0+?)/3 ≠ 0.5）
        assert overall["n_positive"] == 2
        assert overall["n_negative"] == 1

    def test_overall_matches_generation_block(self, eval_env, monkeypatch):
        """overall.citation_coverage 与 generation 块同源一致。"""
        monkeypatch.setattr(
            "app.services.llm.llm_service.chat_completion_sync", _fake_llm_answer)
        report = _run_and_load_report(eval_env, ["--with-llm"])

        assert report["overall"]["citation_coverage"] == pytest.approx(
            report["generation"]["citation_coverage"])

    def test_per_item_citation_coverage(self, eval_env, monkeypatch):
        """per-item 正例记录含 citation_coverage（既有行为，此处锁定）。"""
        monkeypatch.setattr(
            "app.services.llm.llm_service.chat_completion_sync", _fake_llm_answer)
        report = _run_and_load_report(eval_env, ["--with-llm"])

        items = {it["qa_id"]: it for it in report["items"]}
        assert items["pos1"]["citation_coverage"] == pytest.approx(1.0)
        assert items["pos1"]["citations"] == ["p1_c0"]
        assert items["pos2"]["citation_coverage"] == pytest.approx(0.0)
        # 负例只有拒答判定，不参与 citation_coverage
        assert items["neg1"]["refused"] is True
        assert "citation_coverage" not in items["neg1"]

    def test_llm_error_string_counts_as_zero(self, eval_env, monkeypatch):
        """LLM 带内错误串被当 answer 记录：无引用可提取，该条按 0 计。"""
        def fake_error(messages):
            if "库中不存在" in _question_of(messages):
                return "不知道"
            return "[调用 LLM 出错: Kimi API 响应超时]"

        monkeypatch.setattr(
            "app.services.llm.llm_service.chat_completion_sync", fake_error)
        report = _run_and_load_report(eval_env, ["--with-llm"])

        assert report["overall"]["citation_coverage"] == pytest.approx(0.0)
        items = {it["qa_id"]: it for it in report["items"]}
        assert items["pos1"]["citation_coverage"] == pytest.approx(0.0)
        assert items["pos1"]["citations"] == []

    def test_without_with_llm_overall_lacks_field(self, eval_env, monkeypatch):
        """非 --with-llm 运行：overall 无 citation_coverage，且不调 LLM。"""
        def spy_llm(messages):
            raise AssertionError("未开 --with-llm，不应调用 LLM")

        monkeypatch.setattr(
            "app.services.llm.llm_service.chat_completion_sync", spy_llm)
        report = _run_and_load_report(eval_env, [])

        assert report["with_llm"] is False
        assert "citation_coverage" not in report["overall"]
        assert "generation" not in report


# ---------------------------------------------------------------------------
# trend.py 兼容：缺 citation_coverage 的旧报告不崩；含该字段的新报告不崩
# ---------------------------------------------------------------------------

def _write_fake_report(report_dir: Path, stem: str, *,
                       with_citation_coverage: bool) -> Path:
    """写一份结构对齐 eval.run 输出的假报告（可选 overall.citation_coverage）。"""
    overall = {
        "recall@5": 0.5,
        "mrr": 0.5,
        "ndcg@5": 0.5,
        "n_positive": 4,
        "n_negative": 1,
    }
    report = {
        "timestamp": "2026-08-05T10:00:00",
        "top_k": 5,
        "retrieval_mode": "hybrid",
        "overall": overall,
        "by_question_type": [
            {"question_type": "factoid", "n": 4, "recall": 0.5,
             "mrr": 0.5, "ndcg": 0.5},
        ],
        "items": [],
    }
    if with_citation_coverage:
        # 新报告：overall 增量字段 + generation 块（对齐 C3 后的 schema）
        overall["citation_coverage"] = 0.75
        report["with_llm"] = True
        report["generation"] = {
            "citation_coverage": 0.75,
            "keyword_hit_rate": 0.6,
            "negative_refusal_rate": 1.0,
            "negative_refused": 1,
            "negative_total": 1,
        }
    path = report_dir / f"{stem}.json"
    path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    return path


class TestTrendCitationCoverageCompat:
    def test_old_report_without_field_loads(self, tmp_path):
        """旧报告（overall 无 citation_coverage）→ trend 读取不崩（向后兼容）。"""
        report_dir = tmp_path / "reports"
        report_dir.mkdir()
        path = _write_fake_report(
            report_dir, "20260728_100000", with_citation_coverage=False)

        summary = trend.summarize_report(path)  # 不抛异常
        assert summary.recall == pytest.approx(0.5)
        summaries = trend.load_summaries(report_dir)
        assert len(summaries) == 1
        assert trend.main(["--report-dir", str(report_dir)]) == 0

    def test_new_report_with_field_loads(self, tmp_path):
        """新报告（overall 含 citation_coverage + generation 块）→ 不崩。"""
        report_dir = tmp_path / "reports"
        report_dir.mkdir()
        path = _write_fake_report(
            report_dir, "20260805_100000", with_citation_coverage=True)

        summary = trend.summarize_report(path)  # 未知字段忽略，不抛异常
        assert summary.recall == pytest.approx(0.5)
        summaries = trend.load_summaries(report_dir)
        assert len(summaries) == 1
        assert trend.main(["--report-dir", str(report_dir)]) == 0

    def test_mixed_old_and_new_reports(self, tmp_path):
        """新旧报告混排（趋势对比场景）→ trend 主流程正常完成。"""
        report_dir = tmp_path / "reports"
        report_dir.mkdir()
        _write_fake_report(
            report_dir, "20260728_100000", with_citation_coverage=False)
        _write_fake_report(
            report_dir, "20260805_100000", with_citation_coverage=True)

        summaries = trend.load_summaries(report_dir)
        assert len(summaries) == 2
        assert trend.main(["--report-dir", str(report_dir)]) == 0
