# Batch 32 阶段报告：论文内语义选块候选 train Gate

## 当前结论

`within-paper-semantic-rerank-v1` 已完成 SDD、RED/GREEN、完整真实 train 配对与全量回归。
候选复用一次 query embedding，冻结生产 top-5 的论文 slot；局部 semantic top-5 中已有的
incumbent 原位锁定，其他 slot 才使用同论文最佳未使用 chunk。

真实 train Gate 为 **PASS**。随后建立与 train Gate 指纹绑定的一次性 dev 授权并消费唯一
claim；dev 质量继续提升，但候选绝对 P95=`1081ms` 超过预注册 `<1000ms`，最终 Gate 为
**FAIL**。没有重跑 dev 或事后放宽门槛，生产默认保持 `hybrid`。

## SDD / TDD 轨迹

- RED `10226ff`：6 个测试全部失败，覆盖缺失选择器、范围错误、单向量复用和 eval profile。
- GREEN `6191fe6`：实现显式候选、冻结公式、train-only CLI 与配对 Gate；相关测试 42/42。
- 授权 `5a60c5c`：dev 必须绑定已通过 train Gate，Gate SHA 必须为当前祖先，且核心候选实现
  自 train 后不得改变；原子 claim 只允许创建一次。
- dev Gate `dd8e21c`：固定完整 12 题、同提交/同指纹、四项质量与分型非回退、候选 P95<1s。

## 完整 train 配对

绑定 clean Git `6191fe6`、完整 13 题、相同 SQLite/PDF/qrels/page-span-v2/向量/HNSW 指纹；
无 LLM、无运行时降级。

| 指标 | 生产基线 | 候选 | 变化 |
|---|---:|---:|---:|
| span coverage@5 | 0.452 | 0.606 | +0.154 |
| Recall@5 | 0.346 | 0.500 | +0.154 |
| MRR | 0.338 | 0.392 | +0.054 |
| NDCG@5 | 0.296 | 0.374 | +0.078 |
| factoid Recall | 0.375 | 0.625 | +0.250 |
| method_detail Recall | 0.375 | 0.375 | 0 |
| summary Recall | 0 | 0 | 0 |
| P95 | 965.5ms | 846.0ms | -119.5ms（仅作本次配对观察） |

配对 Gate 输入报告 SHA：基线
`93e74135320fb61b2117cd5e7b863138b239522905f27bb256d0a6e090e354d3`，候选
`e9266e627a5f0f51c773d244323cb13b22886cb5e01888158b4522389de296c7`。

## 当前回归

- 后端全量：**1087 passed**，1441 warnings，17.04s。
- 前端：**66 passed**，lint/build PASS；既有 `ui`/`StatsPage` 大 chunk 警告仍在。
- Electron：**26 passed / 2 skipped / 0 failed**。

## 一次性 dev 配对

| 指标 | dev 基线 | dev 候选 | Gate |
|---|---:|---:|---|
| span coverage@5 | 0.667 | 0.750 | PASS |
| Recall@5 | 0.667 | 0.708 | PASS |
| MRR | 0.392 | 0.419 | PASS |
| NDCG@5 | 0.459 | 0.485 | PASS |
| method_detail Recall / span | 0.667 / 0.667 | 0.833 / 1.000 | PASS |
| factoid Recall / span | 0.667 / 0.667 | 0.667 / 0.667 | PASS |
| summary Recall / span | 0.667 / 0.667 | 0.667 / 0.667 | PASS |
| P95 | 1012.7ms | 1081.0ms | **FAIL**（要求 <1000ms） |

dev claim 已消费，holdout 未运行。候选虽然在 train/dev 均显著改善质量，但按冻结 Gate 不得
激活生产默认。

## 最终回归与结论

- 发布 E2E：**10/10 passed**。
- 公开 BM25 RAG：Recall/MRR/NDCG=`0.900/0.783/0.813`。
- 公开生成 Guardrail：P/R/F1/拒答率均 `1.000`；失败事务 11/11 PASS。
- Python 依赖：`pip check` 无冲突。

下一批不继续消费 dev 或调整当前候选；转入前端 `ui`/`StatsPage` 大 chunk 和关键页面性能收尾。
