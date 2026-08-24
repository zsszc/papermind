# Batch 22B 规格：检索消费者收敛与 chunk 质量审计

## 1. 背景

Batch 20 已让聊天、重新生成与 eval 共用 `RetrievalPipeline`，但全仓只读 inventory 发现
两个 chunk RAG 消费者仍直接调用 VectorStore：深度综述子问题与论文引用推荐。它们仍是
旧纯语义 top-5，无法获得生产 shared hybrid 与同范围 BM25 降级。

同时发现两个安全/逻辑问题：论文引用推荐在零本地证据时仍要求 LLM 推荐 3–5 篇，可能产生
无依据建议；显式 paper-scoped 对话若开启 graph expansion，会被扩展到其他论文。

## 2. 范围与契约

### S1：仅迁移真正的 chunk RAG 旁路

- `deep_review.execute` 显式接收 DB Session，经 `RetrievalPipeline` 使用生产
  `chat_profile/lexical_profile`、top_k=5、filters={}；路由必须传入当前请求 DB。
- `thesis.suggest_citations` 使用现有路由 DB 与相同生产 profile，经共享管线返回引用候选。
- 两处都保留原有 prompt、引用字段、顺序与异常脱敏；semantic 不可用时允许同范围关键词
  降级，只有最终零 chunk 才跳过或拒绝生成。
- `/api/search` 保持论文级标题/作者/摘要发现契约，不迁入 chunk 管线；MCP 论文元数据工具、
  processor/index cleanup 也不是 RAG 旁路，不迁移。

### S2：零证据与限制性范围

- 论文引用推荐最终零 chunk 时不得调用 LLM，返回 `citations=[]` 与明确“未找到本地证据”提示。
- 深度综述最终零 chunk 时继续返回 `INSUFFICIENT_NOTICE`，不得调用 LLM。
- graph expansion 开启但 state 带 `paper_id` 时必须直接跳过扩展，绝不加入其他论文。
- 搜索页语义 `available()` 或 `search()` 异常时，应保留可用关键词结果并返回 200；不得让
  语义适配器异常拖垮论文级搜索。

### S3：chunk/section 只读质量基线

- 只统计数量、比例、长度分位和匿名 qrel 聚合，不输出论文/QA/证据原文。
- 本批不修改真实 SQLite、PDF、Chroma 或 chunk；不运行 dev/holdout/LLM。
- 审计结果用于冻结下一批隔离式 chunk 重建，不在本批混入排序变量。

## 3. 验收标准

1. RED/GREEN 覆盖两处消费者的 profile、lexical、top_k、filters、顺序和关键词降级。
2. Thesis 零证据、Deep Review 零证据均证明 LLM 调用次数为 0。
3. paper scope + graph 开启时结果中只有指定论文；搜索语义异常仍保留关键词响应。
4. inventory 文档明确哪些 VectorStore 调用合法保留；chunk 审计只含聚合数据。
5. 后端、前端、Electron、公开评测、健康检查与依赖 Harness 全绿，分批提交并 push。

## 4. 明确延期

- chat/regenerate 的可选 graph 后处理 parity 与 regenerate paper scope 持久化另开批次；当前
  graph 默认关闭，且不能从旧 citations 安全推断原请求范围。
- 超长段落硬切、section_title 识别、stage Chroma 重建是下一独立 train-first 批次；不得
  原地修改 464-chunk 生产快照。
