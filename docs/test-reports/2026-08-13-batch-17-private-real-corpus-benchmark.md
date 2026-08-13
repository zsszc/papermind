# Batch 17 测试报告：私有真实语料 Benchmark v1（2026-08-13）

## 1. 结论

Batch 17 已将 RAG 质量评测从单篇示例/公开合成语料扩展到用户真实论文库。去标识化盘点为 36 个物理 PDF、19 份唯一内容、19 篇已入库且处理完成的论文、464 个分块；17 个重复副本未被删除或移动。

私有 Benchmark v1 包含 72 条已审阅中文 QA，覆盖 18 篇真实论文，train/dev/holdout 各 24 条，四种正例题型各 18 条。全部 72 条 evidence quote 均能经稳定 DOI/SHA-256 UID 唯一解析到一个 chunk，失败数为 0。真实 QA、答案和证据保留在 gitignore 私有目录，本报告不包含标题、路径或原文。

在 dev 上冻结了可审计的中英领域术语扩展，Recall@5 从 0.333 提升到 0.458；一次性 holdout 验收中 Recall@5 从 0.542 提升到 0.583，MRR 从 0.308 提升到 0.353，NDCG@5 从 0.365 提升到 0.410。公开冻结基准 Recall@5 保持 0.900。

## 2. Harness / SDD / TDD 证据

| 环节 | RED | GREEN |
|---|---|---|
| 隐私与语料身份 | 私有路径未隔离；旧 qrels 依赖动态 paper ID | 私有目录/QA 显式 ignore；DOI 规范化 + PDF SHA-256 稳定 UID |
| evidence 与审稿 Gate | 短 locator 可多重命中；未审候选可混入 | 20–500 字符唯一 quote；reviewed/覆盖率/论文级 split fail-close |
| 基准组装 | 缺少多份审稿产物合并器 | 校验后临时文件原子换入，中断不会留下半截正式集 |
| 开发/留出隔离 | CLI 无 split 过滤 | `--split train/dev/holdout`，空分区立即失败；报告记录 split |
| 词法提升 | 中文问题的 ASCII BM25 只能依赖模型名/缩写 | 可审计领域词表将中文术语扩展为英文锨点，原始 BM25 profile 保持不变 |
| hybrid 正确性 | 发现 Chroma 历史日志回放与 HNSW/metadata 不一致；运行期降级仍可以 hybrid 名义 PASS | 将未来写入改为 upsert；任一语义降级使 Gate 失败并标记 `runtime-degraded` |
| 全量回归 | 首轮 10 fail：旧最小夹具只有 chunk，稳定 manifest 查不到 paper | 以 chunk 内容哈希作隔离夹具稳定身份，不退回动态 ID；530 用例全绿 |

规格、计划与任务位于 `specs/phases/batch-17-private-real-corpus-benchmark/`。私有 qrels 指纹为 `f8d9c1b19804b29835cf3c205bdda06e270c5c55863e2cbb68c5c2f945716eb9`，语料 manifest 指纹为 `ebec9b8f9342d4f525338ab9a5385d2323dd0aa29f49b1bd017c711fb4c875df`。

## 3. 真实语料指标

| 范围 / Profile | Recall@5 | MRR | NDCG@5 | P95 | 状态 |
|---|---:|---:|---:|---:|---|
| 全集 count（72） | 0.222 | 0.125 | 0.148 | 12.8ms | 有效词法基线 |
| 全集 BM25（72） | 0.458 | 0.270 | 0.317 | 64.7ms | 有效词法基线 |
| 全集 hybrid（72） | 0.278 | 0.120 | 0.160 | 262.7ms | **无效诊断，禁止作质量结论** |
| dev BM25（24） | 0.333 | 0.212 | 0.243 | 61.1ms | 选择基线 |
| dev BM25 + 中英术语（24） | **0.458** | **0.285** | **0.329** | 78.6ms | 冻结候选 |
| holdout BM25（24） | 0.542 | 0.308 | 0.365 | 70.0ms | 一次性对照 |
| holdout BM25 + 中英术语（24） | **0.583** | **0.353** | **0.410** | 75.1ms | 一次性验收 |

hybrid 运行虽未报语义降级计数，但启动时已出现大量重复 ID 回放警告。后续只读审查确认当前 HNSW 快照为 1000 个旧 ID，而 metadata 为 464 个当前 ID，且隔离读取可触发 `IndexError`，因此该行数值不可信，不用于选型。本批未删除、原地重建或换入真实向量库。

留出集较弱的仍是 factoid（Recall@5=0.333）；全集 BM25 中 factoid 仅 0.222。下一阶段应优先改进数值/实体锨点与语义候选库一致性，而不是盲目提高 RRF 权重。

## 4. 最终工程 Gate

| 门禁 | 结果 |
|---|---|
| 私有 Benchmark 审查 | **72 条 / 18 篇 / 72 条唯一解析 / 0 失败** |
| 后端 pytest | **530 passed**，967 warnings，11.70s |
| 后端 `pip check` | No broken requirements found |
| 公开冻结 BM25 | **Recall@5=0.900 / MRR=0.783 / NDCG@5=0.813，PASS** |
| 前端 Vitest | **11 passed / 4 files** |
| 前端 lint / build | 通过；保留既有 ui/StatsPage 大 chunk warning |
| Electron node:test | **26 passed** |
| 前端 / Electron 官方 npm audit | **0 vulnerabilities / 0 vulnerabilities** |

ErrorBoundary 测试会故意输出 React 错误栈，测试本身通过。Ruff 仍未运行：当前 venv 没有安装 Ruff，与 Batch 16 记录的 Harness 债务一致。

## 5. 下一批计划（Batch 18）

1. SQLite 使用在线 backup API 产生一致快照，在副本上执行 `integrity_check` / `foreign_key_check` / dry-run repair，不吞启动损坏错误。
2. 为 Chroma 建立显式备份和临时新库重建器：从 SQLite 464 chunks 重建，校验 ID 集合、count、embedding 维度与 query smoke 后才原子换入；失败时保留旧库。
3. hybrid 评测改用隔离向量快照与 `get_collection` fail-close，禁止评测在真实数据目录上 `get_or_create_collection`。
4. 上传链路增加 PDF/DOCX 魔数、ZIP 条目数/解压总量/压缩比限制，异常不遗留文件或数据库孤儿。
