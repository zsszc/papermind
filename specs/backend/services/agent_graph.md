# Agent 对话编排规格（Batch 20 当前实现）

> 适用文件：`app/services/agent_graph.py`。最后核对：2026-08-24。

## 1. 图结构

```text
START → load_memory → retrieve → graph_expand → external_tools → build_messages → END
```

图只负责 LLM 调用前编排；流式生成仍由 `routers/chat.py` 驱动，以保持现有 SSE
`delta / finished+citations / error` 契约。

## 2. 状态与节点

- 输入至少包含 db、conversation_id、user_message；可选 skill、paper_id、enable_web_search。
- `load_memory`：读取最近 10 条历史与统一 Memory 上下文，构造基础 system prompt。
- `retrieve`：调用共享 `RetrievalPipeline`，top_k=5；paper_id 同时限制语义与词法分支。
- `graph_expand`：配置默认 false；开启时沿引用图扩展代表 chunk，与当前结果做 chunk RRF；
  无边或异常保持内容不变。共享管线可能为缓存隔离复制对象，因此不保证对象 identity。
- `external_tools`：仅在触发词、可用工具和 10 秒预算同时满足时补充外部上下文；异常降级。
- `build_messages`：按 system、history、RAG、外部上下文、联网提示、Skill 顺序组装消息。

## 3. 检索配置

- `retrieval.chat_profile` 缺失时默认 `hybrid`；可显式回退 `semantic`。
- `retrieval.lexical_profile` 缺失时默认 `bm25-bilingual`。
- top_k 固定 5；Hybrid 两路内部候选池由管线扩到 10。
- rerank/graph_expand 默认关闭，不能因实验配置隐式开启。
- 任何检索异常不得中断聊天；结果为空时注入零检索拒答约束。

`POST /api/chat` 首次回答经 Agent 图；消息重新生成虽不重新跑完整图，但必须调用同一
RetrievalPipeline 和相同 profile/lexical 配置，禁止纯语义旁路。

## 4. 引用契约

- 本地 chunk 按上下文顺序编号 `[1]`，模型引用格式为 `[^n^]`。
- `verify_citations` 只保留 `1 <= n <= len(context_chunks)` 的标记；越界标记删除并记录。
- 外部工具结果不进入本地 citations；本地结果保留 paper/title/authors/year/page/content。
- 无检索结果时模型不得编造引用。

## 5. 失败安全

- Memory、检索、Graph、MCP 任一可选环节失败不得阻断基本对话。
- 日志只记录异常类型/通用上下文，不向前端回显底层异常或密钥。
- paper_id 是限制性范围，底层过滤失败必须 fail-closed，不能放宽到全库。

## 6. Harness

- `test_agent_graph.py`：图结构、Memory、检索配置/filters、消息组装和降级。
- `test_retrieval_pipeline_parity.py`：聊天与 eval 排序一致。
- `test_graph_expand.py`：图扩展开关、RRF、异常透传。
- `test_agent_external_tools.py`：外部工具触发、预算与降级。
- `test_chat.py`：SSE、重新生成和引用 Guardrail。
