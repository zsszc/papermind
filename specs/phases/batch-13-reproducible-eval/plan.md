# Batch 13 实施计划

## 1. 变更范围

- `backend/eval/fixtures/`：原创合成语料与 schema 说明。
- `backend/eval/dataset/`：公开稳定 QA。
- `backend/eval/fixture.py`：fixture 校验与临时 SQLite seed。
- `backend/eval/dataset.py`：evidence qrels 校验和解析。
- `backend/eval/run.py`：`--fixture`、benchmark/qrels 指纹与确定性报告字段。
- `.github/workflows/eval.yml`：改为完全离线、可复现的公开基准。
- `backend/tests/`、`docs/test-reports/`：TDD 与执行证据。

## 2. TDD 顺序

1. RED：固定 DOI fixture seed 到临时库，且不接触真实 `SessionLocal`。
2. GREEN：实现严格 fixture schema 与临时 SQLite 生命周期。
3. RED：evidence quote 唯一解析、零/多命中失败、旧 locator 兼容。
4. GREEN：实现 evidence qrels 解析与 qrels 指纹。
5. RED：同 fixture 连续两跑比较键和质量指标一致。
6. GREEN：接入 CLI/report，并把 CI 切到离线公开赛道。
7. 运行 count/BM25 消融、全量回归和测试报告。

## 3. 风险控制

- 不把私人论文派生正文放进 Git；fixture 全部原创合成。
- 不使用 QA ground truth 扩写查询；检索 profile 与评测标注相互独立。
- 不把同一语料上的训练式调参结果当 holdout；本批只建立 CI correctness/stability gate。
- 临时数据库在进程结束后释放，不污染应用数据目录。
