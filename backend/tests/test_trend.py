"""eval.trend 单元测试：假报告驱动趋势表与差值计算，覆盖边界行为。

本测试模块只导入 eval.trend（纯标准库），
所有用例都在 tmp_path 下构造假报告，不触碰真实 eval/reports/ 目录，
不触发 Embedding 模型加载、不调用 LLM、不连数据库。
"""

import json
from pathlib import Path
from typing import List, Optional

import pytest

from eval import trend


def _write_report(report_dir: Path, stem: str, *, timestamp: Optional[str] = None,
                  mode: str = "hybrid", recall: float = 0.5, mrr: float = 0.5,
                  ndcg: float = 0.5, n_positive: int = 4, n_negative: int = 1,
                  by_type: Optional[List[dict]] = None) -> Path:
    """在 report_dir 下写入一份结构对齐 eval.run 输出的假报告。"""
    report = {
        "timestamp": timestamp or f"2026-07-28T{stem[9:11]}:{stem[11:13]}:{stem[13:15]}",
        "top_k": 5,
        "retrieval_mode": mode,
        "overall": {
            "recall@5": recall,
            "mrr": mrr,
            "ndcg@5": ndcg,
            "n_positive": n_positive,
            "n_negative": n_negative,
        },
        "by_question_type": by_type if by_type is not None else [
            {"question_type": "factoid", "n": 2, "recall": 0.5, "mrr": 0.5, "ndcg": 0.5},
            {"question_type": "summary", "n": 2, "recall": 0.25, "mrr": 0.3, "ndcg": 0.2},
        ],
        "items": [],
    }
    path = report_dir / f"{stem}.json"
    path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture
def three_reports(tmp_path: Path) -> Path:
    """三份按时间升序的假报告，指标单调变化，便于断言差值。"""
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    _write_report(report_dir, "20260728_100000",
                  recall=0.400, mrr=0.500, ndcg=0.300,
                  by_type=[{"question_type": "factoid", "n": 2,
                            "recall": 0.5, "mrr": 0.5, "ndcg": 0.5}])
    _write_report(report_dir, "20260728_110000",
                  recall=0.500, mrr=0.550, ndcg=0.400,
                  by_type=[{"question_type": "factoid", "n": 2,
                            "recall": 0.6, "mrr": 0.6, "ndcg": 0.6},
                           {"question_type": "summary", "n": 1,
                            "recall": 0.0, "mrr": 0.0, "ndcg": 0.0}])
    _write_report(report_dir, "20260728_120000",
                  recall=0.480, mrr=0.550, ndcg=0.430,
                  by_type=[{"question_type": "factoid", "n": 2,
                            "recall": 0.7, "mrr": 0.7, "ndcg": 0.7}])
    return report_dir


# ---------------------------------------------------------------------------
# 加载与摘要
# ---------------------------------------------------------------------------

class TestLoadSummaries:
    def test_sorted_by_filename(self, three_reports: Path):
        summaries = trend.load_summaries(three_reports)
        assert [s.stem for s in summaries] == [
            "20260728_100000", "20260728_110000", "20260728_120000"]

    def test_fields_extracted(self, three_reports: Path):
        summaries = trend.load_summaries(three_reports)
        first = summaries[0]
        assert first.retrieval_mode == "hybrid"
        assert first.recall == pytest.approx(0.400)
        assert first.mrr == pytest.approx(0.500)
        assert first.ndcg == pytest.approx(0.300)
        assert first.k == 5
        assert first.n_total == 5  # 4 正 + 1 负

    def test_non_json_ignored(self, three_reports: Path):
        (three_reports / "trend.md").write_text("# 旧趋势", encoding="utf-8")
        (three_reports / "说明.txt").write_text("hello", encoding="utf-8")
        summaries = trend.load_summaries(three_reports)
        assert len(summaries) == 3

    def test_broken_json_skipped(self, three_reports: Path, capsys):
        (three_reports / "20260728_130000.json").write_text("{损坏", encoding="utf-8")
        summaries = trend.load_summaries(three_reports)
        assert len(summaries) == 3
        assert "跳过无法解析" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# 差值计算
# ---------------------------------------------------------------------------

class TestDeltas:
    def test_delta_values(self, three_reports: Path):
        summaries = trend.load_summaries(three_reports)
        deltas = trend.compute_deltas(summaries)
        assert deltas[0] == {"recall": None, "mrr": None, "ndcg": None}
        assert deltas[1]["recall"] == pytest.approx(0.100)
        assert deltas[1]["mrr"] == pytest.approx(0.050)
        assert deltas[1]["ndcg"] == pytest.approx(0.100)
        assert deltas[2]["recall"] == pytest.approx(-0.020)
        assert deltas[2]["mrr"] == pytest.approx(0.0)
        assert deltas[2]["ndcg"] == pytest.approx(0.030)

    def test_delta_format_in_table(self, three_reports: Path):
        summaries = trend.load_summaries(three_reports)
        table = trend.format_overall_table(summaries)
        assert "+0.100" in table
        assert "-0.020" in table

    def test_delta_none_when_field_missing(self, tmp_path: Path):
        _write_report(tmp_path, "20260728_100000", recall=0.4)
        path = _write_report(tmp_path, "20260728_110000", recall=0.5)
        # 第二份报告抹掉 recall@5，差值应为 None（展示为 "-"）
        data = json.loads(path.read_text(encoding="utf-8"))
        del data["overall"]["recall@5"]
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        summaries = trend.load_summaries(tmp_path)
        deltas = trend.compute_deltas(summaries)
        assert deltas[1]["recall"] is None
        assert deltas[1]["mrr"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 控制台表格
# ---------------------------------------------------------------------------

class TestConsoleTables:
    def test_overall_table_content(self, three_reports: Path):
        summaries = trend.load_summaries(three_reports)
        table = trend.format_overall_table(summaries)
        assert "recall@5" in table and "NDCG@5" in table
        assert "Δrecall@5" in table  # 3 份报告 → 含差值列
        assert "0.400" in table and "0.480" in table
        assert "5 (正4/负1)" in table

    def test_type_table_content(self, three_reports: Path):
        summaries = trend.load_summaries(three_reports)
        table = trend.format_type_table(summaries)
        assert "factoid" in table and "summary" in table
        assert "0.600 (n=2)" in table
        # factoid 在三份报告中的 recall 变化 0.5 -> 0.6 -> 0.7
        assert "0.500 (n=2)" in table and "0.700 (n=2)" in table

    def test_type_table_missing_type_shows_dash(self, three_reports: Path):
        # summary 题型只在第二份报告中出现，其余两列应为 "-"
        summaries = trend.load_summaries(three_reports)
        table = trend.format_type_table(summaries)
        summary_row = next(l for l in table.splitlines() if l.startswith("summary"))
        assert summary_row.count("-") >= 2


# ---------------------------------------------------------------------------
# 边界行为
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_empty_dir_friendly_exit(self, tmp_path: Path, capsys):
        report_dir = tmp_path / "empty"
        report_dir.mkdir()
        assert trend.main(["--report-dir", str(report_dir)]) == 0
        out = capsys.readouterr().out
        assert "没有任何 JSON 报告" in out
        assert not (report_dir / "trend.md").exists()

    def test_missing_dir_friendly_exit(self, tmp_path: Path, capsys):
        report_dir = tmp_path / "not_exist"
        assert trend.main(["--report-dir", str(report_dir)]) == 0
        assert "不存在" in capsys.readouterr().out

    def test_single_report_no_delta_columns(self, tmp_path: Path, capsys):
        _write_report(tmp_path, "20260728_100000")
        assert trend.main(["--report-dir", str(tmp_path)]) == 0
        out = capsys.readouterr().out
        assert "Δrecall" not in out  # 单份报告无差值列
        assert "0.500" in out
        # trend.md 正常生成且同样无差值列
        md = (tmp_path / "trend.md").read_text(encoding="utf-8")
        assert "Δrecall" not in md
        assert "20260728_100000" in md

    def test_missing_fields_show_dash(self, tmp_path: Path, capsys):
        # 一份缺 overall、缺 by_question_type 的报告不应让脚本崩溃
        (tmp_path / "20260728_100000.json").write_text(json.dumps({
            "timestamp": "2026-07-28T10:00:00",
            "retrieval_mode": "hybrid",
        }), encoding="utf-8")
        assert trend.main(["--report-dir", str(tmp_path)]) == 0
        out = capsys.readouterr().out
        assert "-" in out
        assert "缺少 by_question_type" in out


# ---------------------------------------------------------------------------
# trend.md 生成
# ---------------------------------------------------------------------------

class TestTrendMarkdown:
    def test_md_generated_with_key_metrics(self, three_reports: Path, capsys):
        assert trend.main(["--report-dir", str(three_reports)]) == 0
        md_path = three_reports / "trend.md"
        assert md_path.exists()
        md = md_path.read_text(encoding="utf-8")
        assert "# 评测趋势报告" in md
        assert "总体指标趋势" in md
        assert "分题型 recall@5 趋势" in md
        assert "各次报告备注" in md
        assert "0.480" in md and "+0.100" in md and "-0.020" in md
        assert "factoid" in md
        # 每份报告都有备注位
        for stem in ("20260728_100000", "20260728_110000", "20260728_120000"):
            assert f"**{stem}**" in md

    def test_manual_notes_preserved(self, three_reports: Path, capsys):
        trend.main(["--report-dir", str(three_reports)])
        md_path = three_reports / "trend.md"
        md = md_path.read_text(encoding="utf-8")
        # 模拟人工填写备注
        md = md.replace("- **20260728_110000**（2026-07-28T11:00:00）：（暂无）",
                        "- **20260728_110000**（2026-07-28T11:00:00）：本次换了 Embedding 模型")
        md_path.write_text(md, encoding="utf-8")
        trend.main(["--report-dir", str(three_reports)])
        md_new = md_path.read_text(encoding="utf-8")
        assert "本次换了 Embedding 模型" in md_new
