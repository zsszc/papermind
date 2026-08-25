# Batch 22H 任务清单

- [x] T1：审计当前生产 SQLite/vector 与 Batch 22G 确定性证据。
- [x] T2：提交 stage/重复性/dev/激活 Gate RED。
- [x] T3：实现 GREEN 并验证 embedding 内容零变化。
- [x] T4：执行候选 train 双跑与自动重复性 Gate。
- [x] T5：仅 train 通过时运行一次 dev 并决定是否激活（严格提升 Gate 失败，未激活）。
- [x] T6：跳过不合格候选激活，完成全量 Harness、报告与 push。
