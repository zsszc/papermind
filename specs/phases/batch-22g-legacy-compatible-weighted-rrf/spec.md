# Batch 22G 规格：Legacy-Compatible Weighted-RRF

## 1. 背景与单一变量

Batch 22F 的新去重/tie 契约使等权候选仅 11/24 与生产 top-5 完全一致，无法把后续差异归因于
权重。本批只改变词法分支权重，严格保留旧 `rrf_fuse_chunks()` 的其他全部语义。

## 2. 冻结公式与兼容契约

- 公式：`S(c)=ws/(60+r_sem)+wl/(60+r_lex)`，`ws=1.0`，`wl` 网格仍为
  `1.0/1.25/1.5/2.0`。
- rank 仍按原始输入位置 1-based；同一路重复仍按旧逻辑重复贡献并占 rank。
- metadata 仍取首次见到的结果；总分 tie 仍按 `first_seen_order`、再按 chunk ID。
- 保留旧 source-as-chunk-id 兼容 fallback；不在本批修正历史异常域行为。
- 旧函数继续不改；新增显式 `weighted-rrf-compat-v1` 纯函数/profile。
- `1.0/1.0` 必须对任意旧函数可接受输入实现返回值深度相等，并在真实 train 达到 24/24
  retrieved IDs parity，否则立即停止。

## 3. 评测协议

- 复用 Batch 22F 的同一 464-chunk/1024 维隔离快照、向量内容指纹与完整样本审计。
- 只有兼容等权通过前置 Gate，才顺序运行 `1.25/1.5/2.0` 三个 train 候选。
- Train Gate 保持：总体 Recall 与 any-hit 不回退；factoid Recall 至少 +1/6；MRR/NDCG 回退
  不超过 0.02；P95 < 1 秒；零降级。
- 合格者按 factoid Recall、总体 Recall、MRR、NDCG、较小词法权重词典序选优。
- 仅一个 train 胜者允许运行一次 dev；dev 四项不回退且至少一项严格提升才可提出生产激活。
- holdout 封存，不调用 Kimi，不按 QA/论文/证据内容调参。

## 4. 验收标准

1. 全域等权 parity、权重公式、历史重复/tie/source fallback 和 copy 隔离均有测试。
2. CLI/report/profile 与 Batch 22F 严格区分，失败候选不能伪装成生产 hybrid。
3. 自动 Gate 锁定完整网格、公共快照与两侧零降级。
4. 未通过 dev 前不修改生产配置。

## 5. Harness 勘误：确定性 HNSW 副本

实现后发现同一生产 hybrid 在未改代码的两个独立进程间仅 21/24 top-5 顺序相同；固定
PyTorch/BLAS 线程和 CPU device 后仍可复现。定位到 464-vector HNSW 快照的默认
`search_ef=10` 会在近邻边界产生跨进程候选抖动，令 parity 与权重归因失效。

- 原快照保持只读；由显式 CLI 原子复制出评测副本。
- 副本冻结 `hnsw:num_threads=1`、`hnsw:search_ef=vector_count(464)`，向量内容指纹必须不变。
- 报告记录 HNSW 配置指纹，compat Gate 同时校验两侧配置与向量指纹。
- 先做 production/production 24/24 重复性 Gate，再重新做 production/compat 等权 Gate。
- 该副本只服务离线可复现实验，不修改生产 `vector_db/` 或聊天配置。
