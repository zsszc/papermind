# Batch 19 测试报告：前端可靠性与受控生成评测（2026-08-14）

## 1. 结论

Batch 19 已完成 Chat、WritingDesk、PaperDetail 和生成评测的可靠性加固。Kimi 最小健康检查在用户充值后恢复为 `ok=true`，模型为 `kimi-k2.6`。

真实论文生产对齐检索基线首次被单独测量：只复刻聊天的 BGE-M3 语义 top5，不混入评测专用 BM25/RRF。private dev 的 Recall@5=0.500、MRR=0.268、NDCG@5=0.324、P95=245.6ms；factoid Recall=0，是下一批明确弱项。之前 hybrid Recall@5=0.625 仍是有效评测 profile，但不能等价代表当前聊天链路。

固定四题 Kimi 生成烟测在发出请求前被隐私门禁阻断：该动作会把真实论文 QA 与 top-5 证据发送到外部服务，而本轮没有得到这组私有内容的明确出站授权。实际生成调用为 0、费用为 0。代码已具备 QA 白名单、四次调用硬预算、512 token 上限、健康预检和失败 Gate，待用户明确授权后即可执行。

## 2. Harness / SDD / TDD 证据

| 环节 | RED | GREEN |
|---|---|---|
| SSE 协议 | body/提前 EOF 被当成功，终态后 reader 依赖 GC | 缺 body/EOF fail-close；error/finish 互斥；reader 释放；60s 首事件/180s 空闲/10min 总预算 |
| Chat 操作 | 非幂等 POST 自动重放；双击并发；图片/regen 停止无效 | 同步 single-flight；不自动重放；signal 贯通；卸载/切会话取消；按 temp/message id 更新 |
| 编辑与隔离 | 编辑先截断本地历史；旧流可写当前最后消息 | 服务端删除成功后再截断；操作固定目标会话与消息，不按数组末尾/漂移 index |
| WritingDesk | 正文进入 URL；空初始状态覆盖草稿；旧请求污染新论文 | JSON body、20k/空白 Gate、不回显正文；同步 hydrate、按 thesis 草稿、取消与 request-id |
| PaperDetail | debounce 离开时取消；手动/自动保存竞态 | 串行 latest-wins；返回先 flush；失败保留 dirty；paper 404/1MiB/原子 replace |
| 生成评测 | 无 QA 白名单、预算、health、错误有效性；检索 PASS 可掩盖生成失败 | dev-only、私有目录、调用硬预算、512 tokens、health preflight、`generation.valid` fail-close |
| 生产检索对齐 | hybrid 指标被误当聊天质量 | `semantic-production` 只调用生产 `VectorStore.search(top_k=5)`，显式快照与 rerank off/on 诊断 |

规格、计划与任务位于 `specs/phases/batch-19-frontend-reliability-generation-eval/`。

## 3. 真实 dev 指标

| Profile | Recall@5 | MRR | NDCG@5 | P95 | 状态 |
|---|---:|---:|---:|---:|---|
| hybrid + bilingual（Batch 18） | 0.625 | 0.394 | 0.452 | 291.2ms | 有效评测 profile，不等价生产聊天 |
| semantic-production / rerank OFF | **0.500** | **0.268** | **0.324** | **245.6ms** | 当前生产对齐基线 |
| semantic-production / rerank ON | — | — | — | 首题 >约4分钟 | 延迟 Gate 失败，安全中止，不采用 |

production semantic 分题型 Recall：experiment_data=0.500、factoid=0、method_detail=0.833、summary=0.667。下一批应优先用数值、单位和实体锚点改善 factoid 候选召回，并把同一 RetrievalPipeline 同时提供给聊天和评测。

本地 2.1GiB `bge-reranker-v2-m3` 正常加载，说明不是模型缺失；但 CPU 推理首题超过约 4 分钟，远超默认 P95<1s 目标，因此没有改变生产 `rerank=false`。本批没有读取或运行 holdout。

## 4. 最终工程 Gate

| 门禁 | 结果 |
|---|---|
| 后端 pytest | **601 passed**，1012 warnings，14.49s |
| 后端依赖 | `pip check`：No broken requirements found |
| 公开冻结 BM25 | **Recall@5=0.900 / MRR=0.783 / NDCG@5=0.813，PASS** |
| 前端 Vitest | **39 passed / 12 files** |
| 前端 lint / build | 通过；仅保留既有大 chunk warning |
| Electron node:test | **26 passed** |
| npm 官方 audit | 前端 **0 vulnerabilities**；Electron **0 vulnerabilities** |
| Kimi 最小探活 | **ok=true / kimi-k2.6** |

ErrorBoundary 测试会故意输出 React 错误栈，测试本身通过。Ruff 仍未运行，因为项目 venv 尚未安装该工具。

## 5. 待办与下一步

1. 用户若明确授权将固定 4 条 private dev QA 及各自 top-5 证据发送给 Kimi，再运行最多 4 次的生成烟测；未授权前保持 0 调用。
2. Batch 20 统一聊天与 eval 的 RetrievalPipeline，以 production semantic 0.500/0.268/0.324 为真实起点；优先提升 factoid Recall=0。
3. 当前 Chat 单飞解决了客户端重复请求，但服务端尚无持久 `client_request_id` 唯一约束；极端的客户端崩溃后人工重发仍可能重复落库，可在下一批增加 DB 幂等键。
4. 笔记已保证单实例 latest-wins 和原子文件替换，但没有跨窗口 revision/CAS；多窗口同时编辑仍按最后落盘者获胜。
