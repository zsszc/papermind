# Batch 18 测试报告：数据一致性、上传安全与有效向量评测（2026-08-14）

## 1. 结论

Batch 18 已消除 Batch 17 暴露的三类高风险问题：SQLite 备份不一致、Chroma 历史索引失配、上传只信扩展名。真实数据操作均先备份、后隔离验证；没有删除论文、修改私有 QA 或使用 holdout 调参。

当前生产 `vector_db/` 已从 SQLite 的 464 个 chunk 重新生成，ID 集合 464/464 完全一致，BGE-M3 向量维度为 1024，并通过 query smoke。旧失配向量库保留为本地恢复目录，另有 209MiB 的完整 ZIP 备份。显式生产向量目录上的 private dev hybrid 达到 Recall@5=0.625、MRR=0.394、NDCG@5=0.452，Recall 门槛 0.60 通过。

真实 SQLite 主库 `quick_check=ok`，有 4 条历史 `paper_tags` 外键孤儿。修复只在候选副本执行：4 条均被清理、候选外键违规为 0，源库 SHA-256 前后不变。候选没有自动换入。

## 2. Harness / SDD / TDD 证据

| 环节 | RED | GREEN |
|---|---|---|
| SQLite 一致备份 | 直接复制 WAL 主库，手动导出路径不统一 | SQLite backup API 单文件快照；quick-check；ZIP 排除 DB/WAL/SHM；临时文件 fsync + 原子换名 |
| 数据完整性 | 迁移错误可能被吞；主库损坏后仍可能继续启动 | schema/FTS 错误传播；任何写入前只读 quick-check fail-close；审计只返回聚合计数 |
| 仅副本修复 | 无安全修复通道 | 只对白名单 `paper_tags` 孤儿操作；dry-run/幂等；候选二次 quick/FK Gate；源库不变 |
| Chroma 重建 | 旧 HNSW 1000 个历史 ID 与 464 条 metadata 失配 | 全新同父目录 stage；精确 ID/count/维度/query Gate；两段 rename 与失败回滚；保留旧库 |
| 评测隔离 | hybrid 可隐式打开或创建真实向量目录 | CLI 强制 `--vector-dir`；只 `get_collection`；目录或 collection 缺失立即失败 |
| 上传安全 | PDF/DOCX 只检查扩展名 | PDF `%PDF-`；DOCX 有界 ZIP、必要成员、路径/加密/重复/CRC/解压资源门禁；异常回滚文件与 DB |
| 依赖安全 | npm 官方审计发现 `nanoid 3.3.17` 高危 | 锁文件最小升级至 3.3.18；前端回归和官方 audit 均通过 |

规格、计划和完成任务位于 `specs/phases/batch-18-data-integrity-upload-security/`。

## 3. 真实数据验证

| 项目 | 结果 |
|---|---|
| 完整 ZIP | 209MiB；`unzip -t` 无错误；含一致 SQLite 快照、不含 WAL/SHM |
| SQLite 源库 | quick-check 通过；4 条外键违规，均为历史 `paper_tags` 孤儿 |
| 修复候选 | 删除 4 条孤儿；quick-check 通过；外键违规 0 |
| 源库保护 | 修复前后 SHA-256 相同；未将候选换入生产 |
| 新 Chroma | 464 个 ID，和 SQLite 精确相等；1024 维；query smoke 通过；当前目录 14MiB |
| 旧 Chroma | 37MiB，保留于已忽略的 `vector_db.backup-*` 恢复目录 |

备份和候选副本均位于已 gitignore 的本地数据目录，本报告不记录论文标题、正文、绝对路径或密钥。

## 4. dev 指标实验（未使用 holdout）

| private dev / Profile | Recall@5 | MRR | NDCG@5 | P95 | 结论 |
|---|---:|---:|---:|---:|---|
| BM25 + 中英术语 | 0.458 | 0.285 | 0.329 | 77.3ms | 对照 |
| BM25 + 中英术语 + 相邻块 | 0.583 | 0.310 | 0.377 | 83.0ms | 词法实验提升，保留为可选 profile |
| 重建 hybrid + 中英术语 | **0.625** | **0.394** | **0.452** | **291.2ms** | 当前有效开发结果，Recall Gate 0.60 PASS |
| 重建 hybrid + 相邻块叠加 | 0.583 | 0.361 | 0.417 | 392.0ms | 相比 hybrid 对照回退，不采用 |

相邻块词法实验使 factoid Recall 从 0.167 提升到 0.333、method_detail 从 0.667 提升到 0.833、summary 从 0.167 提升到 0.333；但其公开基准排序指标和 hybrid 叠加结果回退，因此没有替换默认 profile。有效 hybrid 的 summary Recall 为 0.667，factoid 仍只有 0.333，是后续主要弱项。

本批只读取 private dev 24 条进行选择；没有运行或重新读取 holdout 报告。上述数值是开发集诊断，不宣称为新的盲测指标。

## 5. 最终工程 Gate

| 门禁 | 结果 |
|---|---|
| 后端 pytest | **571 passed**，988 warnings，11.88s |
| 后端依赖 | `pip check`：No broken requirements found |
| 公开冻结 BM25 | **Recall@5=0.900 / MRR=0.783 / NDCG@5=0.813，PASS** |
| 前端 Vitest | **11 passed / 4 files** |
| 前端 lint / build | 通过；保留既有 ui/StatsPage 大 chunk warning |
| Electron node:test | **26 passed** |
| npm 官方 audit | 前端 **0 vulnerabilities**；Electron **0 vulnerabilities** |
| 真实启动 | 启动完成；`GET /api/health` 返回 200 与 `status=ok` |

ErrorBoundary 测试会故意输出 React 错误栈，测试本身通过。Ruff 仍未运行，因为现有 venv 未安装该工具；这是延续的 Harness 债务。

## 6. 已知限制与下一步

1. Kimi 健康检查返回 429，应用报告 `llm_ready=false`；错误指向 Moonshot 账户额度不足或被冻结。恢复额度后需补一次真实生成侧验收。
2. 主 SQLite 的 4 条历史孤儿仍在，候选副本已验证但未获授权换入；下一批可在新备份后做显式切换与回滚烟测。
3. PDF 当前执行文件头门禁，深层结构损坏仍由解析器发现；未来可增加页树/尾标记和超复杂 PDF 资源预算。
4. 并行运行多个真实评测进程时曾观察到日志轮转文件竞争；指标报告未受影响，后续应给轮转器增加跨进程保护，正式评测保持串行。
5. Batch 19 按计划进入前端可靠性；RAG 继续改进时优先统一生产与评测 RetrievalPipeline，并针对 factoid 的数值、单位和实体锚点做 dev 单变量实验。
