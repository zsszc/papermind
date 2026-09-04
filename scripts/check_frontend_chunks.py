#!/usr/bin/env python3
"""检查前端生产 JS chunk 原始大小，防止懒加载边界被人工聚合破坏。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


JS_BUDGET_BYTES = 600 * 1024
PDF_WORKER_BUDGET_BYTES = 1100 * 1024


def is_pdf_worker(name: str) -> bool:
    return name.startswith("pdf.worker.") or name.startswith("pdf.worker.min-")


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
        worker = is_pdf_worker(path.name)
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
    normal_sizes = [size for name, size in rows if not is_pdf_worker(name)]
    worker_sizes = [size for name, size in rows if is_pdf_worker(name)]
    largest_normal = max(normal_sizes, default=0)
    largest_worker = max(worker_sizes, default=0)
    print(
        f"[frontend-chunks] PASS: {len(rows)} 个 JS chunk，"
        f"普通最大 {largest_normal / 1024:.1f}KiB，"
        f"PDF worker 最大 {largest_worker / 1024:.1f}KiB"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
