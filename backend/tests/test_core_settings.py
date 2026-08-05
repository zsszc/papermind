"""core/settings.py 的环境变量覆盖与启动校验特征化测试（只测不改）。

锁定以下现有行为：
- ``apply_env_overrides`` / ``EnvOverrides.apply_to``：PAPERMIND_* 环境变量
  覆盖进 Config 内存配置，空字符串不覆盖，只改内存不落盘；
- ``_dotted_set``：中间层缺失自动建 dict；中间层已存在且非 dict 时抛 TypeError
  （当前无防御，测试钉死该行为以便后续改动时显式决策）；
- ``validate_startup_config``：4 条告警规则（空 Key / 占位符 / model 缺失 /
  base_url 缺协议头），只告警不阻断，每条告警写 ``[config]`` 前缀 WARNING 日志。

所有用例通过 monkeypatch 控制环境变量与 Config._config，不触碰真实 config.yaml。
"""

import logging

import pytest

from app.core.config import Config
from app.core.settings import (
    EnvOverrides,
    _dotted_set,
    apply_env_overrides,
    validate_startup_config,
)

_PAPERMIND_ENVS = (
    "PAPERMIND_LLM_API_KEY",
    "PAPERMIND_LLM_MODEL",
    "PAPERMIND_LLM_BASE_URL",
)


@pytest.fixture(autouse=True)
def _clean_env_and_singleton(monkeypatch):
    """清理 PAPERMIND_* 环境变量并重置 Config 单例，保证用例间隔离。"""
    for name in _PAPERMIND_ENVS:
        monkeypatch.delenv(name, raising=False)
    Config._instance = None
    Config._config = {}
    yield
    Config._instance = None
    Config._config = {}


def _make_config(cfg_dict) -> Config:
    """构造一个加载完成的 Config 实例，内存配置替换为指定 dict。"""
    c = Config()
    c._config = cfg_dict
    return c


class TestApplyEnvOverrides:
    """环境变量覆盖行为。"""

    def test_no_env_returns_empty_and_config_unchanged(self):
        """锁定：无 PAPERMIND_* 环境变量时返回 [] 且配置完全不变。"""
        c = _make_config({"llm": {"api_key": "sk-real-abc", "model": "m1"}})
        before = {"llm": {"api_key": "sk-real-abc", "model": "m1"}}

        applied = apply_env_overrides(c)

        assert applied == []
        assert c._config == before

    def test_api_key_override_applied_in_memory(self, monkeypatch, caplog):
        """锁定：PAPERMIND_LLM_API_KEY 覆盖内存值，返回 ["llm.api_key"]，并写 INFO 日志。"""
        monkeypatch.setenv("PAPERMIND_LLM_API_KEY", "sk-env-override-123")
        c = _make_config({"llm": {"api_key": "sk-old", "model": "m1"}})

        with caplog.at_level(logging.INFO, logger="papermind"):
            applied = apply_env_overrides(c)

        assert applied == ["llm.api_key"]
        assert c.get("llm.api_key") == "sk-env-override-123"
        # 原有其他键不受影响
        assert c.get("llm.model") == "m1"
        assert any("[config] 环境变量覆盖: llm.api_key" in r.message for r in caplog.records)

    def test_override_not_persisted_to_disk(self, tmp_path, monkeypatch):
        """锁定：环境变量覆盖只改内存，不写回 config.yaml（不落盘）。"""
        monkeypatch.setenv("PAPERMIND_LLM_API_KEY", "sk-env-override-123")
        c = _make_config({"llm": {"api_key": "sk-old"}})
        # 把配置路径指到 tmp 文件，覆盖后断言文件字节纹丝不动
        cfg_file = tmp_path / "config.yaml"
        original = "llm:\n  api_key: sk-old\n"
        cfg_file.write_text(original, encoding="utf-8")
        c._config_path = cfg_file

        apply_env_overrides(c)

        assert c.get("llm.api_key") == "sk-env-override-123"  # 内存已改
        assert cfg_file.read_text(encoding="utf-8") == original  # 磁盘未动

    def test_empty_string_env_does_not_override(self, monkeypatch):
        """锁定：环境变量显式设为空字符串时视为未设置，不覆盖原值。"""
        monkeypatch.setenv("PAPERMIND_LLM_API_KEY", "")
        c = _make_config({"llm": {"api_key": "sk-real-abc"}})

        applied = apply_env_overrides(c)

        assert applied == []
        assert c.get("llm.api_key") == "sk-real-abc"

    def test_only_model_env_applied(self, monkeypatch):
        """锁定：只设 PAPERMIND_LLM_MODEL 时仅覆盖 llm.model。"""
        monkeypatch.setenv("PAPERMIND_LLM_MODEL", "kimi-env-model")
        c = _make_config({"llm": {"api_key": "sk-real-abc", "model": "old-model"}})

        applied = apply_env_overrides(c)

        assert applied == ["llm.model"]
        assert c.get("llm.model") == "kimi-env-model"
        assert c.get("llm.api_key") == "sk-real-abc"

    def test_all_three_applied_in_fixed_order(self, monkeypatch):
        """锁定：三个环境变量同时设置时，返回列表顺序固定为 api_key/model/base_url。"""
        monkeypatch.setenv("PAPERMIND_LLM_API_KEY", "sk-env-key")
        monkeypatch.setenv("PAPERMIND_LLM_MODEL", "env-model")
        monkeypatch.setenv("PAPERMIND_LLM_BASE_URL", "https://env.example.com/v1")
        c = _make_config({})

        applied = apply_env_overrides(c)

        assert applied == ["llm.api_key", "llm.model", "llm.base_url"]
        assert c.get("llm.base_url") == "https://env.example.com/v1"

    def test_creates_nested_llm_dict_when_missing(self, monkeypatch):
        """锁定：config 中原本没有 llm 键时，_dotted_set 自动创建嵌套结构。"""
        monkeypatch.setenv("PAPERMIND_LLM_MODEL", "env-model")
        c = _make_config({})

        applied = apply_env_overrides(c)

        assert applied == ["llm.model"]
        assert c._config == {"llm": {"model": "env-model"}}

    def test_dotted_set_raises_on_nondict_intermediate(self, monkeypatch):
        """锁定：llm 已存在但为非 dict（如 str）时，_dotted_set 抛 TypeError（无防御）。"""
        monkeypatch.setenv("PAPERMIND_LLM_API_KEY", "sk-env-key")
        c = _make_config({"llm": "not-a-dict"})

        with pytest.raises(TypeError):
            apply_env_overrides(c)

    def test_dotted_set_direct_call(self):
        """锁定：_dotted_set 直接调用时按点分路径写入并自动建中间层。"""
        cfg = {}
        _dotted_set(cfg, "a.b.c", 1)
        assert cfg == {"a": {"b": {"c": 1}}}

    def test_env_overrides_class_reads_env_at_instantiation(self, monkeypatch):
        """锁定：EnvOverrides 实例化时读取环境，未设置的字段为空字符串。"""
        monkeypatch.setenv("PAPERMIND_LLM_MODEL", "m-x")

        env = EnvOverrides()

        assert env.llm_model == "m-x"
        assert env.llm_api_key == ""
        assert env.llm_base_url == ""


class TestValidateStartupConfig:
    """启动校验的 4 条告警规则与「只告警不阻断」契约。"""

    def test_warns_when_api_key_missing(self):
        """锁定：llm.api_key 缺失时告警「为空，LLM 功能不可用」。"""
        c = _make_config({"llm": {"model": "m1", "base_url": "https://a.com/v1"}})

        warnings = validate_startup_config(c)

        assert "llm.api_key 为空，LLM 功能不可用" in warnings

    def test_warns_when_api_key_whitespace_only(self):
        """锁定：api_key 为纯空白时 strip 后按空处理，告警「为空」。"""
        c = _make_config({"llm": {"api_key": "   ", "model": "m1"}})

        warnings = validate_startup_config(c)

        assert "llm.api_key 为空，LLM 功能不可用" in warnings

    def test_warns_on_placeholder_sk_xxxx(self):
        """锁定：api_key 含 sk-xxxx 时告警「仍是占位符」。"""
        c = _make_config({"llm": {"api_key": "sk-xxxxxxxx", "model": "m1"}})

        warnings = validate_startup_config(c)

        assert "llm.api_key 仍是占位符，请在 config.yaml 中填入真实 Key" in warnings

    def test_warns_on_placeholder_your_prefix(self):
        """锁定：api_key 含 your- 子串时告警「仍是占位符」。"""
        c = _make_config({"llm": {"api_key": "your-api-key", "model": "m1"}})

        warnings = validate_startup_config(c)

        assert "llm.api_key 仍是占位符，请在 config.yaml 中填入真实 Key" in warnings

    def test_warns_on_xxxx_substring_even_in_real_key(self):
        """锁定：子串匹配的已知粗糙点——真实 Key 碰巧含 xxxx 也会误报占位符。"""
        c = _make_config({"llm": {"api_key": "sk-realxxxx123", "model": "m1"}})

        warnings = validate_startup_config(c)

        assert "llm.api_key 仍是占位符，请在 config.yaml 中填入真实 Key" in warnings

    def test_real_key_no_api_key_warning(self):
        """锁定：真实 Key 不产生任何 api_key 相关告警。"""
        c = _make_config({"llm": {"api_key": "sk-real-abc123", "model": "m1"}})

        warnings = validate_startup_config(c)

        assert not any("api_key" in w for w in warnings)

    def test_warns_when_model_missing(self):
        """锁定：llm.model 缺失时告警「未配置」。"""
        c = _make_config({"llm": {"api_key": "sk-real-abc123"}})

        warnings = validate_startup_config(c)

        assert "llm.model 未配置" in warnings

    def test_base_url_without_protocol_warns(self):
        """锁定：base_url 非空但不以 http(s):// 开头时告警「格式异常: {base_url}」。"""
        c = _make_config(
            {"llm": {"api_key": "sk-real-abc123", "model": "m1", "base_url": "api.moonshot.cn/v1"}}
        )

        warnings = validate_startup_config(c)

        assert "llm.base_url 格式异常: api.moonshot.cn/v1" in warnings

    def test_base_url_empty_no_warning(self):
        """锁定：base_url 为空时不告警（允许走 llm.py 内部默认）。"""
        c = _make_config({"llm": {"api_key": "sk-real-abc123", "model": "m1", "base_url": ""}})

        warnings = validate_startup_config(c)

        assert not any("base_url" in w for w in warnings)

    def test_all_good_returns_empty(self):
        """锁定：三项全部合法时返回空列表。"""
        c = _make_config(
            {
                "llm": {
                    "api_key": "sk-real-abc123",
                    "model": "kimi-k2-7",
                    "base_url": "https://api.moonshot.cn/v1",
                }
            }
        )

        warnings = validate_startup_config(c)

        assert warnings == []

    def test_multiple_warnings_accumulate_in_order(self):
        """锁定：多条问题同时存在时按规则顺序全部追加（空 Key + 模型缺失 + 格式异常）。"""
        c = _make_config({"llm": {"base_url": "no-protocol"}})

        warnings = validate_startup_config(c)

        assert warnings == [
            "llm.api_key 为空，LLM 功能不可用",
            "llm.model 未配置",
            "llm.base_url 格式异常: no-protocol",
        ]

    def test_never_raises_and_logs_each_warning(self, caplog):
        """锁定：无论配置多坏都不抛异常；每条告警写一条 [config] 前缀 WARNING 日志。"""
        c = _make_config({})  # 全缺：应产生「为空」+「未配置」两条告警

        with caplog.at_level(logging.WARNING, logger="papermind"):
            warnings = validate_startup_config(c)

        assert len(warnings) == 2
        logged = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        for w in warnings:
            assert f"[config] {w}" in logged
