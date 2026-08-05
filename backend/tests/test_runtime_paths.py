"""Batch 8：Electron 运行时数据根与安全打包契约测试。"""

from pathlib import Path

import yaml

from app.core.config import Config
from app.routers import papers, thesis


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_runtime_root_defaults_to_project_root(monkeypatch):
    monkeypatch.delenv("PAPERMIND_DATA_DIR", raising=False)
    assert Config().runtime_root == PROJECT_ROOT


def test_runtime_root_uses_electron_data_dir(tmp_path, monkeypatch):
    runtime_root = tmp_path / "PaperMindData"
    monkeypatch.setenv("PAPERMIND_DATA_DIR", str(runtime_root))

    assert Config().runtime_root == runtime_root
    assert runtime_root.is_dir()


def test_mutable_content_dirs_follow_runtime_root(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPERMIND_DATA_DIR", str(tmp_path))

    assert papers.get_papers_dir() == tmp_path / "papers"
    assert papers.get_notes_dir() == tmp_path / "notes"
    assert papers.get_summaries_dir() == tmp_path / "summaries"
    assert thesis.get_thesis_dir() == tmp_path / "my-thesis"


def test_electron_package_excludes_secrets_and_personal_data():
    builder = yaml.safe_load((PROJECT_ROOT / "electron/electron-builder.yml").read_text(encoding="utf-8"))
    resources = builder.get("extraResources", [])
    sources = {item.get("from") for item in resources if isinstance(item, dict)}

    assert "../config.yaml.example" in sources
    assert "../config.yaml" not in sources
    for private_dir in (
        "../data",
        "../papers",
        "../notes",
        "../summaries",
        "../my-thesis",
        "../vector_db",
        "../logs",
        "../backups",
    ):
        assert private_dir not in sources

