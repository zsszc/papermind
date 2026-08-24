"""core/config.py 的 PAPERMIND_DATA_DIR 分支契约测试。

锁定 Electron 生产包配置加载路径的现有行为：
- 数据目录自动创建（mkdir -p）
- 生产环境只复制公开 example，绝不复制项目根真实 config.yaml
- 已有用户配置不被覆盖，损坏 YAML 显式报错并保留原文件
- 开发模式缺失 config.yaml 时只读回退 example

注意：
- Config 是单例，每个用例前后重置 ``Config._instance`` 与类属性 ``Config._config``，
  避免用例间互相污染；
- 断言文件一致性时用 sha256 摘要对比，避免失败时把真实 API Key 打进测试日志
  （宪法第 14 条：密钥不出现在日志中）；
- 本文件绝不写真实 ``config.yaml``：所有写操作只发生在 tmp_path 内，
  涉及 bundled 的用例只读它。
"""

import hashlib
from pathlib import Path

import pytest
import yaml

import app.core.config as config_module
from app.core.config import Config

# 与生产代码一致的项目根定位（backend/app/core/config.py 上溯 3 级）
PROJECT_ROOT = Path(config_module.__file__).resolve().parents[3]
BUNDLED_CONFIG = PROJECT_ROOT / "config.yaml"
EXAMPLE_CONFIG = PROJECT_ROOT / "config.yaml.example"


def _sha256(path: Path) -> str:
    """计算文件内容摘要，用于不泄露内容的相等性断言。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_public_template_uses_gated_production_defaults():
    """新安装必须获得已通过 Batch20 Gate 的检索策略与当前可用 Kimi 模型。"""
    public = yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8"))

    assert public["retrieval"]["chat_profile"] == "hybrid"
    assert public["retrieval"]["lexical_profile"] == "bm25-bilingual"
    assert public["retrieval"]["rerank"] is False
    assert public["llm"]["model"] == "kimi-k2.6"
    assert public["llm"]["temperature"] == 1.0


@pytest.fixture(autouse=True)
def _reset_config_singleton(monkeypatch):
    """每个用例前重置 Config 单例并清除 PAPERMIND_DATA_DIR，结束后同样复位。"""
    monkeypatch.delenv("PAPERMIND_DATA_DIR", raising=False)
    Config._instance = None
    Config._config = {}
    yield
    Config._instance = None
    Config._config = {}


@pytest.fixture()
def hide_bundled_config(monkeypatch):
    """仅对「项目根 config.yaml」伪装成不存在，不改动磁盘上的真实文件。"""

    real_exists = Path.exists

    def fake_exists(self):
        if self == BUNDLED_CONFIG:
            return False
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", fake_exists)


class TestDataDirBranch:
    """PAPERMIND_DATA_DIR 已设置（Electron 生产包）时的加载分支。"""

    def test_data_dir_created_when_missing(self, tmp_path, monkeypatch):
        """锁定：数据目录不存在时自动 mkdir -p 创建。"""
        target_dir = tmp_path / "nested" / "appdata"
        assert not target_dir.exists()
        monkeypatch.setenv("PAPERMIND_DATA_DIR", str(target_dir))

        Config()

        assert target_dir.is_dir()

    def test_example_copied_when_target_missing(self, tmp_path, monkeypatch):
        """目标缺失时只复制公开模板，绝不复制项目根真实配置。"""
        monkeypatch.setenv("PAPERMIND_DATA_DIR", str(tmp_path))

        c = Config()

        target = tmp_path / "config.yaml"
        assert c.config_path == target
        assert target.exists()
        assert _sha256(target) == _sha256(EXAMPLE_CONFIG)
        example_cfg = yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
        assert c.get("llm.model") == example_cfg["llm"]["model"]

    def test_existing_placeholder_is_not_overwritten(self, tmp_path, monkeypatch):
        """已有用户配置即使仍为占位符也不被安装包内容覆盖。"""
        monkeypatch.setenv("PAPERMIND_DATA_DIR", str(tmp_path))
        target = tmp_path / "config.yaml"
        original = "llm:\n  api_key: sk-xxxx\n  model: custom-placeholder-model\n"
        target.write_text(original, encoding="utf-8")

        c = Config()

        assert target.read_text(encoding="utf-8") == original
        assert c.get("llm.model") == "custom-placeholder-model"

    def test_existing_empty_key_config_is_not_overwritten(self, tmp_path, monkeypatch):
        """已有空 Key 配置保持原样，避免升级时覆盖用户的其他设置。"""
        monkeypatch.setenv("PAPERMIND_DATA_DIR", str(tmp_path))
        target = tmp_path / "config.yaml"
        original = "llm:\n  api_key: ''\n  model: offline-model\n"
        target.write_text(original, encoding="utf-8")

        c = Config()

        assert target.read_text(encoding="utf-8") == original
        assert c.get("llm.model") == "offline-model"

    def test_existing_your_prefix_config_is_not_overwritten(self, tmp_path, monkeypatch):
        """your- 占位配置同样只由用户主动修改。"""
        monkeypatch.setenv("PAPERMIND_DATA_DIR", str(tmp_path))
        target = tmp_path / "config.yaml"
        original = "llm:\n  api_key: your-api-key-here\n  model: local-only\n"
        target.write_text(original, encoding="utf-8")

        c = Config()

        assert target.read_text(encoding="utf-8") == original
        assert c.get("llm.model") == "local-only"

    def test_unparseable_existing_config_raises_without_overwrite(self, tmp_path, monkeypatch):
        """损坏配置显式报错且保留原文件，禁止静默覆盖用户内容。"""
        monkeypatch.setenv("PAPERMIND_DATA_DIR", str(tmp_path))
        target = tmp_path / "config.yaml"
        original = "llm: 'unclosed quote\n"
        target.write_text(original, encoding="utf-8")

        with pytest.raises(yaml.YAMLError):
            Config()

        assert target.read_text(encoding="utf-8") == original

    def test_real_key_target_not_overwritten(self, tmp_path, monkeypatch):
        """锁定：目标已含真实 Key（非占位符）时不被 bundled 覆盖，且按目标内容加载。"""
        monkeypatch.setenv("PAPERMIND_DATA_DIR", str(tmp_path))
        target = tmp_path / "config.yaml"
        original_text = (
            "llm:\n"
            "  api_key: sk-real-test-key-123456\n"
            "  model: custom-model-for-test\n"
        )
        target.write_text(original_text, encoding="utf-8")

        c = Config()

        # 文件内容保持原样（未被 bundled 覆盖）
        assert target.read_text(encoding="utf-8") == original_text
        # 加载的是目标文件里的自定义值
        assert c.get("llm.api_key") == "sk-real-test-key-123456"
        assert c.get("llm.model") == "custom-model-for-test"

    def test_example_copied_when_bundled_missing(
        self, tmp_path, monkeypatch, hide_bundled_config
    ):
        """锁定：bundled 缺失且目标不存在时，复制 config.yaml.example 兜底。"""
        monkeypatch.setenv("PAPERMIND_DATA_DIR", str(tmp_path))

        c = Config()

        target = tmp_path / "config.yaml"
        assert c.config_path == target
        assert target.exists()
        assert _sha256(target) == _sha256(EXAMPLE_CONFIG)

    def test_data_dir_property_uses_env_dir(self, tmp_path, monkeypatch):
        """锁定：data_dir 属性在 PAPERMIND_DATA_DIR 下返回该目录并确保其存在。"""
        env_dir = tmp_path / "appdata"
        monkeypatch.setenv("PAPERMIND_DATA_DIR", str(env_dir))

        c = Config()

        assert c.data_dir == env_dir
        assert env_dir.is_dir()


class TestDevModeFallback:
    """未设置 PAPERMIND_DATA_DIR（开发模式）时的定位与回退。"""

    def test_dev_mode_uses_project_root_config(self):
        """锁定：开发模式下 config_path 指向项目根 config.yaml。"""
        if not BUNDLED_CONFIG.exists():
            pytest.skip("项目根无 config.yaml")

        c = Config()

        assert c.config_path == BUNDLED_CONFIG

    def test_dev_mode_falls_back_to_example_when_config_missing(
        self, monkeypatch, hide_bundled_config
    ):
        """锁定：项目根 config.yaml 缺失时只读回退 example，config_path 指向 example。

        回退是纯读取，不在项目根创建/复制任何文件。
        """
        c = Config()

        assert c.config_path == EXAMPLE_CONFIG
        example_cfg = yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
        assert c.get("llm.model") == example_cfg["llm"]["model"]
