# Batch 22E 测试报告：Parent-Child 聚合检索

## 1. 结论

Batch 22E 完成了 parent 映射、双路 child 初召回、parent 有界聚合、round-robin 返回、隔离快照
审计和严格配对 Gate。真实论文 train 结果明确拒绝 512/50 child 候选：其字符证据覆盖率、
any-hit、factoid、MRR 与 NDCG 均显著低于同 profile 的旧粗粒度基线。

候选未进入 dev、holdout 或 Kimi 生成评测，也未换入生产 SQLite/Chroma。当前算法只利用 parent
作为分组/排序信号，并未向 LLM 注入 parent 正文，不能描述为已经完成上下文扩展。

## 2. SDD/TDD 与审查痕迹

- 先冻结映射与公式，再提交映射/聚合 RED，随后实现 `parent-child-v1` GREEN。
- 正文 child 只映射到同论文同页最大字符交集 parent；摘要 `c-1` 对应同论文摘要。
- 两路各 top-40，RRF `k=60`；每个 parent 最多前三 child 以 `1/0.5/0.25` 计分。
- 同一路重复 child 先去重再计算 rank；child/parent 均显式稳定排序。
- semantic、keyword、映射或聚合任一故障均 fail-close 为空结果，不伪装成单路 hybrid。
- 多 Agent 只读审查补充了非法 ID、重复 rank、论文稳定身份、parent 内容指纹、完整 train 与
  baseline 降级检查；未读取 holdout 或调用外部模型。

## 3. 隔离快照审计

| 项目 | 旧粗粒度基线 | 512/50 child 候选 |
|---|---:|---:|
| SQLite chunks | 464 | 2,904 |
| Chroma vectors | 464 | 2,904 |
| SQLite/Chroma ID 差异 | 0 | 0 |
| Embedding 维度 | 1,024 | 1,024 |
| child→parent 映射 | 464/464 | 2,904/2,904 |
| 映射未命中/并列 | 0/0 | 0/0 |

两组报告的 parent manifest 与冻结算法 contract SHA 完全相同；child corpus、向量与 mapping
manifest 按粒度分别记录，不错误要求两组相等。Parent manifest 使用稳定论文身份、坐标和正文
SHA，不依赖 SQLite 行主键，也不在报告中泄露正文。

## 4. 真实 train 配对结果

固定配置：24 条真实 train QA、page-span-v2、`bm25-bilingual`、top-5、无 reranker、零运行时降级。

| 指标 | 旧粗粒度基线 | 512/50 child 候选 | 差值 |
|---|---:|---:|---:|
| span_coverage@5 | 0.667 | 0.324 | -0.343 |
| any_hit@5 | 0.667 | 0.375 | -0.292 |
| factoid span coverage | 0.333 | 0.167 | -0.167 |
| MRR | 0.394 | 0.222 | -0.172 |
| NDCG@5 | 0.463 | 0.185 | -0.278 |
| P95 | 311.9 ms | 373.0 ms | +61.0 ms |

冻结 Gate 要求 coverage 至少提升 1/24、any-hit/factoid 不回退、MRR/NDCG 回退不超过 0.02、
P95 < 1 秒且 baseline/candidate 均零降级。候选只通过延迟与运行稳定性，五个质量检查均失败，
因此自动跳过 dev。

## 5. 全量 Harness

- 后端：`697 passed, 1297 warnings`，13.02 秒。
- 公开冻结 BM25：Recall@5/MRR/NDCG@5 = `0.900/0.783/0.813`，0.85 Gate 通过。
- 前端：12 个文件、39 项测试通过；lint 零警告，生产 build 通过。
- Electron：26 项测试通过。
- `pip check`：无依赖冲突。
- 隔离数据目录启动成功，`GET /api/health` 返回 200、`status=ok`；公开模板未配置 Key，故
  `llm_ready=false`，本批未发送真实论文内容。
- 生产库只读复核：19 篇论文、464 chunks、`quick_check=ok`；历史 4 条 `paper_tags` 孤儿仍在，
  本批未修改生产数据。

## 6. 失败原因与后续决策

快照审计排除了 ID、维度和映射错配。结果显示细粒度 child 的初召回排序本身较弱，而按 parent
分组后每个 parent 最多返回三个 child 又进一步牺牲了证据字符覆盖。继续调 parent 权重容易针对
train 过拟合，且仍无法解决“返回 child 不是证据所在 child”的结构矛盾，因此停止该方向。

Batch 22F 回到已验证较强的 464 粗粒度语料，仅做有限、预冻结的语义/词法加权 RRF 校准，优先
改善 factoid 弱项；使用 train 选择、一次 dev 复核、holdout 封存的协议。
