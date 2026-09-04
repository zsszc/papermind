# Batch 30 测试报告：正确论文内全块查询定位候选

## 1. 结论

本批实现并评测唯一候选 `within-paper-query-rerank-v1`。候选保持生产 top-5 的论文 slot
顺序和每篇名额，用一个批量 SQL 读取已选论文全部 chunk，再以生产 `bm25-bilingual` v1
进行局部定位。正分 incumbent 原位锁定，只允许同论文正分 chunk 替换零分 slot。

候选未晋级。真实完整 train 中只有 2/13 个问题发生变化，共替换 2/65 个 slot；Recall@5、
MRR、NDCG@5、span coverage@5 及全部问题类型指标均与生产基线完全相同。预注册主 Gate
要求 span 至少 `+1/13`，实际增益为 0，因此 Gate 为 **FAIL**。没有运行 dev/holdout，
没有调用 Kimi，生产默认没有变化。

## 2. SDD / TDD 轨迹

- RED `0136c0c`：先冻结 spec/plan/tasks、批量论文范围、BM25 参数、slot/锁定/替换不变量、
  显式 profile、公式指纹与完整 train 配对 Gate；缺少 contract metadata 导致 collection ImportError。
- GREEN `84bbbe2`：实现 `within_paper_bm25_search`、纯选择函数、共享 RetrievalPipeline、
  `eval.run` 报告绑定与去标识化 Gate。
- 专项 **17 passed**；相关词法/共享检索/eval 回归 **58 passed**。
- 畸形或重复 ID、chunk/paper 不一致、未选论文返回、非正有限分数、局部 SQL 异常均 fail closed；
  错误诊断不包含查询或异常原文。

## 3. 冻结算法

生产基线仍是 semantic/BM25 各前 10 的 legacy RRF top-5。候选只读取这些 top-5 中出现的
论文，用一次 SQL 获取全部相关 chunk，并在该子语料上以 `k1=1.2`、`b=0.9`、双语 v1 token
计算 BM25。局部分数大于 0 的 incumbent 不动；零分 slot 才可由同论文、非 incumbent 的正分
chunk 替换，排序固定为 `score desc, chunk_id asc`。

冻结公式 SHA：`4f80269d1c40785d867f89c7c9d0f0985794425fe46f5b9794c35f0283dea37e`。

## 4. 真实 train 配对结果

基线与候选绑定 clean Git `84bbbe2`、相同完整 13 题 train、SQLite/PDF/qrels/page-span-v2
及向量/HNSW 指纹，并各自使用从同一冻结源复制的 Chroma 目录。冻结源运行前后聚合文件指纹
保持 `e9f358b90e0d975a36014fdacaae686bc72bf56a2e67f97e0a800276adda5251`。

| 指标 | 生产基线 | 候选 | 差值 | Gate |
|---|---:|---:|---:|---|
| span coverage@5 | 0.4522 | 0.4522 | 0 | **FAIL**（要求 ≥0.0769） |
| Recall@5 | 0.3462 | 0.3462 | 0 | PASS |
| MRR | 0.3385 | 0.3385 | 0 | PASS |
| NDCG@5 | 0.2962 | 0.2962 | 0 | PASS |
| factoid Recall / span | 0.3750 / 0.4848 | 0.3750 / 0.4848 | 0 / 0 | PASS |
| method_detail Recall / span | 0.3750 / 0.5000 | 0.3750 / 0.5000 | 0 / 0 | PASS |
| summary Recall / span | 0 / 0 | 0 / 0 | 0 / 0 | PASS |
| P95 | 962.7ms | 833.6ms | -129.1ms | PASS（<1000ms） |
| 运行时降级 | 0 | 0 | 0 | PASS |

单次小样本延迟差异受运行噪声影响，不据此宣称候选更快。配对报告 SHA：

- 基线：`efd65f6a0413135f609a963d1aa53e3014d17f1114a7ea3821ef5d0f182f4649`
- 候选：`e920ce2dea49f413ba40997a9b57ff4d41a6be67b318f1d531666c050693cd57`

私有逐题报告与 Gate 聚合留在已忽略的 `backend/eval/private/`，未提交问题、论文身份、正文
或逐题结果。

## 5. 完整回归证据

| Gate | 结果 |
|---|---|
| Batch 30 专项 | **17 passed** |
| 共享检索/eval 相关回归 | **58 passed** |
| 后端全量 | **1062 passed**，1441 warnings，19.15s |
| Python 依赖 | `pip check`：No broken requirements found |
| 前端测试 | **15 files / 66 tests passed** |
| 前端 lint / build | **PASS / PASS**；保留既有大 chunk 警告 |
| Electron 默认测试 | **26 passed / 2 skipped / 0 failed** |
| 真实发布 E2E | **10/10 passed**，14.10s |
| 公开 count RAG | Recall@5 **0.900** / MRR **0.775** / NDCG@5 **0.806** |
| 公开 BM25 RAG | Recall@5 **0.900** / MRR **0.783** / NDCG@5 **0.813** |
| 公开生成 Guardrail | P/R/F1/拒答率均 **1.000**，PASS |
| 独立失败事务 | **11/11 scenarios**，PASS |

## 6. 下一步

局部 BM25 的词面信号只触发两个无质量变化的替换，无法处理“正确论文已选但证据词面不匹配”
的问题。Batch 31 先建立只读 train-only 诊断：复用单次 query embedding，测量生产已选论文
内部 evidence 的 semantic rank、可恢复数量与增量延迟。只有诊断证明存在足够覆盖且延迟预算
可行，才另开 SDD 实现论文内语义候选；仍不运行 dev/holdout。
