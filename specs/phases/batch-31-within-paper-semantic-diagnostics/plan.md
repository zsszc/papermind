# Batch 31 实施计划

1. SDD：冻结单向量复用、论文范围、聚合隐私与可行性 Gate。
2. TDD RED：先覆盖聚合分类、rank bucket、延迟阈值、隐私与采集器调用契约。
3. GREEN：实现独立只读诊断 CLI，不接入生产请求链路。
4. 在 clean Git 上运行真实完整 train，输出 ignored 私有聚合报告。
5. 根据预注册 Gate 给出唯一结论；随后执行全量回归并提交测试报告。
