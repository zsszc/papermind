# Batch 22L 规格：真实语料 Benchmark v2 数据建设与盲测基线

> 前情：Batch 22J 建成覆盖审计/冻结工具但 readiness 1/12 未达标，按预案冻结。
> 2026-08-31：用户新增 33 篇论文并全部入库，v2 语料清单重建，readiness 34/12 达标。
> **用户已于 2026-08-31 明确授权真实内容出站**（覆盖本批 QA 生成与 Batch 23B smoke）。

## 1. 目标

在 22J 冻结工具之上完成数据面：论文级 split 预冻结 → LLM 辅助 QA 生成（带证据句）→
人工审稿 → 数据集冻结 → 生产 hybrid 盲测基线（train/dev；holdout 保持封存）。

## 2. 硬约束（继承 22J spec，逐条有效）

- v2 论文与 v1 train/dev/holdout 零重叠（UID+SHA 双重去重，readiness 已证）
- split 先 QA 冻结，同论文不跨 split；每 split ≥ 4 篇
- 每条正例 evidence quote 在指定原页 **100% 唯一解析**（歧义/空证据/跨 split 立即失败）
- 冻结制品排他创建（O_EXCL）+ 0600，写私有 gitignore 目录
- holdout 不经通用 CLI 打开，仅预注册 Gate 消费（claim 文件先行，崩溃也视为已消费）
- 基线只测不调：本批不因盲测结果改任何检索算法

## 3. 任务契约

### T1：split 分配与预冻结

- 输入：readiness 审计的 `eligible_documents`（34 篇）
- 分配策略：按 paper_uid 排序后种子轮换（seed 固定进制品），train 12 / dev 11 / holdout 11
- 调 `freeze_paper_splits` 排他写入 `eval/private/benchmark_v2_splits.json`

### T2：QA 生成器（带证据，新工具 `eval/generate_qa_v2.py`）

- 逐 split 论文：pdfplumber 提取页文本 → LLM 生成 3 条 QA（类型轮换：factoid/method_detail/summary），每条必须含 evidence quote（原文逐字子串）与 evidence page
- 校验器：quote 在指定页**唯一**出现（≥2 次或 0 次即拒），跨页不串；paper 归属与 split 一致
- 输出 `eval/private/qa_v2_candidates.jsonl`（排他创建）；失败条目跳过并计数，不阻塞
- LLM 走 `llm_service`（唯一入口；Langfuse 自动观测）；断点续跑（--resume 同 generate_qa）

### T3：人工审稿（用户）

- 候选清单交用户审；审后标记 `reviewed: true` 的子集进入冻结
- 本批硬门：审稿后每 split ≥ 12 条 QA（不足则补生成一轮）

### T4：数据集冻结 + 盲测基线

- 冻结 dataset/qrels/corpus/database/page/vector 指纹与一次性 ledger（复用 `build_v2_freeze_artifact`）
- 生产 hybrid 跑 train+dev（**不动 holdout**），记录 Recall@5/MRR/NDCG/factoid 分型基线
- 报告与台账归档；holdout claim 文件不存在（未消费）

## 4. 验收标准

- [ ] AC1：split 冻结制品排他存在、三分区计数 12/11/11、与 v1 零重叠
- [ ] AC2：QA 候选每条 evidence 唯一解析（生成器自校验 + 独立复验脚本双证）
- [ ] AC3：用户审稿完成且每 split ≥ 12 条
- [ ] AC4：冻结制品指纹完整；基线报告入档（train/dev 四项指标）
- [ ] AC5：holdout 零消费（无 claim 文件）；全套件回归绿

## 5. 非目标

- 不根据盲测结果调检索算法（后续批次的事）；不消费 holdout；不改 v1 任何冻结制品

## 6. 风险与回退

- LLM 生成质量参差 → T3 人工审稿是质量门；候选可整轮弃用重生成（制品未冻结前）
- 证据唯一解析率低 → 生成器 prompt 要求「短而独特的原文子串」；失败条目跳过不阻塞
- 回退：私有制品目录整体可删，不影响 v1 与生产库
