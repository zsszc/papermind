# Batch 22I 测试报告：Factoid 稀有实体/数值锚点路由

## 1. 结论

Batch 22I 实现了只能显式启用的 `hybrid-anchor-v1`：从问题提取数字、缩写和
ASCII 单位，新增一路不做双语扩展的 BM25，再以生产 legacy RRF `k=60` 等权融合。
候选在真实论文 train 上没有改善 factoid 或总体 Recall，且 MRR/NDCG 和
method_detail Recall 回退。严格 Gate 失败，因此未运行 dev/holdout，未改变生产默认。

## 2. SDD/TDD 轨迹与审查

- SDD：`specs/phases/batch-22i-factoid-anchor-route/` 冻结了算法、train-first Gate 和停止条件。
- RED `3c40a4b`：11 项路由语法、无锚点 parity、过滤和降级测试失败。
- GREEN `78c0e43`：隔离 profile 及共享 `RetrievalPipeline` 接线完成，157 项相关回归通过。
- RED `f22b841`：专用配对 Harness/Gate 模块缺失，测试收集阶段即失败。
- GREEN `728f633`：新增算法公式 SHA、同次遍历配对报告、严格 train Gate；171 项相关回归通过。
- 三个只读 Agent 并行审查了管线语义、Gate 泄漏/过拟合边界和 train 可行性；修正了
  早期 `try/except` 接线错位，并将通用 CLI 收紧为只允许 train。

## 3. 配对评测 Harness

- 使用真实 Benchmark v1 train 24 题、19 篇/464 chunks、BGE-M3 1024 维和
  `num_threads=1/search_ef=464` 确定性 stage。
- 每题只计算一次 semantic 与 `bm25-bilingual` 基础路由，两个排序共享相同输入；
  报告绑定共享路由指纹、锚点决策指纹和算法公式指纹。
- 20/24 问题命中预注册锚点规则，4 题无锚点；无锚点题生产/候选 top-5 完整顺序一致。
- 报告不保存问题原文或锚点明文，仅保存 QA ID、排序、指标和 SHA；未调用 Kimi。

| 指标 | 生产 hybrid | `hybrid-anchor-v1` | 差值 | Gate |
|---|---:|---:|---:|---|
| Recall@5 | 0.6666666667 | 0.6666666667 | 0 | PASS 非回退 |
| factoid Recall | 0.5000000000 | 0.5000000000 | 0 | **FAIL**，要求 +1/6 |
| MRR | 0.4236111111 | 0.3986111111 | -0.0250 | **FAIL** |
| NDCG@5 | 0.4852888182 | 0.4649490727 | -0.02034 | **FAIL** |
| method_detail Recall | 0.8333333333 | 0.6666666667 | -1/6 | **FAIL** |
| experiment_data Recall | 0.8333333333 | 1.0000000000 | +1/6 | PASS |
| P95 | 369.9 ms | 435.9 ms | +66.0 ms | PASS（<1s） |

自动 Gate `passed=false`。单一类型的偶然改善不能抵消 factoid 零改善、排序质量和另一
类型回退。本批没有在失败后调整规则、单位表或权重重跑。

## 4. 全量 Harness

- 后端：`852 passed, 1300 warnings`，14.01 秒。
- 前端：12 个文件、39 项通过；ESLint 零警告；Vite 生产构建通过。
- Electron：26 项通过。
- 公开冻结 BM25：Recall@5/MRR/NDCG@5=`0.900/0.783/0.813`，Gate 通过。
- `pip check`：`No broken requirements found`。
- Uvicorn 实际启动并正常停止；Kimi 最小健康检查 HTTP 200，`/api/health` 返回
  `status=ok`、`llm_ready=true`。未发送真实论文、QA 或证据。
- 主 SQLite 仍报告已知 4 条 `paper_tags` 历史孤儿，本批未覆盖用户数据。

## 5. 决策与下一步

1. 保留显式实验 profile 与 Harness 用于回归，但不晋级、不改生产 `hybrid`。
2. v1 train/dev 已被多轮实验观察；不再根据该集微调“稀有锚点+中文实体”规则。
3. Batch 22J 先对 `papers/` 中尚未进入 Benchmark v1 的真实论文建立盲化 v2；在任何新
   候选开发前冻结论文级 split、qrel、证据唯一解析与评测指纹。
4. 真实生成评测仍需单独的内容出站授权；本批只做了不含论文内容的 Kimi 健康检查。
