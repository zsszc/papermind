# Batch 27B 任务清单

- [x] T1：完成归因驱动的单候选 SDD，冻结参数与 Gate。
- [x] T2：提交融合/profile/配对 Gate RED（融合/profile 6 failed；Gate 因模块缺失在收集阶段失败）。
- [x] T3：实现候选、公式指纹、eval 接线与配对 Gate（16 项专测、1012 项后端回归通过）。
- [ ] T4：运行 fresh clean 配对 train，并按 Gate 停止或运行一次 dev。
- [ ] T5：全量回归、报告、台账与 push。
