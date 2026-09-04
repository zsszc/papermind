# Batch 31 规格：论文内语义深度可行性诊断

## 1. 背景

Batch 30 证明生产已选论文内的局部 BM25 只改变 2/65 个 slot，四项质量指标均无提升。
现有 13 个 v2 train 正例仍有 4 题属于“正确论文已经入选，但现有两路 top-20 未覆盖证据”。
本批不再直接实现排序候选，而是回答一个更窄的问题：复用生产查询向量，在已选论文的全部
chunk 中做过滤语义查询，证据实际排在多深，以及这条路径的增量延迟是否允许进入候选开发。

## 2. 冻结诊断协议

诊断名：`within-paper-semantic-diagnostics-v1`。

1. 只运行完整 Benchmark v2 train：13 个正例，factoid/method_detail/summary=`8/4/1`。
2. 每题只调用一次 `embed_query`；全局 semantic 路由和所有论文过滤查询必须复用同一向量。
3. 生产控制保持不变：semantic 与 `bm25-bilingual` 各前 10，经 legacy RRF（k=60）得到 top-5。
4. 只查询生产 top-5 已选中的唯一论文；每篇查询其全部 DB chunk，Chroma 过滤必须与目标论文一致。
5. 诊断统计基线覆盖、已选论文全部语义结果的可恢复覆盖、证据在正确论文内的首个语义名次，
   并独立记录过滤查询总增量 P50/P95；不构造或评测新 top-5 排序。
6. 报告只允许输出聚合计数、比例、时延与指纹；不得输出问题、QA/chunk/paper ID、标题、路径或正文。
7. DB、PDF、Chroma 源全程只读；Chroma 必须复制到临时目录后打开，并验证源目录前后指纹。

## 3. 可行性 Gate

只有同时满足下列条件，才推荐 Batch 32 实现唯一候选 `within-paper-semantic-rerank-v1`：

- 至少 1/13 题的 evidence span coverage 可在已选论文语义全集中严格高于生产基线；
- 每题 embedding 调用严格为 1，额外重新 embedding 次数为 0；
- 论文过滤语义查询总增量 P95 < 250ms；
- 采集过程零降级、所有范围和指纹契约通过。

否则推荐 `none`，Batch 32 正式停止这一轮 RAG 排序调参并转入发布收尾。诊断不得运行 dev；
holdout 始终禁止。

## 4. 验收标准

- [ ] AC1：完整 train、clean Git、离线环境、私有路径和只读数据契约 fail closed。
- [ ] AC2：同一查询向量被全局与论文过滤查询复用，过滤范围只含生产已选论文。
- [ ] AC3：分类、覆盖、rank bucket、P50/P95 与固定 Gate 有确定性单测。
- [ ] AC4：真实报告只含聚合白名单，并绑定代码/数据/语料/向量/HNSW 指纹。
- [ ] AC5：全量回归、测试报告、台账、分段提交与 push 完成。

## 5. 非目标

- 不实现生产候选，不修改检索默认值、SQLite、PDF 或 Chroma。
- 不调用 Kimi/LLM，不联网下载模型，不运行 dev/holdout。
- 不使用答案、qrel 或问题类型影响运行时排序。
