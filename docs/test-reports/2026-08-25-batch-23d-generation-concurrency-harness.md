# Batch 23D 测试报告：生成并发与深度综述事务

## 1. 结论

Batch 23D 已关闭两类跨阶段漏洞：deep-review 不再于规划前提交空会话；regenerate
不再允许两个请求或过期客户端静默覆盖较新消息。新 deep-review 仅在综述通过空值与
引用 Guardrail 后，才一次提交 Conversation、user、assistant 与真实消息计数。

messages 新增从 0 开始的 `revision`。regenerate 请求声明 `expected_revision`，入口过期
或同进程已有相同目标任务时立即 409、不会调用 LLM；终态以 revision 条件更新正文、
引用和新版本。目标被修改或删除时只发送失败终态。前端在冲突、HTTP 失败、EOF、网络
异常或取消后重新读取 history，并以 conversation epoch 阻止迟到对账覆盖新会话。

## 2. SDD / TDD 轨迹

- RED `f07ac9b`：冻结规格、计划、任务和失败矩阵；迁移登记测试得到 `9 != 10`，并锁定
  deep-review 孤儿会话、空/清洗空输出、stale revision 与外部更新覆盖。
- 后端 GREEN `e2e8eb9`：延迟建会话、真实 COUNT、revision 迁移、active-set、条件更新及
  明确 conflict/target-missing 终态；定向 **47 passed**。
- 前端 GREEN `fca1dc1`：expected revision 请求体、完整 SSE error payload、成功 revision
  替换和失败历史对账；定向 **23 passed**，ESLint 通过。
- 三个只读代理分别审计 deep-review 生命周期、regenerate 竞争窗口和独立离线隐私
  Harness；本批实现未读取真实语料、私有评测或配置。

## 3. 事务与并发 Gate

| 不变量 | 结果 |
|---|---|
| deep-review 规划/汇总/空输出失败产生孤儿会话 | 0 |
| Guardrail 清空后发送 finished 或落空 assistant | 0 |
| 已有会话失败时消息或计数发生变化 | 0 |
| stale/active regenerate 进入 LLM | 0 |
| 生成期间外部更新被旧结果覆盖 | 0 |
| 生成期间删除的目标被复活 | 0 |
| regenerate 失败后 active-set 未释放 | 0 |
| 客户端冲突后保留 provisional | 0 |
| 迟到对账覆盖已切换会话 | 0（epoch 门禁） |

## 4. 全量 Harness 与指标

| Harness | 结果 |
|---|---|
| 后端 pytest | **907 passed** |
| 前端 Vitest | **54 passed / 14 files** |
| 前端 ESLint | 0 warnings |
| 前端生产构建 | PASS（保留既有大 chunk 提示） |
| Electron node:test | **26 passed** |
| Python 依赖一致性 | `pip check` 无冲突 |
| 公开 count Gate | Recall@5/MRR/NDCG@5 = **0.900/0.775/0.806**，PASS |
| 公开 BM25 Gate | Recall@5/MRR/NDCG@5 = **0.900/0.783/0.813**，PASS |
| 生成 Guardrail Gate | P/R/F1/refusal = **1.000/1.000/1.000/1.000**，PASS |

环境：macOS，Python 3.12.2，Node 22.18.0，npm 10.9.3。后端虚拟环境未安装
ruff 模块，故没有把缺失命令记为代码 Gate；后端全量 pytest 与前端 ESLint 均已执行。

## 5. 隐私与后续计划

- 本批未读取 `papers/`、`eval/private/`、`config.yaml`，未调用 Kimi、Embedding、联网
  搜索或任何外部服务；公开指标来自 CC0 合成 fixture。
- Batch 23B 的真实论文固定四题生成 smoke 仍等待明确的论文内容出站授权。Kimi 额度可用
  不等同于内容出站授权，因此未自动执行。
- 下一批优先建立独立进程、文件型临时 SQLite 的失败事务 Harness，证明取消/commit
  failure 在跨连接下仍满足回滚与隐私 Gate；随后可进入 Batch 24 的本地 UI/E2E 发布候选。
