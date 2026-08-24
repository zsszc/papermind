# Batch 22D 实施计划

1. INVENTORY：冻结页内字符坐标 schema、resolver v2 与 any-hit/span-coverage 数学定义。
2. RED：合成跨块、重叠重复、跨页和多处命中用例。
3. GREEN：实现 offset-aware chunk 输出、轻量迁移与 versioned evidence resolver。
4. STAGE：分别构造旧粒度基线副本与模型对齐候选副本，生产数据不变。
5. TRAIN：同一 v2 Benchmark 下比较两者，只执行冻结 train Gate。
6. DEV：仅 train 通过时运行一次；不看 holdout、不调用 Kimi。
7. TRACE：完整 Harness、测试报告、路线图、分批提交与 push。

