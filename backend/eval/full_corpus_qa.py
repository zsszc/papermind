"""全语料 QA v3 的覆盖规划与隐私安全校验。

本模块只处理本地私有评测资产；控制台输出严格限制为聚合计数。问题、答案、
证据、论文身份和路径不得进入可提交报告。
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any

from eval.dataset import load_dataset, resolve_relevant_chunks, validate_dataset
from eval.generate_qa_v2 import load_frozen_splits


PRIVATE_ROOT = Path(__file__).resolve().parent / "private"
_SPLITS = ("train", "dev", "holdout")


def _validate_assignments(assignments: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    if not isinstance(assignments, list) or not assignments:
        raise ValueError("冻结论文分配不能为空")
    by_uid: dict[str, dict[str, Any]] = {}
    seen_hashes: set[str] = set()
    for index, row in enumerate(assignments, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"第 {index} 个论文分配不是对象")
        uid = row.get("paper_uid")
        digest = row.get("pdf_sha256")
        split = row.get("split")
        if not isinstance(uid, str) or not uid:
            raise ValueError(f"第 {index} 个论文分配缺少 paper_uid")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdefABCDEF" for char in digest)
        ):
            raise ValueError(f"第 {index} 个论文分配的 PDF SHA 非法")
        if split not in _SPLITS:
            raise ValueError(f"第 {index} 个论文分配的 split 非法")
        if uid in by_uid or digest.lower() in seen_hashes:
            raise ValueError("冻结论文分配含重复 paper_uid 或 PDF SHA")
        by_uid[uid] = dict(row)
        seen_hashes.add(digest.lower())
    return by_uid


def _item_paper_uid(item: dict[str, Any]) -> str | None:
    if item.get("has_answer") is not True:
        return None
    uids = {
        evidence.get("paper_uid")
        for evidence in item.get("relevant_evidence", [])
        if isinstance(evidence, dict) and evidence.get("paper_uid")
    }
    if len(uids) != 1:
        raise ValueError(
            f"qa_id={item.get('qa_id', '?')}: 正例必须且只能归属一篇论文"
        )
    return next(iter(uids))


def _normalize_question(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").strip().casefold()
    return " ".join(normalized.split())


def build_gap_plan(
    assignments: list[dict[str, Any]],
    items: list[dict[str, Any]],
    *,
    minimum_per_paper: int = 2,
) -> list[dict[str, Any]]:
    """返回低于最小 QA 覆盖的冻结论文；结果含身份，只能写入私有目录。"""
    if minimum_per_paper <= 0:
        raise ValueError("minimum_per_paper 必须为正整数")
    by_uid = _validate_assignments(assignments)
    validate_dataset(items)
    counts: Counter[str] = Counter()
    for item in items:
        uid = _item_paper_uid(item)
        if uid is None:
            continue
        assignment = by_uid.get(uid)
        if assignment is None:
            raise ValueError(f"qa_id={item.get('qa_id')}: paper_uid 不在冻结分配中")
        if item.get("split") != assignment["split"]:
            raise ValueError(f"qa_id={item.get('qa_id')}: split 与冻结分配不一致")
        counts[uid] += 1

    split_order = {name: index for index, name in enumerate(_SPLITS)}
    plan = []
    for uid, assignment in by_uid.items():
        current = counts[uid]
        if current >= minimum_per_paper:
            continue
        plan.append({
            "paper_uid": uid,
            "pdf_sha256": assignment["pdf_sha256"].lower(),
            "split": assignment["split"],
            "current": current,
            "needed": minimum_per_paper - current,
        })
    plan.sort(key=lambda row: (split_order[row["split"]], row["paper_uid"]))
    return plan


def public_gap_summary(plan: list[dict[str, Any]]) -> dict[str, Any]:
    """只返回补题规模，不泄露论文身份或路径。"""
    by_split: dict[str, dict[str, int]] = {}
    for split in _SPLITS:
        rows = [row for row in plan if row.get("split") == split]
        if rows:
            by_split[split] = {
                "gap_papers": len(rows),
                "required_new_qa": sum(int(row["needed"]) for row in rows),
            }
    return {
        "gap_papers": len(plan),
        "required_new_qa": sum(int(row["needed"]) for row in plan),
        "by_split": by_split,
    }


def validate_full_coverage(
    items: list[dict[str, Any]],
    assignments: list[dict[str, Any]],
    *,
    minimum_per_paper: int = 2,
) -> dict[str, Any]:
    """校验冻结 split、问题去重及每篇论文的最小正例覆盖。"""
    if minimum_per_paper <= 0:
        raise ValueError("minimum_per_paper 必须为正整数")
    by_uid = _validate_assignments(assignments)
    validate_dataset(items)
    counts: Counter[str] = Counter()
    split_items: Counter[str] = Counter()
    questions: dict[str, str] = {}
    errors: list[str] = []

    for item in items:
        qa_id = str(item.get("qa_id", "?"))
        split = item.get("split")
        if split not in _SPLITS:
            errors.append(f"qa_id={qa_id}: split 非法")
        else:
            split_items[split] += 1
        normalized = _normalize_question(str(item.get("question", "")))
        previous = questions.setdefault(normalized, qa_id)
        if previous != qa_id:
            errors.append(f"qa_id={qa_id}: 问题文本重复（首次 {previous}）")
        try:
            uid = _item_paper_uid(item)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if uid is None:
            continue
        assignment = by_uid.get(uid)
        if assignment is None:
            errors.append(f"qa_id={qa_id}: paper_uid 不在冻结分配中")
            continue
        if split != assignment["split"]:
            errors.append(f"qa_id={qa_id}: split 与冻结分配不一致")
            continue
        counts[uid] += 1

    gaps = [uid for uid in by_uid if counts[uid] < minimum_per_paper]
    if gaps:
        errors.append(f"论文 QA 覆盖不足: {len(gaps)} 篇")
    if errors:
        raise ValueError("全语料 QA 校验失败:\n" + "\n".join(f"  - {e}" for e in errors))

    split_papers = Counter(row["split"] for row in by_uid.values())
    return {
        "items": len(items),
        "papers": len(by_uid),
        "minimum_per_paper": minimum_per_paper,
        "split_items": dict(sorted(split_items.items())),
        "split_papers": dict(sorted(split_papers.items())),
    }


def _private_path(value: str | Path, *, must_exist: bool) -> Path:
    path = Path(value)
    if path.is_symlink():
        raise ValueError("私有评测路径不得为软链接")
    resolved = path.resolve()
    if not resolved.is_relative_to(PRIVATE_ROOT.resolve()):
        raise ValueError("评测资产必须位于 eval/private")
    if must_exist and not resolved.is_file():
        raise FileNotFoundError(resolved)
    return resolved


def _write_private_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.is_symlink():
        raise ValueError("输出路径不得为软链接")
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            temp_path = Path(stream.name)
            os.chmod(temp_path, 0o600)
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
        temp_path.replace(path)
        os.chmod(path, 0o600)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def _attach_local_sources(plan: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """将缺口绑定到当前主库 PDF；返回值只能保存到私有任务单。"""
    from app.core.config import config
    from app.database import SessionLocal
    from app.models import Paper
    from eval.private_benchmark import paper_uid, sha256_file

    runtime_root = config.runtime_root.resolve()
    papers_root = (runtime_root / "papers").resolve()
    with SessionLocal() as db:
        mapped: dict[str, list[Paper]] = {}
        for paper in db.query(Paper).order_by(Paper.id).all():
            uid = paper_uid(paper, runtime_root)
            mapped.setdefault(uid, []).append(paper)

        rows = []
        for index, gap in enumerate(plan, start=1):
            matches = mapped.get(gap["paper_uid"], [])
            if len(matches) != 1:
                raise ValueError("冻结论文身份未在当前数据库唯一命中")
            paper = matches[0]
            source = (runtime_root / paper.file_path).resolve()
            if not source.is_file() or not source.is_relative_to(papers_root):
                raise ValueError("冻结论文源 PDF 缺失或逃逸 papers 目录")
            if sha256_file(source) != gap["pdf_sha256"]:
                raise ValueError("冻结论文 PDF SHA 与当前源文件不一致")
            rows.append({
                **gap,
                "paper_id": paper.id,
                "file_path": str(source),
                "authoring_prefix": f"b34-{gap['split'][0]}-{index:02d}",
            })
    return rows


def _resolve_all_evidence(items: list[dict[str, Any]]) -> int:
    from app.core.config import config
    from app.database import SessionLocal

    resolved = 0
    with SessionLocal() as db:
        for item in items:
            if item.get("has_answer") is not True:
                continue
            chunk_ids = resolve_relevant_chunks(db, item, runtime_root=config.runtime_root)
            if not chunk_ids:
                raise ValueError("正例证据未解析到 chunk")
            resolved += 1
    return resolved


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PaperMind 全语料 QA v3 覆盖 Harness")
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan_parser = subparsers.add_parser("plan", help="生成私有补题任务单")
    plan_parser.add_argument("--splits", required=True)
    plan_parser.add_argument("--dataset", required=True)
    plan_parser.add_argument("--output", required=True)
    plan_parser.add_argument("--minimum-per-paper", type=int, default=2)

    validate_parser = subparsers.add_parser("validate", help="验证补题后的完整 v2 QA")
    validate_parser.add_argument("--splits", required=True)
    validate_parser.add_argument("--dataset", required=True)
    validate_parser.add_argument("--minimum-per-paper", type=int, default=2)
    validate_parser.add_argument("--resolve-evidence", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    splits_path = _private_path(args.splits, must_exist=True)
    dataset_path = _private_path(args.dataset, must_exist=True)
    assignments = load_frozen_splits(splits_path)
    items = load_dataset(dataset_path)

    if args.command == "plan":
        output_path = _private_path(args.output, must_exist=False)
        plan = build_gap_plan(
            assignments, items, minimum_per_paper=args.minimum_per_paper
        )
        rows = _attach_local_sources(plan)
        _write_private_json(output_path, {
            "schema": "full-corpus-qa-v3-authoring-plan-v1",
            "minimum_per_paper": args.minimum_per_paper,
            "rows": rows,
        })
        print(json.dumps(public_gap_summary(rows), ensure_ascii=False, sort_keys=True))
        return 0

    summary = validate_full_coverage(
        items, assignments, minimum_per_paper=args.minimum_per_paper
    )
    if args.resolve_evidence:
        summary["uniquely_resolved_positive_qa"] = _resolve_all_evidence(items)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
