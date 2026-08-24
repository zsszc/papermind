# Batch 22C 测试报告：超长 chunk 隔离重建（2026-08-24）

## 1. 结论

本批完成了超长单段硬上限、候选 SQLite 原子重建、显式只读候选评测以及 Chroma 1024 维
门禁。工程改动和完整 Harness 全部通过，但真实 512/50 分块候选没有进入排序评测：private
train 的 24 条 evidence qrel 只有 16 条仍能唯一映射，8 条证据被切分到相邻 chunk。

这是预先冻结的 fail-close 条件。因此没有构建 2904 条向量，没有运行 train 排序指标、dev、
holdout 或 Kimi，也没有替换生产 SQLite/Chroma。生产检索仍使用已验证的 464-chunk shared
hybrid 基线 0.625/0.39375/0.4517186825。

## 2. SDD / TDD 留痕

规格、计划和任务位于 `specs/phases/batch-22c-staged-chunk-rebuild/`。

| 微循环 | RED | GREEN |
|---|---:|---|
| 超长单段、分隔符、overlap | 4 fail / 6 pass | embedding + processor 15 passed |
| 英文句末优先于后置空白 | 1 fail | embedding + processor 16 passed |
| 候选 SQLite 隔离重建 | 模块缺失，collection error | 相关 27 passed |
| 显式只读评测、1024 维 Gate | 6 fail | 评测/向量相关 57 passed |
| 后端全量 | — | 658 passed，1220 warnings，13.18s |

关键提交：`3c64874`/`18c3f28`、`7e0761f`/`bbfa651`、
`ce69a38`/`d8e2eac`、`69bb5f3`/`d8267d9`。每项 RED 与 GREEN 分开提交。

## 3. 工程实现

- `TextChunker` 对超长单段执行“句末/分号 → 空白 → 固定窗口”三级切分；拼接分隔符计入
  字符上限，异常 overlap 被限制为最多 `chunk_size-1`，保证不死循环。
- `staged_chunk_rebuild` 使用 SQLite backup API 创建包含 WAL 的候选副本，在单事务中只替换
  `processed=done` 论文的 chunks；不实例化 `PaperProcessor`，不连接 VectorStore。
- parser/chunker 任一步失败会回滚并清理候选 DB/WAL/SHM；候选路径禁止等于源或覆盖已有文件，
  论文相对路径禁止逃逸只读语料根。
- 候选副本清理 4 条历史 `paper_tags` 孤儿后通过外键 Gate；生产源库仍保留 4 条，未修改。
- `eval.run --database --corpus-root` 通过 SQLite `mode=ro` + `query_only` 打开候选，不回连生产
  `SessionLocal`；`vector_rebuild --database` 同样只读，CLI 默认要求向量维度为 1024。

## 4. 真实候选聚合审计

候选位于已忽略的 `backend/eval/private/b22c-staged-chunks/papers.db`，未提交 Git。

| 指标 | 生产源库 | 512/50 候选 |
|---|---:|---:|
| 已处理论文 | 19 | 19 |
| 总 chunks | 464 | 2904 |
| 正文 / 摘要哨兵 | 445 / 19 | 2885 / 19 |
| 正文 P50 / P95 / 最大字符 | 1872 / 约 5750 / 9776 | 452 / 510 / 512 |
| 正文超过 512 | 419 | 0 |
| 正文 token_count=字符数 | 445/445 | 2885/2885 |
| 论文-页坐标覆盖 | 252 | 252 |
| `quick_check` / FK 违规 | ok / 4 | ok / 0 |

源库仍为 464 chunks、4 条历史孤儿且 `quick_check=ok`，证明真实数据没有被候选构建修改。

## 5. qrel 前置 Gate

只读取 private train，未读取 dev/holdout；没有输出问题、证据或论文原文。

| 项目 | 结果 |
|---|---:|
| train QA | 24 |
| 单 chunk 唯一解析 | 16/24 |
| quote 未命中 | 8 |
| 失败 evidence quote 长度范围 | 328–480 字符 |
| 重复命中 / paper UID 错误 | 0 / 0 |

旧 resolver 要求 quote 完整且唯一地位于一个 chunk。细分后 8 条 quote 横跨相邻块，完整性
Gate 失败。按规格停止在向量构建之前，避免约 2904 次无效向量写入；也没有根据 train 逐题
内容临时增大 overlap、修改 quote 或放宽 resolver。

## 6. 完整 Harness

| 门禁 | 结果 |
|---|---|
| 后端 pytest | 658 passed |
| 前端 Vitest | 39 passed / 12 files（ErrorBoundary 错误栈为预期） |
| 前端 lint / build | 通过；仅既有大 bundle warning |
| Electron node:test | 26 passed |
| 公开冻结 BM25 | 0.900/0.783/0.813，Recall Gate 0.85 PASS |
| Python / Node 依赖 | `pip check` 与两端 `npm ls --all` 退出 0 |
| 真实启动 | `/api/health` 200，`status=ok`、`llm_ready=true`，正常停止 |

项目 venv/CI 仍没有 Python Ruff 步骤；本批使用 `git diff --check`、pytest 与现有 CI 等价
门禁。未改依赖，未运行在线 npm audit。

## 7. 决策与下一步

- 512/50 候选失败，不晋级、不运行 dev；生产数据与指标不变。
- 硬分块和隔离工具保留：前者修复资源/截断边界，后者为后续候选提供可回滚 Harness。
- 下一批 Batch 22D 先建立版本化跨块 evidence Benchmark v2：同一个新 resolver 下同时重跑
  旧粒度基线与新候选，报告 any-hit 和 span-coverage，禁止把 v1/v2 指标直接拼接。
- 真实 Kimi 生成评测和主库数据换入仍分别等待用户明确授权。
