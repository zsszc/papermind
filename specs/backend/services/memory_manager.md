# services/memory_manager.py（Agent 记忆管理 MemoryManager）规格说明书

> 本文件描述 `backend/app/services/memory_manager.py` 的**行为契约**（做什么），不描述实现细节。
> 依据源码实际内容反向工程整理（2026-08-04）。函数签名照抄代码。

## 1. 背景与目标

PaperMind 的对话 Agent 需要跨会话记住用户背景（研究方向、偏好、关注主题），否则每轮对话都是「失忆」状态。本模块把 Agent 记忆统一到一张 `memory_summaries` 表上，以 `memory_type` 区分四类记忆，提供统一的读 / 写 / 清 / 删 API，并解决三个工程问题：

1. **容量控制**：每类记忆设上限，写入后自动淘汰，防止无限增长拖慢 prompt 注入与数据库；
2. **短期记忆自动滚动**：对话每累积 5 条消息，用 LLM 生成一句话摘要并覆盖更新该会话的短期记忆；
3. **失败不阻塞主流程**：LLM 或数据库异常一律降级（返回空串 / 跳过 / 返回 None），对话链路永不被记忆模块拖死。

## 2. 范围

### 2.1 包含

- 模块常量：`VALID_MEMORY_TYPES` / `LONG_TERM_TYPES` / `DEFAULT_CAPACITY_LIMITS`
- `MemoryManager` 统一读写 API：`get_memory` / `add_memory` / `clear_memory` / `delete_memory`
- 容量淘汰策略（`_enforce_capacity`）
- LLM 摘要链路：`summarize_conversation` / `update_short_term_memory`（含降级语义）
- Prompt 注入：`build_memory_context`
- 兼容旧接口：`get_recent_short_term_memories` / `get_long_term_memories` / `add_long_term_memory`
- 与对话链路的集成契约（`routers/chat.py` 写路径、`services/agent_graph.py` 读路径）

### 2.2 非目标

- HTTP 路由层 `routers/memory.py` 的完整契约（仅在第 4、5 节标注其与 Manager 的行为差异）
- 长期记忆（long_term / preference / fact）的**自动**抽取：当前代码无任何自动写入这三类的机制，只能经 HTTP 手动 POST 或调用方显式调 `add_memory` / `add_long_term_memory`
- 记忆的向量化 / 语义检索：记忆只做线性读取与 prompt 拼接，不进 ChromaDB
- 跨用户 / 权限隔离：单用户零权限（宪法第 2 条），全表即「该用户」全部记忆

## 3. 行为契约

### 3.0 模块常量

- `VALID_MEMORY_TYPES = ("short_term", "long_term", "preference", "fact")`：合法记忆类型，全部写路径强校验。
- `LONG_TERM_TYPES = ("long_term", "preference", "fact")`：「长期类」聚合读取时使用（见 3.10）。
- `DEFAULT_CAPACITY_LIMITS = {"short_term": 20, "long_term": 100, "preference": 50, "fact": 200}`：各类默认容量上限。

四类记忆语义（依据代码中使用方式归纳）：

| 类型 | 语义 | 默认容量 | 淘汰规则 |
|------|------|---------|---------|
| `short_term` | 近期对话的一句话摘要，每会话至多一条、随对话滚动覆盖 | 20 | 删最旧 |
| `long_term` | 长期研究主题 / 背景 | 100 | 删重要性最低、其次最旧 |
| `preference` | 用户偏好（如「偏好中文回答」） | 50 | 同上 |
| `fact` | 事实性记忆（HTTP 手动写入的默认类型） | 200 | 同上 |

### 3.1 `MemoryManager.__init__(self, db: Session, capacity_limits: Optional[Dict[str, int]] = None)`

- **输入**：`db` SQLAlchemy 会话；`capacity_limits` 可选，按类型覆盖默认上限（浅合并进默认字典的副本，不污染模块级常量）
- **输出**：Manager 实例；同一 `db` 可构造多个实例，互不影响
- **副作用**：无

### 3.2 `get_memory(self, memory_type: Optional[str] = None, limit: Optional[int] = None) -> List[MemorySummary]`

- **输入**：`memory_type=None` 表示读全部类型；`limit` 为 falsy（`None`/`0`）时不限量
- **输出**：`MemorySummary` 列表。排序规则：
  - `memory_type=None`：按 `created_at` 降序、`id` 降序（不区分类型、不按重要性）；
  - `short_term`：按 `created_at` 降序、`id` 降序（最新在前）；
  - 其余三类：按 `importance` 降序，其次 `created_at` 降序、`id` 降序。
- **异常**：`memory_type` 非 None 且不在 `VALID_MEMORY_TYPES` → 抛 `ValueError`
- **副作用**：无（只读）

### 3.3 `add_memory(self, memory_type: str, content: str, importance: int = 5, source_conversation_id: Optional[int] = None, metadata: Optional[Dict[str, Any]] = None) -> Optional[MemorySummary]`

- **输入**：`importance` 默认 5（模型注释称 1–10，但**代码不做范围校验**，任意整数可入库）；`metadata` 为预留参数——`MemorySummary` 模型无对应列，**当前被忽略、不入库**
- **输出**：成功返回写入的 `MemorySummary`（含 `id`）；数据库异常时返回 `None`（**不抛出**）
- **前置处理**：`content` 先 `strip()` 后入库
- **后置条件**：写入提交后立即对该类型执行 `_enforce_capacity` 容量淘汰
- **异常**：
  - 非法 `memory_type` → 抛 `ValueError`（在进入 try 之前，不会触发 rollback 降级）
  - 空内容或纯空白内容 → 抛 `ValueError("记忆内容不能为空")`
  - 其他数据库异常 → `rollback()` + `logger.error` + 返回 `None`
- **副作用**：DB 写入并提交；可能触发同类型内若干条旧记录被删除

### 3.4 `clear_memory(self, memory_type: str) -> int`

- **输出**：实际删除条数；重复清空返回 `0`
- **异常**：非法 `memory_type` → 抛 `ValueError`
- **副作用**：删除该类型全部记录并提交（`synchronize_session=False` 批量删除）

### 3.5 `delete_memory(self, memory_id: int) -> bool`

- **输出**：删除成功 `True`；`memory_id` 不存在返回 `False`（不视为错误）
- **副作用**：DB 删除并提交

### 3.6 `_enforce_capacity(self, memory_type: str) -> None`

- **触发时机**：仅由 `add_memory` 写入成功后调用（`clear_memory` / `delete_memory` / 直接改库不触发）
- **跳过条件**：该类型上限缺失或 `<= 0` → 直接返回；总数 `<= limit` → 直接返回
- **淘汰规则**：
  - `short_term`：按 `created_at` 升序、`id` 升序取最旧的 `total - limit` 条删除；
  - 其余三类：按 `importance` 升序，其次 `created_at` 升序、`id` 升序（即重要性最低者先走，平局时最旧的先走）。
- **后置条件**：淘汰后该类型条数恰为 `limit`；写 `logger.info` 淘汰日志
- **副作用**：DB 批量删除并提交

### 3.7 `async summarize_conversation(self, conversation_id: int) -> str`

- **输入**：会话 ID（不校验会话是否存在，无消息时按「消息不足」处理）
- **输出**：一句话摘要字符串；**以下情况一律返回 `""`**：
  - 会话消息总数 `< 5`；
  - LLM 调用抛任何异常（记 `logger.error`，不向上抛）；
  - LLM 返回 `None` 或空白（`(result or "").strip()`）。
- **摘要取材**：按时间升序取全部消息后，仅取**最后 10 条**，每条内容截断到前 500 字符，拼为 `role: content` 行；system 提示词固定为「你是对话摘要助手。」
- **副作用**：网络调用 LLM（经 `llm_service.chat_completion`，宪法第 8 条唯一入口）；无 DB 写入

### 3.8 `async update_short_term_memory(self, conversation_id: int) -> None`

- **触发条件**：会话消息总数 `> 0` 且为 **5 的倍数**，否则直接返回（不调 LLM）
- **行为**：调 `summarize_conversation` 取摘要；摘要为空 → 不写库；非空时：
  - 已存在 `memory_type="short_term"` 且 `source_conversation_id` 相同的记录 → **原地更新** `content`（每会话至多一条短期记忆；`created_at` 不变，`updated_at` 由 ORM `onupdate` 刷新）；
  - 否则走 `add_memory("short_term", summary, source_conversation_id=...)` 新建（自动应用容量淘汰）。
- **异常兜底**：任何异常（LLM、数据库）→ `rollback()` + `logger.error` + 静默返回，**绝不向调用方抛出**
- **副作用**：可能 LLM 调用 + DB 写入/更新

### 3.9 `build_memory_context(self) -> str`

- **输出**：注入 prompt 的记忆文本，由至多两个段落组成、以 `\n\n` 连接：
  - 「用户背景与偏好：\n- …」——来自 `get_long_term_memories(limit=5)`（聚合 long_term / preference / fact 三类，按重要性降序）；
  - 「近期讨论主题：\n- …」——来自 `get_memory("short_term", limit=3)`（最新 3 条短期摘要）。
- 任一段落无数据则省略该段落；全无记忆返回 `""`
- **副作用**：无（只读）

### 3.10 兼容旧接口（签名保持不变，内部走统一 API）

- `get_recent_short_term_memories(self, limit: int = 5) -> List[MemorySummary]`：等价 `get_memory("short_term", limit=limit)`
- `get_long_term_memories(self, limit: int = 10) -> List[MemorySummary]`：**聚合** `LONG_TERM_TYPES` 三类（不含 short_term），按 `importance` 降序、其次 `created_at` 降序，限量 `limit`
- `add_long_term_memory(self, content: str, memory_type: str = "fact", importance: int = 5) -> Optional[MemorySummary]`：等价 `add_memory(memory_type, content, importance=importance)`，默认类型 `fact`

### 3.11 与对话链路的集成契约

- **写路径**（`routers/chat.py` 的 `POST /api/chat`）：保存 user 消息并 `flush` 后，立即 `await MemoryManager(db).update_short_term_memory(conv.id)`，外层另有 try/except 兜底记 `[chat]` 日志——即记忆更新发生在**本轮编排之前**、判定条件里的消息数**含刚存入的 user 消息**；记忆失败不影响后续回复生成。
- **读路径**（`services/agent_graph.py` 的 `load_memory` 节点）：每轮对话调用 `MemoryManager(db).build_memory_context()`；非空时把基础 system prompt 追加为 `SYSTEM_PROMPT + "\n\n以下是关于用户的背景记忆，请在回答时参考：\n\n" + memory_context`，空串时 system prompt 原样不变。

## 4. 边界条件与错误处理

| 场景 | 期望行为 |
|------|----------|
| 非法 `memory_type`（读/写/清） | 抛 `ValueError`，错误信息列出合法值 |
| 空内容 / 纯空白内容写入 | 抛 `ValueError("记忆内容不能为空")` |
| 内容首尾带空白 | `strip()` 后入库 |
| `limit=0` 或 `None` | falsy 语义，**不限量**（无法表达「取 0 条」） |
| `importance` 越界（0、负数、>10） | 无校验，原样入库；仅影响排序与淘汰 |
| `metadata` 传参 | 被静默忽略，不入库（模型无列，接口预留） |
| 写入时 DB 异常 | rollback + 记日志 + 返回 `None`，不抛出 |
| 容量上限设为 `0`/负数/缺失 | 跳过淘汰，该类型可无限增长 |
| 淘汰时重要性相同 | 平局按 `created_at` 升序、再按 `id` 升序，最旧先删 |
| 会话不存在 / 消息 <5 条 | `summarize_conversation` 返回 `""` |
| 消息数非 5 的倍数 | `update_short_term_memory` 直接返回，不调 LLM |
| LLM 异常 / 返回空 | 摘要不写库，无脏数据，无异常外抛 |
| 同一会话第二次触发摘要 | 更新已有记录而非新增（每会话至多一条 short_term） |
| 无任何记忆 | `build_memory_context` 返回 `""`，system prompt 不追加记忆段 |
| HTTP 层差异（`routers/memory.py`，供参考） | GET 不校验 `memory_type`（非法类型静默返回空列表）；POST 的 `ValueError` 未被捕获，经全局异常处理返回 500 通用文案；DELETE 找不到时返回 404，且未复用 `mgr.delete_memory`（逻辑重复实现） |

## 5. 依赖

- **上游依赖**：`sqlalchemy.orm.Session`；`app.models.MemorySummary` / `Message`；`app.services.llm.llm_service`（LLM 唯一入口）；`app.core.logger`
- **下游消费者**：
  - `routers/chat.py`（`POST /api/chat`：写路径触发短期记忆滚动更新）
  - `services/agent_graph.py`（`load_memory` 节点：读路径注入 system prompt）
  - `routers/memory.py`（`GET/POST/DELETE /api/memory/memories*`：手动管理）
  - 前端 `api.js` 的 `listMemories` / `addMemory` / `deleteMemory`

## 6. 验收标准（可测试）

- [ ] AC1：四类记忆可分别写入并按类型读取；`memory_type=None` 时返回全部类型（按时间倒序）
- [ ] AC2：`short_term` 读取按时间倒序；长期类读取按 `importance` 降序、其次时间倒序
- [ ] AC3：非法 `memory_type` 与空内容均抛 `ValueError`
- [ ] AC4：`clear_memory` 返回删除条数且只清指定类型；`delete_memory` 不存在时返回 `False`
- [ ] AC5：超出容量时 `short_term` 淘汰最旧、长期类淘汰重要性最低者；淘汰只发生在同类型内
- [ ] AC6：消息数非 5 倍数不触发 LLM；达到 5 倍数时写入摘要，同会话再次触发时更新已有记录
- [ ] AC7：LLM 异常 / 返回空 / 消息不足时降级为空串或跳过，不向调用方抛异常、不留脏数据
- [ ] AC8：`build_memory_context` 含「用户背景与偏好」「近期讨论主题」两段；无记忆返回 `""`
- [ ] AC9：兼容接口行为与统一 API 一致（`add_long_term_memory` 默认 `fact`；`get_long_term_memories` 聚合三类且不含 short_term）
- [ ] AC10：对话编排图中，存在记忆时 system prompt 以基础 prompt 开头并含记忆内容

## 7. 现有测试覆盖与盲区

- **已覆盖**（`backend/tests/test_memory.py`，28 个用例，内存 SQLite + mock LLM）：
  - `TestUnifiedAPI`（9 例）：分类型读写、全类型读取排序、short_term 时间倒序、长期类重要性排序、limit、非法类型、空内容、清空计数、按 id 删除 → 覆盖 AC1–AC4
  - `TestCapacityEviction`（4 例）：short_term 淘汰最旧、长期类淘汰低重要性、类型间隔离、默认上限存在 → 覆盖 AC5
  - `TestTypeIsolation`（2 例）：四类读写隔离、清一类不影响其他
  - `TestLegacyCompat`（4 例）：三个兼容接口 → 覆盖 AC9
  - `TestBuildMemoryContext`（2 例）：两段齐全、空记忆返回空串 → 部分覆盖 AC8
  - `TestLLMDegradation`（7 例）：消息不足、摘要成功、LLM 失败降级、非 5 倍数跳过、写入与滚动更新、失败不写脏数据、空摘要不写入 → 覆盖 AC6、AC7
  - 另有 `tests/test_agent_graph.py::test_outputs_three_elements` 验证记忆内容进入 system prompt → 覆盖 AC10
- **盲区**：
  - `add_memory` 数据库异常 → rollback + 返回 `None` 的降级路径无测试（**中**，写失败语义无固化）
  - `build_memory_context` 的截取上限（短期 3 条、长期 5 条）无测试（**中**，超量注入 prompt 的截断行为无保障）
  - chat 路由层 `update_short_term_memory` 的 try/except 兜底与「消息数含当前 user 消息」的计数时序无端到端测试（**中**）
  - HTTP 路由 `/api/memory/*` 完全无测试：GET 不校验类型、POST 非法输入变 500、DELETE 404 均无固化（**中**）
  - 淘汰平局规则（同 importance 比时间、再比 id）无测试（**低**）
  - `metadata` 预留参数被忽略、`limit=0` falsy 语义、`importance` 越界无校验，均无测试（**低**）

## 8. 关键设计决策

- **一张表 + `memory_type` 区分四类**：避免四张结构相同的表；读写路径统一经 `_validate_type` 校验，防止脏类型扩散（宪法第 20 条：改动行为需同步本规格）
- **写入即淘汰而非定时清理**：淘汰只在 `add_memory` 成功后触发，实现简单且无后台线程（宪法第 4 条单进程简单架构）；代价是直接改库（绕过 Manager）不会产生淘汰
- **短期记忆每会话一条、原地覆盖**：以 `source_conversation_id` 定位既有记录更新 `content`，避免同会话摘要无限累积占用 short_term 容量（20 条）；代价是历史摘要不可回溯
- **每 5 条消息触发一次摘要**：在 LLM 调用成本与记忆新鲜度之间取折中；触发点放在 chat 路由保存 user 消息之后，故计数含当前消息
- **全面降级、绝不阻塞对话**：LLM 或 DB 失败一律返回空串 / 跳过 / 返回 None 并记日志——记忆是增强项而非必需项，对话主流程可用性优先
- **长期三类无自动写入机制**：preference / fact / long_term 当前只能靠手动 POST 或代码显式调用写入，属预留能力；引入「LLM 自动抽取长期记忆」前应先补写 `add_memory` 降级路径测试
- **`metadata` 参数预留**：签名先行、模型列未加；新增列时须走宪法第 9 条的 `ensure_schema()` 轻量迁移路径
