# Batch 26 规格：Benchmark v2 train 失败归因 Harness

## 1. 背景与目标

Batch 22L 的既有报告记录 v2 train `span_coverage@5=0.452`，明显低于 dev 的 0.750，
但现有报告只给出总体/分型指标，无法判断主要瓶颈是跨论文召回、论文内定位、证据跨块，
还是运行时空结果。本批先建立纯本地、只读、去标识化的失败归因 Harness，再据主导失败
预注册下一候选；不直接改生产检索算法。

## 2. 输入与隐私边界

- 只读取 `eval.run` 已生成的完整 `train` 报告，不读取 QA 数据集、问题正文、PDF 或 chunk 正文。
- 输入必须是 `split=train`、`top_k=5`、`page-span-v2`、`with_llm=false`、零运行时降级。
- CLI 只接受 `backend/eval/private/` 下的普通文件，显式拒绝 dev/holdout、symlink 与目录逃逸。
- 输出只含计数、比例、指标、枚举归因和 SHA-256；禁止输出 qa_id、chunk_id、标题、DOI、
  路径、问题、答案或正文。
- 本批不调用 Kimi/Embedding，不消费 holdout，不改私有制品。

## 3. 失败分类

每条 train 正例恰好进入一个类别：

1. `full_coverage`：`span_coverage == 1`；
2. `partial_coverage`：`0 < span_coverage < 1`；
3. `same_paper_miss`：覆盖为 0，但 top-5 至少命中证据所属论文的其他 chunk；
4. `cross_paper_miss`：覆盖为 0，且 top-5 未命中任何证据所属论文；
5. `empty_retrieval`：top-5 为空，优先于其他类别。

分类必须总数守恒；按 `question_type` 给出相同聚合，并按固定优先级稳定处理并列。

## 4. 候选预注册

根据失败项中的主导类别生成且只生成一个下一候选方向：

- `cross_paper_miss` → `query-document-expansion-v1`；
- `same_paper_miss` → `paper-first-evidence-rerank-v1`；
- `partial_coverage` → `boundary-aware-evidence-v1`；
- `empty_retrieval` → `runtime-integrity-audit-v1`。

预注册 Gate 固定为：完整 train、span coverage 至少增加 `1/n`、Recall/MRR/NDCG 与各问题
类型均不回退、P95 < 1s、零降级；train Gate 失败禁止运行 dev，holdout 始终禁止。

## 5. 验收标准

- [ ] AC1：坏 schema、dev/holdout、降级、重复/畸形 ID、指标越界均 fail closed。
- [ ] AC2：五类归因互斥且总数守恒，输出双跑字节级一致。
- [ ] AC3：输出严格白名单，不含逐题标识和内容；文件权限 0600、排他创建。
- [ ] AC4：专项、后端全量与公开检索/生成 Gate 全绿。
- [ ] AC5：报告、台账、分段提交与 push 完成；未调用外部服务。

## 6. 非目标

- 不读取或运行 dev/holdout；不根据 dev 调参。
- 不在本批实现候选检索算法或改变生产默认值。
- 不把聚合归因当成模型质量提升；它只决定下一次单变量实验方向。
