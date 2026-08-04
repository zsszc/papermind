# MCP Server（mcp_server）规格说明书

> 本规格由 `backend/app/services/mcp_server.py`（244 行）反向工程而来，描述将 PaperMind 文献库只读能力暴露为 MCP 工具的行为契约。

## 1. 背景与目标

PaperMind 的文献库需要被任意 MCP 客户端（如 Claude Desktop、其他 Agent）连接使用。本模块基于 `mcp==1.3.0` 的 FastMCP 注册 4 个只读工具，并手工构建 Starlette SSE 子应用挂载进现有 FastAPI 进程（`app.mount("/mcp", ...)`），与主应用同进程、同数据库，不引入额外服务。

**版本硬约束**（宪法第 16 条）：`mcp==1.3.0` 锁定——更高版本依赖 `starlette>=0.49` / `pydantic>=2.11`，与 FastAPI 0.110（starlette<0.37）硬冲突。该版本的 FastMCP **未提供 `sse_app()`**，故 `_create_sse_app()` 参照其 `run_sse_async()` 实现手工构建 SSE 应用。升级 mcp 前必须 `pip check` 验证零冲突。

## 2. 范围

### 2.1 包含

- FastMCP 实例（`FastMCP("papermind")`）与 4 个只读工具的输入输出契约：`search_papers` / `list_papers` / `get_paper` / `get_library_stats`
- SSE 传输子应用的构建（`_create_sse_app`）：GET `/mcp/sse` 长连接 + POST `/mcp/messages/` 消息回传
- 挂载契约（`get_mcp_app` 懒加载单例，`main.py` 挂载于 `/mcp`，须先于 `/static` 白名单路由）
- 内部辅助函数 `_paper_brief`（摘要截断）、`_fts_search_ids`（FTS5 检索 + LIKE 回退）
- 工具函数的 DB 会话管理（自建 `SessionLocal`，用完即关，全程只读）

### 2.2 非目标

- 不提供任何写操作工具（不新增/修改/删除文献）
- 不实现 MCP 协议本身（依赖 mcp 库的 FastMCP 与 SseServerTransport）
- 不做鉴权/访问控制（单用户本地应用，宪法第 2 条）；不得暴露公网（宪法第 15 条）
- 不触发 LLM / embedding 调用
- 不复用 FastAPI 的 `Depends(get_db)`（MCP 请求不走 FastAPI 依赖注入）

## 3. 行为契约

### 3.1 模块级对象 `mcp = FastMCP("papermind")`

- 服务名固定为 `"papermind"`；4 个工具经 `@mcp.tool()` 装饰器注册，**工具函数的 docstring 即 MCP 协议中暴露的工具描述**，修改 docstring 即修改对外契约。
- 常量 `_ABSTRACT_MAX_LEN = 200`：列表/检索结果中摘要的截断长度。

### 3.2 `def search_papers(query: str, limit: int = 5) -> List[Dict[str, Any]]`

- **输入**：`query` 检索关键词（支持多词，FTS 路径下词间为 AND 语义）；`limit` 最多返回条数，默认 5
- **输出**：文献简要信息列表，每项含 `id` / `title` / `authors` / `year` / `journal` / `abstract`（>200 字截断并追加 `"..."`）/ `status`
- **前置条件**：无（空库返回 `[]`）
- **后置条件**：
  - `limit` 被钳制到 `[1, 50]`（`max(1, min(int(limit), 50))`）
  - 优先走 FTS5：`papers_fts MATCH :query ORDER BY rank LIMIT :limit`，查询串先经 `routers.search._sanitize_fts_query()` 清洗；命中后按 FTS 相关度顺序返回（`IN` 查询重建 `by_id` 映射、按 ids 顺序输出）
  - 清洗后查询串为空 → 返回 `[]`
  - FTS 不可用（如虚拟表缺失、执行抛异常）→ 记 `[mcp]` warning 日志，回退 ORM `LIKE %query%` 三字段（title/authors/abstract）模糊匹配，按 ORM 默认顺序返回
- **副作用**：`SessionLocal()` 自建会话，只读查询，`finally` 中 `db.close()`
- **异常**：FTS 异常被吞（回退 LIKE）；其他 DB 异常向上抛，但会话保证关闭

### 3.3 `def list_papers(skip: int = 0, limit: int = 20, status: Optional[str] = None) -> Dict[str, Any]`

- **输入**：`skip` 偏移量（默认 0）；`limit` 每页条数（默认 20）；`status` 可选阅读状态过滤（`unread` / `read` / `important` / `todo`）
- **输出**：`{"total": 总条数, "papers": [文献简要信息...]}`（简要结构同 3.2）
- **后置条件**：
  - `skip` 钳制到 `>= 0`；`limit` 钳制到 `[1, 100]`
  - `status` 非空时按 `Paper.status == status` 过滤；传非法值时匹配不到记录，返回空列表而非报错
  - `total` 为过滤后的总条数（不受分页影响）
  - 排序固定为 `created_at DESC, id DESC`（收录时间倒序）
- **副作用**：自建会话只读查询，`finally` 关闭
- **异常**：无显式处理；DB 异常向上抛

### 3.4 `def get_paper(paper_id: int) -> Dict[str, Any]`

- **输入**：`paper_id` 文献 ID
- **输出**：文献完整元数据 dict；**文献不存在时返回 `{"error": f"文献不存在: paper_id={paper_id}"}` 而非抛异常**
- **后置条件**（存在时返回字段）：
  - 标量字段：`id` / `title` / `authors` / `year` / `journal` / `abstract`（完整不截断）/ `doi` / `status` / `source` / `processed` / `last_read_page` / `file_path` / `filename`
  - `tags`：标签名列表（`[t.name for t in p.tags]`）
  - `note_path`：项目根 `notes/{id}.md` 的绝对路径字符串；**文件不存在时为 `None`**（经 `_project_root()` 定位，依赖真实文件系统）
  - `created_at` / `updated_at`：ISO 格式字符串，为 NULL 时为 `None`
- **副作用**：自建会话只读查询；`notes/` 目录文件存在性检查（只读 `Path.exists()`）
- **异常**：无显式处理

### 3.5 `def get_library_stats() -> Dict[str, Any]`

- **输入**：无
- **输出**：`{"total": int, "by_status": {状态: 计数}, "by_processed": {处理状态: 计数}}`
- **后置条件**：
  - `total` 为 `func.count(Paper.id)`，空库为 0
  - `by_status` 按 `Paper.status` 分组计数；`by_processed` 按 `Paper.processed` 分组计数
  - 分组键为 NULL 时归入字符串键 `"unknown"`；所有键经 `str()` 转换
- **副作用**：自建会话只读查询，`finally` 关闭
- **异常**：无显式处理

### 3.6 `def _paper_brief(p: Paper) -> Dict[str, Any]`（内部）

- 列表/检索共用的简要表示：`abstract` 超过 `_ABSTRACT_MAX_LEN(200)` 字时截断为前 200 字 + `"..."`；`abstract` 为 NULL 时按空串处理。返回 7 个键：`id` / `title` / `authors` / `year` / `journal` / `abstract` / `status`。

### 3.7 `def _fts_search_ids(db, query: str, limit: int) -> Optional[List[int]]`（内部）

- 返回按 FTS 相关度（`ORDER BY rank`）排序的 paper id 列表。
- 三分支语义：**清洗后查询串为空 → 返回 `[]`（调用方直接返回空结果）；FTS 执行异常 → 返回 `None`（调用方回退 LIKE）；正常 → 返回 id 列表（可能为空）**。`[]` 与 `None` 的区分是回退机制的核心契约。
- 查询串必须经 `_sanitize_fts_query()` 清洗（宪法第 11 条，杜绝 MATCH 语法错误与注入），SQL 走绑定参数。

### 3.8 `def _create_sse_app(messages_endpoint: str = "/mcp/messages/")`

- **输入**：`messages_endpoint` 客户端可见的完整消息回传路径，默认 `/mcp/messages/`
- **输出**：Starlette 子应用，路由为 `Route("/sse", handle_sse)` + `Mount("/messages/", app=sse.handle_post_message)`
- **后置条件**：
  - GET `/mcp/sse`（挂载后路径）建立 SSE 长连接，`SseServerTransport.connect_sse` 产出读写流，驱动 `mcp._mcp_server.run(...)` 完成 MCP 初始化与会话
  - POST `/mcp/messages/` 接收客户端消息回传
  - **`messages_endpoint` 必须含挂载前缀的完整路径**（`/mcp/messages/` 而非 `/messages/`）：`SseServerTransport` 把它原样写进 endpoint 事件发给客户端，否则客户端 POST 会落到错误路径
  - 使用 mcp 1.3.0 私有属性 `mcp._mcp_server`（该版本无公开 sse_app API）——升级 mcp 版本时此处是必须审查的耦合点
- **副作用**：无（构建期不连库）

### 3.9 `def get_mcp_app()`

- **输出**：可挂载的 MCP Starlette 应用（懒加载单例，模块级 `_mcp_app`；**无锁**，依赖启动期单线程初始化）
- **副作用**：首次调用时构建 SSE 应用并写 `[mcp]` info 日志
- **挂载约束**（`main.py`）：`app.mount("/mcp", get_mcp_app())` 必须位于 `/static` 白名单路由**之前**，避免被子路径静态路由抢先匹配。

## 4. 边界条件与错误处理

| 场景 | 期望行为 |
|------|----------|
| 空库 | 四工具均返回空结构：`[]` / `{"total":0,"papers":[]}` / `{"error":...}` / `total=0`，不抛异常 |
| `query` 含 FTS 特殊字符（如 `"(:*^`） | 清洗后无语法错误，无命中返回 `[]` |
| 清洗后 `query` 为空串 | `search_papers` 返回 `[]`，**不回退 LIKE** |
| FTS 虚拟表缺失/执行异常 | warning 日志 + LIKE 兜底返回匹配结果 |
| `limit=0`/负数/超大 | search 钳到 `[1,50]`；list 钳到 `[1,100]` |
| `skip` 负数 | 钳到 0 |
| `status` 非法值 | 返回空列表（精确匹配无命中），不报错 |
| `paper_id` 不存在 | 返回 `{"error": "文献不存在: paper_id=N"}` |
| 文献 `abstract` 为 NULL | 简要表示中为 `""`；详情中为 `None` |
| 文献无笔记文件 | `note_path` 为 `None` |
| 挂载顺序错误（/static 在前） | 属部署错误；契约要求 /mcp 先挂载 |

## 5. 依赖

- **上游依赖**：
  - `mcp==1.3.0`（`FastMCP`、`mcp.server.sse.SseServerTransport`；宪法第 16 条锁定，配套 `sse-starlette==1.8.2`）
  - `starlette`（`Starlette` / `Route` / `Mount`，随 FastAPI 0.110 约束 <0.37）
  - `app.database.SessionLocal`、`app.models.Paper`（含 `tags` 关系）
  - `app.routers.search._sanitize_fts_query`（FTS 查询串清洗，复用防注入逻辑）
  - `papers_fts` FTS5 虚拟表（启动时由 `ensure_papers_fts()` 建表/重建）
- **下游消费者**：`app.main`（挂载 `/mcp`）；任意外部 MCP 客户端（经 SSE 连接）

## 6. 验收标准（可测试）

- [ ] AC1：`search_papers` 命中标题/摘要关键词，返回结构含 7 个键；无命中返回 `[]`
- [ ] AC2：FTS 特殊字符输入不抛异常（清洗后空结果返回 `[]`）
- [ ] AC3：`list_papers` 分页正确（skip/limit）、`total` 为过滤后总数、`status` 过滤生效
- [ ] AC4：`get_paper` 返回完整字段（含 `tags` 名称列表、`note_path` 键）；不存在时返回 `{"error": ...}` 不抛异常
- [ ] AC5：`get_library_stats` 返回 `total` 与 `by_status` 计数准确，含 `by_processed` 键
- [ ] AC6：空库下四工具均返回空结构不抛异常
- [ ] AC7：`/mcp` 已挂载到主应用（`app.routes` 存在 path 为 `/mcp` 的 Mount），且 `/api/health` 等现有路由不受影响
- [ ] AC8（未覆盖）：GET `/mcp/sse` 能完成 MCP initialize 握手，POST `/mcp/messages/` 消息可回传

## 7. 现有测试覆盖与盲区

- **已覆盖**：`backend/tests/test_mcp.py`（9 用例）
  - `test_search_papers_hit` / `no_match` / `special_chars`：命中、无命中、特殊字符清洗
  - `test_list_papers_pagination_and_filter`：分页、total、status 过滤
  - `test_get_paper_detail` / `not_found`：完整字段、tags、note_path 条件断言、error 分支
  - `test_get_library_stats`：总数与状态计数
  - `test_empty_library`：空库四工具空结构
  - `test_mcp_mounted_and_health_ok`：挂载存在且不影响现有路由
  - 测试经 monkeypatch 将 `mcp_server.SessionLocal` 替换为内存 SQLite 会话工厂，直调工具函数，不走真实 MCP 客户端
- **盲区**：
  - SSE 握手与消息回传链路（GET `/mcp/sse`、POST `/mcp/messages/`、`messages_endpoint` 完整路径写进 endpoint 事件）完全未测——测试明确绕开真实 SSE 连接 —— **高**
  - FTS 执行异常 → `_fts_search_ids` 返回 `None` → LIKE 兜底的分支未测 —— **中**
  - limit/skip 钳制边界（search 上限 50、list 上限 100、负数钳 0/1）未测 —— **中**
  - `_paper_brief` 摘要超 200 字截断 + `"..."` 未测 —— 低
  - `list_papers` 排序（`created_at DESC, id DESC`）未断言 —— 低
  - `status` 传非法值返回空列表的行为未测 —— 低
  - `by_status` / `by_processed` 中 NULL 键归入 `"unknown"` 的分支未测 —— 低
  - 工具经 FastMCP 注册后对外暴露的 schema/docstring（工具描述即 docstring 的契约）未测 —— 低
  - `note_path` 存在分支依赖真实文件系统 `notes/` 目录，测试仅做条件断言，存在环境不确定性 —— 低

## 8. 关键设计决策

- **mcp 锁 1.3.0 + 手工 SSE 应用**：更高版本 mcp 依赖 starlette≥0.49/pydantic≥2.11，与 FastAPI 0.110 硬冲突（宪法第 16 条）；1.3.0 无 `sse_app()`，故镜像 `run_sse_async()` 手工构建，并使用私有属性 `mcp._mcp_server`——升级 mcp 时必须重审 `_create_sse_app` 并 `pip check`。
- **messages_endpoint 用完整路径**：`SseServerTransport` 把端点字符串原样写进 endpoint 事件发给客户端，挂载在 `/mcp` 下时必须是 `/mcp/messages/`，否则客户端消息回传 404。这是接入时最容易踩的坑，禁止「修正」为 `/messages/`。
- **工具自建会话、不走 Depends**：MCP 请求不经 FastAPI 依赖注入，每个工具 `SessionLocal()` 自建、`finally` 关闭；全程只读，不写库。
- **FTS 三态返回**：`_fts_search_ids` 用 `[]`（清洗后为空，不回退）与 `None`（FTS 故障，回退 LIKE）区分「无查询词」与「检索基础设施故障」，保证 FTS 不可用时 MCP 检索仍可用。
- **复用路由层清洗函数**：`_sanitize_fts_query` 从 `routers.search` 导入，保证 HTTP 检索与 MCP 检索的防注入逻辑单点维护（宪法第 11 条）。
- **摘要截断 200 字**：列表/检索场景避免单条工具返回体过大（MCP 客户端上下文有限）；`get_paper` 详情不截断。
- **挂在 /static 之前**：`main.py` 中 `app.mount("/mcp", ...)` 必须先于静态白名单路由注册，改动 main.py 路由顺序时须保持此约束。
