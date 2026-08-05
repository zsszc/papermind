# Batch 12 实施计划

## 1. 变更范围

- `backend/eval/metrics.py`：修正去重与 citation P/R/F1。
- `backend/eval/run.py`：修正引用解析、qrels preflight、逐条降级诊断和报告 v2 元数据。
- `backend/tests/`：先新增失败测试，再做最小实现。
- `docs/开发计划与进度表_2026-08-06.md` 与 `docs/test-reports/`：保留执行证据。

不修改真实数据库、论文、向量库、运行时配置和未审 `qa_candidates.jsonl`；不执行真实 LLM 调用。

## 2. 实施顺序

1. 冻结历史 hybrid 与改动前 keyword-only 基线。
2. RED：引用解析、NDCG 去重、citation P/R/F1、unresolved qrels、报告指纹。
3. GREEN：逐项最小实现。
4. REFACTOR：统一集合与报告辅助函数，不改生产检索行为。
5. 运行离线消融、后端全测、前端 lint/build，生成测试报告。
6. 更新台账，分行为提交并推送。

## 3. 风险控制

- 指标口径变化：报告 schema 与比较键显式区分，不与旧报告直接计算趋势。
- 私有数据泄露：语料指纹只对稳定元数据散列，不落正文。
- 模型不可用：离线门禁使用 keyword-only；hybrid 网络失败单列为基础设施错误。
- 小样本过拟合：不以本批观察实验改变默认生产策略。
