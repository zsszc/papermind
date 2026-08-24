# Batch 20 实施计划

1. Harness：冻结 601/39/26 工程基线，审查聊天、重新生成、搜索页和 eval 检索入口。
2. RED：新增共享管线、缓存隔离、过滤 fail-closed、聊天/eval parity 与多指标 Gate 测试。
3. GREEN A：下沉现有 BM25 bilingual/RRF，先保持生产 `semantic` 默认，完成行为 parity。
4. GREEN B：Agent 图、重新生成和 eval 改用共享入口；评测只接受显式向量快照。
5. REFACTOR：删除 eval 算法副本，保留兼容导出；完善统一诊断与中文日志。
6. EXPERIMENT：只用 private dev 跑现有 hybrid 单变量；通过指标/延迟 Gate 后再切默认。
7. REGRESSION：公开离线基准、后端全量、前端 test/lint/build、Electron、依赖检查。
8. TRACE：每个微循环中文 Conventional Commit；补测试报告、进度台账并 push。

明确不做：private holdout、真实 Kimi 生成、reranker、邻块 profile、Graph 消融、chunks FTS
或词法缓存。这些变量分别留给 Batch 21–23。
