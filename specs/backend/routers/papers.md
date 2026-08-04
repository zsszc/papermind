# routers/papers.py（文献库 HTTP API，挂载前缀 `/api/papers`）规格说明书

> 本文件描述 `backend/app/routers/papers.py` 的**行为契约**（做什么），不描述实现细节。
> 路由层是 HTTP 包装层：处理流水线、PDF 解析、自动打标的内部行为分别归 `specs/backend/services/processor.md`、`pdf_parser.md`、`auto_tag.md`，本规格只在触发/衔接点引用。
> 依据 2026-08-04 时点的源码反向工程整理；规格与代码冲突时以代码为准并立即修订本文件。

## 1. 背景与目标

`papers.py` 是文献库的全部 HTTP 入口：PDF 批量导入（落盘 + 建档 + 触发后台处理）、文献列表/详情/更新/删除、标签 CRUD、阅读进度、Markdown 笔记读写、PDF 文件流、批量操作、手动重处理、AI 概括、PDF 页面标注、LLM 元数据提取。前端 PaperList / PaperDetail / StatsPage 等页面与 MCP Server 之外的所有文献操作都经过它。

安全纪律（宪法第 12/14 条相关）在本模块的体现：上传单文件 50MB 上限（413）、扩展名白名单（400）、上传文件名经 `Path(...).name` 去目录、超限/失败清理残留文件。

## 2. 范围

### 2.1 包含

- 上传安全契约：`MAX_UPLOAD_SIZE = 50MB`、1MB 分块写盘、413/400、`_1/_2` 重名改名、残留文件清理（`_save_upload_file`）
- 导入端点 `POST /import` 的建档契约（解析容错、`parse_error` 兜底、空笔记创建、后台处理触发）
- 后台守护线程 `_enhance_paper_metadata`（LLM 元数据增强 + 触发自动打标）的行为契约
- 列表/详情/更新/删除/统计（`/stats/overview`）端点
- 标签端点（挂/摘标签、全量列表、改标签）与 `_fix_tag_encoding` 乱码修复
- 阅读进度、笔记、PDF 流、批量删除/状态/标签、AI 概括读写、标注 CRUD、`/extract-metadata`
- 手动重处理 `POST /{paper_id}/process` 的路由侧语义（流水线细节引用 processor.md）

### 2.2 非目标

- 处理状态机、重试、`_paper_locks` 并发锁、`PaperProcessor.process` 内部（归 processor.md §3.2–3.4）
- PDF 元数据/正文提取细节与 `enhance_metadata_with_llm` 异步版（归 pdf_parser.md）
- 自动打标规则/LLM 通道内部（归 auto_tag.md）
- `llm_service` 重试/截断/temperature 处理（归 llm 规格）
- PDF 的另一种访问方式 `/static/papers/...` 白名单静态服务（归 routers/static.py）
- 前端展示逻辑、MCP 工具的只读查询（`services/mcp_server.py`）

## 3. 行为契约

端点签名照抄代码（`router = APIRouter()`，挂载时加前缀 `/api/papers`）。凡 `db: Session = Depends(get_db)` 不再逐条赘述。

### 3.1 `POST /import` — `import_papers(files: List[UploadFile] = File(...), background_tasks: BackgroundTasks = None, db: Session = Depends(get_db))`

批量导入 PDF。

- **输入**：multipart 文件列表 `files`。
- **输出**：`PaperListResponse{total, items[PaperListItem]}`，仅含本次成功导入的论文（`db.refresh` 后）。
- **行为（对每个文件，顺序固定）**：
  1. 扩展名白名单：`filename` 为空或不以 `.pdf` 结尾（`lower()` 后判断）→ 400「只支持 .pdf 文件: <文件名>」；
  2. 声明大小快筛：`file.size > MAX_UPLOAD_SIZE`（50MB）→ 413；
  3. `safe_name = Path(file.filename).name`（剥离目录成分）；
  4. **重名改名**：目标已存在则依次尝试 `{stem}_1{suffix}`、`{stem}_2`……直到不重名；
  5. `_save_upload_file` 分块（1MB）异步写盘，按**实际读取字节数**兜底 50MB 上限，超限抛 413；任何失败（含超限）删除残留文件；
  6. `PDFParser().parse_metadata` 放线程池执行；**解析异常不阻断导入**，降级为 `metadata = {"parse_error": str(e)}`；
  7. 建 `Paper`（`title` 取元数据标题或文件 stem，`status="unread"`、`source="local"`、`file_path` 为相对项目根路径、`metadata_json=metadata`），`db.flush()` 分配 id；
  8. 创建空笔记 `notes/{paper.id}.md`，内容为 `# {title 或 filename}\n\n`（线程池写入）；
  9. `background_tasks.add_task(_process_paper_background, paper.id)`——响应返回后触发后台处理（状态机见 processor.md §3.3）。
- **后置条件**：全部文件处理完后统一 `db.commit()`；PDF 已落盘、笔记已创建、每条 Paper 已登记后台任务。
- **副作用**：文件 I/O（`papers/`、`notes/`）；DB 写入；登记后台任务（间接触发解析/向量化/LLM 增强/打标）。
- **异常**：400（扩展名）、413（超限）。**中途失败语义**：第 N 个文件抛错时已处理的前 N-1 个文件因未 `commit` 而**回滚 DB 记录，但 PDF 与笔记文件已残留在磁盘上**（孤儿文件，无清理）。
- **契约引用**：`_process_paper_background` 的状态机/重试/锁见 processor.md §3.3；`parse_metadata` 的字段语义（`journal`/`abstract` 恒 None）见 pdf_parser.md §3.1。

### 3.2 `GET ""` — `list_papers(skip: int = 0, limit: int = 20, status: Optional[str] = None, tag: Optional[str] = None, q: Optional[str] = None, ...)`

- **输出**：`PaperListResponse{total（过滤后总数，分页前）, items}`，按 `created_at desc` 排序。
- **过滤语义**：
  - `status`：精确匹配（`unread/read/important/todo`，不校验合法性，非法值得空列表）；
  - `tag`：逗号分隔多个标签名（去空白）；单标签 `Tag.name ==`，多标签 `Tag.name.in_(...)`——**多标签是 OR 语义**，非 AND；
  - `q`：对 `title/authors/abstract` 做 `ilike %q%`（OR，大小写不敏感）；
  - `limit` 被钳制到 `[1, 200]`（`min(max(limit,1),200)`），`skip` 无上限校验（负数行为未定义，SQLAlchemy offset 负数抛错）。
- **异常**：无显式抛出路径；DB 层异常向外传播。

### 3.3 `GET /stats/overview` — `paper_stats(db)`

文献库统计与引用关系图数据（StatsPage 数据源）。

- **输出**：未声明 response_model 的 dict：`{total, by_year（按年份字符串键、升序）, by_status, by_tag（Top 20）, top_authors（Top 10，按 `authors` 逗号拆分计数）, citation_graph: {nodes, links}}`。
- **citation_graph 语义**：nodes 含全部论文（`id="p{id}"`, type=paper）与被引用章节节点（`id="t{thesis_id}_ch{chapter_index}"`, type=chapter，标题取 `thesis.chapter_structure[chapter_index].title`，缺省 `第N章`）；links 为 `{source: p{paper_id}, target: 章节节点, value: 1}`，来源是 `thesis_citations.paper_id IS NOT NULL` 的记录；`chapter_index` 越界时不建节点不加 link。
- **性能特征**：全表扫描 + 逐条 citation 查 `ThesisFile`（**N+1 查询**，单用户规模接受）。
- **副作用**：无（只读）。

### 3.4 `GET /{paper_id}` — `get_paper(paper_id: int, db)`

- **输出**：`PaperDetail`（含 tags、metadata_json、file_path 等全字段）。**异常**：不存在 → 404「Paper not found」。

### 3.5 `PUT /{paper_id}` — `update_paper(paper_id: int, payload: PaperUpdate, db)`

- **行为**：`payload.model_dump(exclude_unset=True)` 逐字段 `setattr`——**只更新显式传入的字段**；可更新字段为 `PaperUpdate` 全集（title/authors/year/journal/abstract/doi/pages/status/source/processed/metadata_json）。
- **输出**：`PaperDetail`（commit + refresh 后）。**异常**：404。
- **注意**：`status`/`processed` 取值无枚举校验，可写入任意字符串；FTS 表同步依赖 `papers_fts` 触发器（见 models.py，不在本路由）。

### 3.6 `DELETE /{paper_id}` — `delete_paper(paper_id: int, db)` → 204

- **行为（顺序固定）**：
  1. 404 检查；
  2. 清理 ChromaDB 向量（`get_vector_store().delete_by_paper_id`），**失败仅记 warning 不阻断**；
  3. 删除三类本地文件：`{project_root}/{paper.file_path}`、`notes/{paper_id}.md`、`summaries/{paper_id}.md`，**失败仅记 warning**；
  4. 删 `chunks` 表记录 → `db.delete(paper)` → commit。标注（`paper_annotations`）经 ORM `cascade="all, delete-orphan"` 随 paper 删除；`paper_tags` 关联行由多对多关系自动清除。
- **已知悬空**：`thesis_citations.paper_id` 不清理——被大论文引用记录指向已删论文（SQLite 默认不强制外键，无级联；stats/citation-map 读侧以 `paper_id.isnot(None)` + join 容忍）。
- **后置条件**：无论文件/向量清理成败，DB 记录一定删除（清理失败只留日志与孤儿数据）。

### 3.7 `POST /{paper_id}/tags` — `add_tag_to_paper(paper_id: int, tag_name: str = Form(...), db)`

- **行为**：`tag_name` 经 `_fix_tag_encoding`（latin-1→UTF-8 乱码还原防御）+ strip；空 → 400「Tag name cannot be empty」；按 `Tag.name ==` 精确查重，不存在则新建（颜色取 schema 默认 `#1890ff`，与自动打标的随机色不同）；已在 `paper.tags` 中则不重复关联（也不 commit）。
- **输出**：`PaperDetail`。**异常**：404（paper）、400（空名）。

### 3.8 `DELETE /{paper_id}/tags/{tag_id}` — `remove_tag_from_paper(paper_id, tag_id, db)`

- **行为**：paper/tag 各自 404；tag 不在 paper.tags 中时为无害空操作（不 commit）；在则移除并 commit。**输出**：`PaperDetail`。

### 3.9 `GET /tags/all` — `list_all_tags(db)`

- **输出**：`List[TagResponse]`，按 `Tag.name asc`。全量返回（含未被任何论文使用的标签）。

### 3.10 `PUT /tags/{tag_id}` — `update_tag(tag_id: int, payload: TagCreate, db)`

- **行为**：`exclude_unset` 部分更新（`TagCreate` 的 name 在 schema 层必填，但 exclude_unset 允许只传 color/description）；重名改名撞 `tags.name` 唯一约束时 DB 异常向外传播（全局异常脱敏为 500）。**输出**：`TagResponse`。**异常**：404。

### 3.11 `GET|PUT /{paper_id}/read-progress`

- `GET`：返回 `{"paper_id", "last_read_page": paper.last_read_page or 1}`（NULL 兜底 1）。
- `PUT`：`page: int` 为**查询参数**；写入 `max(1, page)`（负数/0 被钳为 1）；返回同上。均 404 检查。

### 3.12 `GET|POST /{paper_id}/note`

- `GET`：读 `notes/{paper_id}.md`，**文件不存在返回 `{"content": ""}`（非 404）**；**不检查 paper 是否存在**（任意 id 都可读，路径由 int 参数保证无穿越）。
- `POST`：`content: str = Form(...)` 整体覆写笔记文件；返回 `{"status": "ok"}`；**同样不检查 paper 存在性**——可对不存在的 id 写出孤儿笔记文件。

### 3.13 `GET /{paper_id}/pdf` — `get_pdf_file(paper_id, db)`

- **输出**：`StreamingResponse`（`media_type="application/pdf"`，分块 `yield from f`）；`Content-Disposition: inline; filename="..."; filename*=UTF-8''<quote(filename)>`（RFC 5987，支持中文名）。
- **异常**：paper 不存在 → 404「Paper not found」；文件缺失 → 404「PDF file not found」。
- **注意**：与 `/static/papers/<name>` 白名单静态路由并存，本端点按 id 寻址、不受文件名白名单约束。

### 3.14 `POST /batch/delete` | `/batch/status` | `/batch/tags`

- `/batch/delete`（`PaperBatchDelete{ids}`）：逐个调用 `delete_paper(paper.id, db)`；**不存在的 id 静默跳过**；204。每个删除内部各自 commit（非单事务）。
- `/batch/status`（`PaperBatchStatus{ids, status}`）：命中的 paper 全部改状态，一次 commit；返回 `{"updated": <命中数>}`；status 无枚举校验。
- `/batch/tags`（`PaperBatchTags{ids, tag_names, action="add"}`）：对每个标签名查重/新建，按 `action` add/remove 挂摘；`action` 非 add/remove 时**静默无操作仍返回 200**；返回 `{"updated": <命中 paper 数>}`。

### 3.15 `POST /{paper_id}/process` — `process_paper(paper_id, db)`

手动同步重处理。**契约（状态机怪癖、500 时异常原文外泄、不触发打标、无并发锁）见 processor.md §3.4**，本规格不重复。404（paper）；`process()` 抛异常 → 置 `processed="error"` + 500。

### 3.16 `POST /{paper_id}/summarize` 与 `GET /{paper_id}/summary`

- `POST`：
  - **前置**：paper 存在（404）；`processed == "done"`（否则 400「论文尚未处理完成，请稍后再试」）；存在 chunk（否则 400「论文内容为空，无法生成概括」）。
  - **行为**：拼接**全部** chunks 内容按字符截断到 6000 → 调 `llm_service.chat_completion(messages, timeout=300)`（prompt 含六段固定结构，第 5 段写死「与结直肠癌 T 分期预测研究的关联」）。
  - **后置**：成功时把 `# {title}\n\n{summary}\n` 覆写 `summaries/{paper_id}.md`；返回 `{"paper_id", "summary"}`。
  - **异常**：LLM 调用抛异常 → 504（detail 含异常原文，与脱敏约定不符）；返回串以 `[调用 LLM 出错` 开头（llm_service 的错误格式化产物）→ 504。
- `GET`：读概括文件；paper 不存在 → 404；文件不存在 → 404「尚未生成 AI 概括」；剥离首行 `# ` 标题行后返回正文 `{"paper_id", "summary"}`。

### 3.17 标注 `GET|POST /{paper_id}/annotations`、`DELETE /{paper_id}/annotations/{annotation_id}`

- `GET`：404 检查；按 `page_number asc, created_at asc` 返回 `List[PaperAnnotationResponse]`。
- `POST`：404 检查；字段 `page_number/selected_text/note/color`（`PaperAnnotationCreate`，selected_text 必填，color 默认 yellow）；commit + refresh 返回。
- `DELETE`：`id` 与 `paper_id` **同时匹配**才删（防跨论文误删），不匹配 → 404「Annotation not found」；204。

### 3.18 `POST /{paper_id}/extract-metadata` — `extract_metadata(paper_id, db)`

- **行为**：404（paper / PDF 文件）；调 `PDFParser().enhance_metadata_with_llm(str(pdf_path))`（**异步版**，请求作用域事件循环内，与后台链路的同步镜像不同）；仅把**非空**字段（title/authors/year/journal/abstract/doi）写回 paper；`metadata_json` 与增强结果合并（含 confidence/source_lines）；commit 返回 `PaperDetail`。
- **异常**：LLM 调用异常不在本端点捕获（pdf_parser.md §3.2 约定异常外抛）→ 全局异常脱敏为 500；返回 `{}`（前页为空/JSON 失败）时仅 `metadata_json` 无实质变化，仍 200。
- **死代码怪癖**：局部变量 `update_data` 只写入 `title` 后从未使用（源码 847–850 行），无行为影响。
- **不触发**自动打标（与后台 `_enhance_paper_metadata` 不同）。

### 3.19 `_enhance_paper_metadata(paper_id: int)`（后台守护线程，非 HTTP 端点）

- **触发**：仅由 `_process_paper_background` 在核心处理标 `done` 后以 `threading.Thread(..., daemon=True)` 启动（processor.md §3.3）；手动 `/process` 与 `/extract-metadata` 均不触发。
- **行为**：
  1. 自建 `SessionLocal()`；paper 不存在 → 记 warning 返回；
  2. 调同步镜像 `_enhance_metadata_with_llm_sync(pdf_path)`：用 `PDFParser()._extract_front_text(path, max_pages=3)` 取前 3 页文本（空 → 返回 `{}`），prompt 截断 `front_text[:4000]`，走 `llm_service.chat_completion_sync(messages, json_mode=True)`；**JSON 解析/字段规整失败（含 year 无法转 int）整体返回 `{}`**；LLM 调用自身异常向外抛，被外层 try 捕获记 error 后 `return`；
  3. 非空字段写回 paper（**只覆盖，不清空**：增强结果为空时保留原值），`metadata_json` 合并，commit；
  4. 随后调 `auto_tag_service.generate_tags_sync(paper, db, timeout=60)`，把返回的新 Tag append 到 `paper.tags`（已有关联去重），commit；打标异常记 error 不影响前序 commit。
- **关键语义**：
  - 全程异常只记日志，**绝不改变 `processed` 状态**（已 `done`）；
  - **连锁跳过**：第 2 步异常 → `return`，第 4 步打标不执行（LLM 故障既丢增强又丢标签，无重试）；
  - **abstract/journal 的唯一写入口**（规则解析恒 None，见 pdf_parser.md §8「16 篇论文 abstract 全空」根源分析）：本线程失败 = abstract 永远为空，且用户无感知；
  - 无独立锁，串行性依赖上游「同一 paper 只有一个核心处理任务」。
- **副作用**：DB 更新（papers、tags、paper_tags）；一次 LLM 同步调用（增强）+ 至多一次 LLM 调用（打标）。

### 3.20 路由注册顺序约定

代码注释要求「固定路径必须放在 `/{paper_id}` 之前」。实际约束更窄：`/{paper_id}` 是**单段**路径，只有同为单段的固定路径会冲突——`/import`、`/stats/overview` 确实定义在前（第 268、385 行 vs 第 446 行）；`/batch/*`、`/tags/*` 为双段路径，虽定义在 `/{paper_id}` 之后也不冲突。新增**单段**固定路径时必须置于 `/{paper_id}` 之前。

## 4. 边界条件与错误处理

| 场景 | 期望行为 |
|------|----------|
| 上传非 .pdf / 无文件名 | 400；不写盘、不建档 |
| 声明大小或实际字节 > 50MB | 413；`_save_upload_file` 清理残留文件，磁盘无半成品 |
| 同名文件重复导入 | 自动改名 `name_1.pdf`、`name_2.pdf`……DB 中 `filename` 为改名后名字，标题元数据不受改名影响 |
| 文件名含路径成分（如 `../../x.pdf`） | `Path(filename).name` 剥目录，只取末段文件名写入 `papers/` |
| 多文件批量导入中途失败 | 已处理文件 DB 回滚（commit 在循环外），但 PDF/笔记文件残留磁盘成孤儿 |
| 损坏 PDF 导入 | `parse_metadata` 异常容错为 `{"parse_error": ...}`，导入继续，标题兜底为文件 stem |
| 列表 `limit > 200` 或 < 1 | 钳制到 200 / 1 |
| 多标签筛选 `tag=a,b` | OR 语义（in_），非 AND |
| 更新不存在的 paper / tag / annotation | 404 |
| 删除 paper 时向量/文件清理失败 | 仅记 warning，DB 记录照删；可能遗留孤儿向量/文件 |
| 笔记读到不存在的 paper_id | 不校验，返回空内容或写出孤儿文件（低危，单用户） |
| `read-progress` 传入 0/负数页码 | 钳为 1 |
| `batch/tags` 传非法 action | 静默无操作，200 |
| summarize 时 `processed != "done"` 或无 chunk | 400 |
| LLM 概括超时/失败 | 504（detail 含异常原文）；已生成的旧概括文件不受影响 |
| 后台增强线程 LLM 故障 | paper 保持规则元数据，abstract/journal 恒空；打标连锁跳过；仅日志可见 |
| 同 paper 并发触发后台处理 | 后到任务取锁失败跳过（processor.md §3.3） |

## 5. 依赖

- **上游依赖**：`app.database.get_db`、`app.models`（Paper/Tag/Chunk/PaperAnnotation/ThesisCitation/ThesisFile）、`app.schemas`、`services/pdf_parser.PDFParser`、`services/processor.PaperProcessor`、`services/llm.llm_service`（宪法第 8 条唯一入口）、`services/auto_tag.auto_tag_service`、`services/retrieval.get_vector_store`、`aiofiles`、`app.core.logger`。
- **下游消费者**：前端 `api.js` 全部文献页面（PaperList/PaperDetail/StatsPage）；`specs/backend/services/processor.md` 所述后台链由本模块触发。

## 6. 验收标准（可测试）

- [ ] AC1：上传 > 50MB（测试中 monkeypatch 调小阈值）返回 413 且目标目录无残留文件
- [ ] AC2：上传非 .pdf 返回 400；上传合法小 PDF 返回 200，PDF 落盘、`notes/{id}.md` 创建、Paper 记录存在
- [ ] AC3：同名文件二次导入落盘为 `name_1.pdf`，两条 Paper 记录各自独立
- [ ] AC4：多文件导入中第 2 个文件 400/413 时，第 1 个文件无 DB 记录（回滚语义；文件残留为已知行为）
- [ ] AC5：`GET ""` 的 status/tag（单、多）/q 过滤与 `limit` 钳制（>200 → 200）符合 §3.2；`total` 为分页前计数
- [ ] AC6：`PUT /{paper_id}` 只更新传入字段，未传字段保持原值
- [ ] AC7：`DELETE /{paper_id}` 后 chunks/annotations/关联标签行清除，PDF/笔记/概括文件删除
- [ ] AC8：tags 端点：重名挂标签不产生重复关联、空名 400、`_fix_tag_encoding` 乱码名还原
- [ ] AC9：`POST /{paper_id}/summarize` 在 `processed != "done"` 时 400；mock LLM 成功后 `summaries/{id}.md` 写入且 `GET /summary` 返回剥离标题行的正文
- [ ] AC10：`_enhance_paper_metadata`：mock 增强返回后非空字段覆盖、空字段保留；增强抛异常时打标不执行且 `processed` 不变
- [ ] AC11：标注三端点：跨 paper_id 删除他篇标注返回 404

## 7. 现有测试覆盖与盲区

- **已覆盖**：`backend/tests/test_upload.py` 三个 papers 用例——`test_import_oversized_pdf_returns_413`（monkeypatch 阈值 + 无残留）、`test_import_invalid_extension_returns_400`、`test_import_small_pdf_success`（落盘 + 记录 + 空笔记，后台处理整体桩掉）。其余 14 个测试文件不触 papers HTTP 端点（test_mcp 直调 mcp_server 函数，test_search 直建 ORM）。
- **盲区**：
  - **高**：列表过滤语义（status/tag 单多与 OR/q/分页钳制、total 语义）无测试（AC5）
  - **高**：`PUT` 部分更新、`DELETE` 的级联清理（chunks/annotations/文件/向量）无测试（AC6、AC7）
  - **高**：`_enhance_paper_metadata` 守护线程全链路（字段覆盖/保留、打标连锁跳过、异常不改 processed）无测试（AC10）——abstract 空值问题的核心链路
  - **高**：summarize 的前置 400（未 done/无 chunk）、504 路径、文件覆写与 `GET /summary` 标题行剥离无测试（AC9）
  - **中**：同名 `_1/_2` 改名、文件名路径剥离无测试（AC3）
  - **中**：批量导入中途失败的「DB 回滚 + 文件残留」语义无测试（AC4）
  - **中**：批量三端点（静默跳过不存在 id、非法 action 空操作）无测试
  - **中**：`/stats/overview`（含 citation_graph、N+1）无测试
  - **中**：标注 CRUD 与跨论文删除防护无测试（AC11）
  - **低**：标签乱码修复 `_fix_tag_encoding`、read-progress 钳制、笔记不校验 paper 存在性无测试
  - **低**：`/extract-metadata`（异步增强、死代码 `update_data`）与 `/pdf` 流式响应头（RFC 5987 中文名）无测试

## 8. 关键设计决策

- **上传先快筛后兜底**：声明大小（`file.size`）快筛 + 实际字节数兜底——声明可伪造/缺失，分块计数才是最终防线；任何写盘失败清理残留，避免损坏 PDF 入库（宪法第 12 条配套）。
- **解析失败不阻断导入**：`parse_metadata` 异常降级为 `parse_error` 记录，保证「任何 PDF 都能建档」；更精确的元数据交给后台 LLM 增强（pdf_parser.md §8 的设计配套）。
- **commit 在循环外**：批量导入一次 commit 换吞吐；代价是中途失败留下孤儿文件（DB 回滚而文件已落盘），单用户场景接受，未做补偿清理。
- **核心处理与 LLM 增强解耦 + 同步镜像**：后台线程无事件循环，复用 async client 会跨事件循环崩溃（历史事故），故路由层维护一份 `_enhance_metadata_with_llm_sync` 同步镜像，prompt 需与 pdf_parser 异步版手工保持一致——**双份维护是已知腐化点**（pdf_parser.md §3.2 注释亦明示）。
- **增强/打标失败静默化**：只记日志、不动 `processed`——保证检索可用性不被 LLM 故障连累；代价是 abstract/journal/标签缺失用户无感知（真实库 19/19 篇 abstract 为空的直接成因，见 pdf_parser.md §8）。
- **删除尽力清理**：向量/文件清理失败不阻断 DB 删除——避免一次 ChromaDB 故障导致无法删文献；代价是可能留孤儿数据。
- **笔记/阅读进度端点不校验 paper 存在**：单用户信任前端，路径由 int 参数保证安全；孤儿笔记无害。
- **`/process` 手动端点刻意轻量**：不挂锁、不触发打标、异常原文透传 detail（与全局脱敏约定不一致，属历史遗留，见 processor.md §3.4）。
