# Batch 22F 规格：粗粒度 Weighted-RRF 校准

## 1. 背景与目标

Batch 22D/22E 连续证明细粒度重分块及 parent 聚合都会损害真实证据覆盖。当前 464-chunk
共享 hybrid dev 基线为 Recall@5/MRR/NDCG@5 `0.625/0.394/0.452`，其中 factoid Recall 仅
`0.333`；生产语义单路 factoid Recall 为 `0`，说明词法分支能补回事实型问题，但等权 RRF
可能没有充分利用该信号。

目标是在不改分块、不重建向量、不引入慢 reranker 的前提下，用有限且可审计的 Weighted-RRF
校准改善 factoid，同时守住总体覆盖、排序与延迟。

## 2. 预冻结候选

- 基线：现有等权 RRF，语义/词法权重 `1.0/1.0`。
- 候选网格仅允许：`1.0/1.25`、`1.0/1.5`、`1.0/2.0`；RRF `k=60`、两路召回深度与
  `bm25-bilingual` 保持不变。
- 同一路先按 canonical chunk ID 去重，重复不得占 rank；总分 tie 以 chunk ID 稳定排序；元数据
  固定优先取 semantic 分支，否则取 lexical 分支。
- 旧等权 parity 只定义在生产可达规范域：两路各自 ID 唯一且总分无并列。在该域 `1.0/1.0`
  必须与旧函数顺序及元数据完全一致；异常域按新函数的去重/tie 契约测试，旧函数保持零改动。
- 不按 QA ID、论文、具体术语或 evidence quote 写规则；运行 train 前冻结整个网格和选优顺序。
- 选优采用词典序：先满足硬 Gate，再最大化 factoid Recall、总体 Recall、MRR、NDCG；仍并列时
  选择更接近等权的较小词法权重。
- 基线与三组候选统一使用显式 `weighted-rrf-v1` profile；权重不编码进 profile 名，报告单独记录。
- 进入网格前，使用同一快照另跑一次旧生产 `hybrid` 控制；其 24 条 train 的 QA ID 与 top-5
  `retrieved_ids` 顺序必须与 weighted `1.0/1.0` 逐题完全一致，否则立即停止本批。

## 3. 数据协议与 Gate

- 只使用现有真实库 train 24 条做候选选择，报告只提交去标识化聚合。
- train 硬 Gate：总体 Recall 与 any-hit 不低于等权基线；factoid 至少提升 1/6；MRR/NDCG
  回退不超过 0.02；P95 < 1 秒；零运行时降级。
- 仅一个 train 胜者允许执行一次 dev 配对；dev 要求总体 Recall、factoid、MRR、NDCG 均不回退，
  且至少一个主指标严格提升，才允许提出生产激活。
- holdout 保持封存；本批不调用 Kimi。若所有候选 train 失败，记录拒绝并转向 query expansion
  的自动语料统计方案，不继续调网格。
- 自动选择器必须一次接收恰好一份 `1.0/1.0` 基线及三份冻结候选，输入顺序不影响结果；缺失、
  重复或额外权重均 fail-close。公共配对指纹至少包含 dataset、qrels、corpus、page text、resolver、
  SQLite/Chroma ID 与 embedding、不含权重的融合公式 SHA。每次运行另记录包含权重的配置 SHA，
  该字段必须按冻结权重不同，不能错误加入公共相等键。

## 4. 工程范围

- 新增不改变旧 `rrf_fuse_chunks()` 默认行为的加权纯函数或显式参数。
- 候选仅通过评测 profile 接线；dev Gate 通过前不改变聊天/搜索生产配置。
- 报告记录权重、公式 SHA、SQLite/Chroma manifest、split、完整样本数和两侧降级计数。
- CLI 权重仅允许 `1.0/1.25/1.5/2.0`，并强制显式 database/corpus/vector、page-span-v2、top-5、
  `bm25-bilingual`、train/dev；禁止 QA 子集、keyword-only、reranker、parent DB 与 LLM。
- 继续遵循 SDD → RED → GREEN → private train → 条件 dev → Harness → 报告 → Git。

## 5. 验收标准

1. 单/双路贡献、重复去重、tie、输入不变性和旧等权 parity 均有测试。
2. 网格与选优规则在查看 train 结果前提交。
3. 配对 Gate 拒绝子集、错权重、错快照或任一路降级报告。
4. 生产数据、holdout 与 Kimi 均不被候选实验触碰。
