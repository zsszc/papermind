# Batch 22 测试报告：病理术语双语扩展 v2（2026-08-24）

## 1. 结论

本批按 train-first 流程实现了显式候选 `bm25-bilingual-v2`，只新增四条通用病理术语映射，
旧 `bm25-bilingual` 与生产 shared hybrid 保持不变。合成 TDD、聊天/eval parity、全量回归
均通过，但候选在 private train 的 Recall/MRR/NDCG 与各类型指标和基线完全相同，没有达到
“至少新增 1 题”的预设 Gate。

因此流程在 train 阶段停止：**未运行 dev、未修改生产默认**。这避免了在已经多次使用的 dev
上继续挑词。生产质量基线仍是 Batch 20 的 dev 0.625/0.39375/0.4517186825。

## 2. Harness / SDD / TDD 证据

规格、计划和任务位于 `specs/phases/batch-22-bilingual-query-expansion/`。

| 微循环 | RED | GREEN |
|---|---:|---|
| v2 token/profile/CLI | 4 fail / 11 pass | 28 个词法、旧管线、邻域相关测试通过 |
| 聊天/eval parity | 参数化新增 v2 路径 | 19 个词法/parity 测试通过 |
| 后端全量 | — | 638 passed，1168 warnings，11.73s |

新增 Harness 冻结：四条映射声明顺序、重叠 `feature` 去重、未知术语不猜测、缩写/数字/百分比
保真、v2 opt-in、旧 v1 零变化、CLI 接受候选，以及聊天/eval 在 v1/v2 下逐项排序一致。

## 3. train-only 匿名诊断与冻结变量

private train 共 24 条。生产基线 8 个 miss 中，最终 top-5 已全部命中正确论文，问题集中在
同论文内的证据块排序；不适合扩大论文召回或引入高漂移 PRF。三位 Agent 分别审查 train
匿名聚合、共享管线架构与 TDD 后，冻结唯一变量：

- 切片 → slide；
- 肿瘤 → tumor；
- 特征提取 → feature / extraction；
- 特征 → feature。

semantic top-10、BM25 参数、RRF、top-5、filters、rerank 均未改变。候选是独立 profile，
失败不会影响生产 v1。

## 4. private train Gate

使用当前 464-chunk Chroma 的临时隔离快照，Embedding 强制离线；没有读取/运行 dev 或
holdout，没有调用 Kimi，没有发送 QA/证据。

| Profile | Recall@5 | MRR | NDCG@5 | factoid Recall | P95 |
|---|---:|---:|---:|---:|---:|
| shared hybrid + bilingual v1 | 0.66667 | 0.42361 | 0.48529 | 0.500 | 326.2ms |
| shared hybrid + bilingual v2 | 0.66667 | 0.42361 | 0.48529 | 0.500 | 281.0ms |

四类型 Recall 也完全相同：experiment_data=0.833、factoid=0.500、method_detail=0.833、
summary=0.500。P95 通过，但质量没有严格提升；预设 Recall Gate 为至少 17/24=0.70833，
实际仍为 16/24，故退出非零。按 SDD，dev 阶段被跳过。

## 5. 公开与工程回归

| 门禁 | 结果 |
|---|---|
| 公开冻结 BM25 | 0.900/0.783/0.813，Gate 通过 |
| 后端 pytest | 638 passed |
| 前端 Vitest | 39 passed / 12 files（ErrorBoundary 错误栈为预期） |
| 前端 lint / build | 通过；仅既有大 chunk warning |
| Electron node:test | 26 passed |
| Python / Node 依赖 | `pip check` 通过；两端 `npm ls --all` 退出 0 |
| 真实启动 | `/api/health` 200，`status=ok`、`llm_ready=true`，正常停止 |

在线 npm audit 未运行：本批未改依赖，避免向外部发送完整依赖元数据。

## 6. 决策与后续

- v2 保留为显式、可复现的失败候选；不写入 `config.yaml.example`，不晋级。
- 两批本地排序启发式都没有改善 factoid，下一步不继续在 dev 调参；先做检索旁路收敛和
  chunk/section 元数据质量审计，再决定新的 train-first 变量。
- private 逐题报告仍只在已忽略目录；Git 报告只有聚合指标。
- 主库 4 条历史 `paper_tags` 外键孤儿仍未自动覆盖；用户简历和 UI Prompt 未改动/暂存。
- 提交留痕：`3136775`、`919c85f`、`0a1702a`、`a6b2052`。
