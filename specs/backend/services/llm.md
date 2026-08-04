# services/llm.py（LLM 调用唯一入口 LLMService）规格说明书

> 本文件描述 `backend/app/services/llm.py` 的**行为契约**（做什么），不描述实现细节。
> 依据源码实际内容反向工程整理（2026-08-04）。

## 1. 背景与目标

项目宪法第 8 条规定：所有 OpenAI 兼容调用（当前指向 Kimi/Moonshot API）必须经本模块的全局单例 `llm_service`，不得绕过它直接实例化 openai client。本模块统一承担横切关注点：

1. **双客户端**：异步 `AsyncOpenAI`（供 FastAPI 协程）与同步 `OpenAI`（供后台线程，无事件循环）各一份，配置完全一致；
2. **模型差异屏蔽**：kimi-k2 系列只支持 `temperature=1`，由本模块自动改写，调用方无感知；
3. **上下文保护**：消息总字符数超预算时统一截断；
4. **韧性**：指数退避重试、超时控制；
5. **错误体验**：异常不抛给业务层，统一格式化为中文用户文案，以「带内错误串」返回/yield。

设计动机：Kimi API 复杂问题响应可达 60–120 秒且会返回 `429 engine_overloaded_error`，若让每个调用方各自处理重试与文案，行为必然发散；集中一处可保证全站错误提示一致、可测试。

## 2. 范围

### 2.1 包含

- 模块级单例 `llm_service` 及其构造期配置读取
- `_get_temperature()` 的 kimi-k2 系列特判
- `_truncate_messages()` 的字符预算与截断策略
- `_async_retry()` / `_sync_retry()` 的重试语义
- `chat_stream()` 异步流式生成（含与路由层拼接后的 SSE 协议）
- `chat_completion()` / `chat_completion_sync()` 非流式补全（含 `json_mode`）
- `_format_error()` 错误文案映射（429/401/超时）
- `is_configured()` 配置自检、`health_check()` 轻量探活

### 2.2 非目标

- SSE 帧封装、会话与消息落库、引用（citations）组装：归 `routers/chat.py`（本规格仅在 3.6 节描述拼接后的对线协议）
- LLM 调用前的编排（记忆加载、向量检索、消息组装）：归 `services/agent_graph.py`
- 多模态图片分析：`services/image_analyzer.py` 自建 AsyncOpenAI client，是宪法第 8 条「唯一入口」的已知例外，不在本规格覆盖范围
- 配置文件的读写与环境变量覆盖：归 `core/config.py` / `core/settings.py`
- RAG 评测中的 LLM 调用封装：归 `eval/`

## 3. 行为契约

### 3.0 模块级副作用

模块底部执行 `llm_service = LLMService()`：**导入即构造单例**，构造时立即读取 `config` 快照（api_key / base_url / model / max_tokens / temperature / max_total_chars）。此后即使 `config.yaml` 被修改（如经设置接口），已创建的 client 不会自动更新——需重启进程生效。

### 3.1 `LLMService.__init__(self)`

- **输出**：实例持有 `self.client`（`AsyncOpenAI`）与 `self.sync_client`（`OpenAI`）两个客户端
- **配置来源与默认值**：

| 属性 | 配置键 | 默认值 |
|------|--------|--------|
| api_key（两 client 共用） | `llm.api_key` | 无（空则后续调用由 SDK 报错） |
| base_url | `llm.base_url` | `https://api.moonshot.cn/v1` |
| `self.model` | `llm.model` | `moonshot-v1-8k` |
| `self.max_tokens` | `llm.max_tokens` | `4096` |
| `self.temperature` | `llm.temperature` | `0.3` |
| `self.max_total_chars` | `llm.max_total_chars` | `200000` |

- **客户端固定参数**：两 client 均为 `max_retries=1`（SDK 层自动重试 1 次）、`timeout=120` 秒
- **副作用**：创建两个 httpx 客户端；不写日志、不发网络请求

### 3.2 `LLMService._get_temperature(self) -> float`

- **输出**：`self.model` 字符串包含 `"kimi-k2.6"` **或** `"kimi-k2"` 时返回 `1.0`；否则返回配置的 `self.temperature`
- **设计依据**：kimi-k2 系列模型当前只支持 `temperature=1`，传其他值会被 API 拒绝；子串匹配意味着 `kimi-k2-7`、`kimi-k2.6` 等同族模型名均被覆盖
- **适用面**：本模块所有真实 API 调用（含 `health_check`）都经此函数，调用方无法覆盖

### 3.3 `LLMService._truncate_messages(self, messages: List[Dict[str, str]]) -> List[Dict[str, str]]`

- **输入**：OpenAI 风格消息列表，每项含 `role` / `content`
- **输出**：截断后的**新列表**（不修改入参元素本身，截断项用 `{**m, "content": ...}` 复制）
- **预算语义**：以**字符数**（`len(content)`，非 token 数）计量；全部消息 content 长度之和 `<= self.max_total_chars` 时**原样返回**（连顺序都不动）
- **超预算时的规则**（按序执行）：
  1. 拆分：`system` 消息全部保留且**永不截断**；非 system 消息进入截断流程；
  2. 逐条截断：只要总量仍超预算且该条 content 长度 > 300，则保留**尾部** `keep` 个字符（`content[-keep:]`），`keep = max(300, len(content) - 超出量 // 非system消息数)`——即超出量在所有非 system 消息间**均摊**，每条至少保底 300 字符；长度 ≤ 300 的消息不动；
  3. 兜底：若经第 2 步总量仍超预算且非 system 消息多于 2 条，**只保留最近 2 条**非 system 消息；
  4. 返回 `system_messages + truncated_non_system`——注意这会**重排**：所有 system 消息被集中到列表头部，非 system 消息保持相对顺序。
- **不保证**：若 system 消息自身已超预算，返回值仍超预算（system 不受任何截断）
- **副作用**：无

### 3.4 `LLMService._async_retry(self, coro_factory, max_retries: int = 3, base_delay: float = 1.0, max_delay: float = 10.0)`

- **输入**：`coro_factory` 为零参可调用对象，每次调用返回一个新 coroutine（工厂模式，保证重试拿到新协程）
- **输出**：首次成功的协程返回值
- **可重试异常**：`(APIError, APITimeoutError, TimeoutError, asyncio.TimeoutError)`——命中时按 `min(base_delay * 2 ** attempt, max_delay)` 指数退避（默认 1s → 2s → 放弃），每次重试写 warning 日志，最后一次写 error 日志
- **其他异常**：写 error 日志后**立即原样上抛**，不重试
- **耗尽**：`max_retries` 次全部命中可重试异常后，抛出**最后一次**捕获的异常
- **副作用**：`asyncio.sleep` 退避等待；`[LLM]` 前缀日志

### 3.5 `LLMService.chat_stream(self, messages: List[Dict[str, str]], enable_web_search: bool = False) -> AsyncIterator[str]`

- **输入**：消息列表（进入后先经 `_truncate_messages`）；`enable_web_search=True` 时注入 Kimi 内置联网工具
- **输出**：异步迭代器，逐个 yield 模型输出的**文本增量**（空增量被过滤）；正常结束自然耗尽；失败时 yield 一个带内错误串（见下）
- **请求参数**：`model`、`max_tokens`、`temperature=_get_temperature()`、`stream=True`、`timeout=180`；`enable_web_search=True` 时附加 `tools=[{"type": "builtin_function", "function": {"name": "web_search"}}]` 与 `tool_choice="auto"`
- **重试**：最多 3 次尝试，可重试异常集合与 3.4 相同，退避 `min(1.0 * 2 ** attempt, 10.0)`（1s → 2s → 放弃）；**非预期异常不重试**，立即 yield `\n[调用 LLM 出错: {格式化文案}]` 并结束迭代
- **失败契约（重要）**：本方法**不向调用方抛异常**表达失败；重试耗尽或非预期异常时，把 `f"\n[调用 LLM 出错: {self._format_error(e)}]"` 作为**普通文本增量** yield 出去。上游若把增量原样拼接入库，该错误串会成为助手回复内容的一部分
- **已知边界**：若异常发生在**已开始产出增量之后**（流中途断开），已 yield 的内容不会回收，重试将从头重新生成，调用方会看到重复前缀
- **副作用**：网络调用、`asyncio.sleep` 退避、`[LLM]` 日志

### 3.6 SSE 对线协议（与 `routers/chat.py` 拼接后的完整契约）

`llm.py` 只产出裸文本增量；SSE 帧封装在路由层完成，协议为三段式：

| 阶段 | 帧格式（`data: ` 前缀 + JSON + `\n\n`） | 产生方 |
|------|------------------------------------------|--------|
| 增量 | `{"delta": "<文本>", "finished": false, "conversation_id": N}` | chat.py 逐增量包装 |
| 完成 | `{"delta": "", "finished": true, "conversation_id": N, "citations": [...]}` | chat.py 流结束后发一帧 |
| 错误 | `{"error": "<文案>"}` | **前端 ChatPanel 解析层兼容此帧，但当前后端路由从不产生它** |

- 当前后端的 LLM 错误**不走 `{error}` 帧**，而是以 3.5 的带内错误串作为普通 `delta` 送达，随后照常发 `{finished: true}` 帧；前端因此把它当作正常回复文本渲染（并落库）
- 响应头固定 `Cache-Control: no-cache`、`Connection: keep-alive`、`X-Accel-Buffering: no`
- 客户端中断：路由层每增量后 `await asyncio.sleep(0)` 让出控制权，依赖 `asyncio.CancelledError` 终止生成；`llm.py` 自身不感知取消

### 3.7 `LLMService.chat_completion(self, messages: List[Dict[str, str]], json_mode: bool = False, timeout: Optional[int] = None) -> str`

- **输入**：消息列表（先截断）；`json_mode=True` 时附加 `response_format={"type": "json_object"}`（模型被约束输出合法 JSON，**解析责任在调用方**）；`timeout` 缺省或为 `0` 时按 120 秒（`timeout or 120`）
- **输出**：成功时返回 `response.choices[0].message.content or ""`；**任何异常**（含重试耗尽与非预期异常）都不上抛，返回 `f"[调用 LLM 出错: {格式化文案}]"`（注意无 `\n` 前缀，与流式版不同）
- **后置条件**：调用方必须以「返回值是否以 `[调用 LLM 出错:` 开头」区分成功与失败——这是全项目（`auto_tag`、`memory_manager`、`eval/generate_qa` 等）共同依赖的判别约定
- **副作用**：网络调用、退避等待、`[LLM]` 日志

### 3.8 `LLMService._sync_retry(self, func_factory, max_retries: int = 3, base_delay: float = 1.0, max_delay: float = 10.0)`

- **语义与 3.4 完全对齐**，差异仅：同步执行（`time.sleep` 退避）；可重试异常集合为 `(APIError, APITimeoutError, TimeoutError)`（无 `asyncio.TimeoutError`，因同步语境不存在）
- **耗尽**：断言 `last_exception is not None` 后抛出最后一次异常

### 3.9 `LLMService.chat_completion_sync(self, messages: List[Dict[str, str]], json_mode: bool = False, timeout: Optional[int] = None) -> str`

- **定位**：`chat_completion` 的同步入口，供后台线程（无事件循环）使用；参数、截断、temperature 特判、重试、错误格式化与异步版**逐项对齐**
- **输出契约**：与 3.7 相同——成功返回 content，失败返回 `[调用 LLM 出错: ...]` 错误串，不抛异常
- **底层**：走 `self.sync_client`（同步 `OpenAI` client）
- **消费者**：`routers/papers.py` 后台元数据增强线程、`services/auto_tag.py` 同步打标入口

### 3.10 `LLMService._format_error(self, e: Exception) -> str`

按以下顺序对 `str(e)` 做子串匹配，首个命中即返回对应**固定中文文案**；全部未命中返回异常原文：

| 匹配条件（按序） | 返回文案 |
|------------------|----------|
| 含 `exceeded_current_quota` / `insufficient balance` / `suspended`+`account`（优先于 429 判断） | `Kimi 账户额度不足或已被冻结，请登录 Moonshot 控制台检查账单与额度。`（Batch7-F2 新增，99a64bf） |
| 含 `"429"`，或 lower 后含 `"overloaded"` | `Kimi API 当前负载过高或请求频繁，请稍后再试。` |
| 含 `"401"` 或含 `"Authentication"`（区分大小写） | `API Key 无效或已过期，请检查 config.yaml 中的 llm.api_key。` |
| lower 后含 `"timeout"` 或 `"timed out"` | `Kimi API 响应超时，请稍后重试。` |
| 其他 | `str(e)` 原文 |

- **未覆盖面**：余额不足 / 额度耗尽类错误（如含 `insufficient`、`balance`、`quota` 等关键词）**没有专属文案**，会走兜底分支把英文异常原文透出给用户
- **注意**：子串匹配可能误伤（如正常文本中恰好含 "timeout" 的其他错误），属可接受的启发式

### 3.11 `LLMService.is_configured(self) -> bool`

- **输出**：`llm.api_key` 非空、strip 后**不**以 `sk-xxxx` 开头、**不**以 `your-` 开头、且长度 `>= 20` 时返回 `True`；否则 `False`
- **语义**：识别占位符 / 明显非法 Key，**不代表 Key 真实可用**（不发起网络验证）
- **副作用**：无（每次调用实时读 `config`，不走 3.0 的构造期快照）

### 3.12 `LLMService.health_check(self, timeout: int = 8) -> Dict[str, Any]`

- **输出**：
  - `is_configured()` 为假 → `{"ok": False, "error": "llm.api_key 未配置或无效"}`（不发请求）；
  - 成功 → `{"ok": True, "model": self.model}`；
  - 异常 → `{"ok": False, "error": _format_error(e)}`，并写 warning 日志
- **探活方式**：真实调用一次极短 completion（user: `"hi"`，`max_tokens=1`，`temperature=_get_temperature()`，独立 `timeout` 参数默认 8 秒）；**不经过重试包装**
- **消费者**：`main.py` lifespan 启动流程，结果写入 `app.state.llm_ready`，经 `GET /api/health` 暴露

## 4. 边界条件与错误处理

| 场景 | 期望行为 |
|------|----------|
| 空消息列表 | 截断函数直接返回（总量 0 ≤ 预算）；API 层行为由 Kimi 决定，本模块不校验 |
| content 为 `None` 的消息 | 按空串计量（`or ""`），不崩溃 |
| 消息总量略超预算 | 仅超长（>300 字符）的非 system 消息被均摊截尾，短消息原样保留 |
| 均摊截断后仍超预算 | 非 system 消息砍到只剩最近 2 条；system 消息永不截断，总量仍可能超预算 |
| API 返回 429 / engine_overloaded | 可重试异常：流式与非流式均退避重试至多 3 次；最终失败返回「负载过高」文案错误串 |
| API Key 无效（401） | SDK 抛 `AuthenticationError`（`APIError` 子类），会被当作可重试异常**重试 3 次后才失败**——已知行为，重试无意义但不影响正确性 |
| 余额/额度不足 | 无专属文案，异常英文原文经错误串透出（见 3.10） |
| 流中途断连（已产出部分增量） | 重试从头生成，调用方看到重复前缀（见 3.5） |
| 客户端主动断开 SSE | 由路由层 `asyncio.sleep(0)` + `CancelledError` 处理；llm.py 不感知 |
| `json_mode=True` 但模型返回非法 JSON | 本模块不管解析；调用方（如 `eval/generate_qa.py`）自行捕获并视 `[调用 LLM 出错:` 前缀为失败重试 |
| 配置运行期被修改 | 已构造的 client 不更新（3.0），需重启进程；`is_configured()` 例外，实时读配置 |
| SDK 层重试与本模块重试叠加 | SDK `max_retries=1` × 应用层 3 次尝试，最坏情况单次逻辑调用发起约 6 次 HTTP 请求 |

## 5. 依赖

- **上游依赖**：`openai==1.12`（`AsyncOpenAI` / `OpenAI` / `APIError` / `APITimeoutError`，与 `httpx==0.27.2` 版本互锁，见宪法第 16 条）；`app.core.config.config`（`llm.*` 配置键）；`app.core.logger.logger`
- **下游消费者**：
  - `routers/chat.py`：`chat_stream`（SSE 对话、消息重生成）、`chat_completion`（非流式对话）
  - `routers/papers.py`：`chat_completion`（AI 概括）、`chat_completion_sync`（后台元数据增强）
  - `routers/thesis.py`：`chat_completion`（论文写作建议）
  - `services/pdf_parser.py`、`services/memory_manager.py`、`services/auto_tag.py`（异步 + 同步两入口）
  - `app/main.py`：`health_check`（启动探活 → `app.state.llm_ready`）

## 6. 验收标准（可测试）

- [ ] AC1：模型名含 `kimi-k2` / `kimi-k2.6` 时所有调用实际请求参数的 `temperature` 为 `1.0`；其他模型使用配置值
- [ ] AC2：消息总量未超 `max_total_chars` 时 `_truncate_messages` 原样返回（含顺序）；超预算时 system 消息全部保留且不被截断，超长非 system 消息保留尾部且每条不少于 300 字符
- [ ] AC3：均摊截断仍超预算时，非 system 消息仅保留最近 2 条，返回列表以全部 system 消息开头
- [ ] AC4：对可重试异常（`APIError` / 超时类），`chat_completion` 恰好尝试 3 次且按 1s/2s 退避后返回 `[调用 LLM 出错: ...]` 串；对非预期异常只尝试 1 次即返回错误串，均**不抛异常**
- [ ] AC5：`chat_stream` 正常路径 yield 全部非空增量；重试耗尽后最后一次 yield 的内容以 `\n[调用 LLM 出错:` 开头
- [ ] AC6：429/overloaded、401/Authentication、timeout 三类异常分别映射到 3.10 的三条固定中文文案；其他异常返回原文
- [ ] AC7：`is_configured` 对空 Key、`sk-xxxx` / `your-` 前缀、长度 < 20 的 Key 返回 `False`，其余返回 `True`
- [ ] AC8：`health_check` 在未配置时短路返回 `{"ok": False, ...}` 且不发网络请求；成功路径返回 `{"ok": True, "model": ...}`
- [ ] AC9：`enable_web_search=True` 时请求携带 `builtin_function web_search` 工具与 `tool_choice="auto"`
- [ ] AC10：`json_mode=True` 时请求携带 `response_format={"type": "json_object"}`；异步与同步入口行为一致

## 7. 现有测试覆盖与盲区

- **已覆盖**：
  - `tests/test_generate_qa.py::test_parse_llm_error_string_raises`——间接锁定错误串前缀格式 `[调用 LLM 出错: `（评测解析层将其判为失败）
  - `tests/test_memory.py`——monkeypatch `llm_service.chat_completion` 验证 MemoryManager 的降级路径（mock 掉本模块，不测本模块行为本身）
  - `tests/test_health.py::test_health`——仅断言 `/api/health` 响应含 `llm_ready` 字段（测试不触发 lifespan，`health_check` 本身未被执行）
  - **没有任何测试直接实例化 `LLMService` 或断言其内部方法行为**
- **盲区**：
  - `_get_temperature` 的 kimi-k2 系列 → `1.0` 特判完全无测试（**高**，配置改为 k2 系列后回归无保障）
  - `_truncate_messages` 的预算判断、均摊截尾、300 字符保底、最近 2 条兜底、system 重排均无测试（**高**，直接决定 prompt 正确性）
  - `_async_retry` / `_sync_retry` 的重试次数、退避序列、可重试异常集合、非预期异常直抛均无测试（**高**）
  - 429/401/timeout 三类文案映射与兜底原文透出无测试（**中**，用户体验文案回归无保障）
  - `chat_stream` 的带内错误 yield、流中途失败后重试产生重复前缀的行为无测试（**中**）
  - 错误串被路由层当作正常助手回复落库的端到端行为无测试（**中**）
  - `is_configured` 占位符/长度判定无测试（**低**，纯函数易补）
  - `health_check` 未配置短路、成功/失败两路径无测试（**低**）
  - `json_mode` 与 `enable_web_search` 的请求参数注入无测试（**低**）
  - 401 被当作可重试异常空转 3 次的已知浪费无测试固化（**低**）

## 8. 关键设计决策

- **错误带内化（返回错误串而非抛异常）**：所有非流式入口把失败格式化为 `[调用 LLM 出错: ...]` 字符串返回，流式入口将其作为普通增量 yield。取舍：业务层无需 try/except，UI 能直接把错误渲染进对话气泡；代价是每个调用方必须自觉识别该前缀（`auto_tag`、`memory_manager`、`eval` 均已遵守），且错误串会被当作助手回复落库。任何重构不得悄悄改为抛异常——那会同时破坏多个下游
- **双 client 而非复用**：后台线程（文献处理、自动打标）没有事件循环，必须走同步 `OpenAI` client；两 client 参数逐项保持一致是手工维护的约定，改一处必须同步改另一处
- **应用层重试 + SDK `max_retries=1` 叠加**：SDK 的 1 次重试覆盖瞬时网络抖动，应用层的 3 次指数退避覆盖 429/过载；两者独立生效
- **kimi-k2 温度特判放在 `_get_temperature()` 单点**：保证流式、非流式、健康检查三条路径一致；新接模型时在此扩展
- **字符数而非 token 数做截断预算**：避免引入 tiktoken 编码开销与模型词表耦合；200000 字符的默认预算对 Kimi 上下文是宽松近似
- **截断保留尾部**：对话场景中最新消息语义最重要，故 `content[-keep:]` 保留末尾而非开头
- **`timeout or 120` 的 falsy 语义**：`timeout=0` 会被当作未指定，属刻意简化（0 秒超时无意义）
