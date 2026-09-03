# Batch 27 测试报告：v2 诊断准入与论文优先证据重排

## 1. 结论

本批先修复了 v2 历史 train 报告的证据准入缺口，再用同一 clean Git 提交、同一真实
SQLite/PDF/QA/向量指纹完成生产基线与唯一候选的配对 train。历史报告仅获准用于候选选择，
不能作为晋级证据；fresh clean 基线确认 13 题中有 6 题属于“已召回正确论文、但没有定位到
证据块”，因此预注册并实现 `paper-first-evidence-rerank-v1`。

候选没有晋级。它将 `span_coverage@5` 从 **0.4522** 降至 **0.3753**，Recall@5 从
**0.3462** 降至 **0.3077**，factoid Recall 从 **0.3750** 降至 **0.3125**；MRR 与 NDCG@5
分别微升 0.0077 与 0.0005，P95 为 933.7ms，但无法抵消核心覆盖回退。配对 Gate 为 FAIL，
所以没有运行 dev，holdout 继续封存，生产 `hybrid + bm25-bilingual` 默认值没有变化。

## 2. 接班审计与 SDD / TDD 轨迹

- Batch 27 准入审计发现：2026-09-01 的既有 v2 “hybrid”报告实际使用
  `lexical_profile=count`，不能代表当前生产 `bm25-bilingual`。
- RED `a142f09`：历史 dirty 报告准入新增 4 个失败测试。
- GREEN `c55fede`：只有显式 `--allow-historical-dirty-report`、Git 祖先与完整冻结指纹同时
  通过时，才允许生成 `candidate-selection-only` 聚合；`promotion_eligible=false`。
- fresh clean 生产基线重新运行后，五类归因仍为：完整覆盖 5、同论文定位失败 6、跨论文失败
  1、部分覆盖 1、空结果 0；因此只选择一个候选，没有网格调参。
- Batch 27B RED `8692f37`：融合/profile 6 fail，专用 Gate 因模块缺失在收集阶段失败。
- GREEN `5802d80`：实现固定双路 top-20、RRF k=60、论文先验 0.25、每篇最多 2 块的候选，
  绑定公式 SHA，并加入严格完整 train 配对 Gate；16 项专项与后端全量通过。

## 3. 真实 train 配对结果

两次运行均绑定 clean Git `5802d80`，使用相同 dataset/qrels/corpus/database/page/vector/HNSW
指纹和各自从同一冻结源复制的 Chroma 临时快照；Embedding 使用本地缓存并设置离线环境变量。
私有逐题报告和 Gate 聚合留在已忽略的 `backend/eval/private/`，没有提交论文身份或正文。

| 指标 | 生产基线 | 论文优先候选 | 差值 | Gate |
|---|---:|---:|---:|---|
| span coverage@5 | 0.4522 | 0.3753 | -0.0769 | FAIL（要求 ≥ +1/13） |
| Recall@5 | 0.3462 | 0.3077 | -0.0385 | FAIL |
| MRR | 0.3385 | 0.3462 | +0.0077 | PASS |
| NDCG@5 | 0.2962 | 0.2967 | +0.0005 | PASS |
| factoid Recall | 0.3750 | 0.3125 | -0.0625 | FAIL |
| factoid span coverage | 0.4848 | 0.3598 | -0.1250 | FAIL |
| method_detail Recall / span | 0.3750 / 0.5000 | 0.3750 / 0.5000 | 0 / 0 | PASS |
| summary Recall / span | 0 / 0 | 0 / 0 | 0 / 0 | PASS |
| P95 | 883.6ms | 933.7ms | +50.1ms | PASS（<1000ms） |
| 运行时降级 | 0 | 0 | 0 | PASS |

配对 Gate 输入报告 SHA：

- 基线：`a979612b9a6000a370577aac367d0accfd9232ee15995d6dd6be89fc6d5620d6`
- 候选：`6bc162af045801f4f49bf522a771c3ecb0c89f623ecf112255e67f3fef8d1379`

## 4. 回归证据

| Gate | 结果 |
|---|---|
| Batch 27B 专项 | **16 passed** |
| 共享检索/eval 相关回归 | **78 passed** |
| 后端全量 | **1012 passed**，1434 warnings，15.95s |
| Python 依赖 | `pip check`：No broken requirements found |
| 前端测试 | **15 files / 66 tests passed** |
| 前端 lint / build | **PASS / PASS**；保留既有大 chunk 警告 |
| Electron 默认测试 | **26 passed / 2 skipped / 0 failed** |
| 真实发布 E2E | **10/10 passed**，13.82s |
| 公开 count RAG | Recall@5 **0.900** / MRR **0.775** / NDCG@5 **0.806** |
| 公开 BM25 RAG | Recall@5 **0.900** / MRR **0.783** / NDCG@5 **0.813** |
| 公开生成 Guardrail | P/R/F1/拒答率均 **1.000**，PASS |
| 独立失败事务 | **11/11 scenarios**，PASS |

`python -m ruff` 未运行：锁定的项目虚拟环境没有安装 ruff 模块；相关文件已通过
`git diff --check`，完整 pytest、前端 ESLint 与构建均通过。首次发布 E2E 在受限沙箱因
回环监听得到 EPERM，按测试设计在允许本地回环后重跑并 10/10 通过。

## 5. 安全边界与下一步

- 本批没有调用 Kimi，没有把问题、证据或论文正文发送到外部。
- 没有运行 dev/holdout；失败候选只保留为显式 eval profile，生产默认未改。
- 失败说明“给已占优论文再加全局先验”会挤掉 factoid 所需的其他论文，不能解决论文内部
  证据定位。下一批先建立 train-only 的双路 route-depth 聚合 Harness，测量真实证据是否
  已存在于 semantic/BM25 top-20 及其深度区间，再决定是做论文内二阶段选择还是回到
  boundary-aware 分块；仍坚持单候选、train-first、失败不看 dev、holdout 禁止。
