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

### 4.1 Parent 映射

- `--parent-database` 必须显式指向只读旧粗分块候选；基线和 child 候选使用同一 parent 库与
  parent manifest。
- 正文 child 只与同论文、同页、坐标有效的 parent 比较，选择字符交集最大的 parent；零交集
  fail-close，并列按数值 `parent.chunk_index` 升序。
- 摘要 sentinel `c-1` 映射到同论文 parent `c-1`；parent 坐标/身份重复或缺失均 fail-close。
- 2026-08-25 结果前聚合审计：2,904/2,904 child 可映射，零并列、零未命中；3 个 child
  跨两个 parent，最大交集归属比例最小 0.816、均值 0.9995。该审计未读取 QA/quote。

### 4.2 检索与聚合

1. child 语义与 BM25 各取 top-40，使用 `k=60` 的 RRF 生成 child 基础分。
2. 每个 parent 按 child 基础分降序，仅取前三个不同 child，parent 分数固定为
   `s1 + 0.5*s2 + 0.25*s3`；多余 child 不加分，避免 parent 因 child 数量获益。
3. parent 按 `(-parent_score, parent_chunk_id)` 稳定排序；parent 内 child 按
   `(-child_score, child_chunk_id)` 稳定排序。
4. 最终结果按 parent round-robin：第一轮每个 parent 最多取一个 child，后续轮次再取各 parent
   的第二、第三个 child，直到返回 5 个可引用 child ID。
5. 相同 child 只计一次；任一初召回 child 缺 parent 映射时整个 profile fail-close，不回退成
   看似有效的普通 hybrid。

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
