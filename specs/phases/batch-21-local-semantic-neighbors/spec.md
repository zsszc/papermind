# Batch 21 规格：论文内语义邻域证据定位

## 1. 背景与目标

Batch 20 已将生产聊天与评测统一为 BGE-M3 + `bm25-bilingual` + chunk RRF，private
dev 的 Recall@5/MRR/NDCG@5 达到 0.625/0.39375/0.4517186825，但 factoid Recall
仍只有 2/6。匿名失败审计显示：精确证据均位于某个同论文 semantic top-20 命中块的
`±2` 范围，因此本批只改变一个变量——在语义候选论文内部扩展局部 chunk 邻域。

先新增显式候选 profile `hybrid-local-neighbor`，不直接修改生产默认。只有 private dev、
公开冻结基准、聊天/评测 parity、延迟与工程回归全部通过，才单独提交晋级默认配置。

## 2. 冻结算法

- semantic seed pool 固定为 20；最终 top-k 仍为 5。
- 对语义 seed `s` 的 1-based 排名 `r_s` 与同论文候选 chunk `c`，距离
  `d = |index(c) - index(s)|`，只接受 `d ∈ {0, 1, 2}`。
- 单 seed 传播分：`P(c;s) = 0.5^d / r_s`；多 seed 重叠时
  `P(c) = max_s P(c;s)`，禁止求和，避免长论文或密集命中获得不合理加成。
- 排序键固定为 `(-P, min_distance, best_seed_rank, chunk_id)`，扩展后的语义候选池截断 20。
- 词法分支保持 `bm25-bilingual` top-10；两路继续使用既有 `k=60` RRF，最终取 top-5。
- `chunk_index=-1` 是摘要哨兵：只允许自身作为直接 seed，不与正文 `c0+` 跨边界传播。

## 3. 正确性与安全契约

- 邻域只认 canonical `p{paper_id}_c{chunk_index}`，且 ID 中的 paper_id 必须与 metadata
  一致；非法或不一致 seed 跳过。
- 候选必须是 SQLite 中真实存在的 chunk，引用元数据以 SQLite 为准；不得虚构边界块。
- 邻域读取复用与 BM25 相同的 `paper_id/year_gte/year_lte` 过滤，绝不跨论文或越过年份范围。
- 最多 100 个 `(paper_id, chunk_index)` 键通过一次批量 SQL 读取，禁止 N+1。
- 不修改 VectorStore 返回的字典或缓存对象；重叠候选按 chunk_id 去重并取最大传播分。
- 邻域扩展异常时，生产回退到未扩展的 baseline hybrid，但 diagnostics 必须标记
  `degraded=true`、`effective_profile=hybrid`、`reason=semantic_neighbor_expansion_failed`；
  eval 据此 fail-close。
- 旧 `semantic` 与 `hybrid` profile 的候选数量、排序和降级契约保持不变。

## 4. 质量与隐私 Gate

private Benchmark v1 只运行 dev，不读取或运行 holdout，不调用 Kimi，不发送真实 QA/证据：

- Recall@5 >= 0.625；
- MRR >= 0.39375；
- NDCG@5 >= 0.4517186824830735；
- factoid Recall >= 0.50；
- P95 < 500ms，运行期降级数为 0；
- 公开冻结 BM25 Recall@5 >= 0.90，且公开三项指标不回退。

任一门禁失败时保留候选 profile 与报告，但不修改 `retrieval.chat_profile`。本批不同时增加
数据库索引、术语表、query expansion、reranker 或 Graph，以保持指标变化可归因。

## 5. 验收标准

1. 合成 Harness 覆盖传播公式、半径/边界、摘要哨兵、重叠去重、过滤、非法 seed、稳定排序、
   单次 SQL、缓存隔离与失败诊断，并保留 RED/GREEN 证据。
2. 聊天首次发送、重新生成与 eval 使用同一候选 profile 时，chunk ID 和顺序逐项一致。
3. private dev 通过全部晋级 Gate 后才切换生产默认；公开基准无回退。
4. 后端、前端、Electron 全量测试及 lint/build/依赖检查通过；测试报告只提交聚合指标。
