# Batch 22C 规格：超长 chunk 隔离重建与 train-first 评测

## 1. 背景

Batch 22B 对真实库做了只读审计：19 篇论文共 464 个 chunk，正文 445 个；437/464
超过配置的 512 字符，415/464 超过 1024 字符，211/464 超过 2048 字符。正文长度中位数
1872、P95 约 5.75k、最大 9776 字符。根因是 `TextChunker` 只在段落之间切分，单个超长
段落不会二次切分；Embedding 又会按空白词截到前 512 词，证据可能位于未编码的块尾。

private train 的 24 条 qrel 均可唯一解析，但 23 条证据落在 >512 字符块，说明下一变量应是
证据粒度，而非继续添加查询扩展或启用高延迟 reranker。

## 2. 范围与不变量

### S1：纯分块算法

- 为单个超长段落增加确定性硬切：优先句号/分号/空白边界，找不到安全边界时固定窗口切分。
- 每个正文 chunk 的 `len(content)` 不超过配置 `chunk_size`；非末块保留不超过
  `chunk_overlap` 的有界重叠，不得产生空块、死循环或重复全文。
- 不跨页合并；保留 page_number；chunk_index 按论文内输出顺序从 0 连续编号，摘要继续为 -1。
- 本批唯一检索变量是 chunk 粒度；不改 query expansion、BM25/RRF 参数、top_k、reranker
  或生产 profile。`section_title` 识别另批处理。

### S2：隔离数据构建

- 不在生产 SQLite/Chroma 上原地 reprocess。先备份主 SQLite，再复制为候选数据库，只在副本
  中重新解析 19 篇已处理论文并重建 chunks。
- 候选数据库通过 `quick_check`、`foreign_key_check`、chunk 坐标唯一/连续、非空、页面覆盖和
  qrel 唯一解析后，才允许从该副本构建全新 stage Chroma。
- stage Chroma 必须继续使用原子构建/校验流程，验证 ID 全等、1024 维与 query smoke；失败
  删除 stage，不触碰当前 `vector_db`。

### S3：train-first 晋级

- 先在同一候选 SQLite + stage Chroma 上复跑 private train；不得读取 dev/holdout。
- train 晋级门：Recall@5 至少新增 1 题；MRR 与 NDCG@5 不低于旧 train 超过 0.02；
  factoid 不回退；P95 < 1s；运行期零降级；24/24 qrel 仍唯一解析。
- 只有 train 通过才运行一次 dev。dev 门：Recall@5 不低于 0.625、factoid 不低于 0.333，
  MRR/NDCG 至少一项严格提升且另一项回退不超过 0.01，P95 < 1s、零降级。
- 不运行 holdout；不调用 Kimi。候选失败则保留报告，不激活生产数据。

## 3. 激活边界

候选通过 train/dev 仍不自动替换主 SQLite 或 `vector_db`。激活会同时改变真实 chunks 与
向量快照，必须先生成同批恢复包并获得用户明确确认；换入后复跑完整 Harness 与健康检查。

## 4. 验收标准

1. RED/GREEN 覆盖单段超长、中英文标点、无边界长串、重叠、页码、顺序和配置回退。
2. 候选构建可重复，失败不修改生产 SQLite/Chroma；源与候选路径在报告中使用匿名标签。
3. 候选质量审计给出长度分布、qrel 解析率、chunk/向量 ID 一致性和 train/dev Gate。
4. 后端、前端、Electron、公开基准、依赖与真实健康检查全绿；提交和报告完整。

## 5. 执行结论（2026-08-24）

512/50 候选成功生成 19 篇、2904 chunks（正文 2885、摘要 19），正文 P50/P95/最大为
452/510/512 字符，DB 完整性与源库隔离通过。但 private train 的 evidence qrel 仅 16/24
仍能唯一解析，另 8 条 328–480 字符证据跨越硬切边界而未命中。

因此候选在任何向量构建前被拒绝：没有生成 stage Chroma，没有运行 train 排序指标、dev、
holdout 或 Kimi，也没有激活生产 SQLite/Chroma。硬上限与隔离工具作为工程能力保留；下一批
先版本化解决重叠/跨块证据的评测语义，不能为了该候选临时放宽旧 qrel。
