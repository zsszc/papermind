# 对话编排 Agent 图（agent_graph）规格说明书

> 本规格由 `backend/app/services/agent_graph.py`（244 行）反向工程而来，描述 LLM 调用前编排链路的行为契约。

## 1. 背景与目标

`POST /api/chat` 在调用 LLM 之前需要完成一串前置编排：加载会话历史与用户背景记忆、向量检索相关文献片段、组装最终消息列表（含 RAG 上下文、联网搜索提示、Skill 角色注入）。该链路原先内嵌在 `routers/chat.py` 中，本模块将其建模为 LangGraph StateGraph，使编排步骤显式化、可独立测试。

**刻意边界**：流式生成（generate）不放进图里。LangGraph 的流式语义与既有 SSE 契约（`{delta}` / `{finished, citations}` / `{error}` 三种事件）差异较大，强行图内化会破坏契约。因此本图只负责「LLM 调用前的上下文编排」，流式生成与 SSE 发送仍由路由层驱动。

## 2. 范围

### 2.1 包含

- LangGraph StateGraph 的节点/边拓扑：`START → load_memory → retrieve → graph_expand → external_tools → build_messages → END`（Phase G 起为 5 节点；graph_expand 为引用图谱扩展节点，开关 `retrieval.graph_expand` 默认 false——沿 paper_citations 1 跳扩展邻居文献代表 chunk 并与向量召回 RRF 融合，排除命中文献自身，任何异常透传不回归）
- `AgentState` 状态 schema（输入字段 + 中间/输出字段）
- 五个节点函数（`load_memory` / `retrieve` / `graph_expand` / `external_tools` / `build_messages`）的行为契约
- 模块级常量（`SYSTEM_PROMPT` / `WEB_SEARCH_HINT` / `HISTORY_LIMIT` / `RETRIEVE_TOP_K`）
- `build_rag_prompt()` 的 RAG 提示词拼装格式
- 图的构建、编译与单例获取（`build_agent_graph` / `get_agent_graph`）
- 对外入口 `run_pre_orchestration()` 的输入输出契约
- 与 `routers/chat.py` 的分工：图负责 LLM 前编排，路由负责会话/消息落库、记忆异步更新、流式生成与 SSE 事件格式

### 2.2 非目标

- 不执行任何 LLM 调用（LLM 调用唯一入口为 `services/llm.py`，由路由层使用）
- 不做流式生成、不产出 SSE 事件
- 不写数据库（节点均为近似纯函数，仅读 db / 向量库；会话与消息的写入是路由层职责）
- 不实现 ReAct / 工具调用循环；图为固定线性拓扑，无条件分支
- 不负责联网搜索的实际执行（只做开关判定与提示注入，实际搜索在 LLM 侧由 `llm_service.chat_stream(enable_web_search=...)` 触发）

## 3. 行为契约

### 3.1 模块级常量

| 常量 | 值 | 语义 |
|------|----|------|
| `SYSTEM_PROMPT` | 多行中文字符串 | 基础 system prompt（PaperMind 学术文献助手人设 + 4 条规则，含 `[^i^]` 引用标注要求）。与原 chat.py 内嵌版本逐字一致 |
| `WEB_SEARCH_HINT` | 单行中文字符串 | 联网搜索提示，启用时作为独立 system 消息追加。与原 chat.py 内嵌版本逐字一致 |
| `NO_RETRIEVAL_GUARD` | 单行中文字符串 | 零检索拒答硬约束（Phase C C2，已实现）：「未检索到相关文献片段。必须明确回答「文献库中没有相关内容」，禁止编造任何引用标记。」检索结果为空时追加到 system prompt 尾部 |
| `HISTORY_LIMIT = 10` | int | 注入的历史消息条数上限（取最近 10 条） |
| `RETRIEVE_TOP_K = 5` | int | 向量检索 top_k |

### 3.2 `class AgentState(TypedDict, total=False)`

图状态。`total=False` 表示所有字段均可缺省，节点函数返回部分字典由 LangGraph 合并进状态。

**输入字段（路由层填入）**：

| 字段 | 类型 | 语义 |
|------|------|------|
| `db` | `Session` | SQLAlchemy 会话（只读使用，节点不得 commit） |
| `conversation_id` | `int` | 当前会话 ID |
| `user_message` | `str` | 用户本轮输入 |
| `skill` | `Optional[str]` | Skill ID（可为空） |
| `paper_id` | `Optional[int]` | 限定检索的文献 ID（可为空） |
| `enable_web_search` | `bool` | 前端显式开启联网搜索 |

**中间/输出字段（图执行后读取）**：

| 字段 | 类型 | 产出节点 | 语义 |
|------|------|----------|------|
| `memory_context` | `str` | load_memory | 用户背景记忆文本（可为空串） |
| `system_prompt` | `str` | load_memory | 基础 system prompt（含记忆段拼接） |
| `history_messages` | `List[Dict[str, str]]` | load_memory | 最近 ≤10 条历史消息（含当前 user 消息），`{"role", "content"}` 结构 |
| `history_total` | `int` | load_memory | 会话消息总数（路由层用于更新 `message_count`） |
| `context_chunks` | `List[Dict[str, Any]]` | retrieve | 检索片段（含引用信息），失败时为空列表 |
| `web_search_enabled` | `bool` | build_messages | 最终判定的联网开关 |
| `skill_prompt` | `Optional[str]` | build_messages | 注入的 Skill 角色 prompt |
| `messages` | `List[Dict[str, str]]` | build_messages | 最终发给 LLM 的消息列表 |

### 3.3 `def build_rag_prompt(query: str, retrieved: List[dict]) -> str`

- **输入**：`query` 用户问题；`retrieved` 检索片段列表，每个 dict 可含 `title` / `authors` / `year` / `page_number` / `content` 键
- **输出**：带引用编号的 RAG system prompt 字符串。格式：固定前缀说明 → 每个片段以 `[i] 标题 - 作者 (年份) 第N页` 开头（`i` 从 1 编号；`title` 缺失回退 `"未知文献"`；`authors`/`year`/`page_number` 缺失则省略对应片段）→ 各片段间以 `\n---\n` 分隔 → 尾部附 `用户问题：{query}` 与引用标注要求
- **前置条件**：无（空列表亦可调用，产出无片段的模板，但正常路径中 `build_messages` 仅在 chunks 非空时调用）
- **后置条件**：返回字符串中片段编号与该片段在 `retrieved` 中的序号一致，供 `[^i^]` 引用对应
- **副作用**：无
- **异常**：无显式处理；片段缺 `content` 键时取空串

### 3.4 `def load_memory(state: AgentState) -> Dict[str, Any]`（节点 1）

- **输入**：`state`（须含 `db`、`conversation_id`）
- **输出**：`{"history_messages", "history_total", "memory_context", "system_prompt"}`
- **前置条件**：`db` 为可用会话；`conversation_id` 对应的会话可不存在（历史为空，不报错）
- **后置条件**：
  - `history_messages` 为该会话按 `created_at` 升序的全部消息中取**最后 `HISTORY_LIMIT`（10）条**，每项仅保留 `role` / `content`
  - `history_total` 为该会话消息总数（不受截断影响）
  - `memory_context` 为 `MemoryManager(db).build_memory_context()` 的返回值
  - `system_prompt` = `SYSTEM_PROMPT`；当 `memory_context` 非空时追加 `\n\n以下是关于用户的背景记忆，请在回答时参考：\n\n{memory_context}`
- **副作用**：DB 只读查询（`messages` 表、`memory_summaries` 表）
- **异常**：无显式处理；DB 异常向上抛出

### 3.5 `def retrieve(state: AgentState) -> Dict[str, Any]`（节点 2）

- **输入**：`state`（读取 `user_message`、`paper_id`）
- **输出**：`{"context_chunks": chunks}`
- **前置条件**：无
- **后置条件**：
  - `paper_id` 非空时构造 `filters = {"paper_id": paper_id}`，否则 `filters = {}`（空 dict，不是 None）
  - `user_message` 非空且 `get_vector_store().available()` 为真时，以 `query=user_message`、`top_k=RETRIEVE_TOP_K(=5)`、`filters=filters` 调 `store.search()`
  - 向量库不可用、`user_message` 为空、或检索抛任何异常时，`context_chunks` 为空列表（**检索失败不阻断对话**）
- **副作用**：读取向量库（ChromaDB）；异常时写错误日志（前缀 `[agent_graph]`）
- **异常**：内部捕获一切 `Exception`，记录日志后降级为空列表，不向外抛

### 3.6 `def build_messages(state: AgentState) -> Dict[str, Any]`（节点 3）

- **输入**：`state`（须含 `system_prompt`、`user_message`；读取 `history_messages`、`context_chunks`、`enable_web_search`、`skill`）
- **输出**：`{"messages", "web_search_enabled", "skill_prompt"}`
- **前置条件**：`load_memory`、`retrieve` 已执行（线性拓扑保证）
- **后置条件**（消息组装顺序固定，与原 chat.py 一致）：
  1. `messages[0]` = `{"role": "system", "content": system_prompt}`（含记忆）；**Phase C C2（已实现）**：`context_chunks` 为空时 content 追加 `\n\n` + `NO_RETRIEVAL_GUARD` 硬约束段，非空时与现状逐字一致
  2. 追加全部 `history_messages`（其中已包含当前 user 消息）
  3. `context_chunks` 非空时，追加 `{"role": "system", "content": build_rag_prompt(user_message, chunks)}`
  4. `web_search_enabled = bool(enable_web_search) or web_search_service.should_search_online(user_message)`；为真时追加 `{"role": "system", "content": WEB_SEARCH_HINT}`
  5. `skill_prompt = build_skill_prompt(skill, user_message)`；非 None 时追加 `{"role": "system", "content": skill_prompt}`（恒为最后一条）
- **副作用**：调用 `web_search_service.should_search_online()`（纯启发式判断）；调用 `skills` 模块注册表（只读）
- **异常**：无显式处理

### 3.7 `def build_agent_graph()`

- **输入**：无
- **输出**：编译后的 LangGraph Runnable
- **后置条件**：图为固定线性拓扑——`add_edge(START, "load_memory")`、`("load_memory","retrieve")`、`("retrieve","build_messages")`、`("build_messages", END)`；三个节点函数在**编译时绑定**（编译后再 monkeypatch 模块级函数对已编译图无效，需重新编译）
- **副作用**：无

### 3.8 `def get_agent_graph()`

- **输入**：无
- **输出**：编译后图的全局单例
- **后置条件**：双检锁（`_graph_lock`）懒加载，多次调用返回同一对象，线程安全
- **副作用**：首次调用时编译图并写入模块级 `_compiled_graph`

### 3.9 `def run_pre_orchestration(db: Session, conversation_id: int, user_message: str, skill: Optional[str] = None, paper_id: Optional[int] = None, enable_web_search: bool = False) -> AgentState`

- **输入**：见 `AgentState` 输入字段；`skill` / `paper_id` / `enable_web_search` 有默认值
- **输出**：图执行后的完整最终状态（`AgentState`），路由层从中读取 `messages` / `context_chunks` / `web_search_enabled` / `history_total`
- **前置条件**：当前 user 消息**已由路由层落库**（chat.py 在调用前先 `db.add(user_msg)` + `flush`），因此 `history_messages` 天然包含本轮 user 消息
- **后置条件**：不修改数据库；返回状态满足 3.4–3.6 全部后置条件
- **副作用**：等同于三节点副作用之和（DB 只读、向量库只读、可能写错误日志）
- **异常**：`load_memory` / `build_messages` 的 DB 或内部异常向上抛（由路由层全局异常处理兜底）；`retrieve` 内部异常被吞

### 3.10 与 routers/chat.py 的分工

| 职责 | 归属 |
|------|------|
| 会话创建/查找、user 消息落库、`message_count` 更新（= `history_total` + 1） | 路由层 |
| 记忆异步更新（`update_short_term_memory`，失败仅记日志） | 路由层 |
| 记忆加载 → 检索 → 消息组装（本规格全部内容） | **agent_graph** |
| LLM 流式/非流式调用（`llm_service.chat_stream` / `chat_completion`） | 路由层 |
| assistant 消息落库（流式路径用新 Session） | 路由层 |
| SSE 事件格式（`{delta}` / `{finished, citations}` / `{error}`） | 路由层 |

### 3.11 `def verify_citations(answer_text, retrieved_chunks) -> (cleaned_text, report)`（Phase C C1，已实现）

- **输入**：答案全文；本次检索返回的 chunk 列表（编号 1-based，与 prompt 中一致）
- **规则**：`[^n^]` 且 1 ≤ n ≤ len(retrieved) → 保留并计入有效引用；越界（含 0、负数）或 retrieved 为空 → 从文本剔除该标记（保留语句本身）
- **输出**：`(清洗后文本, {"total": n, "valid": m, "removed": k, "verified": bool})`；全部有效或无引用时 `verified=True`，有剔除时 `False`
- **日志**：有剔除时记 `[guardrails]` warning（脱敏：只记被剔除编号列表，不记答案全文）
- **副作用**：无——幂等纯函数，无 DB/网络/LLM 调用，天然符合宪法第 8 条
- **调用方**：`routers/chat.py` 流式分支在落库前调用（chat.md 3.10）；非流式分支与 regenerate 路径本轮不接入（phase-c-guardrails spec 2.2）

## 4. 边界条件与错误处理

| 场景 | 期望行为 |
|------|----------|
| 向量库 `available()` 为假 | `context_chunks=[]`，消息列表仅 system + history，图正常完成 |
| `store.search()` 抛异常 | 记 `[agent_graph]` 错误日志，`context_chunks=[]`，图正常完成 |
| `user_message` 为空串 | 跳过检索（`context_chunks=[]`），其余节点照常 |
| 会话无任何历史消息 | `history_messages=[]`、`history_total=0`，不报错 |
| 历史消息 > 10 条 | 仅取最近 10 条进入 `history_messages`；`history_total` 仍为真实总数 |
| 无背景记忆（`memory_context` 为空） | `system_prompt` 即 `SYSTEM_PROMPT` 原文，不追加记忆段 |
| 检索片段缺 `title`/`authors`/`year`/`page_number` | RAG prompt 中分别回退「未知文献」/省略对应片段 |
| `skill=None` 或未注册 ID | `skill_prompt=None`，不注入 Skill 消息 |
| `enable_web_search=False` 且启发式未命中 | `web_search_enabled=False`，不注入 `WEB_SEARCH_HINT` |
| 并发调用 `get_agent_graph()` | 双检锁保证只编译一次 |

## 5. 依赖

- **上游依赖**：
  - `langgraph.graph`（`StateGraph` / `START` / `END`，langgraph 1.2.9 锁定）
  - `app.models.Message`（历史消息查询）
  - `app.services.memory_manager.MemoryManager`（`build_memory_context()`）
  - `app.services.retrieval.get_vector_store`（向量库单例，`available()` / `search()`）
  - `app.services.skills.build_skill_prompt`
  - `app.services.web_search.web_search_service`（`should_search_online()` 启发式）
  - `app.core.logger`
- **下游消费者**：`app.routers.chat`（`POST /api/chat` 唯一调用方）

## 6. 验收标准（可测试）

- [ ] AC1：`get_agent_graph()` 返回非空对象且多次调用为同一实例（单例）
- [ ] AC2：编译图包含 `load_memory` / `retrieve` / `build_messages` 三节点，边为 `START→load_memory→retrieve→build_messages→END`
- [ ] AC3：实际执行顺序为 load_memory → retrieve → build_messages
- [ ] AC4：有记忆、有检索结果时，输出状态同时含 `system_prompt`（以 SYSTEM_PROMPT 开头且含记忆文本）、`context_chunks`（原样透出）、`history_messages`（含当前 user 消息）与 `history_total`
- [ ] AC5：最终 `messages` 顺序为 system(含记忆) → history → RAG system（ chunks 非空时）；RAG 消息含 `[1] 标题` 与 `用户问题：`
- [ ] AC6：传入 `paper_id` 时检索 `filters == {"paper_id": paper_id}` 且 `top_k == RETRIEVE_TOP_K`
- [ ] AC7：`skill="translator"` 时 `skill_prompt` 非空且为 `messages` 最后一条；不传 skill 时无注入
- [ ] AC8：向量库不可用或检索抛异常时 `context_chunks == []` 且图正常完成（无 RAG system 消息）
- [ ] AC9：`enable_web_search=True` 或启发式命中（如「最新的…」）时 `web_search_enabled is True` 且消息含 `WEB_SEARCH_HINT`；默认均为否

## 7. 现有测试覆盖与盲区

- **已实现（Phase C）**：
  - C1 `verify_citations` 纯函数全边界用例（有效/越界/零检索/无引用/混合/0 与负数/重复标记/日志脱敏）——`backend/tests/test_guardrails.py::TestVerifyCitations`（10 用例）
  - C2 零检索拒答硬约束（空检索注入 / 非空不回归）——`backend/tests/test_guardrails.py::TestNoRetrievalGuard`（2 用例）
  - C1 路由集成（落库文本剔除越界标记、citations 附 verified/removed）——`backend/tests/test_chat.py::TestGuardrailsIntegration`（2 用例）
- **已覆盖**：`backend/tests/test_agent_graph.py`（13 用例）
  - `TestGraphStructure`：编译与单例、节点存在、边拓扑、插桩验证执行顺序
  - `TestGraphOutputs`：三要素输出、记忆拼接、RAG 消息内容与位置、filters/top_k 透传
  - `TestSkillInjection`：skill 注入（含位置为最后一条）、无 skill 不注入
  - `TestEmptyRetrieval`：向量库不可用、检索抛异常两种降级
  - `TestWebSearchToggle`：显式开启、启发式命中、默认关闭
  - 全程 monkeypatch `get_vector_store`，不调真实 LLM / embedding
- **盲区**：
  - 历史消息超过 `HISTORY_LIMIT=10` 时的截断行为（只取最近 10 条、`history_total` 仍为总数）未测 —— **中**
  - `build_rag_prompt` 的字段缺省分支（title 缺失回退「未知文献」、authors/year/page 省略）未测 —— 低
  - `memory_context` 为空时 `system_prompt` 不追加记忆段的负分支未测 —— 低
  - `user_message` 为空时 retrieve 跳过检索的分支未测 —— 低
  - 多轮对话中 assistant/user 交替历史的保序性未测 —— 低
  - `get_agent_graph()` 双检锁的并发安全性未测 —— 低
  - 节点不 commit（只读承诺）无直接断言 —— 低

## 8. 关键设计决策

- **流式生成不进图**：LangGraph 流式语义与既有 SSE 三事件契约（delta / finished+citations / error）差异大，图内化会破坏前端契约；故图只做 LLM 前编排，生成留路由层。修改 SSE 契约前不得把 generate 移入图。
- **Prompt 逐字保留**：`SYSTEM_PROMPT` / `WEB_SEARCH_HINT` / `build_rag_prompt` 与原 chat.py 内嵌版本逐字一致，保证图化重构对模型行为零影响；改动任一字符串属行为变更，须先改规格并补测试。
- **检索失败静默降级**：retrieve 吞掉一切异常返回空列表——对话可用性优先于检索完整性，错误只进日志。
- **节点函数走模块级导入**：`get_vector_store` / `MemoryManager` / `build_skill_prompt` / `web_search_service` 均为模块级导入，专供测试 monkeypatch（测试 fixture 即 `monkeypatch.setattr(agent_graph, "get_vector_store", ...)`）；重构时不得改为函数内 import 的不可替换形式。
- **节点编译时绑定**：monkeypatch 节点函数后须重置 `_compiled_graph` 重新编译才生效（测试 `test_node_execution_order` 即如此）；新增节点时同步更新本规格与拓扑测试。
- **history 已含当前消息**：依赖路由层「先落库 user 消息再调图」的顺序约定，`build_messages` 不再单独追加 user 消息；调整该顺序会导致 user 消息缺失或重复。
