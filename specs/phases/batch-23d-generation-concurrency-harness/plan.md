# Batch 23D 实施计划

1. HARNESS：盘点 deep-review 会话生命周期和 regenerate 并发写入窗口。
2. RED：锁定空会话清理、空/清洗空结果、已有会话保护、revision 迁移与乐观冲突。
3. GREEN：实现延迟建会话、真实计数提交、revision 条件更新、active-set 和客户端对账。
4. PARITY：验证成功路径、失败路径、取消释放、脱敏和现有 SSE 协议不回归。
5. TRACE：运行三端 Harness、公开 Gate，生成报告并分段提交、推送。
