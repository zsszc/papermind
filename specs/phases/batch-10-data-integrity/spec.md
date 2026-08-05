# Batch 10：文献处理与数据库一致性规格说明书

## 1. 背景与目标

文献、引用边、会话记忆、SQLite 与 ChromaDB 共同组成 PaperMind 的本地知识库。当前外键约束未启用，删除文献可能留下引用边；PDF 缺失会被误判为处理完成；手动重处理绕过并发锁；向量变化后语义缓存仍可返回旧结果。本批次收紧这些一致性契约。

## 2. 范围

### 2.1 包含

- 每个 SQLite 连接启用外键约束。
- 删除文献时清理入边和出边，删除会话时保留记忆但解除来源关联。
- PDF 源文件缺失以异常表达，处理状态必须为 `error`。
- 手动重处理与后台处理共享同一篇文献的互斥锁。
- ChromaDB 增删后清除语义检索缓存。

### 2.2 非目标

- 不重建 SQLite 旧表以加入 `ON DELETE` 规则。
- 不在本批次实现 SQLite 与 ChromaDB 的跨存储事务。
- 不改变处理接口的成功响应结构。

## 3. 行为契约

### 3.1 SQLite 连接

连接建立后 `PRAGMA foreign_keys` 必须为 `1`，同时保留 WAL 与 `synchronous=NORMAL`。

### 3.2 删除关联数据

- 删除 Paper 前清理 `paper_citations` 中 `citing_id` 或 `cited_id` 命中的全部边。
- 删除 Conversation 前把 `memory_summaries.source_conversation_id` 命中项置空，记忆内容保留。
- 删除操作在外键开启时仍应成功提交。

### 3.3 文献处理状态

- `PaperProcessor.process()` 找不到源 PDF 时抛 `FileNotFoundError`，不返回伪成功结果。
- 手动处理端点开始时置 `processing`；处理器抛异常或返回非 `ok` 结果时置 `error` 并返回脱敏 500；仅 `status=ok` 时置 `done`。
- 同一 paper 已有后台或手动任务持锁时，手动端点返回 409，不启动第二次处理。

### 3.4 语义缓存失效

- 向量块成功写入后，清除全部 `semantic_search:` 前缀缓存。
- 删除某 paper 向量后，无论 ChromaDB 删除是否抛错，都清除语义缓存，避免数据库已删但缓存仍返回旧文献。
- 其他用途的缓存键保持不变。

## 4. 边界条件与错误处理

| 场景 | 期望行为 |
|------|----------|
| 删除被其他文献引用的论文 | 相关入边、出边均删除，其余边保留 |
| 删除作为记忆来源的会话 | 会话删除，记忆保留且来源变为 null |
| PDF 文件不存在 | 处理状态 error，不产生 done 假状态 |
| 同篇论文正在处理 | 第二个手动请求 409 |
| ChromaDB 删除失败 | 记录 warning，仍清语义缓存 |

## 5. 依赖

- SQLAlchemy、SQLite、`PaperProcessor`、`VectorStore`、`SimpleCache`。
- 下游：文献删除/重处理 API、对话删除 API、混合检索与 RAG。

## 6. 验收标准（可测试）

- [x] AC1：生产 PRAGMA 初始化函数把外键状态设为 1。
- [x] AC2：开启外键后删除文献会清理全部相关引用边。
- [x] AC3：开启外键后删除会话会保留并解除关联记忆。
- [x] AC4：PDF 缺失与处理器错误结果均不会被标为 done。
- [x] AC5：手动处理遵守同 paper 锁，冲突返回 409。
- [x] AC6：向量增删只失效语义检索缓存，不清除其他缓存。

## 7. 现有测试覆盖与盲区

- 已覆盖：chunk 重建、摘要 chunk、论文删除时 thesis citation 清理、处理异常响应脱敏。
- 盲区：SQLite 外键状态、paper citation 删除、conversation memory 关联、缺文件假完成、手动处理锁和缓存失效。

## 8. 关键设计决策

- 旧库不做高风险表重建，路由显式清理关联记录。
- 处理器用异常表达失败，避免调用方遗漏检查；路由仍防御性检查返回状态。
- 语义缓存采用前缀失效，避免误清未来的其他缓存类别。
