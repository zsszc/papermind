# Batch 22B 实施计划

1. INVENTORY：分类所有 VectorStore 消费者，冻结迁移/保留清单。
2. AUDIT：只读统计真实 chunk/section/page/length 与 private train qrel 匿名质量。
3. RED A：锁定 paper scope graph expansion 不越界。
4. RED B：锁定 Deep Review/Thesis 共享 profile、关键词降级与零证据不调用 LLM。
5. GREEN：最小迁移两处消费者，保留不同响应和提示词契约。
6. HARDEN：搜索页语义适配器异常时保留论文级关键词结果。
7. REGRESSION：后端全量、前端 test/lint/build、Electron、公开基准、健康与依赖。
8. TRACE：聚合测试报告、路线图、AGENTS、中文提交和 push。
