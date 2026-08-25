# Batch 23F 实施计划

1. HARNESS：冻结 v2 场景、同步点、外部提交与报告证据。
2. RED：新增 v2 fixture/schema/subprocess 契约，确认旧 runner fail closed。
3. GREEN：引入每场景并发 controller，实现双 Client、外部连接和取消重试。
4. HARDEN：补精确 409 原因、调用增量、worker/timeout、状态覆盖与报告一致性 Gate。
5. TRACE：运行专项/全量门禁，生成测试报告并同步进度台账后 push。
