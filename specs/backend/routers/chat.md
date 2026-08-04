# routers/chat.py（对话端点组：SSE 流式对话 / 会话 CRUD / 消息删除与重生成 / 图片分析 / Skill 列表）规格说明书

> 本文件描述 `backend/app/routers/chat.py` 的**行为契约**（做什么），不描述实现细节。
> 本模块是对话子系统的 HTTP 包装层：LLM 调用契约归 `specs/backend/services/llm.md`（含 SSE 对线协议 3.6 节）、LLM 调用前编排归 `specs/backend/services/agent_graph.md`、记忆读写归 `specs/backend/services/memory_manager.md`、图片分析归 `specs/backend/services/image_analyzer.md`、Skill 注册表归 `specs/backend/services/skills.md`。本规格聚焦端点签名、参数校验、状态码、SSE 事件序列与落库时序，服务层细节一律引用衔接、不重复。
> 依据源码实际内容反向工程整理（2026-08-04，chat.py 全文 362 行）。

## 1. 背景与目标

`routers/chat.py` 承载 PaperMind 的全部对话交互，解决四类问题：

1. **流式对话**：`POST /api/chat` 以 SSE 把 LLM 增量实时推给前端 `ChatPanel`，并在生成前后完成会话/消息落库、记忆更新、RAG 编排；
2. **会话管理**：会话的列表/创建/删除/历史读取，以及「从某条消息起截断重聊」的消息删除；
3. **回答重生成**：对已有 assistant 消息原地重新生成（当前存在使端点不可用的已知缺陷，见 3.7）；
4. **辅助端点**：多模态图片分析（截图/表格/公式）与可用 Skill 列表。

设计边界（与 agent_graph.md 3.10 的分工一致）：**图负责 LLM 调用前的上下文编排；路由层负责会话/消息落库、记忆更新触发、流式生成与 SSE 事件格式**。

## 2. 范围

### 2.1 包含

- `POST /api/chat`：SSE 流式与非流式双分支、会话自动创建、用户消息落库时序、记忆更新时序、`message_count` 计数语义、assistant 消息落库条件与 citations 组装、SSE 事件序列与客户端取消语义
- 会话 CRUD：`GET/POST /api/chat/conversations`、`GET /api/chat/conversations/{id}/history`、`DELETE /api/chat/conversations/{id}`
- 消息操作：`DELETE .../messages/{message_id}`（自该消息起截断删除）、`POST .../messages/{message_id}/regenerate`（含 **NameError 已知缺陷**的规约）
- `POST /api/chat/analyze-image`：multipart 上传 + SSE 流式分析
- `GET /api/chat/skills`：Skill 列表透传
- 模块级死代码与死导入的规约（`_stream_response`、`Paper`、`ImageAnalysisRequest`）

### 2.2 非目标

- LLM 调用的重试/截断/temperature 特判/错误格式化：归 `specs/backend/services/llm.md`
- 记忆加载 → 向量检索 → 消息组装的编排逻辑（`run_pre_orchestration`、`SYSTEM_PROMPT`、`build_rag_prompt`、`HISTORY_LIMIT=10`、`RETRIEVE_TOP_K=5`）：归 `specs/backend/services/agent_graph.md`
- 短期/长期记忆的触发条件（消息数为 5 的倍数）、容量淘汰、`build_memory_context` 内容格式：归 `specs/backend/services/memory_manager.md`
- 图片 base64 编码、mime 猜测、多模态请求组装：归 `specs/backend/services/image_analyzer.md`
- Skill 注册表与 6 个默认 Skill 的内容：归 `specs/backend/services/skills.md`
- 全局 500 脱敏：归 `specs/backend/main.md`（本规格仅在错误路径中引用）

## 3. 行为契约

### 3.0 路由挂载与通用约定

- `main.py`：`app.include_router(chat.router, prefix="/api/chat", tags=["chat"])`。
- 所有端点 `db: Session = Depends(get_db)` 为请求级会话。
- 业务 404/400 的 `detail` 为**英文原文**（如 `"Conversation not found"`），直接透给前端——与全局异常处理器的中文脱敏 500（`error_code: "internal_error"`）是两条独立错误路径。
- Pydantic 校验失败（如 `ChatRequest` 缺 `message`）由 FastAPI 自动返回 422。

### 3.1 `list_conversations(db)`（`GET /api/chat/conversations`）

- **输入**：无
- **输出**：`List[ConversationResponse]`（`id / title / summary / message_count / created_at / updated_at`），按 `updated_at` **降序**（最近活动的会话在前）
- **副作用**：DB 只读
- **异常**：无显式处理；DB 异常 → 全局 500

### 3.2 `create_conversation(db)`（`POST /api/chat/conversations`）

- **输入**：无（**不接受请求体**，标题不可指定）
- **输出**：新建的 `ConversationResponse`，固定 `title="新对话"`、`message_count=0`
- **副作用**：`conversations` 表插入一行并 commit
- **注意**：对话中的首次 `POST /api/chat` 也会自动建会话（3.8），本端点仅为「先建空会话」场景存在

### 3.3 `get_history(conversation_id, db)`（`GET /api/chat/conversations/{conversation_id}/history`）

- **输入**：路径参数 `conversation_id: int`
- **输出**（无 response_model，手写 dict）：
  ```json
  {"conversation": <Conversation ORM 序列化全字段>,
   "messages": [{"id": int, "role": str, "content": str, "citations": list}, ...]}
  ```
  消息按 `created_at` **升序**；`skill_used` / `token_usage` 字段**不返回**（模型有列，接口裁剪）
- **异常**：会话不存在 → 404 `"Conversation not found"`
- **副作用**：DB 只读

### 3.4 `delete_conversation(conversation_id, db)`（`DELETE /api/chat/conversations/{conversation_id}`）

- **输出**：204 No Content
- **后置条件**：会话行删除；其全部消息经 ORM `cascade="all, delete-orphan"`（`models.py` Conversation.messages）**级联删除**
- **异常**：会话不存在 → 404
- **副作用**：`conversations` + `messages` 两表写入（删除）；**不清理** `memory_summaries` 中 `source_conversation_id` 指向该会话的短期记忆（残留为无属主数据，memory 路由可独立清理）

### 3.5 `delete_messages_from(conversation_id, message_id, db)`（`DELETE /api/chat/conversations/{conversation_id}/messages/{message_id}`）

- **语义**：「从这条消息开始重聊」——删除**目标消息本身及其后（`created_at` 升序）的所有消息**
- **输出**：204 No Content
- **异常**：会话不存在 → 404；消息不存在于该会话 → 404 `"Message not found"`
- **副作用**：`messages` 表批量删除并 commit
- **已知契约缺陷**：**不回溯 `Conversation.message_count`**——删除后计数保持删除前的值，与真实消息数脱节（`updated_at` 因会话对象未触碰也不刷新，会话列表排序不变）

### 3.6 `regenerate_message(conversation_id, message_id, db)`（`POST /api/chat/conversations/{conversation_id}/messages/{message_id}/regenerate`）

- **语义**：以目标 assistant 消息**前一条 user 消息**为 query，重新流式生成并**原地替换**目标消息内容
- **输出**：`StreamingResponse`（SSE，帧格式同 3.10）
- **前置校验**（按序）：
  1. 会话不存在 → 404；
  2. 目标消息不存在或 `role != "assistant"` → 404 `"Assistant message not found"`；
  3. 目标消息是该会话第一条消息（`created_at` 升序索引为 0）→ 400 `"No user message before this assistant message"`；
  4. 前一条消息 `role != "user"` → 400 `"Previous message is not a user message"`
- **编排（不走 agent_graph，独立实现）**：
  1. 检索：`store.available()` 为真时 `store.search(query=prev_user_msg.content, top_k=5)`，任何异常 try/except 吞掉 → `retrieved=[]`（记 `[regenerate]` error 日志）；
  2. 记忆：`MemoryManager(db).build_memory_context()` 非空时拼到 `SYSTEM_PROMPT` 尾部（格式与 agent_graph 的 load_memory 一致）；
  3. 消息组装：`[system(含记忆)] + 目标之前的全部历史消息 + [RAG system（retrieved 非空时，build_rag_prompt）]`；
  4. 与 `POST /api/chat` 的差异：**无 `HISTORY_LIMIT=10` 截断（全量历史注入）、无 skill 注入、无 paper_id 过滤、无联网搜索提示注入、不触发 `update_short_term_memory`、不动 `message_count`**。
- **已知缺陷（严重程度高，端点实际不可用）**：`event_stream` 闭包内 `llm_service.chat_stream(messages, enable_web_search=enable_web_search)` 引用的 `enable_web_search` 在 `regenerate_message` 函数作用域与模块级**均未赋值**（AST 实证；自初始提交 `cfbc1b2` 起即如此，P3.2 LangGraph 重构未触及）。后果：通过全部前置校验后，流式迭代**首次求值即抛 `NameError`**——SSE 响应头已发出、连接中断，客户端收不到任何帧，目标消息内容**不被修改**；异常不经全局 500 脱敏（响应已开始），仅由服务器日志记录。修复前应将该端点视为不可用；修复（如补 `enable_web_search=False`）属行为变更，须先改本规格并补 RED 测试（宪法第 5 条）。
- **成功路径后置条件（缺陷修复后的预期契约）**：用**新 `SessionLocal`** 把 `full_content` 写回目标消息 `content`，`citations` 覆写为 `[{"source": r["source"], "paper_id": r["paper_id"]} for r in retrieved]`（**比 3.8 落库更裁剪**，仅 2 键）；随后发 finished 尾帧（`citations` 为原始 `retrieved`）。
- **取消语义**：`asyncio.CancelledError`（客户端断开）→ 记 info 日志并 return，**原消息内容保留不变**。

### 3.7 `analyze_image(file, question)`（`POST /api/chat/analyze-image`）

- **输入**：`multipart/form-data`；`file: UploadFile` 必填；`question: str = Form("请描述这张图片的内容，并解释其在学术论文中可能的含义。")` 可选
- **前置校验**：文件字节为空 → 400 `"图片内容为空"`；`filename` 缺失按 `"image.jpg"` 处理
- **路由层无校验项**：不限制文件大小、不校验真实图片格式/mime（mime 由服务层按文件名后缀猜测，见 image_analyzer.md）
- **输出**：SSE 流——增量帧 `{"delta": ..., "finished": false}`（**无 `conversation_id`**，不落库），尾帧 `{"delta": "", "finished": true}`
- **异常**：分析过程异常由服务层吞掉并以带内错误串（`\n[图片分析失败: ...]`）作为普通 delta 产出（image_analyzer.md），路由层无 try/except
- **副作用**：网络调用（多模态 LLM）；**无任何 DB 写入**
- **死导入**：`schemas.ImageAnalysisRequest` 已定义且被本模块 import，但端点用 `Form` 参数、从未使用该模型

### 3.8 `chat(request: ChatRequest, db)`（`POST /api/chat`）——核心端点

- **输入**（JSON，`ChatRequest`，照抄 schemas）：

| 字段 | 类型 | 缺省 | 语义 |
|------|------|------|------|
| `message` | `str` | **必填**（缺失 → 422） | 无长度限制；空串合法（检索节点会跳过，agent_graph.md 3.5） |
| `conversation_id` | `Optional[int]` | `None` | 缺省时自动创建新会话 |
| `paper_id` | `Optional[int]` | `None` | 限定检索范围（透传编排层） |
| `stream` | `Optional[bool]` | `True` | **仅 `is False` 走非流式**；`None` 视为流式 |
| `enable_web_search` | `Optional[bool]` | `False` | 显式联网开关（经 `bool()` 归一后透传编排层，与启发式判定取或） |
| `skill` | `Optional[str]` | `None` | Skill ID（未注册 ID 编排层不注入，不报错） |

- **处理时序（计数与记忆写入顺序为本节核心契约，按代码顺序）**：
  1. 写 `[chat]` info 日志（含 `message[:50]`）；
  2. `conversation_id` 给定：查会话，不存在 → 404；缺省：`Conversation(title=message[:30] or "新对话", message_count=0)`，add + **flush**（未 commit）；
  3. 用户消息 `Message(role="user", content=message, citations=[])` add + **flush**（未 commit）；
  4. **记忆更新**：`await MemoryManager(db).update_short_term_memory(conv.id)`——注释称「异步后台触发，不阻塞回复」，**实际为内联 await**：当会话消息总数（含刚 flush 的用户消息）为 5 的倍数时，真实调用 LLM 生成会话摘要（`summarize_conversation`，Kimi 复杂调用可达 60–120 秒，**阻塞本请求**）；服务层内部全吞异常，路由层再包一层 try/except（双重兜底，失败仅记 `[chat]` error 日志）；写入经 `add_memory` 内部 commit + 容量淘汰（memory_manager.md）。**时序后果**：第 5 步编排中的 `build_memory_context` 能读到本轮刚写入的短期记忆（当轮生效）；
  5. **前置编排**：`run_pre_orchestration(db, conversation_id, user_message, skill, paper_id, enable_web_search=bool(...))` → 取 `messages` / `context_chunks`（即 `retrieved`）/ `web_search_enabled` / `history_total`（编排规则全部归 agent_graph.md；检索失败内部降级为空，不阻断）；
  6. **计数与提交**：`conv.message_count = history_total + 1`，`db.commit()`——用户消息此刻才真正落盘；`message_count` 语义 = **当前消息总数（含本条 user）+ 预记 1 条即将生成的 assistant**；
  7. 分支生成（见下）。
- **非流式分支**（`request.stream is False`）：
  - `content = await llm_service.chat_completion(messages)`——**不传 `enable_web_search`**（`chat_completion` 无此参数），**联网开关在非流式路径不生效**（已知不对称，流式路径生效）；
  - assistant 消息落库并 commit，`citations` 为裁剪版 7 键 dict（`source / paper_id / title / authors / year / page_number / content`）；
  - 返回 JSON `{"conversation_id": int, "content": str, "citations": retrieved}`（**`citations` 为原始 chunk dict**，与落库裁剪版不同）；
  - LLM 失败时 `content` 为 `[调用 LLM 出错: ...]` 错误串且**照样落库**（llm.md 3.7 的既定契约），HTTP 仍 200。
- **流式分支**（默认）：`StreamingResponse(event_stream(), media_type="text/event-stream")`，响应头固定 `Cache-Control: no-cache`、`Connection: keep-alive`、`X-Accel-Buffering: no`；SSE 事件序列与取消语义见 3.10。
- **副作用汇总**：`conversations` 可能插入；`messages` 插入 1–2 行；`conversations.message_count`/`updated_at` 更新；`memory_summaries` 可能写入（第 4 步）；LLM 网络调用；流式路径 assistant 落库使用**新 `SessionLocal`**（原请求会话已随响应生命周期关闭）。
- **异常**：编排层 `load_memory`/`build_messages` 的 DB 异常向上冒泡 → 全局 500 脱敏（用户消息已 flush 未 commit，随会话回滚，**不会产生半截落库**）；LLM 异常不冒泡（带内错误串契约，llm.md 3.5）。

### 3.9 `get_skills()`（`GET /api/chat/skills`）

- **输入**：无
- **输出**：`list_skills()` 原样透传——`[{"skill_id": str, "display_name": str, "description": str}, ...]`（无 response_model）；当前为 6 个默认 Skill（translator / proofreader / method_comparator / outline_generator / data_analyst / writing_assistant，skills.md）
- **副作用**：无（内存注册表只读；DB `skills` 表不参与）
- **异常**：无显式处理

### 3.10 SSE 对线协议（`POST /api/chat` 流式分支与 regenerate 共用）

| 阶段 | 帧格式（`data: ` + JSON + `\n\n`，`ensure_ascii=False`） |
|------|----------------------------------------------------------|
| 增量 | `{"delta": "<文本>", "finished": false, "conversation_id": N}` |
| 完成 | `{"delta": "", "finished": true, "conversation_id": N, "citations": <原始 retrieved chunk dict 列表>}` |
| 错误 | `{"error": "..."}`——**前端 ChatPanel 兼容解析，但当前后端所有端点从不产生**；LLM 失败走带内错误串（作为普通 delta，随后照常发 finished 帧，错误串会随 `full_content` 落库） |

- **落库时序**：尾帧**之前**完成 assistant 落库——`full_content.strip()` 非空才写库（全空回复不落库）；落库用新 `SessionLocal`；落库 `citations` 为裁剪版 7 键 dict，与尾帧的原始 dict 不同。
- **取消语义**：每发一帧后 `await asyncio.sleep(0)` 让出控制权，使客户端断开能触发 `asyncio.CancelledError` → 记 `[chat]` info 日志并 return；**已提交的用户消息与 `message_count` 保留（计数因此比实际消息数多 1）**，assistant 消息不落库。
- `analyze-image` 的帧为子集：`{"delta", "finished"}`，无 `conversation_id` / `citations`。

### 3.11 模块级死代码规约

- `_stream_response(messages)`（第 27–30 行）：产生**无 `conversation_id`** 的旧式帧的异步生成器，**全项目零调用**（grep 实证），属 LangGraph 重构残留；
- `from app.models import ... Paper`、`ImageAnalysisRequest`：导入未使用；
- 清理上述三者不改变任何外部行为，但按宪法第 6 条（最小改动）应独立成提交。

## 4. 边界条件与错误处理

| 场景 | 期望行为 |
|------|----------|
| `conversation_id` 指向不存在会话 | 404 `"Conversation not found"`（chat / history / delete / delete_messages / regenerate 一致） |
| 新会话首条消息（无 `conversation_id`） | 自动建会话，`title = message[:30] or "新对话"` |
| `message` 为空串 | 合法：建会话标题回退 `"新对话"`；编排层跳过检索；LLM 行为由 Kimi 决定 |
| `stream=None` | 走流式分支（仅 `is False` 走非流式） |
| 非流式 + `enable_web_search=true` | **联网不生效**（`chat_completion` 无该参数），静默忽略 |
| 消息总数恰为 5 的倍数 | `update_short_term_memory` 触发真实 LLM 摘要调用，**阻塞当前请求**；失败双重兜底不影响对话 |
| 记忆更新抛异常 | 仅 error 日志，对话照常（服务层 + 路由层双 try/except） |
| 检索失败 / 向量库不可用 | 编排层降级 `retrieved=[]`，对话照常（agent_graph.md 3.5） |
| LLM 全部重试失败 | 带内错误串作为普通回复渲染并落库；HTTP 200；随后照常 finished 帧 |
| 客户端流式中断 | CancelledError → assistant 不落库；用户消息已落库；`message_count` 比实际多 1 |
| LLM 返回全空/纯空白 | 流式：assistant 不落库，仍发 finished 帧 |
| 删除会话 | 消息级联删除；`memory_summaries` 短期记忆残留（不级联） |
| 删除消息（from 语义） | 目标及其后全部删除；`message_count` 不回溯（缺陷） |
| regenerate 通过前置校验 | **当前必因 NameError 失败**（3.6 缺陷）；修复后：原地替换内容与 citations |
| regenerate 目标为 user 消息 / 首条消息 / 前条非 user | 404 / 400 / 400 |
| analyze-image 空文件 | 400 `"图片内容为空"` |
| analyze-image 任意大小/格式 | 路由层不拦截；行为由服务层与 Kimi 决定 |
| 并发同一会话发两条 chat | 无锁；两请求各自落库用户消息，`message_count` 后写覆盖先写（单用户场景未规约） |

## 5. 依赖

- **上游依赖**：
  - `app.models.Conversation / Message`（`Paper` 为死导入）；`app.schemas.ChatRequest / ConversationResponse`（`ImageAnalysisRequest` 为死导入）
  - `app.database.get_db / SessionLocal`（流式落库用新会话）
  - `app.services.llm.llm_service`（`chat_stream` / `chat_completion`，llm.md）
  - `app.services.agent_graph.run_pre_orchestration / SYSTEM_PROMPT / build_rag_prompt`（agent_graph.md）
  - `app.services.retrieval.get_vector_store`（仅 regenerate 直接使用）
  - `app.services.memory_manager.MemoryManager`（memory_manager.md）
  - `app.services.image_analyzer.image_analyzer_service`（image_analyzer.md）
  - `app.services.skills.list_skills`（skills.md）
  - `app.core.logger.logger`
- **下游消费者**：前端 `components/ChatPanel.jsx`（SSE 解析，兼容 `{error}` 帧；`analyze-image` 亦由其发起），经 `App.jsx` 全局挂载；HTTP 封装在 `api.js`。无其他后端消费者。

## 6. 验收标准（可测试）

- [ ] AC1：`POST /api/chat` 无 `conversation_id` 时自动建会话（`title=message[:30]`）、用户消息落库、响应帧序列符合 3.10（首帧起 delta、末帧 finished=true + conversation_id + citations）
- [ ] AC2：`conversation_id` 不存在 → 404；缺 `message` → 422
- [ ] AC3：非流式分支返回 `{conversation_id, content, citations}`，assistant 消息落库且 citations 为 7 键裁剪版
- [ ] AC4：消息总数为 5 的倍数时 `update_short_term_memory` 被触发（mock 断言调用）；其抛异常时响应不受影响
- [ ] AC5：`message_count` 在提交后等于 `history_total + 1`；流式取消后计数不回退（固化现状）
- [ ] AC6：会话 CRUD——列表按 `updated_at` 降序；创建固定 `"新对话"`；history 消息升序且字段裁剪为 4 键；删除会话级联删除消息 → 204；不存在均 404
- [ ] AC7：`delete_messages_from` 删除目标及其后全部消息；`message_count` 不更新（固化现状缺陷）
- [ ] AC8：regenerate 前置校验 404/400 四分支；**通过校验后当前抛 NameError（缺陷固化用例，标注 xfail/已知缺陷）；修复后**：目标消息内容被替换、citations 为 2 键裁剪、取消时原内容保留
- [ ] AC9：analyze-image 空文件 → 400；正常文件 SSE 帧序列 `delta* + finished`；服务层异常时带内错误串
- [ ] AC10：`GET /api/chat/skills` 返回 6 个默认 Skill 的 3 键 dict 列表
- [ ] AC11：LLM 失败时错误串作为普通 delta 送达并落库，无 `{error}` 帧（与 llm.md AC5 联动的端到端用例）

## 7. 现有测试覆盖与盲区

- **已覆盖**：**无。`backend/tests/` 中不存在 `test_chat.py`**，grep 全测试目录无任何 `/api/chat` 或 `routers.chat` 引用；相关测试仅有 `test_agent_graph.py`（编排层，mock 掉路由）与 `test_skills.py`（服务层注册表，未经路由）、`test_memory.py`（MemoryManager，mock LLM）。
- **盲区**（按严重程度）：
  - **高**：`POST /api/chat` 全链路零覆盖——SSE 帧序列、自动建会话、用户消息落库时序、`message_count = history_total + 1`、非流式分支、404/422（AC1–AC5）
  - **高**：regenerate 的 **NameError 缺陷无任何测试暴露**——该端点自初始提交起实际不可用，前置校验四分支与成功/取消路径均无覆盖（AC8）
  - **高**：会话 CRUD 与 `delete_messages_from` 的 from 截断语义、级联删除、message_count 不回溯缺陷，均无测试（AC6/AC7）
  - **中**：`update_short_term_memory` 的触发时机（5 的倍数）与双重兜底在路由层无测试（AC4，memory_manager 服务层另有覆盖）
  - **中**：analyze-image 的 400 与 SSE 帧、服务层带内错误串透传无测试（AC9）
  - **中**：`GET /api/chat/skills` 路由层无测试（AC10；服务层 `list_skills` 已由 test_skills.py 覆盖）
  - **低**：流式取消（CancelledError）后计数多 1、LLM 空回复不落库、错误串落库（AC5/AC11）无测试
  - **低**：非流式路径联网开关静默失效、死代码 `_stream_response` / 死导入，无测试固化

## 8. 关键设计决策

- **流式生成不进 LangGraph**：编排（记忆/检索/组装）与生成解耦——图的流式语义与既有 SSE 三事件契约差异大，强行图内化会破坏前端契约（agent_graph.md 第 8 节）；故本路由保留手写 `event_stream`。
- **用户消息「先 flush 编排、后随计数 commit」**：编排的 `load_memory` 必须在同一事务内看到本轮 user 消息（否则历史缺失），而最终提交与 `message_count` 更新合并为一次 commit——保证「用户消息 + 计数」原子落盘；取消/LLM 失败时不会出现「有用户消息但计数缺失」的反向不一致（只会计数多 1，见 4 节）。
- **记忆更新内联 await 而非真后台**：注释声称「不阻塞回复」，实现却是阻塞式（5 的倍数时调 LLM 摘要）。保留现状的隐性收益是**当轮 prompt 即可读到新摘要**；代价是该轮首帧延迟可达分钟级。改真后台（`asyncio.create_task`）属行为变更，需先改规格。
- **assistant 落库用新 `SessionLocal`**：StreamingResponse 迭代发生在请求会话（Depends 生命周期）关闭之后，必须用全新会话写库；非流式分支无此问题，直接复用请求会话。
- **citations 双形态**：尾帧给前端的 `citations` 是原始 chunk dict（信息全，供展示）；落库的是裁剪 7 键 dict（regenerate 进一步裁为 2 键）——历史遗留的不一致，前端两种都消费，固化现状。
- **regenerate 独立编排而非复用 agent_graph**：历史代码路径（初始提交即存在），与主链路相比缺历史截断/skill/联网提示，且携带 3.6 的 NameError 缺陷——**重构方向是复用 `run_pre_orchestration` 消除分叉**，但属行为变更，须规格先行。
- **后端从不发 `{error}` 帧**：错误一律带内化（llm.md 第 8 节决策），前端把错误串当正常回复渲染并落库——体验上「对话不中断」，代价是错误内容进入对话历史与后续 prompt 上下文。
