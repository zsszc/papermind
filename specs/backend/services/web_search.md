# services/web_search.py（联网搜索服务 WebSearchService）规格说明书

> 本文件描述**行为契约**（做什么），不描述实现细节。依据 `backend/app/services/web_search.py`、`backend/app/services/agent_graph.py`、`backend/app/services/llm.py`、`backend/app/routers/chat.py` 实际代码反向整理（2026-08-04）。

## 1. 背景与目标

为对话提供「联网搜索」能力：当用户问题涉及最新进展、新闻等时效性内容时，借助 Kimi 服务端内置的 `web_search` 工具获取实时资料，让回答超出本地文献库的时效边界。

需要澄清的关键事实：**本模块并不承担对话链路中的实际搜索执行**。对话链路的联网搜索由 `llm_service.chat_stream(enable_web_search=True)` 在调用参数中注入 Kimi 内置工具完成；本模块唯一被实际使用的是 `should_search_online()` 启发式开关，`search()` / `search_stream()` 两个主动搜索方法当前**无任何调用方**。

## 2. 范围

### 2.1 包含

- `should_search_online()` 启发式触发规则（对话链路实际使用的部分）
- `search()` / `search_stream()` 独立搜索 API 的输入/输出/异常契约（当前无调用方，作为存量行为记录）
- 联网搜索在对话链路中的完整触发条件与结果注入方式（横跨 agent_graph → llm → chat 路由）
- 模块级单例 `web_search_service`

### 2.2 非目标

- 不自行执行 HTTP 搜索请求（搜索由 Kimi 服务端内置工具完成，本模块只发 chat.completions 调用）
- 不解析/结构化搜索结果中的 URL、标题、摘要列表（`search()` 只原样收集 tool_call 参数与 content 文本）
- 不负责把搜索来源写入消息的 `citations` 字段（citations 仅来自 RAG 检索片段，见第 3.5 节）
- 不承担重试与错误格式化（绕过 `services/llm.py`，见第 8 节）

## 3. 行为契约

### 3.0 模块级副作用

- 模块导入时创建全局单例 `web_search_service = WebSearchService()`，构造时同步读取 `config` 并实例化 `AsyncOpenAI` 客户端；**导入期不发网络请求**。
- 本服务直接实例化 `openai.AsyncOpenAI`，**不经过** `services/llm.py` 的 `llm_service`。

### 3.1 `WebSearchService.__init__(self)`

- **输入**：无（从全局 `config` 读取）。
- **输出**：无返回值，初始化实例属性：
  - `self.client`：`AsyncOpenAI(api_key=config.get("llm.api_key"), base_url=config.get("llm.base_url", "https://api.moonshot.cn/v1"), max_retries=1, timeout=120)`
  - `self.model`：`config.get("llm.model", "moonshot-v1-8k")`
- **前置条件**：`app.core.config.config` 已可读取。
- **后置条件**：实例持有 OpenAI 兼容异步客户端。
- **副作用**：读取全局配置单例；创建 HTTP 客户端对象。
- **异常**：构造本身不主动抛错。

### 3.2 `WebSearchService.search(self, query: str, top_n: int = 5) -> List[Dict[str, Any]]`

> ⚠️ 当前代码库内**无调用方**；`top_n` 参数被接受但**从未使用**（不影响发给模型的任何参数）。

- **输入**：
  - `query: str` —— 搜索问题，原样作为单条 user 消息发送
  - `top_n: int = 5` —— 无效参数（死参数），传任何值行为相同
- **输出**：`List[Dict[str, Any]]` —— 按 `response.choices` 顺序组装的字典列表，元素两种形态：
  - `{"type": "search_args", "query": <模型实际搜索词>, "raw": <tool_call arguments 原文>}` —— 当模型产生 `web_search` 工具调用时；`args.get("query", query)` 缺失时回退为入参 `query`；arguments 不是合法 JSON 时该条静默跳过
  - `{"type": "search_summary", "content": <message.content 原文>}` —— 当 choice 含非空 content 时
  - 两种元素可同时在列表中；列表可为空
- **前置条件**：`llm.api_key` 有效、网络可达；所用模型支持 `builtin_function` 工具。
- **后置条件**：任何异常下都返回（可能为空的）列表，**绝不抛异常**。
- **副作用**：
  - 网络调用：`client.chat.completions.create(model=self.model, messages=[{"role":"user","content":query}], max_tokens=2048, temperature=0.3, tools=[{"type":"builtin_function","function":{"name":"web_search"}}], tool_choice="auto")`（非流式；注意此处**不特判 kimi-k2 温度**，固定 0.3）。
  - 失败时写 warning 日志 `[web_search] 联网搜索失败: ...`（含堆栈）。
- **异常**：**不向外抛**，一切异常捕获后返回 `[]`。

### 3.3 `WebSearchService.search_stream(self, query: str) -> AsyncIterator[str]`

> ⚠️ 当前代码库内**无调用方**。

- **输入**：`query: str` —— 同 3.2。
- **输出**：`AsyncIterator[str]` —— 逐个 yield 模型增量文本（`delta.content`，空增量跳过）。
- **前置条件**：同 3.2。
- **后置条件**：成功路径只 yield 增量文本；异常时 yield 一个 `\n[联网搜索调用失败: {e}]` 错误增量后结束。
- **副作用**：
  - 网络调用：同 3.2 但 `stream=True`。
  - 失败时写 warning 日志 `[web_search_stream] 联网搜索流式失败: ...`。
- **异常**：**不向外抛**，转为末尾错误增量。

### 3.4 `WebSearchService.should_search_online(self, query: str) -> bool`

> 对话链路中**唯一被实际调用**的方法（`agent_graph.build_messages` 节点）。

- **输入**：`query: str` —— 用户消息原文。
- **输出**：`bool` —— 命中任一启发式规则即 `True`：
  - `query.lower()` 包含以下任一子串：`最新`、`最近`、`news`、`latest`、`recent`、`2024`、`2025`、`2026`、`搜索`、`查一下`、`网上`、`google`、`百度`、`arxiv`
  - 或 `query.lower()` 以 `搜索` 开头（注：`搜索` 已在子串列表中，此条件被前者完全覆盖，属冗余规则）
- **前置条件**：`query` 为 str（传 None 会抛 `AttributeError`）。
- **后置条件**：纯函数，返回值仅取决于入参；大小写不敏感（仅对 ASCII 有效，中文无大小写概念）。
- **副作用**：无。
- **异常**：无（对 str 入参）。

### 3.5 对话链路中的联网搜索：触发条件与结果注入（跨模块契约）

对话主流程的联网搜索**不经过** `search()`/`search_stream()`，完整链路如下：

1. **入口开关**：`POST /api/chat`（含重新生成）请求体 `ChatRequest.enable_web_search: Optional[bool] = False`（`schemas.py`）——用户在前端显式开启。
2. **编排判定**：`routers/chat.py` 调 `run_pre_orchestration(..., enable_web_search=...)` → `agent_graph.build_messages` 节点计算：
   `web_search_enabled = bool(state.get("enable_web_search")) or web_search_service.should_search_online(state["user_message"])`
   即**显式开启 OR 启发式命中**任一即可触发。
3. **提示注入**：`web_search_enabled` 为真时，向消息列表末尾追加 system 消息 `WEB_SEARCH_HINT = "用户问题可能涉及最新信息。如果现有文献片段不足以回答，请调用联网搜索工具获取最新资料并标注来源。"`（注入顺序：RAG 上下文之后、Skill prompt 之前）。
4. **工具注入**：路由层读 `state["web_search_enabled"]` 传给 `llm_service.chat_stream(messages, enable_web_search=...)`；为真时 `llm.py` 在调用参数中加入 `tools=[{"type":"builtin_function","function":{"name":"web_search"}}]` 与 `tool_choice="auto"`（走 llm.py 统一重试/温度治理，`stream=True`）。
5. **搜索执行**：由 Kimi 服务端根据模型自主决策执行实际搜索（`tool_choice="auto"`，模型可以不搜）。
6. **结果注入对话**：搜索结果**不作为独立消息插入**；Kimi 模型把搜索所得综合进回答正文，随普通流式增量经 SSE `{"delta": ...}` 事件发给前端；`WEB_SEARCH_HINT` 要求模型在正文中标注来源。
7. **来源不入 citations**：SSE 结束事件的 `citations` 仅来自 RAG 检索片段（`source`/`paper_id`），联网搜索来源不进入该结构化字段。

## 4. 边界条件与错误处理

| 场景 | 期望行为 |
|------|----------|
| `query` 为空串 | `should_search_online` 返回 `False`；`search` 仍原样发送（行为由模型侧决定） |
| `query` 含英文大写关键词（如 `LATEST`、`ArXiv`） | 先 `lower()` 再匹配，判定为 `True` |
| 年份边界（`2023`/`2027`） | 不在关键词表中，`2023` 不触发；`2027` 不触发（仅 2024–2026 三个年份硬编码） |
| `query` 为 None | `should_search_online` 抛 `AttributeError`（调用方 agent_graph 保证传 str） |
| 显式 `enable_web_search=True` 但问题无时效词 | 仍触发（显式优先，OR 语义） |
| 模型决定不调用工具（`tool_choice="auto"`） | 回答照常流出，无报错 |
| Kimi 接口失败：`search()` | 返回 `[]`，记 warning 日志 |
| Kimi 接口失败：`search_stream()` | 末尾 yield `\n[联网搜索调用失败: {e}]`，记 warning 日志 |
| Kimi 接口失败：对话主链路 | 由 `llm.py` 统一治理：最多 3 次指数退避重试，最终失败时 SSE 流出 `\n[调用 LLM 出错: ...]` 增量（见 `specs/backend/services/llm.md`） |
| tool_call arguments 非合法 JSON | `search()` 静默跳过该条（`except Exception: pass`） |
| 配置缺 `llm.api_key` | 构造不拦截，调用时按上述失败路径降级 |

## 5. 依赖

- **上游依赖**：
  - `app.core.config.config`（`llm.api_key` / `llm.base_url` / `llm.model`）
  - `app.core.logger.logger`
  - `openai.AsyncOpenAI`（锁定 `openai==1.12.0` + `httpx==0.27.2`，见宪法第 16 条）
  - Kimi 服务端内置 `web_search` 工具（`builtin_function`）
- **下游消费者**：
  - `app/services/agent_graph.py` `build_messages` 节点 —— 仅调用 `should_search_online()`
  - `search()` / `search_stream()` —— **无调用方**（死代码）
  - 间接链路：`routers/chat.py` → `services/llm.py`（实际工具注入与搜索执行）

## 6. 验收标准（可测试）

- [ ] AC1：显式 `enable_web_search=True` 时 `web_search_enabled` 为真且消息列表含 `WEB_SEARCH_HINT`（已有测试）
- [ ] AC2：`enable_web_search` 缺省且问题命中启发式关键词（如「最新」）时 `web_search_enabled` 为真（已有测试）
- [ ] AC3：两者均不满足时 `web_search_enabled` 为假且消息列表不含 `WEB_SEARCH_HINT`（已有测试）
- [ ] AC4：`should_search_online` 对关键词表中每个条目（含大写变体）返回 `True`，对无关键词输入返回 `False`
- [ ] AC5：`web_search_enabled` 为真时，发给 LLM 的调用参数含 `builtin_function web_search` 工具与 `tool_choice="auto"`（mock client 断言）
- [ ] AC6：联网搜索来源不出现在 SSE 结束事件的 `citations` 字段中
- [ ] AC7：`search()` 在 client 抛异常时返回 `[]` 且不抛异常；`search_stream()` 在异常时末尾 yield `\n[联网搜索调用失败: ...]`
- [ ] AC8：`search()` 对模型返回的 tool_calls/content 正确组装 `search_args`/`search_summary` 两种元素，非法 JSON arguments 静默跳过

## 7. 现有测试覆盖与盲区

- **已覆盖**：`backend/tests/test_agent_graph.py` `TestWebSearchToggle` 类 3 个用例：
  - `test_explicit_enable`（AC1）
  - `test_heuristic_enable`（AC2，仅「最新」一个关键词）
  - `test_disabled_by_default`（AC3）
- **盲区**：
  - 【高】`search()` / `search_stream()` 零测试且零调用方（AC7/AC8 无对应测试）——死代码行为完全未固定，删除或改动均无告警；`top_n` 死参数同理
  - 【中】`should_search_online` 13 个关键词中仅「最新」被间接验证；其余 12 个关键词、`startswith("搜索")`、大小写不敏感、年份硬编码边界（2023 不触发/2026 触发）均未测（AC4）
  - 【中】`enable_web_search=True` 时 `llm.py` 注入 tools/tool_choice 的参数断言无测试（AC5，属 llm.py 盲区但与本契约强相关）
  - 【中】联网搜索失败时对话主链路的降级表现（llm.py 重试后错误增量）无端到端测试
  - 【低】「搜索来源不进 citations」这一行为未以测试固定（AC6）

## 8. 关键设计决策

- **双通道并存（判定在本模块，执行在 llm.py）**：`should_search_online` 的启发式判定放在本模块，而实际工具注入与流式调用复用 `llm.py` 的统一治理——这是「显式开关 + 启发式兜底」的最小改动落地方式；`search()`/`search_stream()` 是早期独立搜索 API 的残留，现已无调用方。
- **绕过 `llm.py` 直连 `AsyncOpenAI`**：本模块自身构造 OpenAI 客户端，是宪法第 8 条「LLM 调用唯一入口」的现存例外之一（另一个是 `image_analyzer.py`）；因 `search()`/`search_stream()` 无调用方，实际对话流量不受影响，但该例外应在后续治理中消除或在宪法登记。
- **搜索由 Kimi 服务端内置工具执行**：后端不直接发搜索 HTTP 请求、不解析 SERP，换取零额外依赖；代价是搜索过程不可观测（无 query 日志、无结果数统计），`search()` 中对 tool_calls 的解析正是为弥补可观测性预留的。
- **搜索来源不进 citations**：citations 语义绑定本地文献（`paper_id`），网络来源无此概念，故只在正文由模型标注；前端因此无法对网络来源做结构化渲染。
- **年份关键词硬编码 2024–2026**：简单启发式的权宜之计，随时间推移会自然失效（2027 年起需手工扩充），属已知技术债。
- **温度固定 0.3 且不做 kimi-k2 特判**：`search()`/`search_stream()` 未复用 `llm.py` 的 `_get_temperature()`；在 kimi-k2 系模型（仅支持 temperature=1）下这两个方法可能因温度参数被拒而总是走失败降级——因无调用方，该隐患当前不可触发。
