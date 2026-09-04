# Batch 28 规格：v2 train 证据 Route-Depth 归因 Harness

## 1. 背景与问题

Batch 27B 证明全局论文先验会挤掉 factoid 所需论文，不能解决主导的同论文证据定位失败。
在启动下一个排序或分块候选前，必须先回答：相关证据 chunk 是否已经出现在 production
semantic / `bm25-bilingual` 的 top-20 深层候选中，以及它位于 1–5、6–10、11–20 还是完全缺失。

## 2. 目标与冻结口径

- 仅处理 Benchmark v2 完整 train：13 个正例，factoid/method_detail/summary=`8/4/1`。
- semantic 请求并要求完整 top-20；`bm25-bilingual` 请求 top-20，但因零分结果按生产逻辑
  丢弃，允许自然返回 0–20。生产基线仍按两路前 10 做 legacy RRF top-5，不得用诊断深池
  改变生产基线。
- 对两路及并集统计 evidence first-hit 深度桶、any-hit@5/10/20、span coverage@5/10/20。
- 将每题互斥归为：`baseline_full`、`deep_route_recoverable`、
  `correct_paper_only`、`paper_absent`。
- 输出仅含计数、比例、均值、冻结指纹与候选枚举，不含 qa_id、chunk_id、问题、正文、
  标题、路径、DOI、paper UID 或逐题记录。

## 3. 下一候选冻结映射

在失败题中按计数选择主导类别，同数按以下优先级：

1. `deep_route_recoverable` → `paper-preserving-deep-route-v1`；
2. `correct_paper_only` → `within-paper-query-rerank-v1`；
3. `paper_absent` → `query-document-expansion-v1`。

本批只做归因和选型，不实现候选、不调参数、不运行 dev/holdout。

## 4. 运行与安全契约

- CLI 必须显式指定私有 dataset、只读 SQLite、语料根、向量快照与私有输出。
- tracked Git 必须 clean；split/page resolver/top-k/route limit/lexical profile 固定为
  train/page-span-v2/5/20/bm25-bilingual。
- Chroma 必须从传入冻结源复制到临时目录后打开，禁止改写源快照。
- 必须设置 HuggingFace/Transformers 离线环境；禁止 LLM、网络、子进程生成调用。
- dataset/qrels/corpus/database/page/vector/HNSW 指纹全部写入 binding；异常 fail closed。
- 输出使用 0600 排他创建，拒绝 symlink 与私有目录逃逸。

## 5. 验收标准

- [x] AC1：纯聚合对深度桶、span、互斥类别和候选映射计算正确且确定性。
- [x] AC2：畸形/重复 ID、非完整 train、dirty、越界指标和不一致基线 fail closed。
- [x] AC3：CLI 只读、临时复制向量、离线、无身份/正文输出。
- [x] AC4：在 clean 提交完成一次真实 train 聚合，冻结唯一下一候选。
- [x] AC5：全量/公开 Gate、测试报告、台账、分段提交与 push 完成。

## 6. 非目标

- 不读取或评分 dev/holdout，不调用 Kimi。
- 不修改生产 RetrievalPipeline 默认、SQLite、Chroma 或论文文件。
- 不根据逐题身份手工调参，不同时实现多个候选。
