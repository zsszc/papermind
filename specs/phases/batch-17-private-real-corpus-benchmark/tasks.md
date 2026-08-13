# Batch 17 TDD 任务

- [x] H0：只读确认 36 PDF / 19 唯一论文 / 19 done / 464 chunks，以及候选集覆盖不足。
- [x] T1 RED：私有路径 ignore、去重 manifest 与不删除文件测试失败。
- [x] T2 GREEN：实现私有 corpus manifest 与去标识化摘要。
- [x] T3 RED：SHA-256 UID、DOI 规范化和唯一 evidence 测试失败。
- [x] T4 GREEN：扩展稳定 UID qrels resolver。
- [ ] T5 RED：全文分层素材、reviewed/coverage/split Gate 测试失败。
- [ ] T6 GREEN：实现候选与正式集审计工具。
- [ ] T7：生成并审查 50–100 条真实论文 QA，冻结 private v1。
- [ ] T8：运行 count/BM25/hybrid 基线与单变量实验。
- [ ] T9：全量回归、报告、台账、分批提交并推送。
