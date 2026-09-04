# Batch 30 实施计划

1. RED：冻结批量论文内 BM25、slot/锁定/替换不变量、显式 profile、公式绑定与 train Gate。
2. GREEN：实现局部候选查询和纯选择函数，接入共享 RetrievalPipeline 与 eval。
3. CLEAN RUN：同一 clean Git、同一冻结源的隔离副本分别运行生产基线与唯一候选完整 train。
4. GATE：输出脱敏配对判定；失败立即停止，禁止 dev/holdout。
5. VERIFY：全量/公开/发布回归、测试报告、台账、提交与 push。
