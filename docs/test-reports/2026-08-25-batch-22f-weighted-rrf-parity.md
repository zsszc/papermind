# Batch 22F 测试报告：Weighted-RRF 基线 Parity

## 1. 结论

Batch 22F 完成了严格去重的 Weighted-RRF 候选、隔离评测 CLI、SQLite/Chroma embedding
指纹、完整权重网格选择器、条件 dev Gate 及生产基线逐题 parity Gate。真实 train 前置 Gate
证明新等权函数并不等价于当前生产 RRF：仅 11/24 条查询的 top-5 顺序完全一致。

因此本批在运行 `1.25/1.5/2.0` 三组权重前停止，不查看 dev、holdout，不调用 Kimi，也不改变
生产检索配置。该结果避免把去重/tie 行为变化错误归因成权重收益。

## 2. SDD/TDD 与多 Agent 审查

- 三路只读审查分别覆盖公式、评测接线与选择 Gate，均未读取私有逐题内容或运行受限分区。
- 审查发现旧 RRF 会让同路重复占 rank，总分 tie 采用 first-seen；新规格则 first-win 去重并按
  chunk ID tie。两者无法在异常域同时保持 parity，因此在查看 train 前补充了真实逐题前置 Gate。
- 先后提交公式/CLI/选择器 RED、GREEN，以及可独立输出的 parity 停止制品 RED/GREEN。
- 旧 `rrf_fuse_chunks()` 与生产 `hybrid` 路径保持原样；失败候选仅能显式调用。

## 3. 快照与公平性

- 两次 train 均使用同一 464-chunk SQLite、464-vector Chroma、BGE-M3 1024 维快照。
- 报告自动校验 SQLite/Chroma ID 全等，并对排序后的 ID 与 float32 embedding 内容生成 SHA。
- dataset、qrels、corpus、page text、resolver、vector manifest 与 Git SHA 全部一致。
- 两次均为完整 24 条 train、零运行时降级、无 LLM。

## 4. 真实 train 前置结果

| 指标 | 生产 hybrid | 新 weighted 1.0/1.0 | 差值 |
|---|---:|---:|---:|
| Recall@5 | 0.667 | 0.625 | -0.042 |
| factoid Recall | 0.500 | 0.333 | -0.167 |
| MRR | 0.424 | 0.392 | -0.032 |
| NDCG@5 | 0.485 | 0.452 | -0.033 |
| P95 | 320.6 ms | 329.4 ms | +8.8 ms |
| top-5 顺序完全一致 | — | 11/24 | FAIL |

前置 Gate 要求 24/24 完全一致，实际只有 11/24。主要变化来自新函数的去重与 chunk-ID tie，
而不是权重（两路仍为 1.0/1.0）。按预注册协议，本批没有运行三组候选或任何 dev 评测。

## 5. 全量 Harness

- 后端：`712 passed, 1300 warnings`，13.11 秒。
- 公开冻结 BM25：Recall@5/MRR/NDCG@5 = `0.900/0.783/0.813`，Gate 通过。
- 前端：12 个文件、39 项测试通过；lint 零警告，生产 build 通过。
- Electron：26 项测试通过。
- `pip check`：无依赖冲突。
- 私有报告与逐题 parity 细节保留在已忽略的 `eval/private/b22f-weighted-rrf/`。

## 6. 后续决策

Batch 22G 将只引入一个变量：在完全保留旧 RRF 的重复计分、rank、first-seen tie 和 metadata
语义下乘以 lexical weight。其 `1.0/1.0` 必须对任意输入与旧函数严格 parity；通过真实 24/24
前置 Gate 后，才允许运行同一冻结权重网格。严格去重/tie 修正不与权重实验混合。
