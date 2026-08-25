# Batch 22I 实施计划

1. Harness：冻结 Batch 22H 生产 hybrid、真实 train 和公开回归指纹。
2. RED：锚点提取、无锚点 parity、三路 legacy RRF、profile/CLI/Gate 失败关闭。
3. GREEN：在共享 RetrievalPipeline 实现隔离 `hybrid-anchor-v1`。
4. PARITY：先证明无锚点输入与生产 hybrid 深度相等。
5. TRAIN：同次生产/候选配对 train，自动多指标 Gate。
6. DEV：仅 train 通过时运行一次，不基于结果回调规则。
7. TRACE：全量 Harness、测试报告、路线图、中文分段提交与 push。
