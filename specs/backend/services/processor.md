# services/processor.py（论文入库处理流水线 PaperProcessor）规格说明书

> 本文件描述 `backend/app/services/processor.py` 的**行为契约**（做什么），不描述实现细节。
> 由于「处理状态机、重试、并发锁」等语义实现在调用方 `routers/papers.py` 的后台任务中，本规格将其一并纳入（第 3.3–3.5 节），以构成完整的行为契约。
> 依据源码实际内容反向工程整理（2026-08-04）。

## 1. 背景与目标

论文 PDF 上传后，需要把「文件」变成「可检索的知识」：提取全文文本 → 分块 → 向量化 → 写入 SQLite（`chunks` 表元数据）与 ChromaDB（向量本体）。`PaperProcessor` 是这条入库流水线的唯一执行器，串联 `PDFParser`（解析）、`TextChunker`（分块，见 embedding 规格）、`VectorStore`（向量化与写入，见 retrieval 规格）三个组件。

处理是耗时操作（PDF 解析 + 本地 BGE-M3 批量 embedding），因此上传接口（`POST /api/papers/import`）只落盘建库记录，实际处理由 FastAPI BackgroundTasks 触发的后台函数 `_process_paper_background` 在请求返回后执行，并通过 `papers.processed` 字段（`pending / processing / done / error`）对外暴露处理状态机。另提供同步端点 `POST /api/papers/{paper_id}/process` 供手动重新处理。

## 2. 范围

### 2.1 包含

- `PaperProcessor.process()`：单篇论文「解析 → 分块 → 清旧 → 写 SQLite → 写 ChromaDB」完整流水线及其返回契约
- 处理状态机与后台任务语义（`routers/papers.py` 的 `_process_paper_background`）：状态迁移、失败重试、按 paper_id 的并发锁、与元数据增强任务的解耦
- 手动重处理端点 `POST /api/papers/{paper_id}/process` 的同步处理契约

### 2.2 非目标

- PDF 文本提取细节（归 `services/pdf_parser.py`）
- 分块策略与 chunk 字典 schema（归 `services/embedding.py` 规格的 `TextChunker`，本规格只引用其输出契约）
- 向量化、chunk id 生成（`p{paper_id}_c{i}`）、`add_chunks` / `delete_by_paper_id` 的内部行为（归 `services/retrieval.py` 规格）
- LLM 元数据增强与自动打标的内部逻辑（归 `services/auto_tag.py` 规格；本规格只描述它与核心流水线的触发/解耦关系）
- 上传接口本身的校验与落盘（大小限制 413、扩展名白名单 400、重名改名、空笔记创建等，归 papers 路由规格）

## 3. 行为契约

### 3.1 `class PaperProcessor` / `__init__(self)`

- **输出**：`PaperProcessor` 实例
- **后置条件**：持有三个组件：`PDFParser()`（无状态）、`TextChunker()`（无参，chunk_size/overlap 取硬编码默认 512/50，**不读 config.yaml**，见 embedding 规格）、`get_vector_store()`（全局单例，首次调用触发 ChromaDB 初始化与 EmbeddingService worker 线程启动，见 retrieval 规格）
- **副作用**：首次构造时触发 `get_vector_store()` 的全部首次副作用（建 `vector_db/` 目录、打开 ChromaDB 持久化文件、启动 embedding worker 线程）

### 3.2 `PaperProcessor.process(self, paper: Paper, db: Session) -> Dict[str, Any]`

- **输入**：
  - `paper`：已持久化的 `Paper` ORM 对象，`paper.file_path` 为相对项目根的 PDF 路径（由导入路由写入）
  - `db`：调用方持有的 SQLAlchemy Session；**本方法内部会执行一次 `db.commit()`**（见副作用），调用方不应假定事务仍打开
- **输出**：成功返回 `{"status": "ok", "pages": <提取页数 int>, "chunks": <写入块数 int>}`。
- **前置条件**：`paper.file_path` 指向项目根下的相对路径；Embedding 模型可用（不可用则在第 5 步抛 `RuntimeError`，见 retrieval 规格 `add_chunks` 前置条件）
- **后置条件**（成功路径）：
  1. 该 paper 在 SQLite `chunks` 表中的旧记录**全部删除**，替换为本次分块结果（`chunk_index` 从 0 连续编号，`section_title`/`chunk_type`/`token_count` 透传自 `TextChunker`）；
  2. 该 paper 在 ChromaDB 中的旧向量**全部删除**（`delete_by_paper_id`），替换为本次全量 chunk 的向量与 metadata（`title`/`authors`/`year` 取自 paper 当前值）；
  3. 即：**重处理是幂等的全量重建**，不产生重复 chunk。
- **执行顺序（固定）**：检查文件存在 → `extract_text` 解析全文 → `chunk_pages` 分块 → 删 SQLite 旧 chunks + 删 ChromaDB 旧向量 → 写入新 chunks 并 `db.commit()` → `add_chunks` 向量化写 ChromaDB → 返回统计
- **副作用**：
  - DB：删除并重建 `chunks` 表记录，**第 4 步末尾即 `db.commit()`**（向量化之前）
  - ChromaDB：删除并重建该 paper 的全部向量条目
  - 文件 I/O：读 PDF（只读）
  - 触发 embedding 计算（本地模型，无网络；首次可能触发模型加载）
- **异常**：
  - PDF 缺失抛 `FileNotFoundError`；
  - 解析失败（pdfplumber 异常）、分块 `KeyError`、embedding 不可用（`RuntimeError`）、ChromaDB 写入失败等均**直接向外抛**，本方法不做任何兜底；
  - **不一致窗口**：若第 5 步（ChromaDB 写入）抛出，SQLite chunks 已提交、ChromaDB 旧向量已删——paper 处于「有 SQLite 块、无（或部分）向量」状态，语义检索会漏掉该论文，需重处理修复。

### 3.3 `routers/papers.py :: _process_paper_background(paper_id: int)`（后台任务与状态机）

- **触发时机**：`POST /api/papers/import` 对每个成功导入的 paper 经 FastAPI `BackgroundTasks.add_task` 注册，响应返回后执行
- **状态机**（`papers.processed` 字段，默认 `pending`）：

| 时机 | 迁移 |
|------|------|
| 任务开始（取到锁、paper 存在） | → `processing`（立即 commit） |
| 核心处理成功 | → `done`（commit） |
| 连续 2 次尝试均抛异常 | → `error`（commit）后返回 |
| paper 已被删除 | 不变更状态，记 warning 后返回 |
| 锁被占用（同 paper 已有任务在跑） | 不变更状态，记 info「正在处理中，跳过重复任务」后返回 |

- **重试语义**：`for attempt in range(2)`——最多 2 次尝试；异常或非 `ok` 结果均视为失败，连续失败后标 `error`。
- **并发语义**：按 paper_id 的细粒度 `threading.Lock`（模块级 `_paper_locks` 字典 + `_paper_locks_lock` 保护）；非阻塞 `acquire(blocking=False)`，同 paper 重复任务直接跳过；**不同 paper 之间可并发**；任务结束（含异常路径，finally）释放锁并从字典中移除
- **Session 语义**：任务内自建 `SessionLocal()`，不使用请求作用域的 db
- **解耦设计**：核心处理标记 `done` **之后**，才以独立守护线程启动 `_enhance_paper_metadata(paper_id)`（LLM 元数据增强 + 自动打标，见 auto_tag 规格）；增强/打标失败**不影响** `processed` 状态
- **异常**：`process()` 之外步骤的异常未显式兜底（finally 保证锁释放；`processed` 可能停留在 `processing`，属已知盲区）

### 3.4 `POST /api/papers/{paper_id}/process`（手动同步重处理）

- **行为**：与后台任务共用 paper_id 互斥锁；冲突返回 409。取得锁后先置 `processing`，仅 `status="ok"` 时置 `done`；异常或非成功结果置 `error` 并返回脱敏 500。
- **不触发后续任务**：此端点**不会**触发元数据增强与自动打标（与导入路径不同）

### 3.5 组件交互契约（引用其他规格，不重复定义）

| 交互 | 契约来源 |
|------|----------|
| `parser.extract_text(str(pdf_path))` 返回 `[{"page_number", "text", "width", "height"}, …]`，页码从 1 起 | pdf_parser.py |
| `chunker.chunk_pages(pages)` 返回 `[{"content", "page_number", "chunk_type", "token_count"}, …]`；`section_title` 键当前不由 TextChunker 产出（`cd.get("section_title")` 恒为 None） | specs/backend/services/embedding.md §3.2/3.5 |
| `vector_store.add_chunks(paper_id, chunks, {"title","authors","year"})`：chunk id 为 `p{paper_id}_c{i}`，**不幂等**，重复 add 同 id 抛错——本模块先在第 3 步 `delete_by_paper_id` 保证安全 | specs/backend/services/retrieval.md §3.3 |
| `vector_store.delete_by_paper_id(paper_id)`：异常全捕获只记日志，调用方无法感知删除失败 | specs/backend/services/retrieval.md §3.6 |

## 4. 边界条件与错误处理

| 场景 | 期望行为 |
|------|----------|
| PDF 文件不存在 | `process()` 返回 `{"status": "error", "message": "PDF file not found"}`，不抛异常、不动 DB/ChromaDB；**两个调用方都将其当成功**，paper 被标 `done` 且无 chunk（怪癖） |
| PDF 解析出 0 页文本 / 分块结果为 0 块 | 不视为错误：旧 chunks 照常清除，写入 0 条新 chunk，`add_chunks` 以空列表调用（ChromaDB 空 upsert 无害），返回 `{"status":"ok","pages":N,"chunks":0}`，paper 标 `done` |
| Embedding 模型不可用（未下载/加载失败） | `add_chunks` 抛 `RuntimeError`；后台任务重试 1 次后标 `error`；SQLite chunks 已提交、ChromaDB 旧向量已删（不一致窗口） |
| 处理中 paper 被删除 | 后台任务查不到 paper：记 warning 直接返回，状态不变；若删除发生在 `process()` 执行中，依赖 SQLite/ChromaDB 各自行为，无防护 |
| 同一 paper 并发触发后台处理 | 后到任务取锁失败，记 info 跳过，**排队等待不会发生** |
| 手动 `/process` 与后台任务并发 | 无锁保护，可能交叉删建 chunks；当前单用户场景风险接受 |
| ChromaDB 删除旧向量失败 | `delete_by_paper_id` 内部吞异常只记日志；随后 `add_chunks` 可能因 chunk id 重复抛错 → 整体失败标 `error` |

## 5. 依赖

- **上游依赖**：`services/pdf_parser.py`（`PDFParser.extract_text`）、`services/embedding.py`（`TextChunker`）、`services/retrieval.py`（`get_vector_store` / `VectorStore.add_chunks` / `delete_by_paper_id`）、`app.models`（`Paper` / `Chunk`）、`app.database`（`SessionLocal`，后台任务侧）
- **下游消费者**：`routers/papers.py`——`POST /api/papers/import`（经 `_process_paper_background`）、`POST /api/papers/{paper_id}/process`；间接消费者为所有依赖 chunks/向量的检索与对话功能（search、chat、agent_graph、summarize）

## 6. 验收标准（可测试）

- [ ] AC1：给定有效 PDF 与 paper 记录，`process()` 返回 `{"status":"ok","pages":N,"chunks":M}`，且 SQLite 中该 paper 恰有 M 条 chunk（`chunk_index` 从 0 连续）、ChromaDB 中对应向量存在
- [ ] AC2：`paper.file_path` 指向不存在的文件时，`process()` 返回 `{"status":"error","message":"PDF file not found"}`，且不增删任何 chunk
- [ ] AC3：重复调用 `process()` 后 chunks 数量与首次相同（幂等重建，无重复）
- [ ] AC4：后台任务在 `process()` 连续 2 次抛异常后将 `processed` 置为 `error`；首次抛异常、第 2 次成功时置为 `done`
- [ ] AC5：同一 paper_id 的后台任务并发触发时，只有一个实际执行，另一个记日志跳过（状态不被并发改写）
- [ ] AC6：核心处理完成后才启动 `_enhance_paper_metadata` 线程，且后者抛异常不改变 `processed="done"`

## 7. 现有测试覆盖与盲区

- **已覆盖**：`test_processor_abstract_chunk.py` 覆盖成功分块/摘要重建；`test_process_integrity.py` 覆盖缺文件异常、非成功结果和手动锁冲突。后台状态机仍由导入测试整体桩掉。
- **盲区**：
  - **高**：`process()` 成功路径全链路（解析→分块→清旧→双库写入→返回统计）无测试（AC1、AC3）
  - **高**：后台任务状态机（pending→processing→done/error）与「失败重试 1 次」语义无测试（AC4、AC5）
  - PDF 缺失、非成功结果和手动锁冲突已由 `tests/test_process_integrity.py` 覆盖。
  - **中**：第 5 步失败后的「SQLite 有块、ChromaDB 无向量」不一致窗口无测试、无修复机制
  - 手动 `/process` 的成功响应与处理期间状态可见性仍缺测试；失败脱敏和状态已覆盖。
  - **低**：`_paper_locks` 锁泄漏（任务异常路径锁已由 finally 覆盖，但字典清理与锁复用语义无测试）
  - **低**：`section_title` 恒为 None、`token_count` 为字符数等透传字段语义无断言

## 8. 关键设计决策

- **处理失败统一为异常/非成功防御检查**：缺 PDF 抛 `FileNotFoundError`；调用方仍校验结果状态，避免第三方处理器返回错误字典时误标完成。
- **先 commit SQLite 再写 ChromaDB**：保证 SQLite 块元数据不丢失，代价是第 5 步失败留下不一致窗口；单用户本地场景接受最终需重处理修复
- **重试仅 1 次、无退避**：处理失败的常见原因是模型未就绪或 PDF 损坏，快速重试一次覆盖瞬时故障即可；反复重试对损坏文件无意义
- **按 paper_id 细粒度锁而非全局锁**：不同 PDF 可并行处理（embedding worker 本身是串行队列，见 embedding 规格），锁只为防同 paper 重复处理；非阻塞取锁 + 跳过，避免任务堆积
- **核心处理与 LLM 增强解耦**：`done` 只代表「可检索」，不等 LLM；增强/打标放独立守护线程，失败不影响检索可用性
- **手动 `/process` 共享核心锁但不触发打标**：避免与后台交叉重建 chunks，同时保留调试/修复入口的轻量语义。
