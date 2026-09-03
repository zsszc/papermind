# Batch 27B 实施计划

1. RED：冻结纯融合、共享 profile、公式指纹和配对 train Gate。
2. GREEN：实现论文先验重排及 eval 显式 profile，生产默认不变。
3. CLEAN RUN：同提交、同快照分别运行生产基线与候选完整 train。
4. GATE：候选失败立即停止；通过才一次性运行 dev。
5. VERIFY：全量/公开 Gate、测试报告、台账、分段提交与 push。
