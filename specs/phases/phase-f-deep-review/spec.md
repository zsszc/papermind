# Phase F：Deep Agents 文献综述长任务 规格说明书

> 来源：Phase 2 计划 Phase F 章节 + agent_graph.md / chat.md / llm.md 现状契约。
> 目标：支持「帮我综述这个方向的 N 篇论文」类长任务（规划 → 分派 → 汇总）。

## 1. 背景与目标

现状对话是单轮 RAG 问答。综述类任务需要多步：把主题拆成子问题、逐子问题检索生成、汇总成结构化综述。本阶段新增独立的深度综述链路（不动现有 /api/chat）。

## 2. 现状（代码实证）

- `services/agent_graph.py`：4 节点前置编排图（Phase E 后），服务单轮问答
- `services/llm.py`：`chat_stream` / `chat_completion` / `chat_completion_sync`（Langfuse 观测已接入）
- `routers/chat.py`：SSE 事件契约 `{delta}` / `{finished, citations}` / `{error}`，Guardrails 落库前校验
- 复用锚点：子问题的检索走 `retrieval` 服务，生成走 `llm_service`（宪法第 8 条唯一入口）

## 3. 设计

### 3.1 F1：deep_review 服务（`services/deep_review.py`，新建）

**依赖决策（第一道工序，同 Phase D 模式）**：`pip install deepagents` → `env -u PYTHONPATH venv/bin/pip check`。deepagents 依赖 langchain-core 新版，与本项目锁定栈冲突概率高；**冲突则不引入，改为基于现有 LangGraph 1.2.9 手写「规划→分派→汇总」三节点子图**（行为契约不变，库选型是实现细节）。决策结果记入本文件 7 节。

行为契约（无论用哪个实现路径）：

```
plan(topic, n_papers?) -> List[SubQuestion]     # LLM 拆 3-5 个子问题
execute(sub_question, db=...) -> SubAnswer      # 共享 RetrievalPipeline + LLM，带本地引用
synthesize(topic, sub_answers) -> Review        # LLM 汇总：引言/分节/结论，保留 [^n^] 引用
```

- 中间产物不落库（内存态即可；deepagents 虚拟 FS 仅在其可用时使用）
- 每步失败降级：单个子问题失败不阻塞整体（该节标记「该子问题检索不足」）；plan 失败 → 返回错误事件
- 全程经 llm_service（Langfuse 自动观测）

### 3.2 F2：API 端点 `POST /api/chat/deep-review`

- 请求：`{topic: str, conversation_id?: int}`；SSE 流式
- 事件序列：`{type:"plan", questions:[...]}`（新增 plan 事件类型）→ 多个 `{delta}` → `{finished, citations}`
- 复用 chat.py 的 SSE 帧格式与 Guardrails 落库前校验（citations 仅本地 chunk，外部来源不进）
- 前端开关留 Phase H；本阶段仅 API

### 3.3 测试计划（全程 mock LLM，Moonshot 冻结不影响）

- plan 拆分：mock LLM 返回固定子问题列表 → 断言解析结构
- 子问题执行：mock 共享 hybrid + llm → 断言每子问题独立检索、关键词降级与引用传递
- 汇总：断言结构化输出与引用保留
- 降级：单子问题失败 / plan 失败两条路径
- API：TestClient 流式帧序列断言（含 plan 事件）

## 4. 接口与数据

- 新增：`POST /api/chat/deep-review`（SSE）；`services/deep_review.py` 内部 API
- schemas.py 新增 DeepReviewRequest
- 无 DB 表变化

## 5. 验收标准（可测试）

- [ ] AC1：依赖决策有明确结论记录（装了什么/或为什么手写）
- [ ] AC2：mock 下 plan/execute/synthesize 三段行为契约用例全绿
- [ ] AC3：SSE 帧序列断言通过（plan 事件在先）
- [ ] AC4：全套件回归全绿；pip check 零冲突（若引入 deepagents 则验证其兼容版本锁定）
- [ ] AC5（遗留门控）：Moonshot 解冻后真实 3 篇综述端到端跑一次

## 6. 现有测试覆盖与盲区

- 新增 `tests/test_deep_review.py` + chat 路由 deep-review 用例
- 盲区/遗留：真实 LLM 端到端（AC5）；长任务取消；前端开关（Phase H）

## 7. 风险与回退

- **deepagents 依赖冲突**（大概率）→ 回退手写三节点（契约不变）
- **长任务耗时**（5 子问题 × 生成 60-120s 可能超 10 分钟）→ 初版子问题数硬上限 5；超时由前端/网关层后续处理
- **Moonshot 冻结** → AC5 遗留；mock 测试不受阻
- 回退：不调用新端点即零影响（独立链路，不改现有 /api/chat）

### 7.1 F1 依赖决策结论（2026-08-05 实测记录）

**结论：不引入 deepagents，回退手写「规划→分派→汇总」三段（行为契约不变）。**

实测过程（`env -u PYTHONPATH venv/bin/pip install deepagents` → `pip check`）：

1. deepagents 0.7.4 拖入 `httpx-0.28.1`、`pydantic-2.13.4`、`langchain-1.3.14` 等 18 个包；
2. `pip check` 表面零冲突（openai 1.12 声明约束为 `httpx<1`，声明层拦不住 0.28.1）；
3. **运行时实测击穿**：openai 1.12.0 + httpx 0.28.1 构造 client 即
   `TypeError: Client.__init__() got an unexpected keyword argument 'proxies'`，
   即宪法 §16 记录的不兼容；`services/llm.py` 导入即构造 client，引入会炸掉全站 LLM 唯一入口。

恢复与验证：卸载 deepagents 及其传递依赖，`httpx==0.27.2` / `pydantic==2.7.4` 回锁，
`langchain-core==1.4.7`（langgraph 1.2.9 声明下限）恢复；`pip check` 零冲突，
全套件 387 基线无回归。requirements.txt / pyproject.toml 零改动。
