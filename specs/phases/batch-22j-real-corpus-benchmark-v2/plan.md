# Batch 22J 实施计划

1. Harness：冻结 v1 论文 UID/SHA 和当前 `papers/` 只读清单。
2. RED：锁定重复 PDF、v1 重叠、跨 split、证据歧义和 ledger 覆盖失败。
3. GREEN：实现去原文覆盖审计、分区冻结与一次性消费工具。
4. DATA：仅在私有目录生成 v2 候选 manifest/QA/qrel，人工审核后冻结。
5. BASELINE：建立生产 hybrid 盲化基线，不在同一批根据结果调算法。
6. TRACE：全量 Harness、测试报告、台账、分段提交与 push。
