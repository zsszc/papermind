# Batch 26 测试报告：Benchmark v2 train 失败归因 Harness

## 1. 结论

已建立纯本地、只读、去标识化的 v2 train 失败归因 Harness。它不读取问题/PDF/chunk 正文，
只使用 `eval.run` 报告中的覆盖值与匿名 chunk 归属，将每条正例互斥归为：

- `cross_paper_miss`：top-5 未进入证据所属论文；
- `same_paper_miss`：进入正确论文但未定位证据 chunk；
- `partial_coverage`：命中部分证据字符跨度；
- `empty_retrieval`：无返回结果；
- `full_coverage`：证据跨度完整覆盖。

输出只含聚合计数、比例、基线指标、枚举候选和指纹，不含 qa_id、chunk_id、标题、DOI、
路径或正文。本批没有读取真实 v2 train/dev/holdout 报告，没有调用 Kimi/Embedding，也没有
修改生产检索默认值，因此不宣称检索指标已经提升。

## 2. SDD / TDD 轨迹

- SDD：`spec/plan/tasks` 冻结五类归因、严格 train-only 输入、隐私白名单和候选 Gate。
- RED：模块不存在，专项测试在 collection 阶段出现预期 `ImportError`。
- GREEN：实现 `eval.train_failure_diagnostics`；13/13 专项通过。
- 合并验证：与 span pair/质量 Gate 共同运行 27/27 通过。

提交：`f8b45cb`（SDD/RED）→ `1e9a1aa`（GREEN）。

## 3. 安全与质量契约

- 只接受 `report_schema=2.0`、clean Git SHA、完整 train、top-5、`page-span-v2`、无 LLM、
  零运行时降级的报告。
- 重复/畸形 chunk ID、重复 qa_id、指标越界、计数不守恒、dev/holdout 均 fail closed。
- CLI 输入输出只能位于 `backend/eval/private/`，拒绝 symlink 和目录逃逸。
- 输出排他创建、权限精确为 0600；序列化稳定，同一输入双跑字节一致。
- 主导失败只注册一个下一候选；train Gate 失败禁止运行 dev，holdout 始终禁止。

## 4. 回归证据

| Gate | 结果 |
|---|---|
| Batch 26 专项 | **13 passed** |
| 后端全量 | **992 passed**，1434 warnings，21.59s |
| Python 依赖 | `pip check`：No broken requirements found |
| 前端测试 | **15 files / 66 tests passed** |
| 前端 lint / build | **PASS / PASS**；保留既有大 chunk 警告 |
| Electron 默认测试 | **26 passed / 2 skipped / 0 failed** |
| 真实发布 E2E | **10/10 passed**，14.21s |
| 公开 count RAG | Recall@5 **0.900** / MRR **0.775** / NDCG@5 **0.806** |
| 公开 BM25 RAG | Recall@5 **0.900** / MRR **0.783** / NDCG@5 **0.813** |
| 公开生成 Guardrail | P/R/F1/拒答率均 **1.000**，PASS |
| 独立失败事务 | **11/11 scenarios**，PASS |

## 5. 已知限制与下一步

- 本批只证明归因工具本身可信，未打开真实 train 报告，所以尚不知道实际主导失败类别。
- 分类依赖 top-5 报告，不能判断相关证据在第 6 名以后多远；若主导为跨论文失败，下一候选
  需要单独记录更深候选池，但不得把扩大 top-k 直接冒充生产质量提升。
- 下一批仅在本地读取既有 v2 train 报告生成聚合，按主导类别实现一个单变量候选；train
  Gate 未通过则停止，不运行 dev；holdout 保持封存。
