# Batch 29 实施计划

1. RED：冻结论文 slot 保持、深层替换、严格输入、显式 profile、报告绑定与 train Gate 契约。
2. GREEN：实现纯融合函数与共享 RetrievalPipeline/eval 接线，不改生产默认。
3. CLEAN RUN：在同一 clean Git 上分别运行生产基线与唯一候选完整 train。
4. GATE：生成脱敏配对判定；失败立即停止，成功才允许下一批设计 dev 一次性 Gate。
5. VERIFY：全量/公开/发布回归、测试报告、进度台账、提交与 push。
