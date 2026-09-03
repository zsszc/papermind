# Batch 27B 规格：论文优先证据重排候选

## 1. 决策依据

fresh clean 的生产配置 v2 train 归因与历史 count 归因一致：13 题中完整覆盖 5、失败 8；
`same_paper_miss=6`（46.15%）为绝对主导，跨论文失败和部分覆盖各 1，空结果 0。因此只验证
`paper-first-evidence-rerank-v1`，不做权重网格或多候选试探。

## 2. 冻结算法

- 输入仍为生产 semantic 与 `bm25-bilingual` 两路；每路候选池固定 20。
- chunk 基础分保持生产 RRF：`Σ 1/(60 + rank)`，rank 从 1 开始。
- 论文先验取该论文候选中的最大 chunk RRF 分；候选分固定为
  `chunk_rrf + 0.25 * paper_prior`。
- 最终 top-5 每篇论文最多 2 个 chunk，避免长论文垄断；不足时按候选分继续填充其他论文。
- 并列依次按首次出现顺序、chunk ID；重复/畸形 ID 或 `paper_id` 不一致 fail closed。
- 算法只作为显式 eval profile，生产默认 `hybrid` 不变。

## 3. 配对 train Gate

基线与候选必须在同一 clean Git 提交、相同冻结 dataset/qrels/corpus/database/page/vector/HNSW
指纹下各跑完整 13 题 train，均为 `bm25-bilingual`、page-span-v2、top-5、零降级、无 LLM。

候选同时满足才可晋级：

- `span_coverage@5` 至少提升 `1/13`；
- Recall@5、MRR、NDCG@5 均不回退；
- factoid/method_detail/summary 的 span coverage 与 Recall 均不回退；
- P95 < 1000ms；
- 逐题集合完整一致，报告与公式 SHA 绑定。

train Gate 失败立即停止；通过后才允许一次配对 dev。holdout 始终禁止。

## 4. 验收标准

- [ ] AC1：纯融合函数稳定、不改输入、每论文上限 2，坏输入 fail closed。
- [ ] AC2：共享 RetrievalPipeline 显式 profile 生效，异常标记 degraded；默认 hybrid 排序不变。
- [ ] AC3：配对 Gate 对跨配置、dirty、子集、降级、指标/分型/延迟回退 fail closed。
- [ ] AC4：fresh clean train 基线/候选完成；按 Gate 停止或一次运行 dev。
- [ ] AC5：三端/公开 Gate、报告、台账、提交与 push 完成。

## 5. 非目标

- 不调 0.25、候选池或每论文上限，不运行网格。
- 不修改 Embedding、分块、query expansion 或生产默认配置。
- 不调用 Kimi，不读取 dev（除非 train Gate 全部通过），不消费 holdout。
