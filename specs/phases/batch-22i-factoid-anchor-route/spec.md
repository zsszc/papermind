# Batch 22I 规格：Factoid 稀有实体/数值锚点路由

## 1. 目标与假设

Batch 21–22H 证明邻域传播、词表扩展、细粒度分块、Parent-Child、RRF 权重和 HNSW 搜索深度
都没有改善真实 dev factoid。下一变量回到尚未被单独验证的 factoid 信号：问题中的方法缩写、
带数字实体、百分比和单位通常比通用中英文词更稀有，适合作为第三条精确锚点路由。

目标是在不改变 embedding、chunk、生产两路 hybrid 或 BM25 词表的前提下，仅为含合法锚点的
查询增加一条可审计候选路由；所有选型先在 train 完成。

## 2. 冻结算法

- 新 profile：`hybrid-anchor-v1`，只能通过显式评测参数启用。
- 锚点仅来自查询本身，不调用 LLM、不访问网络：含数字的技术 token、百分比、单位组合、
  2–12 字符的大写/混合大小写方法缩写；去重并保留出现顺序。
- 无锚点查询必须与生产 `hybrid` top-5 深度相等。
- 有锚点时保持现有 semantic 与 `bm25-bilingual` 两路结果不变，新增只使用锚点的 BM25 route；
  三路沿用生产 legacy RRF 的 `k=60`、rank 与 tie 语义，权重均为 1，不在本批调权。
- 候选仅在显式 profile 生效，不修改聊天生产默认配置。

## 3. 评测与 Gate

1. RED 先覆盖锚点语法、无锚点 parity、第三路重复/tie 语义、过滤和降级透传、CLI 隔离。
2. 使用真实论文 train 24 条，与同次生产 hybrid 配对；禁止先查看新 dev 结果。
3. 晋级要求：factoid Recall 至少提升 `1/6`；总体 Recall@5、MRR、NDCG@5 均不回退；
   任一问题类型 Recall 不回退；零运行时降级；P95 < 1 秒。
4. train 通过才运行一次 dev；dev 四项不回退且 factoid 或总体至少一项严格提升才可晋级。
5. holdout 封存，不运行 Kimi 生成，不发送真实问题或证据到外部服务。

## 4. 验收标准

- 算法、profile、报告/选择器与 SDD/TDD 轨迹齐全。
- 报告绑定 tracked-clean Git、数据库/语料/页文本、向量/HNSW 与算法公式指纹。
- 失败候选自动停止且不能改变生产默认；全量 834+/39/26 Harness 与公开基准无回退。
