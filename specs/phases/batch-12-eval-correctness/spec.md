# Batch 12 规格：RAG 评测正确性与可追溯基线

## 1. 背景

现有评测可以输出 Recall@k、MRR、NDCG、延迟与生成指标，但存在四类会误导结论的问题：摘要 chunk `c-1` 无法解析；裸 chunk id 会被误判为正式引用；NDCG 会重复计算相同结果；`citation_coverage` 实际只表达 recall。报告还缺少数据集与语料指纹，语义检索运行期降级也不能被完整追踪。

## 2. 行为规格

### S1：引用解析

- 只解析方括号中的引用，如 `[p1_c2]`、`[p1_c-1]`。
- 支持摘要 chunk 的负索引 `-1`。
- 裸文本 `p1_c2` 不算引用。
- 重复引用去重并保持首次出现顺序。

### S2：检索指标

- NDCG 对重复 retrieved id 只计算首次出现，结果始终位于 `[0, 1]`。
- citation 拆分为 precision、recall、F1；空集合边界返回 `0.0`。
- 旧 `citation_coverage` 保持兼容，其定义明确为 citation recall。

### S3：评测诊断

- 正例相关集合解析为空时，评测在检索前失败，不得把标注失效混成 Recall=0。
- 每条记录实际使用的检索模式与是否降级；报告汇总运行期降级次数。
- 模型/网络基础设施错误不得伪装成正常 hybrid 质量结果。

### S4：报告可追溯性

- 报告 schema 升级为 v2，至少记录 Git SHA、Python 版本、数据集 SHA256、语料清单 SHA256、论文数、chunk 数、top_k、检索模式和 gate 结果。
- 不在报告中写入论文正文、API Key 或个人配置。
- 只有数据集、语料、pipeline、top_k 的比较键一致，趋势才可视为可比。

### S5：质量实验边界

- Batch 12 可以运行检索改进实验，但不能使用 ground truth 扩写查询。
- 现有动态 qrels 上的变化必须标记为“观察实验”；Batch 13 稳定 qrels 前不得据此修改生产默认策略。

## 3. 验收标准

1. 新增测试覆盖 S1–S4，并亲眼确认 RED 后转为 GREEN。
2. 后端全量 pytest 通过。
3. 离线 `--keyword-only` 能生成 v2 JSON 报告，且无 unresolved qrels。
4. 记录历史 hybrid、改动前 keyword-only、改动后 keyword-only 的指标与限制。
5. 生成中文测试报告，并更新开发计划台账。
