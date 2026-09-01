#!/usr/bin/env python3
"""包体预算 Gate（Batch 24 / T3）。

扫描 frontend/out/（或 --dir 指定目录）中的发布制品（dmg/zip）：
1. 制品必须存在且非空；
2. 单个制品大小 <= BUDGET_MB；
3. zip 内容清单与输出目录松散文件中不得夹带数据文件
   （config.yaml / .env / data/ / papers/ 等，路径模式对齐
   electron/scripts/verify-artifact.js 的 Batch 15 禁止清单）。

无制品时显式 SKIP（exit 0 并打印说明）；有制品则硬 Gate，违规 exit 1。
dmg 是二进制镜像，无法低成本列目录；其内容审查由
electron/scripts/verify-artifact.js 在 unpacked 目录上完成，本脚本不重复。
"""

from __future__ import annotations

import argparse
import os
import posixpath
import re
import sys
import zipfile
from pathlib import Path

# 预算集中在顶部常量；调整须在提交信息中说明理由（batch-24 spec 第 6 节）
BUDGET_MB = 800

ARTIFACT_SUFFIXES = {".dmg", ".zip"}

# 禁止夹带的数据文件路径模式（对齐 Batch 15 verify-artifact.js 的 FORBIDDEN_PATHS，
# 数据目录锚定在 Resources 根 / backend 根，避免误伤 venv 内第三方包的 data/ 目录）
FORBIDDEN_PATTERNS = [
    # 真实配置任意深度禁止（config.yaml.example 模板不匹配）
    re.compile(r"(?:^|/)config\.yaml$", re.IGNORECASE),
    re.compile(r"^(?:data|papers|notes|summaries|my-thesis|vector_db|logs|backups)(?:/|$)"),
    re.compile(r"^backend/(?:data|papers|notes|summaries|my-thesis|vector_db|logs|backups)(?:/|$)"),
    re.compile(r"(?:^|/)\.env(?:\.|$)", re.IGNORECASE),  # 环境密钥文件任意深度都禁止
    re.compile(r"\.(?:db|sqlite|sqlite3)$", re.IGNORECASE),  # 数据库文件任意深度都禁止
]

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT_DIR = PROJECT_ROOT / "frontend" / "out"

# mac 包内路径前缀：<App>.app/Contents/Resources/，比对前归一到 Resources 根
_APP_RESOURCES_RE = re.compile(r"(?:^|/)[^/]+\.app/Contents/Resources/(.*)$")


def normalize_entry(name: str) -> str:
    """把 zip 条目 / 松散文件的相对路径归一到「应用资源根」再比对禁止清单。"""
    normalized = posixpath.normpath(name.replace("\\", "/"))
    match = _APP_RESOURCES_RE.search(normalized)
    if match:
        return match.group(1)
    # 只剥离 "./" 前缀（lstrip 会误吃 .env 的前导点）
    return normalized[2:] if normalized.startswith("./") else normalized


def is_forbidden(relative_path: str) -> bool:
    normalized = normalize_entry(relative_path.replace(os.sep, "/"))
    return any(pattern.search(normalized) for pattern in FORBIDDEN_PATTERNS)


def find_artifacts(out_dir: Path) -> list[Path]:
    """输出目录顶层的 dmg/zip 视为发布制品（blockmap 等附属文件不算）。"""
    return sorted(
        entry
        for entry in out_dir.iterdir()
        if entry.is_file() and entry.suffix.lower() in ARTIFACT_SUFFIXES
    )


def scan_zip(zip_path: Path) -> list[str]:
    """列出 zip 内容清单，返回夹带数据文件的违规描述。"""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
    except zipfile.BadZipFile:
        return [f"zip 制品无法解析（文件损坏）: {zip_path.name}"]
    return [f"zip 夹带数据文件: {zip_path.name}!{name}" for name in names if is_forbidden(name)]


def scan_loose_files(out_dir: Path, artifacts: list[Path]) -> list[str]:
    """扫描输出目录树的松散文件（含 unpacked 应用目录），数据文件不得落在制品旁。"""
    artifact_names = {artifact.name for artifact in artifacts}
    errors = []
    for dirpath, _dirnames, filenames in os.walk(out_dir):
        for filename in filenames:
            if dirpath == str(out_dir) and filename in artifact_names:
                continue  # 制品二进制本体不做文件名比对
            relative = Path(dirpath, filename).relative_to(out_dir).as_posix()
            if is_forbidden(relative):
                errors.append(f"输出目录夹带数据文件: {relative}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PaperMind 包体预算 Gate（Batch 24 / T3）")
    parser.add_argument(
        "--dir",
        default=str(DEFAULT_OUT_DIR),
        help="制品输出目录（默认 frontend/out/）",
    )
    args = parser.parse_args(argv)

    out_dir = Path(args.dir).resolve()
    if not out_dir.is_dir():
        print(f"[artifact-budget] SKIP: 制品目录不存在 {out_dir}（尚未执行 electron 构建）")
        return 0

    artifacts = find_artifacts(out_dir)
    if not artifacts:
        print(f"[artifact-budget] SKIP: {out_dir} 内无 dmg/zip 制品")
        return 0

    errors: list[str] = []
    for artifact in artifacts:
        size = artifact.stat().st_size
        if size == 0:
            errors.append(f"制品为空: {artifact.name}")
        elif size > BUDGET_MB * 1024 * 1024:
            errors.append(
                f"制品超预算 {BUDGET_MB}MB: {artifact.name}（{size / 1024 / 1024:.1f}MB）"
            )
        if artifact.suffix.lower() == ".zip":
            errors.extend(scan_zip(artifact))
    errors.extend(scan_loose_files(out_dir, artifacts))

    if errors:
        for error in errors:
            print(f"[artifact-budget] FAIL: {error}", file=sys.stderr)
        return 1
    print(f"[artifact-budget] PASS: {len(artifacts)} 个制品均在 {BUDGET_MB}MB 预算内且无数据夹带")
    return 0


if __name__ == "__main__":
    sys.exit(main())
