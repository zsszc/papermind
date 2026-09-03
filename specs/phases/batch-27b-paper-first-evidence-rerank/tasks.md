# Batch 27B 任务清单

- [x] T1：完成归因驱动的单候选 SDD，冻结参数与 Gate。
- [x] T2：提交融合/profile/配对 Gate RED（融合/profile 6 failed；Gate 因模块缺失在收集阶段失败）。
- [x] T3：实现候选、公式指纹、eval 接线与配对 Gate（16 项专测、1012 项后端回归通过）。
- [x] T4：运行同提交 fresh clean 配对 train；Gate FAIL，按协议停止且未运行 dev/holdout。
- [x] T5：完成 1012 项后端及三端/公开 Gate 回归，报告与台账待本批文档提交后 push。
