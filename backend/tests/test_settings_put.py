"""settings PUT 保存失败异常脱敏测试（Batch7b-F8，宪法第 13 条）。

行为契约（specs/phases/batch-7b-fixes/spec.md 3.1）：
- config.save() 抛任何异常 → HTTP 500，detail 为通用文案，不含异常类型名/消息文本；
- 异常原文 + 堆栈只写日志（logger.error(..., exc_info=True) 或等价）。
"""

import logging

from app.core.config import config

# 异常原文特征串：模拟含敏感路径/key 的异常消息，响应中绝不允许出现
MARKER = "sk-SECRET-敏感标记-7f3a9c"


def test_put_save_failure_sanitizes_detail(client, monkeypatch, caplog):
    """config.save 抛异常：500 + 通用文案，detail 不透传原文，原文仅入日志。"""

    def boom():
        raise RuntimeError(f"磁盘写入失败 {MARKER}")

    monkeypatch.setattr(config, "save", boom)

    # save 被 mock 不落盘，但路由会直改内存单例 _config，测试后还原避免污染其他用例
    saved_llm = dict(config._config.get("llm", {}))
    try:
        with caplog.at_level(logging.ERROR, logger="papermind"):
            r = client.put("/api/settings", json={"llm_api_key": "newkey123456"})
    finally:
        config._config["llm"] = saved_llm

    assert r.status_code == 500
    detail = r.json()["detail"]
    # 通用文案，不含异常原文/类型名/特征串
    assert detail == "保存配置失败，请稍后再试"
    assert MARKER not in r.text
    assert "RuntimeError" not in detail
    # 异常原文只进日志
    assert any(MARKER in rec.getMessage() for rec in caplog.records)
