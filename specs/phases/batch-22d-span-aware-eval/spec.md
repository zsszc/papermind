# Batch 22D 规格：跨块证据 Benchmark v2

## 1. 背景

Batch 22C 证明 512 字符硬切能消除 Embedding 前 512 词截断风险，但现有 evidence resolver
要求 quote 必须完整且唯一地出现在单个 chunk。带 overlap 或更细的分块天然会产生两类合法
情况：同一 quote 出现在多个重叠块，或 quote 横跨相邻块。旧 resolver 会分别报“多处命中”
或“未命中”，因此不能公平比较不同 chunk 粒度。

## 2. 范围与版本边界

- 新建 Benchmark v2 证据 span 解析契约，不修改 v1 数据文件或历史报告。
- 为新候选 chunk 增加页内稳定字符坐标；quote 先在原始解析页中唯一定位，再映射到覆盖该
  span 的一个或多个 chunk ID。禁止用具体 QA 文本写规则。
- 旧 464-chunk 基线与新候选必须在同一个 v2 resolver/指标定义下重跑 train，禁止把 v1
  单块 qrel 指标与 v2 span 指标直接比较。
- Recall 同时报告 `any-hit`（至少命中一个证据块）与 `span-coverage`（命中证据块比例）；
  晋级主 Gate 预先冻结，不能在看到结果后切换指标。
- 只运行 private train；train 晋级后才允许一次 dev。不得运行 holdout 或 Kimi。

## 3. Benchmark v2 冻结契约

### 3.1 Chunk 页内坐标

- `chunks.page_start` / `chunks.page_end` 为可空整数，采用相对 `PDFParser.extract_text()`
  返回页文本的 Python 字符下标半开区间 `[page_start, page_end)`。
- 正文 chunk 必须满足 `0 <= page_start < page_end <= len(page_text)`；摘要 sentinel
  (`chunk_index=-1`) 不参与 span qrel，坐标保持 `NULL`。
- v1 历史库不会自动假装拥有坐标。v2 resolver 遇到目标页正文坐标缺失、越界或无法完整
  覆盖证据 span 时必须 fail-close。

### 3.2 Evidence resolver v2

1. 先按稳定 `paper_uid` 唯一解析论文。
2. 在该论文每一页的原始解析文本中逐字查找 quote；整篇必须且只能命中一次。
3. quote 若只能通过拼接相邻页才能命中，视为跨页证据并拒绝。
4. 将唯一 `[quote_start, quote_end)` 映射到同页所有相交正文 chunk；相交区间并集必须
   完整覆盖 quote span。
5. 每条 evidence 保留独立 chunk ID 组；报告不得写入 quote、问题或论文正文。

### 3.3 指标与主 Gate

对每条正例 QA 的 evidence 组 `G={g1...gn}`、检索前 k 个 ID 集合 `Rk`：

- `any_hit@k = mean(1[Rk ∩ gi != ∅] for gi in G)`；
- `span_coverage@k = |Rk ∩ union(G)| / |union(G)|`；
- MRR/NDCG 继续对 `union(G)` 计算，仅用于排序诊断。

报告 overall 为所有正例 QA 单题分数的宏平均。Batch 22D 的冻结晋级条件为：候选 train
`any_hit@5` 不低于同次旧基线，`span_coverage@5`、MRR、NDCG 全部报告但不允许在看到
结果后替换主 Gate。解析失败、运行时降级、指纹不一致均直接拒绝候选。

## 4. 安全与数据

- 所有 schema/offset 重建仍在复制 SQLite 与 stage Chroma 上执行，生产库只读。
- PDF 原文、QA、quote、逐题报告只留 `eval/private`；Git 仅提交聚合数字与原创合成 Harness。
- v1 resolver 保持兼容；报告必须记录 benchmark/resolver 版本，版本不同的 comparison key 不同。

## 5. 验收标准

1. 合成测试覆盖单块、跨两块、overlap 重复、跨页拒绝、quote 多处原文命中 fail-close。
2. chunk 坐标连续、页内有效，旧数据库空坐标不被误当 v2 证据。
3. 同一 v2 train 下同时产生旧基线与候选报告，指纹、指标和 Gate 可复现。
4. train 未晋级则不运行 dev；全量 Harness、报告、提交与 push 完整。
