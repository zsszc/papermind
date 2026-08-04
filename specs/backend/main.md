# main.py（FastAPI 应用入口）规格说明书

> 本文档描述 `backend/app/main.py` 的**行为契约**：lifespan 启动序列、CORS 中间件、路由与子应用挂载顺序、全局异常处理、健康检查接口、每日自动备份调度。
> 依据源码全文反向工程（121 行）。

## 1. 背景与目标

`main.py` 是整个后端单进程应用的组装入口：创建 FastAPI 应用实例、按严格顺序执行启动初始化（配置校验 → 建表/迁移 → FTS → LLM 健康检查 → 备份线程）、挂 CORS 白名单中间件、按 `/api/*` 前缀挂载 7 个业务路由、在静态白名单路由之前挂载 `/mcp` 子应用，并提供全局异常脱敏与 `/api/health` 健康检查。它是宪法「单进程架构」「异常脱敏」「CORS 白名单」「静态文件白名单」条款的落地点。

## 2. 范围

### 2.1 包含

- `lifespan` 启动序列的确切步骤与顺序、失败语义、关闭语义。
- CORS 中间件配置（白名单 origin、方法、头、凭证策略）。
- 7 个业务路由的挂载前缀、`/mcp` 子应用挂载及其与 `/static` 的顺序约束、`/static` 白名单路由挂载。
- `global_exception_handler` 的响应契约。
- `GET /api/health` 的响应契约。
- `_schedule_daily_backup()` 的调度与容错行为。

### 2.2 非目标

- 不描述各 router 内部端点（归各 router 规格）。
- 不描述 `routers/static.py` 的白名单解析细节、`services/mcp_server.py` 的工具实现、`services/backup.py` 的备份格式、`core/settings.py` 的校验规则——仅描述 main.py 对它们的**调用契约与顺序**。
- 不涉及 uvicorn 进程启动参数（归部署文档）。

## 3. 行为契约

### 3.1 `lifespan(app: FastAPI)`（`@asynccontextmanager`）

启动阶段按以下**确切顺序**执行，任何一步抛异常都会中止应用启动：

1. `apply_env_overrides(config)` —— 应用 `PAPERMIND_*` 环境变量覆盖到配置单例。
2. `validate_startup_config(config)` —— 启动校验（失败即抛异常、进程无法启动）。
3. `Base.metadata.create_all(bind=engine)` —— 按 ORM 元数据建缺失表。
4. `ensure_schema()` —— 轻量迁移（手工 `ALTER TABLE` 分支）。
5. `ensure_papers_fts(engine)` —— 建 `papers_fts` FTS5 虚拟表与同步触发器并 rebuild。
6. `logger.info("[startup] 数据库表结构检查完成")`。
7. `llm_status = await llm_service.health_check()` —— LLM 健康检查（真实网络调用 Kimi API）。
8. `app.state.llm_ready = llm_status["ok"]` —— 结果写入应用状态；失败时 `logger.warning("[startup] LLM 服务未就绪: ...")`，成功时 `logger.info("[startup] LLM 服务检测通过")`。**LLM 不可用不阻止启动**（降级运行，`llm_ready=False`）。
9. `_schedule_daily_backup()` —— 启动每日备份后台线程。
10. `logger.info("[startup] 每日自动备份已启动")`。
11. `yield` —— 进入服务运行期。

- **前置条件**：`config.yaml`（或回退 `config.yaml.example`）可读；SQLite 路径可写；`validate_startup_config` 通过。
- **后置条件**：数据库表结构就绪；`app.state.llm_ready` 有布尔值；备份守护线程已启动。
- **副作用**：日志写入 `logs/app.log`；可能修改数据库 schema；发起一次真实 LLM 网络调用；创建 daemon 线程。
- **关闭语义**：`yield` 之后**无任何清理代码**——备份线程是 daemon 线程随进程退出，无显式 shutdown 钩子。
- **异常**：步骤 1–5 抛异常 → 启动失败；步骤 7 的 LLM 异常被 `health_check()` 内部收敛为 `{"ok": False, "error": ...}`，不上抛。

### 3.2 `_schedule_daily_backup()`

- 启动名为 `daily-backup` 的 daemon 后台线程，循环执行：
  1. 计算「今天或明天凌晨 3:00:00」的下一个触发点；
  2. `time.sleep()` 到该时刻；
  3. 依次调用 `auto_backup()` 与 `cleanup_old_backups(keep=10)`（保留最近 10 份）；
  4. 若备份抛异常，`logger.warning(f"[backup] 定时备份失败: {e}")` 后**继续下一轮循环**（单次失败不杀死线程）。
- **副作用**：后台线程常驻；每日产生 `backups/` 目录写入与旧备份清理。
- **注意**：调度依赖本机系统时间；线程内无锁、无幂等保护，单 worker 部署下安全。

### 3.3 应用实例

```python
app = FastAPI(
    title="PaperMind API",
    description="PaperMind 本地文献知识库后端 API",
    version=config.get("app.version", "1.0.0"),
    lifespan=lifespan,
)
```

- 版本号取自配置 `app.version`，缺省 `"1.0.0"`。

### 3.4 CORS 中间件

```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", "null"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)
```

- **契约**：仅上述 3 个 Origin 获得 `Access-Control-Allow-Origin` 回显；`"null"` 是 Electron 生产包以 `file://` 加载前端时 fetch 携带的 Origin，必须显式放行。
- 不携带凭证（`allow_credentials=False`）；非白名单 Origin 的响应不含 CORS 头（浏览器侧拦截）。
- 允许的 HTTP 方法为显式 6 项；允许任意请求头。

### 3.5 路由与子应用挂载（顺序即代码顺序）

| 顺序 | 挂载 | 前缀 | 说明 |
|------|------|------|------|
| 1 | `papers.router` | `/api/papers` | 文献 CRUD/上传/批量/标注 |
| 2 | `search.router` | `/api/search` | 检索 |
| 3 | `chat.router` | `/api/chat` | SSE 对话/会话/图片分析 |
| 4 | `thesis.router` | `/api/thesis` | 大论文/引用 |
| 5 | `memory.router` | `/api/memory` | Agent 记忆 |
| 6 | `export.router` | `/api/export` | 导出/手动备份 |
| 7 | `settings.router` | `/api/settings` | 设置 |
| 8 | `app.mount("/mcp", get_mcp_app())` | `/mcp` | MCP Server 子应用（SSE 传输；子路由 `/mcp/sse` 长连接与 `/mcp/messages/` 回传；`get_mcp_app()` 懒加载单例，import 时即初始化） |
| 9 | `static.router` | （无前缀，路由为 `/static/{file_path:path}`） | 白名单静态文件 |

- **顺序约束（硬契约）**：`/mcp` 挂载**必须**位于 `static.router` 之前。`/static/{file_path:path}` 是路径通配路由，虽路径前缀不同不会直接吃掉 `/mcp`，但代码注释明确要求 `/mcp` 在前以避免被子应用/静态路由匹配顺序问题影响；改动挂载顺序属高危操作。
- `/mcp` 暴露在 API 前缀之外，不经 CORS 白名单约束之外的额外保护——仅限本地使用。

### 3.6 `global_exception_handler(request: Request, exc: Exception)`

`@app.exception_handler(Exception)`，捕获一切未处理异常：

- **日志**：`logger.exception(f"[api] 未处理异常: {request.method} {request.url.path}")`（含完整堆栈，写入 `logs/app.log`）。
- **响应**：HTTP 500，JSON 体固定为：

```json
{
  "detail": "服务器内部错误，请稍后重试",
  "error_code": "internal_error",
  "path": "<请求路径>"
}
```

- **脱敏契约**：响应体绝不包含异常原文/堆栈（宪法第 13 条）。
- 不拦截 `HTTPException`（FastAPI 对 `HTTPException` 有专属处理器，优先级更高）与请求校验错误（422）。

### 3.7 `GET /api/health`

```python
{
    "status": "ok",
    "version": config.get("app.version", "1.0.0"),
    "llm_ready": getattr(app.state, "llm_ready", False),
}
```

- **语义**：进程存活即 `status: "ok"`（不做深度检查）；`version` 与配置一致；`llm_ready` 反映 lifespan 第 8 步写入的状态，**lifespan 未运行时（如测试 TestClient 不作上下文管理器）缺省为 `False`**。
- 无认证、无条件返回 200。

## 4. 边界条件与错误处理

| 场景 | 期望行为 |
|------|----------|
| LLM API 不可达 / Key 无效 | 启动继续，`llm_ready=False`，仅 warning 日志；`/api/health` 如实反映 |
| `validate_startup_config` 校验失败 | 启动中止（异常上抛，uvicorn 退出） |
| 数据库文件不可写 / 迁移 SQL 失败 | 启动中止 |
| 业务路由抛出未处理异常 | 500 + 固定脱敏 JSON；详情只进 `logs/app.log` |
| 备份线程内 `auto_backup()` 抛异常 | warning 日志后继续下一轮，线程不死 |
| 非白名单 Origin 跨域请求 | 响应无 CORS 头，浏览器拦截（服务端仍执行请求） |
| 请求路径不存在 | FastAPI 默认 404（不经全局异常处理器） |
| 请求体校验失败 | FastAPI 默认 422（不经全局异常处理器） |
| Electron `file://` 前端（Origin 为 `null`） | CORS 显式放行 |
| lifespan 未执行（测试环境） | `/api/health` 仍 200，`llm_ready` 缺省 `False` |

## 5. 依赖

- **上游依赖**：`app.database`（`engine`/`Base`/`ensure_schema`）、`app.core.config`（`config` 单例）、`app.core.logger`、`app.models.ensure_papers_fts`、`app.services.llm.llm_service`、`app.services.backup`（`auto_backup`/`cleanup_old_backups`）、`app.core.settings`（`apply_env_overrides`/`validate_startup_config`）、`app.services.mcp_server.get_mcp_app`、8 个 router 模块。
- **下游消费者**：uvicorn（`app.main:app`）、Electron 主进程（spawn `python -m uvicorn`）、`backend/tests/conftest.py`（TestClient 导入 `app`，不触发 lifespan）。

## 6. 验收标准（可测试）

- [ ] AC1：`GET /api/health` 返回 200 且含 `status="ok"`、`version`、`llm_ready` 三键。
- [ ] AC2：白名单 Origin（`http://localhost:5173`）的响应回显 `Access-Control-Allow-Origin`；非白名单 Origin 不回显；预检响应不含 `allow-credentials: true`。
- [ ] AC3：任意未处理异常返回 500 + `{detail, error_code: "internal_error", path}`，响应体不含异常原文。
- [ ] AC4：`/mcp` 已挂载（`app.routes` 中存在 `/mcp`），且挂载不影响既有路由。
- [ ] AC5：lifespan 启动后 `app.state.llm_ready` 为布尔值；LLM 不可用时为 `False` 且进程仍正常服务。
- [ ] AC6：lifespan 各步骤按第 3.1 节顺序执行（可通过 mock 各依赖记录调用顺序验证）。
- [ ] AC7：备份线程在备份异常时不退出（mock `auto_backup` 抛错后线程仍存活并进入下一轮等待）。

## 7. 现有测试覆盖与盲区

- **已覆盖**：
  - `test_health.py::test_health` —— `/api/health` 三键存在（不校验 `llm_ready` 具体值）。
  - `test_security.py::TestCors`（3 用例）—— 白名单 Origin 回显、非白名单不回显、凭证禁用。
  - `test_security.py::TestGlobalExceptionHandler`（2 用例）—— handler 直调 + 临时 app 端到端，验证脱敏与固定结构。
  - `test_security.py::TestStaticTraversal` / `TestStaticWhitelist`（11 用例）—— 静态白名单与穿越防护（经由 main.py 挂载的 static 路由）。
  - `test_mcp.py::test_mcp_mounted_and_health_ok` —— `/mcp` 存在于 `app.routes` 且 health 正常。
- **盲区**：
  - **整个 lifespan 启动序列零覆盖**（conftest 的 TestClient 故意不触发 lifespan）：步骤顺序、`apply_env_overrides`/`validate_startup_config` 调用、`ensure_schema`/`ensure_papers_fts` 执行、`app.state.llm_ready` 写入均无测试（高）。
  - `_schedule_daily_backup` 的调度计算（次日 3 点）、异常容错（失败后继续循环）、`cleanup_old_backups(keep=10)` 调用均无测试（高）。
  - LLM 健康检查失败路径（`llm_ready=False` 时应用仍可用、`/api/health` 如实上报）无测试（中）。
  - CORS 的 `"null"` Origin（Electron `file://` 场景）无测试用例（中）。
  - `allow_methods` 白名单边界（如 PATCH 放行、未列出方法的处理）无测试（低）。
  - 全局异常处理未在**真实主 app** 上端到端验证（现有用例用的是临时 FastAPI 实例 + 直调 handler）（低）。
  - `/mcp` 挂载与 `/static` 通配路由的匹配顺序约束无回归测试（低，但属注释标注的高危点）。

## 8. 关键设计决策

- **启动顺序即依赖序**：配置校验最先（失败早死）、数据库次之、LLM 检查靠后且容错（本地优先原则——LLM 是唯一的云依赖，不可用不应拖垮本地功能）、备份线程最后。
- **`/mcp` 必须先于 `/static` 挂载**：`/static/{file_path:path}` 是通配路由，注释明确要求 MCP 子应用在前，防止挂载顺序变化导致 MCP 端点被抢先匹配。
- **CORS 显式三元组白名单**：dev 前端（localhost/127.0.0.1:5173）+ Electron 生产包的 `"null"` Origin；`allow_credentials=False` 配合单用户零权限模型，同时宪法第 15 条禁止引导公网暴露。
- **异常脱敏集中在全局 handler**：响应只含通用文案 + `error_code` + `path`，堆栈只进 `logs/app.log`（宪法第 13 条）；`HTTPException` 与 422 交给 FastAPI 默认处理器，保持业务错误的可读性。
- **备份用裸 daemon 线程而非任务队列**：宪法第 4 条单进程架构，不引入 Celery/APScheduler；`while True + sleep 到 3:00` 是最小实现，daemon 线程随进程退出故无需 shutdown 钩子。
- **测试不触发 lifespan 是刻意取舍**：conftest 注释明确「不作为上下文管理器使用」以保证离线、快速、不打真实 LLM——代价是启动序列完全无回归保护，构成已知盲区。
