# Batch 23E 测试报告：独立进程失败事务 Harness

## 1. 结论

Batch 23E 已把生成事务契约从内存 SQLite 单连接测试提升到独立进程、文件 SQLite/WAL
和 NullPool 多连接证明。runner 只挂载真实 chat router，不导入 `app.main` 或 lifespan；
服务使用有明确标记和分类计数的 fake，不访问真实 LLM、Embedding、检索或图片服务。

七个公开合成场景全部通过：聊天成功控制、delta 后异常、取消、assistant commit 失败、
deep-review 规划失败、deep-review 原子 commit 失败、regenerate 条件更新 commit 失败。
commit 场景只有在写事务已经开始、commit 调用一次且异常注入一次时才可能通过，随后由
新的数据库连接复核消息、引用、revision 和真实计数。

## 2. SDD / TDD 与审查轨迹

- RED `b505c2e`：fixture 有效，但 runner/Gate/report validator 缺失，得到 **3 failed / 1 passed**。
- GREEN `e096dd3`：独立 runner、七场景、原子报告与重型 eval CI，定向 **4 passed**。
- 审查加固 `46f6dfa`：三个只读代理发现并关闭 fixture 自我放宽、commit 未命中仍可能通过、
  fake 混计数、日志漏扫、deep fake 漏登记、行为失败误判 schema 等风险。
- 生成 Guardrail 从已安装完整依赖的 job 拆到 `python -S`、不安装依赖的独立 CI job。

## 3. 失败事务与隐私 Gate

| Gate | 结果 |
|---|---:|
| 注册场景 / 通过场景 | 7 / 7 |
| scenario/terminal/HTTP/fake call mismatch | 0 |
| 失败场景 finished / 多终态 | 0 / 0 |
| 失败 assistant / 孤儿会话 / count 漂移 | 0 / 0 / 0 |
| regenerate mutation / active-set 泄漏 | 0 / 0 |
| rollback / commit fault proof failure | 0 / 0 |
| 日志/响应/report 内容或 canary 泄漏 | 0 |
| 网络 / 子进程 / 私有路径尝试 | 0 / 0 / 0 |
| 真实服务模块加载 | 0 |
| fake LLM / retrieval / deep-review 调用 | 5 / 6 / 4（与冻结契约一致） |
| 连续双跑报告 | 字节一致 |

报告仅包含固定枚举、计数、布尔值和 SHA256，并同时绑定 fixture、runner 与
`chat.py/database.py/models.py/schemas.py` 的组合实现指纹。

## 4. 全量 Harness 与指标

| Harness | 结果 |
|---|---|
| 后端 pytest | **911 passed** |
| 前端 Vitest | **55 passed / 14 files** |
| 前端 ESLint / build | PASS / PASS（保留既有大 chunk 提示） |
| Electron node:test | **26 passed** |
| 公开 count | Recall@5/MRR/NDCG@5 = **0.900/0.775/0.806** |
| 公开 BM25 | Recall@5/MRR/NDCG@5 = **0.900/0.783/0.813** |
| 生成 Guardrail | P/R/F1/refusal = **1.000/1.000/1.000/1.000** |
| 纯标准库生成 Gate | `python -S` PASS |
| Python 依赖 | `pip check` 无冲突 |

## 5. 隐私边界与下一步

- 未读取 `papers/`、`eval/private/`、项目 `config.yaml`、真实数据库或向量库；未调用 Kimi、
  Embedding、联网搜索或其他网络服务。传入子进程的伪 Key 和伪数据目录均被覆盖且未访问。
- 本批证明的是失败事务与协议，不代表生成答案质量提升；公开检索和生成指标保持基线。
- 下一批可扩展 non-stream 第二次 commit、图片 error、既有 deep-review 取消、regenerate
  真实双请求 active 409，以及外部连接 revision update/delete 的文件库场景。
- Batch 23B 真实论文四题 smoke 仍等待明确的论文内容出站授权；未授权时继续 Batch 24
  本地 UI/E2E 发布候选工作。
