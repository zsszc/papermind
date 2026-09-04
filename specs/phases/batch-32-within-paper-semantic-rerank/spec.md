# Batch 32 规格：论文内语义选块唯一候选

## 1. 冻结候选

候选名：`within-paper-semantic-rerank-v1`。

1. 每题只生成一次 query embedding；生产全局 semantic 与论文过滤 semantic 必须复用该向量。
2. 生产控制仍为 semantic/BM25 各前 10，经 legacy RRF（k=60）得到 top-5；论文 slot 顺序与名额冻结。
3. 对生产已选的每篇论文只取论文内 semantic top-5，排序沿用 Chroma distance；不得查询未选论文。
4. 若 incumbent 位于同论文 semantic top-5，原位锁定；否则用该论文排名最高且未被任何 slot 使用的
   chunk 替换。无合法替换时保留 incumbent。
5. ID、metadata、论文范围、重复、向量复用或局部查询任一契约失败时返回空并显式 degraded。
6. 候选不按 QA 身份、答案、qrel 或问题类型分支，不含阈值/权重网格。

## 2. Train Gate

- 完整 v2 train 13 题，基线/候选来自同一 clean Git 与全部冻结指纹。
- span coverage@5 至少提升 `1/13`。
- Recall@5、MRR、NDCG@5、各题型 Recall/span 均不得回退。
- 候选 P95 <1000ms，零运行时降级，禁止 LLM。
- train 失败立即停止；通过后只允许一次固定配置 dev。holdout 始终禁止。

## 3. 验收标准

- [x] AC1：单向量复用和已选论文 top-5 查询有测试证明。
- [x] AC2：slot/名额不变，top-5 incumbent 锁定，其他 slot 同论文替换。
- [x] AC3：eval CLI、公式指纹和配对 Gate fail closed。
- [x] AC4：真实 train 按预注册 Gate 判定；通过后仅运行一次 dev，最终因延迟 Gate 未晋级。
- [x] AC5：全量回归、测试报告、台账、分段提交与 push 完成。

## 4. 非目标

- 不调 embedding、HNSW、BM25 或 RRF 参数；不调用 Kimi。
- 不修改生产默认，除非 train 与一次 dev 均通过后另行决定。
- 不运行 holdout，不写入真实 SQLite/PDF/Chroma。
