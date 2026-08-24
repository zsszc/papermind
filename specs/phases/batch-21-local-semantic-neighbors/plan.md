# Batch 21 实施计划

1. Harness：确认 Batch 20 基线、真实库 chunk 不变式与工作区边界。
2. SDD：冻结 `top20 + ±2 + 0.5^d/rank + max + cap20 + RRF` 单变量算法。
3. RED：先写邻域传播、过滤/边界、异常诊断和聊天/eval parity 合成测试。
4. GREEN：实现批量邻域读取与显式 `hybrid-local-neighbor` profile，保持旧 profile 不变。
5. REFACTOR：统一过滤查询和内部常量，确保一次 SQL、稳定排序与返回对象隔离。
6. EXPERIMENT：复制当前 464-chunk Chroma 为隔离快照，只跑 private dev；按五项精确 Gate
   一次判断是否晋级，不运行 holdout/LLM。
7. REGRESSION：运行公开冻结评测、后端全量、前端 test/lint/build、Electron 与依赖检查。
8. TRACE：每个 TDD 微循环及时中文 Conventional Commit；补测试报告、进度台账并 push。

明确不做：query expansion、数据库复合索引、reranker、生成评测、holdout、thesis/deep_review
迁移。这些分别留给后续独立批次。
