# Batch 20 测试报告：共享检索管线与生产 Hybrid 晋级（2026-08-24）

## 1. 结论

Batch 20 已把聊天、消息重新生成和 eval 统一到 `RetrievalPipeline`。生产聊天默认从纯语义
top-5 晋级为 BGE-M3 + `bm25-bilingual` + chunk RRF。private dev 的 Recall@5 从
0.500 提升到 0.625（+0.125），MRR 从 0.268 提升到 0.39375，NDCG@5 从 0.324
提升到 0.4517186825；factoid Recall 从 0 提升到 1/3。最终 P95=275.7ms，低于 1 秒预算。

本批未读取或运行 holdout，未把 private QA/证据发送给 Kimi，也未运行 private 生成评测。
实际应用启动只做不含文献内容的 Kimi 最小健康检查，`/api/health` 返回
`status=ok, llm_ready=true`。

## 2. Harness / SDD / TDD 证据

规格、计划和任务位于 `specs/phases/batch-20-shared-retrieval-pipeline/`。

| 微循环 | RED | GREEN |
|---|---:|---|
| 共享管线/过滤/缓存 | 11 fail / 27 pass | 38 pass |
| 聊天与 eval parity | 2 fail / 13 pass | 同查询 chunk ID 与顺序逐项一致 |
| 多指标质量 Gate | 9 fail | 39 个评测相关测试通过 |
| 生产 Hybrid 默认 | 2 fail | dev + 公开 Gate 后切换，65 个相关测试通过 |
| 全量回归发现 | 4 fail（旧测试要求结果对象 identity 相同） | 改为内容不变契约；缓存隔离允许复制对象 |

新增 Harness 覆盖：双路 paper/year filters、Chroma 限制性 where fail-closed、未知过滤拒绝、
语义缓存可变对象隔离、Hybrid RRF/降级诊断、聊天/eval parity、Recall/MRR/NDCG/factoid/P95
联合 Gate。

## 3. 修复的逻辑问题

1. Chroma 拒绝限制性 where 时曾重试无过滤查询，定向论文对话可能混入其他论文；现返回空
   语义结果，只允许同 filters 的词法降级。
2. `/api/search` 关键词路曾忽略 paper_id/year filters；现用固定 SQL 子句与绑定参数应用。
3. 语义缓存曾返回同一可变 list/dict，路由覆写 source 会污染后续请求；现缓存读写及返回均复制。
4. 聊天、重新生成和 eval 曾是三条不同检索路径；现共用服务，排序 parity 自动阻断再次漂移。
5. 旧 eval 只以 Recall 判 Gate；现可同时约束 MRR、NDCG、factoid Recall、P95 和运行期降级。

## 4. 真实 dev 指标

数据集为 private Benchmark v1 dev（24 条、6 篇），向量为显式 464-chunk 快照，rerank 关闭。

| Profile | Recall@5 | MRR | NDCG@5 | factoid Recall | P95 |
|---|---:|---:|---:|---:|---:|
| 历史 production semantic | 0.500 | 0.268 | 0.324 | 0 | 245.6ms |
| 共享 production hybrid | **0.625** | **0.39375** | **0.4517186825** | **0.3333** | **275.7ms** |

分类型 Recall：experiment_data=0.667、factoid=0.333、method_detail=0.833、summary=0.667。
运行期降级数为 0。公开冻结 BM25 仍为 Recall@5/MRR/NDCG@5 =
0.900/0.783/0.813，Recall Gate 0.85 通过。

首次用三位展示值 `0.394/0.452` 作为精确阈值时正确失败；历史有效 production hybrid
报告的未舍入值是 `0.39375/0.4517186824830735`。正式 Gate 改用未舍入基线后通过，避免
“指标完全一致但因展示舍入失败”。

## 5. factoid 匿名失败审计与下一变量

本地 dev 匿名聚合显示：纯语义 factoid 0/6，但 top-5 已命中正确论文 5/6；所有精确证据
都位于某个同论文 semantic top-20 块的 ±2 范围。全局 neighbor 可把 factoid 提到 3/6，
却损失 method 和 summary 各 1 题，使整体 Recall 降到 0.583，因此未启用。

Batch 21 的单变量将是：semantic top-20 后，只在候选论文内部扩展 ±2 chunk，并用固定距离
衰减传播分数。目标 factoid Recall>=0.50，同时 Recall>=0.625、MRR>=0.39375、
NDCG>=0.4517186825、P95<500ms、公开 Recall>=0.90。先做合成 TDD，再跑 dev。

## 6. 最终工程 Gate

| 门禁 | 结果 |
|---|---|
| 后端 pytest | **623 passed**，1066 warnings，11.85s |
| 前端 Vitest | **39 passed / 12 files** |
| 前端 lint / build | 通过；仅既有大 chunk warning |
| Electron node:test | **26 passed** |
| Python 依赖 | `pip check`：No broken requirements found |
| Node 本地依赖树 | 前端/Electron `npm ls --all` 退出 0 |
| 在线 npm audit | 未运行：外部发送依赖元数据被安全门禁拒绝；最近 Batch 19 结果为两端 0，且本批未改依赖 |
| 真实启动 | Uvicorn 正常启动/停止；`GET /api/health` 200、`llm_ready=true` |

React ErrorBoundary 测试故意输出错误栈，测试本身通过。Ruff 仍未运行，因为 venv 未安装。
主库启动仍报告 4 条历史 `paper_tags` 外键孤儿，本批未切换 Batch 18 修复候选副本。

## 7. 隐私与限制

- private 报告只写入已忽略的 `eval/private/`；Git 报告只记录聚合指标和指纹可比性结论。
- 本批没有 holdout 结果，生产晋级表示“基于 dev 的可用策略”，不是新的盲测质量承诺。
- 固定 4 条 private dev Kimi 生成烟测仍需用户明确授权 QA 与 top-5 证据出站。
- 用户根目录的简历 PDF 与 UI Prompt 文件均未修改、未暂存。
