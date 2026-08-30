# Batch 22L 任务清单

- [ ] T1：split 分配与预冻结（train 12 / dev 11 / holdout 11，排他制品）
- [ ] T2：QA 生成器与证据唯一校验器（eval/generate_qa_v2.py，TDD）
- [ ] T3：真实 LLM 候选生成 + 用户人工审稿（每 split ≥ 12 条）
- [ ] T4：数据集冻结 + train/dev 盲测基线 + 报告台账 push
