# Batch 17 实施计划

## 1. 文件范围

- `.gitignore`：私有 QA、manifest 与历史候选防泄漏。
- `backend/eval/private_benchmark.py`：去重 manifest、稳定 UID、split 与审稿审计。
- `backend/eval/dataset.py`：`sha256:` evidence 与 DOI 规范化解析。
- `backend/eval/generate_qa.py`：全文分层素材与默认真实论文覆盖。
- `backend/tests/`：UID、manifest、隐私、候选覆盖与评测 Gate。
- `backend/eval/private/`：本地私有 manifest/候选/正式集，始终不提交。
- `docs/test-reports/` 与开发进度表：仅提交聚合结果。

## 2. TDD 顺序

1. RED/GREEN：私有路径 ignore 与真实 corpus 去重 manifest。
2. RED/GREEN：DOI 规范化、SHA-256 paper UID、唯一 evidence resolver。
3. RED/GREEN：候选全文分层取样、审稿/覆盖/split Gate。
4. 在私有目录生成 50–100 条候选，并按论文拆分给多 Agent 对照 chunks 审稿。
5. 冻结 private v1 train/dev/holdout；先跑 count/BM25/hybrid 基线。
6. 只在 dev 进行单变量检索改进，复核 holdout 后输出聚合报告。

## 3. 风险控制

- 不删除当前约 100.9MB 重复 PDF；本批所有真实数据操作均只读，生成物仅写 `eval/private/`。
- 不把 LLM 自动生成等同人工审稿；自动 quote 校验只证明定位存在，不证明问题和答案正确。
- 调用配置的 Kimi 生成候选会发送抽样论文文本；沿用应用现有 LLM 使用边界，但报告不记录文本或密钥。
- 无法加载 BGE/Chroma 时明确标记降级，不能把 keyword-only 结果标成 hybrid。
