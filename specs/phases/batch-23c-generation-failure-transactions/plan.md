# Batch 23C 实施计划

1. HARNESS：审计 LLM 带内错误、SSE 终态、消息计数与前端引用归属。
2. RED：锁定流式/非流式/重生成失败的脱敏与数据库不变量，并覆盖前端回滚边界。
3. GREEN：以最小兼容改动实现错误识别、计数对账和消息级引用状态。
4. PARITY：验证 error / cancel / EOF / 空输出 / finished 缺正文均 fail-close，成功路径不回归。
5. TRACE：运行三端 Harness、离线公开 Gate，生成测试报告并分段提交、推送。

## 完成证据

- RED：`a832756`（后端 4 fail）、`5531f3c`（前端 3 fail）。
- GREEN：`152c30d`；错误哨兵/异常/空结果均 fail-close，消息级引用原子回滚。
- 全量：后端 894、前端 53、Electron 26；公开检索与生成 Gate 全部 PASS。
