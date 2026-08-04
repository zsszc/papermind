# routers/memory.py（Agent 记忆手动管理 HTTP 端点）规格说明书

> 本文件描述 `backend/app/routers/memory.py` 的**行为契约**（做什么），不描述实现细节。
> 依据源码实际内容反向工程整理（2026-08-04）。端点签名照抄代码。
> 服务层行为（容量淘汰、类型校验、降级语义）详见 `specs/backend/services/memory_manager.md`，本规格只描述 HTTP 包装层与其差异。

## 1. 背景与目标

`services/memory_manager.py` 提供四类 Agent 记忆的统一读写 API，但对话链路只自动写短期记忆；长期记忆（long_term / preference / fact）没有任何自动写入机制。本路由把「手动管理记忆」暴露为 HTTP 端点，供前端设置/记忆面板让用户查看、补录、删除记忆，是长期记忆目前**唯一的写入入口**。

路由是薄包装层：GET 直接查 ORM（不经 Manager），POST 委托 `MemoryManager.add_long_term_memory`，DELETE 自行重实现按 id 删除。三端点与 Manager 均存在行为差异（见第 3、4 节）。

## 2. 范围

### 2.1 包含

- `GET /api/memory/memories`：按类型列出记忆
- `POST /api/memory/memories`：手动写入一条记忆（查询参数传参）
- `DELETE /api/memory/memories/{memory_id}`：按 id 删除
- 与 `MemoryManager` 统一 API 的行为差异契约

### 2.2 非目标

- 短期记忆的自动滚动更新（`update_short_term_memory`，归 `routers/chat.py` 写路径与 memory_manager 服务规格）
- 记忆注入 prompt 的读路径（`build_memory_context`，归 `services/agent_graph.py`）
- 清空某类型全部记忆：`MemoryManager.clear_memory` 未暴露任何 HTTP 端点
- 记忆的修改（PUT/PATCH）：不存在更新端点，改记忆只能删后重录

## 3. 行为契约

路由注册：`app.include_router(memory.router, prefix="/api/memory", tags=["memory"])`（`main.py`）。

### 3.1 `GET /memories` → `list_memories(memory_type: str = None, db: Session = Depends(get_db))`

- **输入**：查询参数 `memory_type`（可选，字符串）
- **输出**：`MemorySummary` ORM 列表，JSON 序列化字段为 `id / memory_type / content / source_conversation_id / importance / created_at / updated_at`（datetime 转 ISO 字符串）；无数据返回 `[]`
- **行为**：
  - 不传 `memory_type` → 返回全表；
  - 传入则按 `memory_type` 精确等值过滤；
  - **排序固定为 `created_at` 降序，无 `id` 次序兜底，不支持 `limit`**——与 Manager 的 `get_memory` 两处不同：Manager 全类型读取时带 `id` 降序次级排序、长期类按 `importance` 降序优先，本端点一律只看时间。
- **与 Manager 的差异**：**不校验 `memory_type` 合法性**——传非法值（如 `bogus`）不会抛 `ValueError`，而是静默匹配不到任何行，返回空列表
- **副作用**：无（只读）
- **异常**：无显式处理；DB 异常 → 全局异常处理 → 500 通用文案

### 3.2 `POST /memories` → `add_memory(content: str, memory_type: str = "fact", importance: int = 5, db: Session = Depends(get_db))`

- **输入**：三个参数均为**查询参数**（FastAPI 对标量类型的默认绑定，非 JSON body——前端 `api.js` 的 `addMemory` 以 `api.post(url, null, { params })` 调用，证实此契约）：
  - `content`（必填，字符串）：记忆内容，Manager 侧 `strip()` 后入库；
  - `memory_type`（默认 `"fact"`）：须 ∈ `("short_term", "long_term", "preference", "fact")`；
  - `importance`（默认 `5`，整数）：**无范围校验**，任意整数可入库。
- **输出**：
  - 成功：写入的 `MemorySummary` JSON（含 `id`），HTTP 200；
  - **Manager 数据库降级时返回 `null`**（`add_memory` 内部 DB 异常返回 `None`，路由原样返回，HTTP 200 + 响应体 `null`）——调用方无法用状态码区分「写成功」与「写失败被降级」。
- **后置条件**：写入成功后 Manager 对该类型执行容量淘汰（short_term 删最旧；长期类删重要性最低者，详见服务规格 3.6）
- **异常**：
  - 非法 `memory_type` 或空/纯空白 `content` → Manager 抛 `ValueError`，**路由未捕获** → 全局异常处理 → **HTTP 500 通用文案**（`error_code: internal_error`，宪法第 13 条）——语义上是客户端输入错误（400 系），实际返回 500；
  - 缺 `content` 参数 → FastAPI 422。
- **副作用**：DB 写入并提交；可能触发同类型旧记录淘汰删除

### 3.3 `DELETE /memories/{memory_id}` → `delete_memory(memory_id: int, db: Session = Depends(get_db))`

- **输入**：路径参数 `memory_id`（整数）
- **输出**：`{"status": "ok"}`
- **行为**：查 `MemorySummary` 按 id 取首条；不存在 → 抛 `HTTPException(404, detail="Memory not found")`（英文文案，HTTPException detail 会原样返给客户端）；存在 → `db.delete` + `commit`
- **与 Manager 的差异**：**未复用 `mgr.delete_memory`**，逻辑重复实现（Manager 版不存在时返回 `False` 而非 404）；两处删除逻辑独立演进存在漂移风险
- **副作用**：DB 删除并提交
- **异常**：`memory_id` 非整数 → FastAPI 422；不存在 → 404

### 3.4 死导入

模块导入 `typing.List` 与 `app.schemas.ConversationResponse`，函数体均未引用，属死导入（现状记录，不代表契约）。

## 4. 边界条件与错误处理

| 场景 | 期望行为 |
|------|----------|
| GET 不传 `memory_type` | 返回全表，按 `created_at` 降序 |
| GET 传非法 `memory_type`（如 `bogus`） | **不报错**，返回 `[]`（与 Manager `get_memory` 抛 `ValueError` 的行为相反） |
| GET 传空串 `memory_type=` | falsy，等同不传，返回全表 |
| POST 非法 `memory_type` | `ValueError` → 全局 500 通用文案（非 400/422） |
| POST 空 / 纯空白 `content` | 同上，500 |
| POST 缺 `content` | 422（FastAPI 参数校验） |
| POST `importance` 越界（0、负数、>10） | 无校验，原样入库，仅影响排序与淘汰 |
| POST 时 DB 异常 | Manager 降级返回 `None` → HTTP 200 + 响应体 `null` |
| POST 用 JSON body 传参 | body 被忽略，参数取自 query string；缺 `content` 时报 422 |
| DELETE 不存在的 id | 404，`detail: "Memory not found"` |
| DELETE 非整数 id | 422 |
| 并发 POST 同类型记忆 | 各自触发容量淘汰；SQLite 单写入者下串行提交，无显式锁 |

## 5. 依赖

- **上游依赖**：`app.database.get_db`；`app.models.MemorySummary`；`app.services.memory_manager.MemoryManager`；全局异常处理（`main.py`，把 `ValueError` 脱敏为 500 通用文案）
- **下游消费者**：前端 `api.js` 的 `listMemories` / `addMemory` / `deleteMemory`（记忆管理面板）；无其他后端调用方

## 6. 验收标准（可测试）

- [ ] AC1：`GET /api/memory/memories` 无参返回全表（按 `created_at` 降序）；带 `memory_type` 时只返回该类型
- [ ] AC2：GET 传非法 `memory_type` 返回 200 + `[]`，不抛错
- [ ] AC3：POST 合法输入（query 参数）写入成功，响应含 `id`，内容经 `strip()`；随后该类型超容量时自动淘汰
- [ ] AC4：POST 非法 `memory_type` / 空内容 → 500 且响应体不含异常原文（`error_code: internal_error`）
- [ ] AC5：POST 缺 `content` → 422
- [ ] AC6：DELETE 存在的 id → 200 `{"status":"ok"}` 且记录消失；DELETE 不存在的 id → 404

## 7. 现有测试覆盖与盲区

- **已覆盖**：**零**——`backend/tests/test_memory.py`（28 例）全部直测 `MemoryManager` 服务层，无任何用例经 TestClient 命中 `/api/memory/*`；grep 确认全部测试文件中无 `api/memory` 字样
- **盲区**：
  - 三个端点全部行为（AC1–AC6）无端到端测试：GET 不校验类型静默返回 `[]`、POST 非法输入变 500、POST 成功返回结构、DELETE 404 均无固化（**中**，HTTP 契约与 Manager 的差异点全部无保障）
  - POST 以查询参数传参（而非 JSON body）这一非直觉契约无测试（**中**，重构为 body 模型时无任何告警）
  - Manager 降级返回 `None` → HTTP 200 + `null` 的「假成功」响应无测试（**中**，前端无法感知写失败的现状被固化风险）
  - GET 排序仅按 `created_at` 降序（与 Manager 排序规则不同）无测试（**低**）
  - 死导入（`List` / `ConversationResponse`）无静态检查拦截（**低**）

## 8. 关键设计决策

- **薄包装层、逻辑下沉 Manager**：POST 复用 `add_long_term_memory` 以自动获得类型校验、strip、容量淘汰；代价是 Manager 的 `ValueError` 在 HTTP 层一律坍缩为 500（输入错误与服务器错误不分），改造时应先把非法输入映射为 400/422 并补测试
- **GET 绕开 Manager 直接查 ORM**：历史原因（Manager 统一 API 是后加的，路由未跟进），造成排序规则与类型校验两处差异；按宪法第 20 条以代码现状为准记录，统一前应补 AC2 测试
- **DELETE 重复实现而非复用 `mgr.delete_memory`**：同样是 Manager 后加未跟进；两处「不存在」语义不同（404 vs `False`），目前以路由的 404 为对外契约
- **查询参数传参**：FastAPI 标量默认绑定的自然结果，前端已按此对接；任何「改成 JSON body」的重构都是破坏性变更，需同步改前端 `addMemory`
