# Batch 28 实施计划

1. RED：冻结纯聚合、完整 train、隐私白名单、路径与离线契约。
2. GREEN：实现 `eval.route_depth_diagnostics`，复用 page-span resolver、生产 legacy RRF、
   只读数据库和隔离向量审计。
3. CLEAN RUN：在实现提交后，以真实 v2 train 和向量冻结源运行一次诊断。
4. DECIDE：按预注册互斥类别与优先级只冻结一个下一候选；不运行 dev/holdout。
5. VERIFY：全量/公开 Gate、测试报告、进度台账、提交与 push。
