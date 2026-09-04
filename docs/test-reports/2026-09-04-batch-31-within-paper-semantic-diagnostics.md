# Batch 31 测试报告：论文内语义深度可行性诊断

## 1. 结论

本批建立 `within-paper-semantic-diagnostics-v1`，在生产已选论文的全部 chunk 中测量证据的
语义排名与过滤查询增量延迟。真实完整 v2 train 的 13 题中，5 题基线已完整覆盖，7 题可由
论文内语义全集严格增加 span coverage，仅 1 题因相关论文未进入生产 top-5 而不可恢复。

可行性 Gate 为 **PASS**：平均 span coverage 的可恢复上限由 `0.4522` 到 `0.9231`，过滤
查询总增量 P95=`58.4ms`（门槛 `<250ms`），每题 embedding 调用严格为 1，额外 embedding
调用为 0。因此 Batch 32 可以实现唯一候选 `within-paper-semantic-rerank-v1`。这仍只是穷举
上限诊断，不等于新 top-5 排序已提升；本批没有修改生产检索、没有运行 dev/holdout，也没有
调用 Kimi。

## 2. SDD / TDD 轨迹

- RED `8f0d1ad`：先提交规格、计划、任务与 12 个测试；因诊断模块不存在，在 collection
  阶段以 `ModuleNotFoundError` 失败。
- GREEN `2854b8c`：实现纯聚合、单向量复用采集器、严格范围校验、只读私有 CLI、临时 Chroma
  副本、源指纹前后审计与 0600 独占报告。
- GREEN 首轮测试同时发现测试夹具把 5 篇基线错误建成 1 条论文路由；修正为每个生产已选
  论文恰好一条路由后，专项 **12/12 passed**。

## 3. 冻结协议与隐私

每题只执行一次 `embed_query`。同一向量先取得生产 semantic 路由，再对 legacy RRF top-5
中的唯一论文逐篇执行 Chroma `where={paper_id}` 查询，深度等于该论文在只读 SQLite 中的
完整 chunk 数。每条返回 ID 与 metadata、目标论文、DB 数量必须一致，否则 fail closed。

真实运行绑定 clean Git `2854b8c9c979a77b97ff78b93c9807c1c2ae074d`，诊断契约 SHA 为
`ec81c13a88d14f76db1dfa5435e7c2fd3ab822e85e249ffa2e6f247a675413f2`。报告权限为 0600，
仅含聚合计数、覆盖、rank bucket、时延与数据/语料/向量/HNSW 指纹，不含问题、QA/chunk/
paper ID、标题、路径或正文。执行前显式设置 HuggingFace/Transformers 离线模式；冻结 Chroma
只复制到临时目录后打开，源目录前后指纹保持不变。

## 4. 真实 train 结果

| 指标 | 结果 | Gate |
|---|---:|---|
| 完整题数 | 13 | PASS |
| baseline full / 可恢复 / 语义缺失 / 论文未选 | 5 / 7 / 0 / 1 | — |
| 基线平均 span coverage | 0.4522 | — |
| 已选论文语义全集覆盖上限 | 0.9231 | — |
| 潜在覆盖增益 | +0.4709 | PASS（至少 1/13 可恢复） |
| 首证据 rank 1–5 / 6–10 / 11–20 / 21–50 / >50 / 未找到 | 8 / 0 / 1 / 1 / 2 / 1 | — |
| 论文过滤查询 | 27 | 范围契约全通过 |
| 过滤查询总增量 P50 / P95 | 34.7ms / 58.4ms | PASS（P95 <250ms） |
| embedding / 额外 embedding | 13 / 0 | PASS |

私有聚合 observation SHA：
`c4ca0b778b5701fa94b1744e16b9ceba8cff310fb409c760c4a5bb1a0c50891d`。

## 5. 完整回归证据

| Gate | 结果 |
|---|---|
| Batch 31 专项 | **12 passed** |
| 后端全量 | **1074 passed**，1441 warnings，21.39s |
| Python 依赖 | `pip check`：No broken requirements found |
| 前端测试 | **15 files / 66 tests passed** |
| 前端 lint / build | **PASS / PASS**；保留既有 `ui`、`StatsPage` 大 chunk 警告 |
| Electron 默认测试 | **26 passed / 2 skipped / 0 failed** |
| 真实发布 E2E | **10/10 passed**，14.63s |
| 公开 count RAG | Recall@5 **0.900** / MRR **0.775** / NDCG@5 **0.806** |
| 公开 BM25 RAG | Recall@5 **0.900** / MRR **0.783** / NDCG@5 **0.813** |
| 公开生成 Guardrail | P/R/F1/拒答率均 **1.000**，PASS |
| 独立失败事务 | **11/11 scenarios**，PASS |

发布 E2E 首次在受限沙箱内因 `listen EPERM 127.0.0.1` 失败，获得回环监听权限后原命令
10/10 通过；这是执行环境权限，不是产品回归。生成 Guardrail 首次因 `/tmp` 不属于其受控
报告目录而按设计拒绝，改用已忽略的 `eval/reports/` 后 PASS，未放宽路径安全契约。

## 6. 下一步与剩余范围

Batch 32 只实现一个固定论文内语义选块候选，先在完整 train 上要求 span 至少 `+1/13`、
Recall/MRR/NDCG/全部分型不回退且 P95 <1s；失败即停止 RAG 调参，成功才允许一次受控 dev。
随后预计 Batch 33 完成前端大 chunk 与关键页面收尾，Batch 34 做最终发布审计。因此当前
预计还剩 **3 个 batch**；若 Batch 32 通过 train 后的单次 dev 暴露需独立修正的问题，最多
扩为 4 个。
