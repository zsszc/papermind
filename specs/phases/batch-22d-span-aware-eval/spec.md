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

## 3. 安全与数据

- 所有 schema/offset 重建仍在复制 SQLite 与 stage Chroma 上执行，生产库只读。
- PDF 原文、QA、quote、逐题报告只留 `eval/private`；Git 仅提交聚合数字与原创合成 Harness。
- v1 resolver 保持兼容；报告必须记录 benchmark/resolver 版本，版本不同的 comparison key 不同。

## 4. 验收标准

1. 合成测试覆盖单块、跨两块、overlap 重复、跨页拒绝、quote 多处原文命中 fail-close。
2. chunk 坐标连续、页内有效，旧数据库空坐标不被误当 v2 证据。
3. 同一 v2 train 下同时产生旧基线与候选报告，指纹、指标和 Gate 可复现。
4. train 未晋级则不运行 dev；全量 Harness、报告、提交与 push 完整。

