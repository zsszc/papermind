"""公开离线生成 Guardrail 指标与门禁（无 LLM / Embedding / 网络）。"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from statistics import mean
from typing import Any, Dict, Iterable, List

from app.services.generation_guardrails import context_chunk_id, verify_citations_detailed
from eval.metrics import contains_refusal


PUBLIC_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "generation_guardrails_public_v1.json"
DEFAULT_REPORT_DIR = Path(__file__).resolve().parent / "reports" / "public-generation"
REPORT_FILENAME = "generation_guardrails_public_v1.json"
REPORT_SCHEMA = "papermind-generation-guardrail-report-v1"
FORBIDDEN_MODULE_PREFIXES = (
    "app.services.llm",
    "app.services.embedding",
    "app.services.retrieval",
    "app.services.web_search",
    "openai",
    "chromadb",
    "sentence_transformers",
)
_GUARDRAIL_CONTRACT = {
    "citation_syntax": "[^n^]",
    "precision": "unique-relevant-first-citations/all-citation-claims",
    "recall": "unique-relevant-cited/all-relevant",
    "aggregation": "per-positive-case-macro-average",
    "safe_refusal": "refusal-phrase-and-zero-claims-and-zero-retrieved",
    "thresholds": {
        "citation_precision": 0.90,
        "citation_recall": 0.90,
        "citation_f1": 0.90,
        "negative_refusal_rate": 0.90,
        "protocol_violation_count": 0,
    },
}


def _f1(precision: float, recall: float) -> float:
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def _mean(values: Iterable[float]) -> float:
    items = list(values)
    return mean(items) if items else 0.0


def evaluate_generation_cases(cases: List[Dict[str, Any]]) -> Dict[str, Any]:
    """评估固定回答；报告仅保留指标和计数，不回写正文或证据。"""
    positive_scores: List[Dict[str, float]] = []
    case_reports: List[Dict[str, Any]] = []
    negative_total = 0
    negative_safe = 0
    negative_citation_count = 0
    totals = {"out_of_range": 0, "malformed": 0, "duplicate_valid": 0}

    seen_case_ids: set[str] = set()
    for case in cases or []:
        case_id = case.get("case_id")
        if not isinstance(case_id, str) or not case_id or case_id in seen_case_ids:
            raise ValueError("case_id 必须是唯一非空字符串")
        seen_case_ids.add(case_id)
        retrieved = case.get("retrieved_chunks") or []
        retrieved_ids = [context_chunk_id(chunk) for chunk in retrieved]
        if any(chunk_id is None for chunk_id in retrieved_ids):
            raise ValueError(f"case {case_id} 包含畸形 chunk id")
        if len(set(retrieved_ids)) != len(retrieved_ids):
            raise ValueError(f"case {case_id} 包含重复 chunk id")
        cleaned, verification, cited_ids = verify_citations_detailed(
            case.get("answer", ""), retrieved
        )
        for key in totals:
            totals[key] += verification[key]

        report: Dict[str, Any] = {
            "case_id": case_id,
            "has_answer": bool(case.get("has_answer")),
            "citation_claim_count": verification["total"],
            "valid_citation_count": verification["valid"],
            "unique_valid_citation_count": verification["unique_valid"],
            "duplicate_citation_count": verification["duplicate_valid"],
            "out_of_bounds_citation_count": verification["out_of_range"],
            "malformed_citation_count": verification["malformed"],
        }
        if case.get("has_answer"):
            relevant = set(case.get("relevant_chunk_ids") or [])
            correct = len(set(cited_ids) & relevant)
            precision = correct / verification["total"] if verification["total"] else 0.0
            recall = correct / len(relevant) if relevant else 0.0
            scores = {
                "citation_precision": precision,
                "citation_recall": recall,
                "citation_f1": _f1(precision, recall),
            }
            positive_scores.append(scores)
            report.update(scores)
        else:
            negative_total += 1
            refused = contains_refusal(cleaned) and not bool(case.get("generation_error"))
            safe_refusal = (
                refused
                and verification["total"] == 0
                and not retrieved
                and not bool(case.get("generation_error"))
            )
            negative_safe += int(safe_refusal)
            negative_citation_count += verification["total"]
            report.update({"refused": refused, "safe_refusal": safe_refusal})
        case_reports.append(report)

    overall = {
        "case_count": len(case_reports),
        "positive_case_count": len(positive_scores),
        "negative_case_count": negative_total,
        "citation_precision": _mean(s["citation_precision"] for s in positive_scores),
        "citation_recall": _mean(s["citation_recall"] for s in positive_scores),
        "citation_f1": _mean(s["citation_f1"] for s in positive_scores),
        "out_of_bounds_citation_count": totals["out_of_range"],
        "malformed_citation_count": totals["malformed"],
        "duplicate_citation_count": totals["duplicate_valid"],
        "negative_refused_count": negative_safe,
        "negative_refusal_rate": negative_safe / negative_total if negative_total else 0.0,
        "negative_citation_count": negative_citation_count,
    }
    return {"overall": overall, "cases": case_reports}


def build_generation_guardrail_gate(report: Dict[str, Any]) -> Dict[str, Any]:
    """构建严格公开 Gate：引用指标不低于 0.90，且协议违规为零。"""
    overall = report["overall"]
    thresholds = {
        "citation_precision": (">=", 0.90),
        "citation_recall": (">=", 0.90),
        "citation_f1": (">=", 0.90),
        "out_of_bounds_citation_count": ("==", 0),
        "malformed_citation_count": ("==", 0),
        "duplicate_citation_count": ("==", 0),
        "negative_refusal_rate": (">=", 0.90),
        "negative_citation_count": ("==", 0),
    }
    checks = {}
    for metric, (operator, threshold) in thresholds.items():
        actual = overall[metric]
        passed = actual >= threshold if operator == ">=" else actual == threshold
        checks[metric] = {
            "actual": actual,
            "operator": operator,
            "threshold": threshold,
            "passed": passed,
        }
    return {"passed": all(item["passed"] for item in checks.values()), "checks": checks}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _validate_public_fixture(fixture: Dict[str, Any]) -> List[Dict[str, Any]]:
    if fixture.get("fixture_schema") != "papermind-generation-guardrail-fixture-v1":
        raise ValueError("公开生成 fixture schema 不兼容")
    if fixture.get("benchmark_id") != "papermind-generation-public-v1":
        raise ValueError("公开生成 benchmark_id 不匹配")
    if fixture.get("license") != "CC0-1.0" or fixture.get("synthetic") is not True:
        raise ValueError("公开生成 fixture 必须是 CC0 原创合成语料")
    cases = fixture.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("公开生成 fixture cases 不得为空")
    return cases


def _report_dir(path: Path | None) -> Path:
    target = (path or DEFAULT_REPORT_DIR).resolve()
    reports_root = DEFAULT_REPORT_DIR.parent.resolve()
    if target != reports_root and reports_root not in target.parents:
        raise ValueError("生成评测报告只能写入 backend/eval/reports")
    return target


def run_public_generation_gate(
    report_dir: Path | None = None,
    audit_counters: Dict[str, int] | None = None,
    forbidden_modules_loaded: List[str] | None = None,
) -> tuple[Dict[str, Any], Path]:
    """运行冻结公开 fixture 并写入不含问题、答案、证据正文的报告。"""
    fixture_bytes = PUBLIC_FIXTURE.read_bytes()
    fixture = json.loads(fixture_bytes)
    cases = _validate_public_fixture(fixture)
    evaluation = evaluate_generation_cases(cases)
    gate = build_generation_guardrail_gate(evaluation)
    forbidden_loaded = sorted(forbidden_modules_loaded or [])
    counters = audit_counters or {}
    report = {
        "report_schema": REPORT_SCHEMA,
        "benchmark_id": fixture["benchmark_id"],
        "license": fixture["license"],
        "synthetic": True,
        "fixture_sha256": _sha256(fixture_bytes),
        "guardrail_contract_sha256": _sha256(
            json.dumps(_GUARDRAIL_CONTRACT, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ),
        "overall": evaluation["overall"],
        "cases": evaluation["cases"],
        "gate": gate,
        "offline_proof": {
            "network_attempts": counters.get("network_attempts", 0),
            "subprocess_attempts": counters.get("subprocess_attempts", 0),
            "private_path_attempts": counters.get("private_path_attempts", 0),
            "forbidden_modules_loaded": forbidden_loaded,
        },
    }
    if forbidden_loaded:
        report["gate"]["passed"] = False
    target_dir = _report_dir(report_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    report_path = target_dir / REPORT_FILENAME
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return report, report_path


def _install_offline_audit() -> Dict[str, int]:
    """CLI 执行阶段 fail-close：拒绝网络、子进程与私有路径读取。"""
    counters = {
        "network_attempts": 0,
        "subprocess_attempts": 0,
        "private_path_attempts": 0,
    }
    project_root = Path(__file__).resolve().parents[2]
    forbidden_paths = [
        project_root / "papers",
        project_root / "data",
        project_root / "vector_db",
        project_root / "config.yaml",
        Path(__file__).resolve().parent / "private",
    ]

    def hook(event: str, args: tuple) -> None:
        if event.startswith("socket."):
            counters["network_attempts"] += 1
            raise RuntimeError("公开生成 Harness 禁止网络")
        if event in {"subprocess.Popen", "os.system", "os.posix_spawn"}:
            counters["subprocess_attempts"] += 1
            raise RuntimeError("公开生成 Harness 禁止子进程")
        if event == "open" and args and isinstance(args[0], (str, bytes)):
            try:
                opened = Path(args[0]).resolve()
            except (OSError, TypeError, ValueError):
                return
            if any(opened == path.resolve() or path.resolve() in opened.parents for path in forbidden_paths):
                counters["private_path_attempts"] += 1
                raise RuntimeError("公开生成 Harness 禁止访问私有路径")

    sys.addaudithook(hook)
    return counters


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PaperMind 公开离线生成 Guardrail Gate")
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    args = parser.parse_args(argv)
    forbidden_loaded = sorted(
        name for name in sys.modules
        if any(name == prefix or name.startswith(prefix + ".") for prefix in FORBIDDEN_MODULE_PREFIXES)
    )
    counters = _install_offline_audit()
    try:
        report, report_path = run_public_generation_gate(
            args.report_dir, counters, forbidden_loaded
        )
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"generation guardrail invalid: {type(exc).__name__}", file=sys.stderr)
        return 2
    overall = report["overall"]
    print(
        "generation guardrail "
        f"P={overall['citation_precision']:.3f} "
        f"R={overall['citation_recall']:.3f} "
        f"F1={overall['citation_f1']:.3f} "
        f"refusal={overall['negative_refusal_rate']:.3f} "
        f"gate={'PASS' if report['gate']['passed'] else 'FAIL'} "
        f"report={report_path.name}"
    )
    return 0 if report["gate"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
