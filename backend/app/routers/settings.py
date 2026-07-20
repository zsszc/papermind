from typing import Any, Dict

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core.config import config
from app.core.logger import logger

router = APIRouter()


class SettingsResponse(BaseModel):
    llm_api_key: str
    llm_model: str
    llm_base_url: str
    embedding_model: str


class SettingsUpdate(BaseModel):
    llm_api_key: str
    llm_model: str | None = None
    llm_base_url: str | None = None


def _mask_key(key: str) -> str:
    """对 API key 进行脱敏展示。"""
    if not key:
        return ""
    key = str(key).strip()
    if len(key) <= 8:
        return "*" * len(key)
    return key[:4] + "*" * (len(key) - 8) + key[-4:]


@router.get("", response_model=SettingsResponse)
def get_settings():
    """返回当前设置（API key 脱敏）。"""
    return SettingsResponse(
        llm_api_key=_mask_key(config.get("llm.api_key", "")),
        llm_model=config.get("llm.model", "moonshot-v1-8k"),
        llm_base_url=config.get("llm.base_url", "https://api.moonshot.cn/v1"),
        embedding_model=config.get("embedding.local_model", "BAAI/bge-m3"),
    )


@router.put("")
def update_settings(payload: SettingsUpdate):
    """更新设置并持久化到 config.yaml。"""
    try:
        cfg = config._config

        if "llm" not in cfg:
            cfg["llm"] = {}

        if payload.llm_api_key:
            # 如果前端传的是脱敏后的值（含 *），则忽略，避免把 * 写入配置
            if "*" not in payload.llm_api_key:
                cfg["llm"]["api_key"] = payload.llm_api_key.strip()

        if payload.llm_model:
            cfg["llm"]["model"] = payload.llm_model.strip()

        if payload.llm_base_url:
            cfg["llm"]["base_url"] = payload.llm_base_url.strip()

        config.save()
        logger.info("[settings] 配置已更新")
        return {"ok": True}
    except Exception as e:
        logger.error(f"[settings] 更新配置失败: {e}")
        raise HTTPException(status_code=500, detail=f"保存配置失败: {e}")
