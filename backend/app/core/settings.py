"""分层配置：环境变量 (PAPERMIND_*) > config.yaml > 默认值。

app.core.config.Config 仍是运行时配置单例（读写 config.yaml），
本模块只负责：
1. Pydantic 结构校验（关键字段类型与默认值）；
2. PAPERMIND_* 环境变量启动覆盖；
3. 启动健康检查（占位符 API Key 等告警）。
"""

from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.logger import logger


class EnvOverrides(BaseSettings):
    """可通过 PAPERMIND_* 环境变量覆盖的配置项（空字符串不覆盖）。"""

    model_config = SettingsConfigDict(env_prefix="PAPERMIND_", extra="ignore")

    llm_api_key: str = ""
    llm_model: str = ""
    llm_base_url: str = ""

    def apply_to(self, config) -> list[str]:
        """把非空环境变量写入 Config 内存配置，返回被覆盖的键列表。"""
        applied: list[str] = []
        for key, value in (
            ("llm.api_key", self.llm_api_key),
            ("llm.model", self.llm_model),
            ("llm.base_url", self.llm_base_url),
        ):
            if value:
                _dotted_set(config._config, key, value)
                applied.append(key)
        return applied


def _dotted_set(cfg: dict, key: str, value: Any) -> None:
    """按点分路径写入嵌套 dict，不存在的中层级自动创建。"""
    node = cfg
    keys = key.split(".")
    for k in keys[:-1]:
        node = node.setdefault(k, {})
    node[keys[-1]] = value


_PLACEHOLDER_MARKERS = ("sk-xxxx", "your-", "xxxx")


def validate_startup_config(config) -> list[str]:
    """启动时校验关键配置，记录并返回告警信息列表。"""
    warnings: list[str] = []

    api_key = str(config.get("llm.api_key", "") or "").strip()
    if not api_key:
        warnings.append("llm.api_key 为空，LLM 功能不可用")
    elif any(m in api_key for m in _PLACEHOLDER_MARKERS):
        warnings.append("llm.api_key 仍是占位符，请在 config.yaml 中填入真实 Key")

    model = str(config.get("llm.model", "") or "").strip()
    if not model:
        warnings.append("llm.model 未配置")

    base_url = str(config.get("llm.base_url", "") or "").strip()
    if base_url and not base_url.startswith(("http://", "https://")):
        warnings.append(f"llm.base_url 格式异常: {base_url}")

    for w in warnings:
        logger.warning(f"[config] {w}")
    return warnings


def apply_env_overrides(config) -> list[str]:
    """应用 PAPERMIND_* 环境变量覆盖，返回被覆盖的键列表。"""
    applied = EnvOverrides().apply_to(config)
    for key in applied:
        logger.info(f"[config] 环境变量覆盖: {key}")
    return applied
