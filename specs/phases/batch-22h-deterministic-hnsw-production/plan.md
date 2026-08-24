# Batch 22H 实施计划

1. Harness：冻结 Batch 22G 报告、当前真实 SQLite/vector 指纹与生产路径。
2. RED：补 stage 内容不变、重复性 Gate、dev Gate、激活前后二次审计与回滚测试。
3. GREEN：扩展确定性快照工具和独立选择制品，不修改生产默认路径。
4. EXPERIMENT：候选 train 双跑；仅通过才运行一次 dev。
5. ACTIVATE：仅 dev 通过时备份并原子切换，随后做完整性和 query smoke。
6. REGRESSION：全量 Harness、测试报告、项目台账、分段提交与 push。
