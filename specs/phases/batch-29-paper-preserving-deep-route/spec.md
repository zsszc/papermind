# Batch 29 规格：保论文集合的深层证据候选

## 1. 背景

Batch 27B 的全局论文先验候选会把 factoid 所需论文挤出最终 top-5，已经被 train Gate 拒绝。
Batch 28 随后证明，13 个 v2 train 正例中有 4 题的证据已存在于 semantic/BM25 各自
top-20 的深层候选；双路并集 span coverage@5/@20 为 `0.529/0.760`。因此本批只验证一个
最小假设：冻结生产 top-5 的论文集合、顺序与每篇名额，只在相同论文内部用深层候选换块。

## 2. 冻结算法

候选名：`paper-preserving-deep-route-v1`。

1. semantic 与 `bm25-bilingual` 各取最多 20 项；每路 chunk ID 必须唯一、规范且与
   `paper_id` 一致。
2. 生产控制仍只读取两路前 10 项，调用现有 legacy RRF（k=60）得到 top-5。
3. 生产 top-5 的 `paper_id` 顺序就是冻结 slot 序列；每篇论文占用的 slot 数不得变化。
4. 在两路完整 top-20 上计算同一 legacy RRF 分数。对每篇已入选论文，按深层 RRF 分数选择
   与其 slot 数相同的 chunk；同分优先保留生产 incumbent，再按首次出现顺序和 chunk ID。
5. 将每篇选中的 chunk 填回该论文原有 slot；不得引入、删除、移动论文 slot，也不得重复 chunk。
6. 任一路异常或契约不合法时返回空并显式标记 degraded；不得静默回退或修改生产默认 profile。

## 3. 评测与晋级 Gate

- 只允许完整 Benchmark v2 train：13 个正例，factoid/method_detail/summary=`8/4/1`。
- 基线与候选必须来自同一 clean Git、同一 dataset/qrels/corpus/database/page/vector/HNSW 指纹。
- `span_coverage@5` 至少提升 `1/13`；Recall@5、MRR、NDCG@5、各类型 Recall 和 span 均不得回退。
- 候选 P95 < 1000ms、运行时降级为 0、禁止 LLM。
- train 任一 Gate 失败即停止，不运行 dev；holdout 始终禁止。通过时 dev 也只能经新的专用一次性 Gate。

## 4. 验收标准

- [ ] AC1：纯函数严格保持生产论文 slot 序列与每篇名额，并能用跨路深层命中替换同论文 chunk。
- [ ] AC2：不变输入得到与生产完全相同输出；畸形/重复/论文不一致输入 fail closed。
- [ ] AC3：共享 RetrievalPipeline 和 eval 仅通过显式 profile 接线，公式指纹写入报告。
- [ ] AC4：同提交完整 train 配对 Gate 只输出脱敏聚合，严格执行提升/不回退/延迟边界。
- [ ] AC5：全量回归、测试报告、台账、分段提交与 push 完成。

## 5. 非目标

- 不改变生产 `hybrid + bm25-bilingual` 默认，不做权重/池深/配额网格。
- 不读取 dev/holdout，不调用 Kimi，不发送论文内容到外部。
- 不重建或修改 SQLite、PDF、生产 Chroma、冻结向量快照。
