# Batch 23E 规格：独立进程失败事务 Harness

## 1. 目标

以独立 Python 进程、真实 chat router 和逐场景文件型 SQLite，证明生成失败、取消与
commit failure 在跨连接条件下仍满足 Batch 23C/23D 的事务契约，并给出不含内容的公开报告。

## 2. Harness 边界

- runner 在任何 `app.*` 导入前建立临时 runtime、清空外部 API Key、安装审计钩子并
  预装明确标记的 LLM/retrieval/memory/image/agent fake。
- 不导入 `app.main`，只构造挂载 chat router 的最小 FastAPI 应用；不运行 lifespan/MCP。
- 每个场景使用独立文件 SQLite、`NullPool`、外键和 WAL；请求结束后关闭连接并以全新
  Session 验证数据库。
- fixture 只含合成场景枚举和预期计数，不含真实问题、回答或引用正文。
- 报告只含 schema、SHA256、固定场景 ID、计数、不变量布尔值和离线审计计数。

## 3. 场景矩阵

1. `chat-success-control`：user+assistant 与真实 count=2，唯一 finished。
2. `chat-stream-failure`：delta 后异常；保留 user、无 assistant、无 finished、脱敏 error。
3. `chat-cancelled`：delta 后 `CancelledError`；保留 user、无 assistant、无 finished。
4. `chat-assistant-commit-failure`：最终 commit 抛合成异常；回滚 assistant，user/count=1。
5. `deep-review-plan-failure`：零 Conversation、零 Message、无 finished。
6. `deep-review-commit-failure`：新会话、两条消息和计数整体回滚，无 finished。
7. `regenerate-commit-failure`：原 assistant content/citations/revision 完全不变，无 finished。

## 4. 硬 Gate

- `scenario_count == registered_scenario_count`，成功控制必须通过。
- 失败场景 finished 数、多终态数、未脱敏错误数、孤儿会话数、失败 assistant 行数、
  `message_count` 漂移、regenerate 变更数和 rollback failure 数均为 0。
- fake LLM 调用次数与 fixture 契约一致，证明 Harness 不是空跑。
- 真实 LLM/Embedding/Web 调用、网络、子进程和私有路径访问均为 0。
- 报告未知字段、正文、异常原文、绝对路径、Key/URL/canary 任一出现即拒绝发布。

## 5. 非目标

- 不访问 `papers/`、`eval/private/`、`config.yaml`、真实数据库、向量库或网络。
- 不调用 Kimi/Embedding，不评估答案质量，不修改生产路由行为。
- 不模拟跨机器或多 worker 分布式事务。
