# Batch 32 实施计划

1. SDD 冻结 top-5 incumbent 锁定与同论文替换。
2. TDD RED 覆盖纯选择、单向量复用、范围失败、eval CLI/指纹/Gate。
3. GREEN 实现显式候选 profile，不改变生产默认。
4. clean Git 生成生产基线与候选完整 train 报告并运行配对 Gate。
5. train 通过才运行一次固定 dev；随后全量回归、报告、提交、push。
