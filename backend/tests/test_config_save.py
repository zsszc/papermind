"""配置持久化的安全契约测试。"""

import os
import stat

import pytest

import app.core.config as config_module
from app.core.config import Config


def _config_for(path, data=None):
    """构造不触发全局单例加载的隔离配置对象。"""
    instance = object.__new__(Config)
    instance._config_path = path
    instance._config = data or {"llm": {"api_key": "sk-test-private-key"}}
    return instance


def test_save_from_example_creates_private_config_without_overwriting_template(tmp_path):
    """回退读取模板后保存时必须写 sibling config.yaml。"""
    example = tmp_path / "config.yaml.example"
    original = "llm:\n  api_key: your-api-key\n"
    example.write_text(original, encoding="utf-8")
    instance = _config_for(example)

    instance.save()

    private_config = tmp_path / "config.yaml"
    assert example.read_text(encoding="utf-8") == original
    assert private_config.exists()
    assert instance.config_path == private_config


def test_save_serialization_failure_preserves_existing_file(tmp_path, monkeypatch):
    """临时文件写入失败不得截断当前配置。"""
    target = tmp_path / "config.yaml"
    original = b"llm:\n  model: stable-model\n"
    target.write_bytes(original)
    instance = _config_for(target)

    def fail_dump(*args, **kwargs):
        raise RuntimeError("模拟 YAML 序列化失败")

    monkeypatch.setattr(config_module.yaml, "dump", fail_dump)

    with pytest.raises(RuntimeError, match="模拟 YAML"):
        instance.save()

    assert target.read_bytes() == original


@pytest.mark.skipif(os.name != "posix", reason="POSIX 文件权限契约")
def test_save_sets_private_file_permissions(tmp_path):
    """配置含明文密钥，成功保存后必须为 0600。"""
    target = tmp_path / "config.yaml"
    target.write_text("llm: {}\n", encoding="utf-8")
    target.chmod(0o644)
    instance = _config_for(target)

    instance.save()

    assert stat.S_IMODE(target.stat().st_mode) == 0o600
