# Batch 21 测试报告：论文内语义邻域候选（2026-08-24）

## 1. 结论

本批实现并验证了显式候选 profile `hybrid-local-neighbor`：semantic top-20 后在同论文内
扩展 `±2` chunk，以 `0.5^distance / semantic_rank` 传播分数、重叠取最大值，再与既有
`bm25-bilingual` 走 RRF。算法、过滤、失败回退与聊天/eval parity Harness 全部通过。

候选没有通过 private dev 晋级 Gate，因此**未修改生产默认**，聊天继续使用 Batch 20 的
`hybrid + bm25-bilingual`。候选 Recall@5 保持 0.625，但 MRR 从 0.39375 降到
0.36389、NDCG@5 从 0.45172 降到 0.43005，factoid Recall 仍为 0.333；正确的
fail-close Gate 阻止了失败实验进入生产。

## 2. Harness / SDD / TDD 证据

规格、计划与任务位于 `specs/phases/batch-21-local-semantic-neighbors/`。

| 微循环 | RED | GREEN |
|---|---:|---|
| 邻域公式/边界/过滤/批量 SQL | 8 fail | 13 个邻域与旧管线定向测试通过 |
| eval profile 与聊天 parity | 2 fail / 1 pass | 34 个检索、评测 Gate 相关测试通过 |
| 后端全量 | — | 633 passed，1155 warnings，12.72s |

新增离线 Harness 覆盖：固定传播公式、`±2` 半径、摘要 `c-1` 哨兵、重叠取 max、稳定排序、
paper/year 过滤、非法及 metadata 不一致 seed、单次批量 SQL、输入复制隔离、邻域失败降级诊断、
旧 hybrid 的 top-10 零变化、新 profile 的 semantic top-20，以及聊天/eval 逐项排序一致。

## 3. 实现与安全边界

- 新 profile 仅显式启用，不改变 `config.yaml.example` 或代码缺省生产配置。
- 邻域候选必须是 SQLite 中真实存在的 chunk，引用元数据以 SQLite 为准；不虚构边界块。
- 所有候选复用 BM25 的 `paper_id/year_gte/year_lte` 过滤；摘要不跨入正文。
- 最多 100 个候选坐标只执行一次 SQL；当前真实库 464 chunks 下无需增加新索引。
- 扩展异常回退 baseline hybrid，同时 diagnostics 标记 degraded，eval 不会把回退结果记作候选成绩。

## 4. private dev 单变量实验

数据集为 Benchmark v1 dev（24 条、6 篇），使用当前 464-chunk Chroma 的临时隔离快照，
Embedding 强制离线，rerank 关闭；未读取或运行 holdout，未调用 Kimi，未发送 QA/证据。

| Profile | Recall@5 | MRR | NDCG@5 | factoid Recall | P95 |
|---|---:|---:|---:|---:|---:|
| 生产 shared hybrid | 0.625 | 0.39375 | 0.45172 | 0.333 | 275.7ms |
| local semantic neighbor | 0.625 | 0.36389 | 0.43005 | 0.333 | 270.1ms |
| 变化 | 0 | -0.02986 | -0.02167 | 0 | -5.6ms |

候选分类型 Recall 为 experiment_data=0.833、factoid=0.333、method_detail=0.667、summary=0.667。
相对生产基线，它新增 1 个 experiment 命中但丢失 1 个 method 命中；factoid 只有 1 题排名
上升，没有新增命中。21/24 个 top-5 列表发生变化，说明固定邻域传播对排序扰动过大。

Gate 结果：Recall 与 P95 通过；MRR、NDCG、factoid Recall 失败；运行期降级数为 0。保留
候选 profile 供可审计复现，但不晋级、不基于同一 dev 临时调衰减参数追分。下一变量将在
train 上先冻结 query expansion，再对 dev 做一次晋级判断，降低开发集过拟合风险。

## 5. 公开与工程回归

| 门禁 | 结果 |
|---|---|
| 公开冻结 BM25 | Recall@5/MRR/NDCG@5 = 0.900/0.783/0.813，Gate 通过 |
| 后端 pytest | 633 passed |
| 前端 Vitest | 39 passed / 12 files（ErrorBoundary 故意错误栈属预期） |
| 前端 lint / build | 通过；仅既有大 chunk warning |
| Electron node:test | 26 passed |
| Python 依赖 | `pip check`：No broken requirements found |
| Node 本地依赖树 | 前端/Electron `npm ls --all` 退出 0 |
| 在线 npm audit | 未运行：本批未改依赖，避免向外部发送完整依赖元数据 |
| 真实启动 | `GET /api/health` 200，`status=ok`、`llm_ready=true`，正常停止 |

## 6. 隐私、限制与留痕

- 私有逐题报告继续只写入已忽略的 `backend/eval/private/`；本报告仅记录聚合指标。
- 没有运行 holdout 或真实论文生成；实际启动仅执行不含论文内容的 Kimi 最小健康检查。
- 主库仍有 4 条历史 `paper_tags` 外键孤儿；本批未覆盖真实 SQLite。
- 用户根目录的简历 PDF 与 UI Prompt 文件未修改、未暂存。
- TDD/实现提交：`645588c`、`f606900`、`5497326`、`9dc1817`、`6a86c98`。
