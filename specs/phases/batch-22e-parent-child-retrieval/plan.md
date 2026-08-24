# Batch 22E 实施计划

1. INVENTORY：审计旧粗块与 512 child 的页内坐标，冻结 parent 映射不变量。
2. RED：锁定 parent 聚合、数量偏置、多样性和稳定排序测试。
3. GREEN：实现隔离 parent 映射与 `parent-child-v1` RetrievalPipeline profile。
4. TRAIN：同一 profile 重跑旧基线与候选，执行 page-span-v2 配对 Gate。
5. DEV：仅 train 通过时运行一次；否则记录拒绝并停止。
6. TRACE：全量 Harness、聚合报告、分批提交与 push。
