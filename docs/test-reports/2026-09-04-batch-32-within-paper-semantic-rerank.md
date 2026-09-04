# Batch 32 阶段报告：论文内语义选块候选 train Gate

## 当前结论

`within-paper-semantic-rerank-v1` 已完成 SDD、RED/GREEN、完整真实 train 配对与全量回归。
候选复用一次 query embedding，冻结生产 top-5 的论文 slot；局部 semantic top-5 中已有的
incumbent 原位锁定，其他 slot 才使用同论文最佳未使用 chunk。

真实 train Gate 为 **PASS**，但 Batch 32 尚未完成：按预注册协议，必须先建立与 train Gate
指纹绑定的一次性 dev 授权，再运行唯一一次固定 dev。在此之前生产默认保持 `hybrid`。

## SDD / TDD 轨迹

- RED `10226ff`：6 个测试全部失败，覆盖缺失选择器、范围错误、单向量复用和 eval profile。
- GREEN `6191fe6`：实现显式候选、冻结公式、train-only CLI 与配对 Gate；相关测试 42/42。

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

- 后端全量：**1083 passed**，1441 warnings，20.03s。
- 前端：**66 passed**，lint/build PASS；既有 `ui`/`StatsPage` 大 chunk 警告仍在。
- Electron：**26 passed / 2 skipped / 0 failed**。

## 待完成

1. 为一次 dev 运行增加 train Gate、Git、公式和数据指纹授权校验。
2. 冻结 dev 非回退 Gate 后，只运行一次基线/候选配对。
3. 根据 dev 结果决定是否激活生产默认，再运行发布 E2E 与全部公开 Gate，完成最终报告。
