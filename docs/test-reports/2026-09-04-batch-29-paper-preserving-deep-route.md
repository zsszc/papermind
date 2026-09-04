# Batch 29 测试报告：保论文集合的深层证据候选

## 1. 结论

本批实现并评测唯一候选 `paper-preserving-deep-route-v1`。它先按生产 semantic/BM25 各前
10 项的 legacy RRF 冻结 top-5 论文 slot，再只允许相同论文中的 top-20 深层 chunk 替换，
从结构上杜绝 Batch 27B 的跨论文挤出。

候选改善了 chunk 级排序指标，但没有改善证据字符覆盖，因此没有晋级：

- Recall@5：`0.3462 → 0.3846`；
- MRR：`0.3385 → 0.3615`；
- NDCG@5：`0.2962 → 0.3265`；
- factoid Recall：`0.3750 → 0.4375`；
- span coverage@5：`0.4522 → 0.4522`。

预注册主 Gate 要求 span 至少 `+1/13`，实际增益为 0，故配对 Gate 为 **FAIL**。没有运行
dev/holdout，没有调用 Kimi，生产 `hybrid + bm25-bilingual` 默认保持不变。

## 2. SDD / TDD 轨迹

- RED `a8b82f5`：先冻结 spec/plan/tasks、论文 slot 不变量、显式 profile、公式指纹及完整
  train 配对 Gate；`eval.paper_preserving_train_gate` 缺失导致 collection ImportError。
- GREEN `17f9ec2`：实现纯融合函数、共享 RetrievalPipeline、`eval.run` 报告绑定及脱敏 Gate。
- GREEN 首轮有 2 个失败：测试夹具错误假设 legacy RRF 会先排完 semantic，实际同排名会按
  首次出现顺序交错 semantic/BM25。修正夹具的冻结期望后专项 **17 passed**，没有修改算法
  来迎合错误测试。
- 相关检索/eval 回归 **58 passed**；生产 `hybrid` 仍走既有分支，候选只能显式选择。

## 3. 冻结算法与不变量

候选读取 semantic 与 `bm25-bilingual` 各最多 20 项，但生产控制只读取各前 10 项。生产
legacy RRF top-5 的论文 ID 顺序及每篇名额被视为不可变 slot；完整 top-20 只用于同论文内部
计算 legacy RRF 分数。同分时依次优先 incumbent、首次出现顺序、chunk ID。畸形 ID、单路
重复、chunk/paper 不一致、语义或词法异常均返回空并显式 degraded。

冻结公式 SHA 为 `0c2780119c40409faf01cd8b18f9d7910a2f3df1f925fcf111afeaec6d8f594c`。

## 4. 真实 train 配对结果

基线与候选均绑定 clean Git `17f9ec2`、相同 13 题完整 train、SQLite/PDF/qrels/page-span-v2
和向量/HNSW 指纹。两次运行分别使用从同一冻结源复制的 Chroma 目录；冻结源运行前后聚合
文件指纹均为 `e9f358b90e0d975a36014fdacaae686bc72bf56a2e67f97e0a800276adda5251`。

| 指标 | 生产基线 | 候选 | 差值 | Gate |
|---|---:|---:|---:|---|
| span coverage@5 | 0.4522 | 0.4522 | 0 | **FAIL**（要求 ≥0.0769） |
| Recall@5 | 0.3462 | 0.3846 | +0.0385 | PASS |
| MRR | 0.3385 | 0.3615 | +0.0231 | PASS |
| NDCG@5 | 0.2962 | 0.3265 | +0.0303 | PASS |
| factoid Recall | 0.3750 | 0.4375 | +0.0625 | PASS |
| factoid span coverage | 0.4848 | 0.4848 | 0 | PASS（不回退） |
| method_detail Recall / span | 0.3750 / 0.5000 | 0.3750 / 0.5000 | 0 / 0 | PASS |
| summary Recall / span | 0 / 0 | 0 / 0 | 0 / 0 | PASS |
| P95 | 953.4ms | 969.8ms | +16.4ms | PASS（<1000ms） |
| 运行时降级 | 0 | 0 | 0 | PASS |

配对报告 SHA：

- 基线：`ba76b6db72a57c72db953c01c7bb9f475e0bdc0a065d24d9518f92374c47aa69`
- 候选：`22f4276fcb7db5cc03f54ffcb4935593c35ecf0337c4793dabb45ff7b4305dc1`

私有逐题报告与脱敏 Gate 聚合保留在已忽略的 `backend/eval/private/`，未提交问题、论文身份、
正文或逐题结果。

## 5. 完整回归证据

| Gate | 结果 |
|---|---|
| Batch 29 专项 | **17 passed** |
| 共享检索/eval 相关回归 | **58 passed** |
| 后端全量 | **1045 passed**，1434 warnings，19.14s |
| Python 依赖 | `pip check`：No broken requirements found |
| 前端测试 | **15 files / 66 tests passed** |
| 前端 lint / build | **PASS / PASS**；保留既有大 chunk 警告 |
| Electron 默认测试 | **26 passed / 2 skipped / 0 failed** |
| 真实发布 E2E | **10/10 passed**，14.55s |
| 公开 count RAG | Recall@5 **0.900** / MRR **0.775** / NDCG@5 **0.806** |
| 公开 BM25 RAG | Recall@5 **0.900** / MRR **0.783** / NDCG@5 **0.813** |
| 公开生成 Guardrail | P/R/F1/拒答率均 **1.000**，PASS |
| 独立失败事务 | **11/11 scenarios**，PASS |

## 6. 下一步

候选说明深层双路信号足以找到更多 qrel chunk，却没有覆盖新的证据字符区间。Batch 28 同时
显示另有 4/13 题属于“正确论文已召回，但两路 top-20 都没有证据”。因此下一批不再扩大或
加权现有双路，而是为单一 `within-paper-query-rerank-v1` 建立新 SDD：保持生产论文 slot，
在这些论文的全部 chunk 中按查询做局部定位。它仍必须先通过完整 train 的 span、三项排序、
分型、延迟与零降级 Gate；失败则不运行 dev，holdout 始终禁止。
