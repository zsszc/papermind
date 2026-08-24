# Batch 22 实施计划

1. Harness：复现生产 shared hybrid 的 private train 基线与分类型指标。
2. AUDIT：仅对 train 做匿名失败分析，多 Agent 审查架构与 TDD 边界。
3. SDD：冻结 `bm25-bilingual-v2` 四条映射和 train/dev 双阶段 Gate。
4. RED：先锁定 v2 token、排序、旧 v1 零变化、共享管线与 CLI 契约。
5. GREEN：实现独立 lexical profile，不改变生产默认与其他检索参数。
6. TRAIN：只跑一次冻结候选；未过门禁则直接结束，不运行 dev。
7. DEV：仅在 train 全过后运行一次；通过且有严格提升才单独晋级默认。
8. REGRESSION/TRACE：公开与三端全量回归、报告、进度台账、分批提交和 push。

明确不做：PRF、LLM query rewrite、增加更多词条、邻域参数再调、reranker、holdout、真实生成。
