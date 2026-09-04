#!/usr/bin/env python3
"""检查前端生产 JS chunk 原始大小，防止懒加载边界被人工聚合破坏。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


JS_BUDGET_BYTES = 600 * 1024
PDF_WORKER_BUDGET_BYTES = 1100 * 1024


def scan_chunks(dist_dir: Path) -> tuple[list[tuple[str, int]], list[str]]:
    assets = dist_dir / "assets"
    if not assets.is_dir():
        return [], ["缺少 dist/assets，请先执行生产构建"]
    chunks = sorted(
        (path for path in assets.iterdir() if path.suffix in {".js", ".mjs"}),
        key=lambda path: path.name,
    )
    if not chunks:
        return [], ["dist/assets 中没有 JS chunk"]
    rows: list[tuple[str, int]] = []
    errors: list[str] = []
    for path in chunks:
        size = path.stat().st_size
        rows.append((path.name, size))
        worker = path.name.startswith("pdf.worker.") or path.name.startswith("pdf.worker.min-")
        budget = PDF_WORKER_BUDGET_BYTES if worker else JS_BUDGET_BYTES
        if size > budget:
            errors.append(
                f"{path.name} 为 {size / 1024:.1f}KiB，超过 {budget / 1024:.0f}KiB"
            )
    return rows, errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PaperMind 前端 JS chunk 预算 Gate")
    parser.add_argument("--dir", default="frontend/dist", help="生产构建目录")
    args = parser.parse_args(argv)
    rows, errors = scan_chunks(Path(args.dir).resolve())
    if errors:
        for error in errors:
            print(f"[frontend-chunks] FAIL: {error}", file=sys.stderr)
        return 1
    largest = max((size for _, size in rows), default=0)
    print(
        f"[frontend-chunks] PASS: {len(rows)} 个 JS chunk，最大 {largest / 1024:.1f}KiB"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
