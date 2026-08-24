# Batch 22E 规格：Parent-Child 检索候选

## 1. 背景

Batch 22D 证明纯 512/50 child 分块虽然解决了单块超长问题，却使真实 train 的字符证据覆盖率
从 0.667 降至 0.453。主要风险是细块上下文不足、候选数量膨胀及 RRF 排名稀释。生产粗粒度
464-chunk 快照继续作为控制组，不激活失败候选。

## 2. 假设

使用 512/50 child 做语义/词法初召回，再把 child 分数聚合到连续 parent span，并在 parent 内
选择最相关 child，可同时保留细粒度向量输入和粗粒度上下文信号。

## 3. 范围

- 所有 parent 映射只在候选 SQLite/Chroma 或评测内存结构中生成，生产数据只读。
- 基线与候选使用同一 `parent-child-v1` retrieval profile；旧粗块的 parent 为自身，保证配对
  配置一致。
- 冻结一次 parent 聚合公式与初召回深度后再看 train，不针对具体 QA/quote 写规则。
- 继续使用 page-span-v2 字符覆盖 Gate；train 未通过不得看 dev，禁止 holdout/Kimi。
- 私有逐题结果留在 `eval/private`，Git 只记录聚合。

## 4. 预冻结算法

1. child 语义与 BM25 各取 top-40，使用 RRF 生成 child 基础分。
2. 按同页连续 parent 坐标聚合；parent 分数取最高 child 分，加不超过两个不同 child 的折扣
   补充分，避免单一 parent 因 child 数多获益。
3. 按 parent 分数选取候选，再从每个 parent 依基础分取 child，最终仍返回 5 个可引用 child ID。
4. 相同 child、parent 或 overlap 区间必须确定性去重；排序并列按稳定 chunk ID。

## 5. Gate

- 与 Batch 22D 相同：train `span_coverage@5` 至少提升 1/24；any-hit 与 factoid 不回退；
  MRR/NDCG 回退不超过 0.02；P95 < 1 秒；零运行时降级。
- 解析、页文本指纹、模型维度、向量 ID、配对配置任一不一致均 fail-close。
- 通过后只允许运行一次 dev；仍不查看 holdout，不调用 Kimi。

## 6. 验收标准

1. 合成测试覆盖 parent 聚合、child 数量偏置、跨 parent、多样性和稳定排序。
2. 旧/新候选均能生成确定性 parent 映射，生产 manifest 不变。
3. 自动配对 Gate 输出可复现，失败候选不得进入 dev。
4. 全量 Harness、测试报告、提交和 push 完整。
