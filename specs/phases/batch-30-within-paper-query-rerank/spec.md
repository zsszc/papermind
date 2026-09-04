# Batch 30 规格：正确论文内全块查询定位候选

## 1. 背景

Batch 28 显示，v2 train 的 13 个正例中有 4 题已经召回正确论文，但 semantic/BM25 两路
top-20 均没有覆盖证据。Batch 29 保持论文 slot 后使用 top-20 换块，虽将 Recall/MRR/NDCG
提升至 `0.385/0.362/0.326`，却没有增加 span coverage，已按 Gate 拒绝。下一步不再扩大
现有路由，而是在生产已选论文的全部 chunk 中，用原查询做一次局部 BM25 定位。

## 2. 冻结算法

候选名：`within-paper-query-rerank-v1`。

1. 生产控制保持不变：semantic 与 `bm25-bilingual` 各前 10，经 legacy RRF（k=60）得到 top-5。
2. 冻结生产 top-5 的论文 slot 顺序与每篇名额；候选不得引入、删除或移动论文。
3. 只用一个批量 SQL 读取已入选论文的全部 chunk；查询 token 使用生产
   `bm25-bilingual` v1，BM25 参数固定 `k1=1.2`、`b=0.9`，在该查询的已选论文子语料上计算。
4. BM25 分数大于 0 的生产 incumbent 原位锁定；只允许替换 BM25=0 的 slot。
5. 替换项必须来自同论文，按 `score desc, chunk_id asc` 选择，且不得与任一 incumbent 或已选项重复；
   没有合法替换时保留原 chunk。
6. 任一路/局部查询异常或 ID、paper、重复、范围契约不合法时，候选返回空并显式 degraded。

该规则不含权重、阈值网格或 QA 身份分支；本批只验证这一套固定算法。

## 3. 评测与晋级 Gate

- 只允许完整 Benchmark v2 train：13 个正例，factoid/method_detail/summary=`8/4/1`。
- 基线与候选必须来自同一 clean Git、同一 dataset/qrels/corpus/database/page/vector/HNSW 指纹。
- `span_coverage@5` 至少提升 `1/13`；Recall@5、MRR、NDCG@5、各类型 Recall/span 均不得回退。
- 候选 P95 < 1000ms、运行时降级为 0、禁止 LLM。
- train 任一 Gate 失败即停止，不运行 dev；holdout 始终禁止。

## 4. 验收标准

- [x] AC1：局部 BM25 只查询生产已选论文，固定词法与排序口径，未选论文无法进入候选。
- [x] AC2：纯选择保持论文 slot；正分 incumbent 锁定，只替换同论文零分 slot，输入不被修改。
- [x] AC3：畸形/重复/越界论文、局部查询异常 fail closed；生产 profile 无变化。
- [x] AC4：eval 报告绑定公式指纹；同提交完整 train 配对 Gate 严格执行全部门槛。
- [x] AC5：全量回归、测试报告、台账、分段提交与 push 完成。

## 5. 非目标

- 不使用 embedding reranker、LLM、Kimi、答案或 qrel 做在线排序。
- 不运行 dev/holdout，不修改生产配置、SQLite、PDF 或 Chroma。
- 不调 BM25 参数，不增加同义词，不同时尝试多个替换策略。
