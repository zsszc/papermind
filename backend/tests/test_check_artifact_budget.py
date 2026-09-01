"""scripts/check_artifact_budget.py 的自测（Batch 24 / T3：包体预算 Gate）。

用临时假目录验证：
- 无制品 / 目录不存在 → 显式 SKIP（exit 0）
- 制品为空、超预算、zip 夹带数据文件、松散数据文件 → 必 fail（exit 1）
- 合规制品 → PASS（exit 0）
"""

import importlib.util
import sys
import zipfile
from pathlib import Path

import pytest

# 脚本位于仓库根 scripts/，经文件路径加载（scripts/ 非 Python 包）
SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "check_artifact_budget.py"
_spec = importlib.util.spec_from_file_location("check_artifact_budget", SCRIPT_PATH)
budget = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("check_artifact_budget", budget)
_spec.loader.exec_module(budget)


def _make_zip(path: Path, entries: list[str]) -> None:
    """构造最小 zip 制品：每个条目写 1 字节内容。"""
    with zipfile.ZipFile(path, "w") as zf:
        for entry in entries:
            zf.writestr(entry, b"x")


def _make_dmg(path: Path, size: int = 64) -> None:
    path.write_bytes(b"\0" * size)


# ---------- SKIP 分支 ----------


def test_目录不存在时显式_SKIP(tmp_path, capsys):
    code = budget.main(["--dir", str(tmp_path / "不存在")])
    assert code == 0
    assert "SKIP" in capsys.readouterr().out


def test_目录存在但无制品时显式_SKIP(tmp_path, capsys):
    code = budget.main(["--dir", str(tmp_path)])
    assert code == 0
    assert "SKIP" in capsys.readouterr().out


# ---------- 合规 PASS ----------


def test_合规制品通过(tmp_path, capsys):
    _make_dmg(tmp_path / "PaperMind-1.0.0.dmg")
    _make_zip(
        tmp_path / "PaperMind-1.0.0-mac.zip",
        [
            "PaperMind.app/Contents/Resources/app.asar",
            "PaperMind.app/Contents/Resources/backend/app/main.py",
            # 配置模板属合法打包内容，不得误伤
            "PaperMind.app/Contents/Resources/config.yaml.example",
        ],
    )
    code = budget.main(["--dir", str(tmp_path)])
    assert code == 0
    assert "PASS" in capsys.readouterr().out


# ---------- 硬 Gate 失败分支 ----------


def test_空制品必失败(tmp_path, capsys):
    (tmp_path / "PaperMind-1.0.0.dmg").write_bytes(b"")
    code = budget.main(["--dir", str(tmp_path)])
    assert code == 1
    assert "为空" in capsys.readouterr().err


def test_超预算必失败(tmp_path, capsys, monkeypatch):
    # 预算收紧到 0MB，任何非空制品都超预算（避免在测试里真写 800MB 文件）
    monkeypatch.setattr(budget, "BUDGET_MB", 0)
    _make_dmg(tmp_path / "PaperMind-1.0.0.dmg", size=128)
    code = budget.main(["--dir", str(tmp_path)])
    assert code == 1
    assert "超" in capsys.readouterr().err


def test_zip夹带config_yaml必失败(tmp_path, capsys):
    _make_zip(
        tmp_path / "PaperMind-1.0.0-mac.zip",
        ["PaperMind.app/Contents/Resources/config.yaml"],
    )
    code = budget.main(["--dir", str(tmp_path)])
    assert code == 1
    assert "config.yaml" in capsys.readouterr().err


@pytest.mark.parametrize(
    "entry",
    [
        "PaperMind.app/Contents/Resources/./config.yaml",
        "PaperMind.app/Contents/Resources/backend/../config.yaml",
        "PaperMind.app/Contents/Resources/.ENV",
    ],
)
def test_zip非规范路径不得绕过密钥扫描(tmp_path, capsys, entry):
    _make_zip(tmp_path / "PaperMind-1.0.0-mac.zip", [entry])
    code = budget.main(["--dir", str(tmp_path)])
    assert code == 1
    assert "夹带" in capsys.readouterr().err


@pytest.mark.parametrize(
    "entry",
    [
        "PaperMind.app/Contents/Resources/data/papers.db",
        "PaperMind.app/Contents/Resources/papers/论文.pdf",
        "PaperMind.app/Contents/Resources/backend/vector_db/chroma.sqlite3",
        "PaperMind.app/Contents/Resources/backend/.env",
    ],
)
def test_zip夹带数据目录或密钥必失败(tmp_path, capsys, entry):
    _make_zip(tmp_path / "PaperMind-1.0.0-mac.zip", [entry])
    code = budget.main(["--dir", str(tmp_path)])
    assert code == 1
    assert entry.split("/")[-1] in capsys.readouterr().err


def test_损坏zip必失败(tmp_path, capsys):
    (tmp_path / "PaperMind-1.0.0-mac.zip").write_bytes(b"not-a-zip")
    code = budget.main(["--dir", str(tmp_path)])
    assert code == 1
    assert "zip" in capsys.readouterr().err.lower()


@pytest.mark.parametrize(
    "loose",
    [
        "config.yaml",
        "mac-arm64/PaperMind.app/Contents/Resources/data/papers.db",
        ".env.local",
    ],
)
def test_输出目录松散数据文件必失败(tmp_path, capsys, loose):
    _make_dmg(tmp_path / "PaperMind-1.0.0.dmg")
    target = tmp_path / loose
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("夹带", encoding="utf-8")
    code = budget.main(["--dir", str(tmp_path)])
    assert code == 1
    assert Path(loose).name in capsys.readouterr().err


def test_松散合规文件不误报(tmp_path, capsys):
    _make_dmg(tmp_path / "PaperMind-1.0.0.dmg")
    # electron-builder 的调试产物与 blockmap 是合法邻居
    (tmp_path / "builder-debug.yml").write_text("# debug", encoding="utf-8")
    (tmp_path / "PaperMind-1.0.0.dmg.blockmap").write_bytes(b"\0" * 8)
    code = budget.main(["--dir", str(tmp_path)])
    assert code == 0
    assert "PASS" in capsys.readouterr().out
