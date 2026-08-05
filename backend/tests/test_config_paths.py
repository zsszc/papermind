"""core/config.py 的 PAPERMIND_DATA_DIR 分支特征化测试（只测不改）。

锁定 Electron 生产包配置加载路径的现有行为：
- 数据目录自动创建（mkdir -p）
- bundled 配置（项目根 config.yaml）复制/覆盖规则
- 占位符配置检测（sk-xxxx / your- 前缀 / 空 Key / 解析异常）
- bundled 缺失时复制 example、开发模式缺失 config.yaml 时只读回退 example

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

    def test_bundled_copied_when_target_missing(self, tmp_path, monkeypatch):
        """锁定：目标 config.yaml 缺失时，bundled（项目根 config.yaml）被复制过去。

        前置：项目根必须存在真实 config.yaml（本仓库开发机/打包机上均成立）。
        """
        if not BUNDLED_CONFIG.exists():
            pytest.skip("项目根无 config.yaml，无法验证 bundled 复制分支")
        monkeypatch.setenv("PAPERMIND_DATA_DIR", str(tmp_path))

        c = Config()

        target = tmp_path / "config.yaml"
        assert c.config_path == target
        assert target.exists()
        # 用摘要对比，避免断言失败时把真实 Key 打进日志
        assert _sha256(target) == _sha256(BUNDLED_CONFIG)
        # 加载结果来自复制后的配置（取非敏感字段验证）
        bundled_cfg = yaml.safe_load(BUNDLED_CONFIG.read_text(encoding="utf-8"))
        assert c.get("llm.model") == bundled_cfg["llm"]["model"]

    def test_bundled_overwrites_placeholder_with_sk_xxxx(self, tmp_path, monkeypatch):
        """锁定：目标含 sk-xxxx 占位符文本时被 bundled 覆盖。"""
        if not BUNDLED_CONFIG.exists():
            pytest.skip("项目根无 config.yaml，无法验证 bundled 覆盖分支")
        monkeypatch.setenv("PAPERMIND_DATA_DIR", str(tmp_path))
        target = tmp_path / "config.yaml"
        target.write_text("llm:\n  api_key: sk-xxxx\n  model: kimi-k2-7\n", encoding="utf-8")

        Config()

        assert _sha256(target) == _sha256(BUNDLED_CONFIG)

    def test_bundled_overwrites_placeholder_with_your_prefix(self, tmp_path, monkeypatch):
        """锁定：目标含 your- 开头占位符（忽略大小写）时被 bundled 覆盖。"""
        if not BUNDLED_CONFIG.exists():
            pytest.skip("项目根无 config.yaml，无法验证 bundled 覆盖分支")
        monkeypatch.setenv("PAPERMIND_DATA_DIR", str(tmp_path))
        target = tmp_path / "config.yaml"
        target.write_text("llm:\n  api_key: your-api-key-here\n", encoding="utf-8")

        Config()

        assert _sha256(target) == _sha256(BUNDLED_CONFIG)

    def test_bundled_overwrites_placeholder_with_empty_api_key(self, tmp_path, monkeypatch):
        """锁定：目标 YAML 合法但 llm.api_key 为空时，视为占位符被 bundled 覆盖。"""
        if not BUNDLED_CONFIG.exists():
            pytest.skip("项目根无 config.yaml，无法验证 bundled 覆盖分支")
        monkeypatch.setenv("PAPERMIND_DATA_DIR", str(tmp_path))
        target = tmp_path / "config.yaml"
        target.write_text("llm:\n  api_key: ''\n  model: some-model\n", encoding="utf-8")

        Config()

        assert _sha256(target) == _sha256(BUNDLED_CONFIG)

    def test_bundled_overwrites_unparseable_config(self, tmp_path, monkeypatch):
        """锁定：目标文件 YAML 解析失败时，保守按占位符处理并被 bundled 覆盖。"""
        if not BUNDLED_CONFIG.exists():
            pytest.skip("项目根无 config.yaml，无法验证 bundled 覆盖分支")
        monkeypatch.setenv("PAPERMIND_DATA_DIR", str(tmp_path))
        target = tmp_path / "config.yaml"
        # 未闭合引号，必然触发 yaml.YAMLError；且不含任何占位符文本标记
        target.write_text("llm: 'unclosed quote\n", encoding="utf-8")

        Config()

        assert _sha256(target) == _sha256(BUNDLED_CONFIG)

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
