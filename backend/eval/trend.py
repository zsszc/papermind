"""评测报告趋势追踪脚本入口。

用法（在 backend/ 目录下）：
    env -u PYTHONPATH venv/bin/python -m eval.trend                 # 扫描默认 reports 目录
    env -u PYTHONPATH venv/bin/python -m eval.trend --report-dir X  # 指定报告目录

行为说明：
- 扫描 eval/reports/*.json（即 eval.run 每次运行写入的 <timestamp>.json），
  按文件名时间戳升序排列；非 JSON 文件（如 trend.md）自动忽略；
- 控制台打印两张表：
  1) 总体指标趋势：每次报告的时间 / 检索模式 / QA 总数 / recall@k / MRR / NDCG@k，
     以及与上一次报告相比的差值（+0.012 / -0.003 格式，仅当报告数 >= 2 时展示）；
  2) 分 question_type 的 recall@k 纵向变化表（行=question_type，列=各次报告）；
- 同时生成 eval/reports/trend.md（中文，同样的两张表 + 每次报告的备注位）；
  备注位中人工填写的内容会在下次生成时保留；
- 边界：reports 目录为空或没有任何 JSON 报告时打印友好提示并以 0 退出；
  只有 1 份报告时正常输出、不含差值列；某份报告缺少字段时该字段显示 "-"，
  不中断整体输出；
- 本模块不导入 app 下任何模块，保证加载本身不触发模型加载或数据库连接。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

# 评测报告默认目录（backend/eval/reports/）
DEFAULT_REPORT_DIR = Path(__file__).resolve().parent / "reports"

# trend.md 中「各次报告备注」条目的解析格式：- **<报告名>**（<时间>）：<备注>
_NOTE_LINE_RE = re.compile(r"^-\s+\*\*(.+?)\*\*（.*?）：(.*)$")

# 报告 overall 中的指标键形如 recall@5 / ndcg@5（k 可变，见 eval.run）
_RECALL_KEY_RE = re.compile(r"^recall@(\d+)$")
_NDCG_KEY_RE = re.compile(r"^ndcg@(\d+)$")


@dataclass
class ReportSummary:
    """单份评测报告的摘要（从报告 JSON 中提取，字段缺失时为 None）。"""

    stem: str  # 文件名时间戳，如 20260728_160433（同时也是报告的唯一标识）
    path: Path
    timestamp: Optional[str] = None  # 报告内的 ISO 时间字符串
    retrieval_mode: Optional[str] = None
    n_positive: Optional[int] = None
    n_negative: Optional[int] = None
    recall: Optional[float] = None  # overall 中的 recall@k
    mrr: Optional[float] = None
    ndcg: Optional[float] = None  # overall 中的 ndcg@k
    k: Optional[int] = None  # 指标截断位置（从 recall@k 键名解析）
    by_type: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    @property
    def n_total(self) -> Optional[int]:
        """QA 总数（正例 + 负例）；两者都缺失时为 None。"""
        if self.n_positive is None and self.n_negative is None:
            return None
        return (self.n_positive or 0) + (self.n_negative or 0)


# ---------------------------------------------------------------------------
# 报告加载与摘要提取
# ---------------------------------------------------------------------------

def _find_metric_key(overall: Dict[str, Any], pattern: re.Pattern) -> Optional[str]:
    """在 overall 字典中定位形如 recall@5 / ndcg@5 的指标键（k 可变）。"""
    for key in overall:
        if isinstance(key, str) and pattern.match(key):
            return key
    return None


def _metric_k(key: Optional[str], pattern: re.Pattern) -> Optional[int]:
    """从指标键（如 recall@5）中解析 k 值；键缺失或不匹配时返回 None。"""
    if key is None:
        return None
    m = pattern.match(key)
    return int(m.group(1)) if m else None


def summarize_report(path: Path) -> ReportSummary:
    """解析一份报告 JSON 并提取趋势所需的摘要字段。

    字段缺失或类型不符时对应字段置 None（展示层显示 "-"），不抛异常；
    但 JSON 本身损坏时抛 json.JSONDecodeError，由调用方决定跳过。
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"报告顶层不是 JSON 对象: {path}")

    summary = ReportSummary(stem=path.stem, path=path)
    summary.timestamp = data.get("timestamp") if isinstance(data.get("timestamp"), str) else None
    mode = data.get("retrieval_mode")
    summary.retrieval_mode = mode if isinstance(mode, str) else None

    overall = data.get("overall")
    if isinstance(overall, dict):
        recall_key = _find_metric_key(overall, _RECALL_KEY_RE)
        ndcg_key = _find_metric_key(overall, _NDCG_KEY_RE)
        summary.k = (_metric_k(recall_key, _RECALL_KEY_RE)
                     or _metric_k(ndcg_key, _NDCG_KEY_RE))

        def _num(key: Optional[str]) -> Optional[float]:
            if key is None:
                return None
            value = overall.get(key)
            return float(value) if isinstance(value, (int, float)) else None

        def _int(key: str) -> Optional[int]:
            value = overall.get(key)
            return int(value) if isinstance(value, (int, float)) else None

        summary.recall = _num(recall_key)
        summary.mrr = _num("mrr")
        summary.ndcg = _num(ndcg_key)
        summary.n_positive = _int("n_positive")
        summary.n_negative = _int("n_negative")

    by_type = data.get("by_question_type")
    if isinstance(by_type, list):
        for row in by_type:
            if not isinstance(row, dict):
                continue
            qtype = row.get("question_type")
            if not isinstance(qtype, str):
                continue
            summary.by_type[qtype] = {
                "n": row.get("n") if isinstance(row.get("n"), (int, float)) else None,
                "recall": (float(row["recall"])
                           if isinstance(row.get("recall"), (int, float)) else None),
                "mrr": (float(row["mrr"])
                        if isinstance(row.get("mrr"), (int, float)) else None),
                "ndcg": (float(row["ndcg"])
                         if isinstance(row.get("ndcg"), (int, float)) else None),
            }
    return summary


def load_summaries(report_dir: Path) -> List[ReportSummary]:
    """扫描报告目录中的 *.json，按文件名时间戳升序返回摘要列表。

    JSON 损坏的报告会打印警告并跳过，不影响其余报告。
    """
    summaries: List[ReportSummary] = []
    for path in sorted(Path(report_dir).glob("*.json"), key=lambda p: p.name):
        try:
            summaries.append(summarize_report(path))
        except (OSError, ValueError) as e:  # 含 json.JSONDecodeError
            print(f"[trend] [warn] 跳过无法解析的报告 {path.name}: {e}", file=sys.stderr)
    return summaries


# ---------------------------------------------------------------------------
# 表格渲染（控制台纯文本，与 eval.run 风格一致）
# ---------------------------------------------------------------------------

def _fmt_value(value: Optional[float]) -> str:
    """指标值格式化：缺失显示 "-"，否则保留三位小数。"""
    return f"{value:.3f}" if value is not None else "-"


def _fmt_delta(value: Optional[float]) -> str:
    """差值格式化：+0.012 / -0.003；任一侧缺失显示 "-"。"""
    return f"{value:+.3f}" if value is not None else "-"


def compute_deltas(summaries: List[ReportSummary]) -> List[Dict[str, Optional[float]]]:
    """计算每份报告相对上一份的 recall/mrr/ndcg 差值。

    返回与 summaries 等长的列表；首份报告三个差值均为 None，
    任一侧指标缺失时对应差值也为 None。
    """
    deltas: List[Dict[str, Optional[float]]] = []
    for idx, cur in enumerate(summaries):
        entry: Dict[str, Optional[float]] = {"recall": None, "mrr": None, "ndcg": None}
        if idx > 0:
            prev = summaries[idx - 1]
            for name in ("recall", "mrr", "ndcg"):
                cur_v = getattr(cur, name)
                prev_v = getattr(prev, name)
                if cur_v is not None and prev_v is not None:
                    entry[name] = cur_v - prev_v
        deltas.append(entry)
    return deltas


def _metric_label(summaries: List[ReportSummary]) -> int:
    """表格表头使用的 k：取最后一份报告的 k，缺失时默认 5。"""
    for summary in reversed(summaries):
        if summary.k is not None:
            return summary.k
    return 5


def _render_plain_table(header: List[str], rows: List[List[str]]) -> str:
    """纯文本表格渲染（列宽按内容自适应，两空格分隔）。"""
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h)
              for i, h in enumerate(header)]
    line = "  ".join(h.ljust(w) for h, w in zip(header, widths))
    body = ["  ".join(c.ljust(w) for c, w in zip(row, widths)) for row in rows]
    return "\n".join([line, "-" * len(line), *body])


def format_overall_table(summaries: List[ReportSummary]) -> str:
    """总体指标趋势表（控制台文本）。仅当报告数 >= 2 时含差值列。"""
    k = _metric_label(summaries)
    with_delta = len(summaries) >= 2
    deltas = compute_deltas(summaries) if with_delta else []

    header = ["时间", "检索模式", "QA总数", f"recall@{k}", "MRR", f"NDCG@{k}"]
    if with_delta:
        header += [f"Δrecall@{k}", "ΔMRR", f"ΔNDCG@{k}"]

    rows: List[List[str]] = []
    for idx, s in enumerate(summaries):
        n_total = s.n_total
        n_text = "-" if n_total is None else (
            f"{n_total} (正{s.n_positive if s.n_positive is not None else '-'}"
            f"/负{s.n_negative if s.n_negative is not None else '-'})")
        row = [
            s.timestamp or s.stem,
            s.retrieval_mode or "-",
            n_text,
            _fmt_value(s.recall),
            _fmt_value(s.mrr),
            _fmt_value(s.ndcg),
        ]
        if with_delta:
            d = deltas[idx]
            row += [_fmt_delta(d["recall"]) if idx > 0 else "-",
                    _fmt_delta(d["mrr"]) if idx > 0 else "-",
                    _fmt_delta(d["ndcg"]) if idx > 0 else "-"]
        rows.append(row)
    return _render_plain_table(header, rows)


def format_type_table(summaries: List[ReportSummary]) -> str:
    """分 question_type 的 recall 纵向变化表（行=question_type，列=各次报告）。

    单元格格式为 "0.667 (n=5)"；某次报告无该题型时显示 "-"。
    """
    k = _metric_label(summaries)
    qtypes = sorted({qt for s in summaries for qt in s.by_type})
    header = [f"question_type (recall@{k})"] + [s.stem for s in summaries]
    rows: List[List[str]] = []
    for qt in qtypes:
        row = [qt]
        for s in summaries:
            cell_data = s.by_type.get(qt)
            if cell_data is None or cell_data["recall"] is None:
                row.append("-")
            else:
                n = cell_data["n"]
                n_text = f"n={n}" if n is not None else "n=?"
                row.append(f"{cell_data['recall']:.3f} ({n_text})")
        rows.append(row)
    if not rows:
        return "（所有报告均缺少 by_question_type 数据）"
    return _render_plain_table(header, rows)


# ---------------------------------------------------------------------------
# trend.md 渲染（含备注保留）
# ---------------------------------------------------------------------------

def _md_escape(text: str) -> str:
    """Markdown 表格单元格转义（竖线会破坏表格结构）。"""
    return text.replace("|", "\\|")


def load_existing_notes(trend_path: Path) -> Dict[str, str]:
    """从已存在的 trend.md 中解析人工填写的备注，键为报告文件名 stem。"""
    notes: Dict[str, str] = {}
    if not trend_path.exists():
        return notes
    try:
        for line in trend_path.read_text(encoding="utf-8").splitlines():
            m = _NOTE_LINE_RE.match(line.strip())
            if m:
                notes[m.group(1)] = m.group(2).strip()
    except OSError:
        pass  # 读取失败按无历史备注处理
    return notes


def render_markdown(summaries: List[ReportSummary], notes: Dict[str, str]) -> str:
    """渲染 trend.md 全文（中文，两张表 + 每次报告的备注位）。"""
    k = _metric_label(summaries)
    with_delta = len(summaries) >= 2
    deltas = compute_deltas(summaries) if with_delta else []

    lines: List[str] = [
        "# 评测趋势报告",
        "",
        f"> 由 `python -m eval.trend` 自动生成于 {time.strftime('%Y-%m-%d %H:%M:%S')}。",
        "> 下方「各次报告备注」中人工填写的内容会在下次生成时保留，其余部分会被覆盖。",
        "",
        "## 总体指标趋势",
        "",
    ]

    header = ["时间", "报告文件", "检索模式", "QA总数",
              f"recall@{k}", "MRR", f"NDCG@{k}"]
    if with_delta:
        header += [f"Δrecall@{k}", "ΔMRR", f"ΔNDCG@{k}"]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join([" --- "] * len(header)) + "|")
    for idx, s in enumerate(summaries):
        n_total = s.n_total
        n_text = "-" if n_total is None else (
            f"{n_total} (正{s.n_positive if s.n_positive is not None else '-'}"
            f"/负{s.n_negative if s.n_negative is not None else '-'})")
        row = [s.timestamp or s.stem, s.stem, _md_escape(s.retrieval_mode or "-"),
               n_text, _fmt_value(s.recall), _fmt_value(s.mrr), _fmt_value(s.ndcg)]
        if with_delta:
            d = deltas[idx]
            row += [_fmt_delta(d["recall"]) if idx > 0 else "-",
                    _fmt_delta(d["mrr"]) if idx > 0 else "-",
                    _fmt_delta(d["ndcg"]) if idx > 0 else "-"]
        lines.append("| " + " | ".join(row) + " |")

    lines += ["", f"## 分题型 recall@{k} 趋势", ""]
    qtypes = sorted({qt for s in summaries for qt in s.by_type})
    if qtypes:
        header = ["question_type"] + [s.stem for s in summaries]
        lines.append("| " + " | ".join(header) + " |")
        lines.append("|" + "|".join([" --- "] * len(header)) + "|")
        for qt in qtypes:
            row = [_md_escape(qt)]
            for s in summaries:
                cell_data = s.by_type.get(qt)
                if cell_data is None or cell_data["recall"] is None:
                    row.append("-")
                else:
                    n = cell_data["n"]
                    n_text = f"n={n}" if n is not None else "n=?"
                    row.append(f"{cell_data['recall']:.3f} ({n_text})")
            lines.append("| " + " | ".join(row) + " |")
    else:
        lines.append("（所有报告均缺少 by_question_type 数据）")

    lines += ["", "## 各次报告备注", ""]
    for s in summaries:
        note = notes.get(s.stem, "").strip() or "（暂无）"
        lines.append(f"- **{s.stem}**（{s.timestamp or s.stem}）：{note}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m eval.trend", description="PaperMind 评测报告趋势追踪")
    parser.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR),
                        help="评测报告目录（默认 eval/reports/）")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    report_dir = Path(args.report_dir)

    if not report_dir.is_dir():
        print(f"[trend] 报告目录 {report_dir} 不存在；"
              f"请先运行 python -m eval.run 生成评测报告。")
        return 0

    summaries = load_summaries(report_dir)
    if not summaries:
        print(f"[trend] 报告目录 {report_dir} 中没有任何 JSON 报告；"
              f"请先运行 python -m eval.run 生成评测报告。")
        return 0

    print(f"[trend] 共加载 {len(summaries)} 份报告（{report_dir}）\n")
    print(format_overall_table(summaries))
    print()
    print(format_type_table(summaries))

    trend_path = report_dir / "trend.md"
    notes = load_existing_notes(trend_path)
    trend_path.write_text(render_markdown(summaries, notes), encoding="utf-8")
    print(f"\n[trend] 趋势报告已写入 {trend_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
