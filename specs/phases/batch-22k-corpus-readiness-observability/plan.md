# Batch 22K 实施计划

1. HARNESS：冻结 Batch 22J CLI 聚合结果与当前统计页/API 契约。
2. RED：新增后端只读契约、隐私字段黑名单、fail-closed 与前端状态卡片测试。
3. GREEN：复用覆盖审计模块实现服务/路由，在统计页展示聚合就绪度。
4. PARITY：对同一真实语料快照比较 CLI、API 与 UI 计数，不读取 holdout。
5. TRACE：全量 Harness、测试报告、进度台账、分段提交与 push。
