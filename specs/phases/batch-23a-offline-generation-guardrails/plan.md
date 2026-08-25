# Batch 23A 实施计划

1. HARNESS：审计现有 guardrails、controlled generation、chat SSE 和 citation 指标契约。
2. RED：手算锁定引用越界/重复/未知、负例拒答和 SSE 失败场景。
3. GREEN：实现公开合成 fixture、共享生成评测器与离线报告。
4. PARITY：证明评测解析与生产聊天 finished/citations 契约一致，无网络调用。
5. TRACE：全量 Harness、公开 Gate、测试报告、台账、分段提交与 push。

## 完成证据

- RED：`ae8dd74`，共享模块缺失且 SSE 终态契约失败。
- GREEN：`51bf764`，共享纯 Guardrail、公开 fixture / CLI / CI 与生产原子终态落地。
- 全量：后端 887、前端 50、Electron 26；公开检索与生成 Gate 全部 PASS。
