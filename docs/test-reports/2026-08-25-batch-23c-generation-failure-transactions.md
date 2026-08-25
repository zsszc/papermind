# Batch 23C 测试报告：生成失败事务闭环

## 1. 结论

Batch 23C 已关闭文本聊天与重新生成的失败事务漏洞：LLM 错误不再作为普通回答
发送或落库，空生成和 Guardrail 清洗后为空均失败关闭；用户消息可独立保留，但
assistant 正文、引用和 `message_count` 只在同一成功事务提交。重新生成失败时原正文
与引用不变。

前端引用已由面板级全局状态迁移到消息级状态。error、取消、EOF、图片分析失败或
缺失最终正文时都会丢弃 provisional；历史回答的来源不会误挂到新回答下。

## 2. SDD / TDD 轨迹

- RED `a832756`：后端定向测试得到 **4 failed / 23 passed**，证明错误哨兵会落库、
  空流会假成功、non-stream 返回 200、regenerate 覆盖原消息。
- RED `5531f3c`：前端定向测试得到 **3 failed / 5 passed**，证明历史引用未渲染、
  finished 缺正文会提交半条、图片 error 会保留半条。
- GREEN `152c30d`：类型化流错误、路由 fail-close、真实计数事务、消息级引用与
  前端严格终态落地；后续补充首 token 后失败禁止重试和异常路径回归测试。
- 三个只读代理分别审计后端事务、前端 SSE/引用状态和隐私 Gate；实现未读取真实语料。

## 3. 失败事务 Gate

| 不变量 | 结果 |
|---|---|
| 错误哨兵 / 异常原文进入 SSE、HTTP 或数据库 | 0 |
| 失败路径发送 `finished` | 0 |
| 失败路径新增 assistant | 0 |
| 失败后 `message_count` 与真实消息行不一致 | 0 |
| regenerate 失败修改原正文或引用 | 0 |
| 空白 / 清洗后为空被当作成功 | 0 |
| 首 token 后从头重试造成答案拼接 | 0 |
| 前端 error / cancel / EOF 保留 provisional | 0 |
| 引用跨消息误归属 | 0 |

日志只记录 conversation/message ID、输入长度、聚合计数和异常类型；不再记录问题、
综述主题、异常原文或非法引用 token。CI 仅在全部 Gate 成功后上传评测报告，避免上传
尚未通过 schema/canary 校验的半成品。

## 4. 全量 Harness

| Harness | 结果 |
|---|---|
| 后端 pytest | **894 passed** |
| 前端 Vitest | **53 passed / 14 files** |
| 前端 ESLint | 0 warnings |
| 前端生产构建 | PASS（保留既有大 chunk 提示） |
| Electron node:test | **26 passed** |
| 公开 count Gate | Recall@5/MRR/NDCG@5 = **0.900/0.775/0.806**，PASS |
| 公开 BM25 Gate | Recall@5/MRR/NDCG@5 = **0.900/0.783/0.813**，PASS |
| 生成 Guardrail Gate | P/R/F1/refusal = **1.000/1.000/1.000/1.000**，PASS |
| Python 依赖一致性 | `pip check` 无冲突 |

环境：macOS，Python 3.12.2，Node 22.23.1，npm 10.9.8。复现命令：

```bash
cd backend && env -u PYTHONPATH venv/bin/python -m pytest tests/ -q
cd frontend && npm run lint && npm test && npm run build
cd electron && npm test
cd backend && env -u PYTHONPATH venv/bin/python -m eval.run --fixture eval/fixtures/rag_public_v1.json --dataset eval/dataset/qa_public_v1.jsonl --keyword-only --lexical-profile count --threshold 0.85 --report-dir eval/reports/public-count
cd backend && env -u PYTHONPATH venv/bin/python -m eval.run --fixture eval/fixtures/rag_public_v1.json --dataset eval/dataset/qa_public_v1.jsonl --keyword-only --lexical-profile bm25 --threshold 0.85 --report-dir eval/reports/public-bm25
cd backend && env -u PYTHONPATH -u OPENAI_API_KEY -u KIMI_API_KEY -u MOONSHOT_API_KEY -u PAPERMIND_DATA_DIR venv/bin/python -m eval.generation_guardrails --report-dir eval/reports/public-generation
cd backend && venv/bin/python -m pip check
```

## 5. 隐私边界与剩余工作

- 本批未读取 `papers/`、`eval/private/`、真实 QA/holdout 或 `config.yaml`，未调用
  Kimi、Embedding、联网搜索或其他网络服务。
- 数据库成功提交后客户端断连无法组成跨 HTTP/SQLite 的分布式回滚；此时数据库为
  权威源，前端通过重新加载会话对账。
- 同一目标的并发 regenerate、deep-review 失败空会话回收和独立进程级取消 Harness
  仍适合后续单独批次处理。
- Batch 23B 的真实论文固定四题 smoke 仍需要明确内容出站授权。
