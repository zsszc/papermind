# PaperMind Phase 2：评测深化 × 检索提升 × Agent 技术栈 × UI 迁移 总体实施计划

> **For Hermes:** 按阶段（Phase A→Z）顺序执行；每个 Phase 开始前先读本文件对应章节 + AGENTS.md，完成后门控验证（pytest 全绿 + 复测对比 + 实机冒烟）再进入下一 Phase。每 Phase 结束 commit + push。

**Goal:** 在 P0–P5 工程底座之上，完成「真实论文数据集上的评测体系与检索质量提升」，接入 7 项 Agent 技术（Reranker / Langfuse / MCP 客户端 / Deep Agents / GraphRAG / Guardrails / AG-UI），并将前端迁移到 Figma Make 产出的新 UI，最终具备部署到阿里云轻量服务器的条件。

**Architecture:** 后端保持单进程 FastAPI，渐进增强：检索层加 Reranker 重排；可观测性接 Langfuse（自托管 docker 服务）；Agent 层在现有 `agent_graph` 上扩展节点（Guardrails 校验、MCP 工具调用、Deep Agents 规划、GraphRAG 图谱检索）；前端整体替换为 Figma Make 导出的 React+TS+shadcn/ui 代码库，并在其上集成 AG-UI/CopilotKit。

**Tech Stack:** FastAPI 0.110 / LangGraph 1.2.9 / mcp 1.3.0 / BGE-M3 + BGE-Reranker / Langfuse（docker 自托管）/ deepagents / React 18 + TS + shadcn/ui + CopilotKit / 阿里云轻量应用服务器（已就绪，部署最后做）

**明确不做：** 沙箱（用户已决定暂缓）；多租户/账号体系（部署前再议）

---

## 现状盘点（2026-07-28 16:30）

- 代码基线：`30f84a9`（origin/main 同步），pytest **154 全绿**，前端 lint/build 通过
- 评测基线（1 篇示例论文）：hybrid recall@5=0.477 / MRR=0.534；报告在 `backend/eval/reports/`（gitignored，含 `评测报告_2026-07-28.md`）
- **新资产**：`papers/` 新增 18 篇真实论文（共 20 个 PDF）；`UI Redesign for PaperMind.zip`（Figma Make 导出，React+TSX+shadcn/ui，110KB）
- **评测缺口盘点**（用户问"是否全部可做的评测"——答案：还有 5 项可做）：
  1. 生成侧评测（`--with-llm` 从未实跑，含幻觉负例拒答检查）
  2. 检索延迟/性能评测（P50/P95，embedding 冷启动耗时）
  3. 引用忠实度评测（AI 答案的 [^n^] 是否真的来自检索 chunk——与 Guardrails 联动）
  4. 多论文扩充 QA 数据集（现 25 条全部针对 1 篇示例论文，18 篇新论文无覆盖）
  5. 评测趋势追踪（多次报告横向对比，用户明确要求"看到每次修改后的过程和结果"）

---

## Phase A：真实论文导入 + 评测数据集扩充 + 趋势追踪

**目标：** 18 篇新论文全部入库并向量化；QA 数据集从 25 条扩到 80+ 条（覆盖新论文）；建立评测趋势机制，产出「旧报告 vs 新报告」对比。

### Task A1：解压并盘点 UI 包（只读侦察，为 Phase H 备料）
- 解压 `UI Redesign for PaperMind.zip` 到 `/tmp/papermind-ui-redesign/`（不入库）
- 盘点页面/组件清单、依赖（package.json）、与现有前端的信息架构差异
- 产出：`docs/ui-migration-notes.md`（不入库可放 /tmp，正式 notes 进 Phase H）

### Task A2：批量导入 18 篇论文
- 文件：`scripts/import_papers.py`（新建，不入库或入 scripts/）
- 逐篇调用 `POST /api/papers/upload`（复用 P1 的异步管线），等待处理完成（轮询状态）
- 验证：`GET /api/papers?limit=50` total=20；每篇 chunks 数 >0（`papers_fts` 与 ChromaDB 均有数据）
- 风险：个别 PDF 解析失败（PyPDF2 fallback 噪音）→ 记录失败清单，不阻塞

### Task A3：LLM 辅助扩充 QA 数据集
- 文件：`backend/eval/generate_qa.py`（新建）
- 对每篇新论文：取摘要+方法段，调 Kimi 生成 3–4 条候选 QA（question_type 分布参考现有种子），写入 `eval/dataset/qa_candidates.jsonl`
- **人工审稿环节**：候选集交给用户确认后合并进 `qa_seed.jsonl`（flag: `source: llm_generated, reviewed: true`）
- 负例同步扩充 3–5 条（新论文领域外的幻觉题）
- 验证：`pytest tests/test_dataset.py` 全绿（schema 校验会拦住坏样本）

### Task A4：评测趋势追踪脚本
- 文件：`backend/eval/trend.py`（新建）+ `backend/tests/test_trend.py`
- `python -m eval.trend`：扫描 `eval/reports/*.json`，按时间输出趋势表（recall@5/MRR/NDCG 逐次对比 + 分类型变化），写 `eval/reports/trend.md`
- 把 2026-07-28 的旧基线纳入趋势起点——**旧报告永不删除**（目录已 gitignore，天然保留）
- 验证：手造 2 份假报告，趋势表数值断言

### Task A5：全量复测 + 更新评测报告
- `python -m eval.run`（hybrid）→ 新基线（20 篇论文、扩充 QA）
- 更新 `eval/reports/评测报告_2026-07-28.md` 为「v2」，新增"历史对比"章节引用 trend.md
- 门控：pytest 全绿 + 趋势表生成 → commit `feat(eval): 真实论文数据集扩充 + 评测趋势追踪` + push

---

## Phase B：检索质量提升（BGE-Reranker + 弱项治理）

**目标：** 针对 factoid 0.100 / summary 0.000 短板，把 recall@5 推到 0.6+（20 篇规模下重新定标）。

### Task B1：接通 BGE-Reranker（代码已预留）
- 文件：`backend/app/services/retrieval.py`、`backend/app/core/config.py`（`retrieval.rerank: true` 已预留）
- 下载 BGE-Reranker-v2-m3（HF 镜像，~1GB），与 embedding 同样的后台线程懒加载模式
- 流程：向量+FTS 召回 top-20 → Reranker 重排 → 取 top-k
- 测试：`tests/test_retrieval_rerank.py`（mock reranker 模型，断言调用顺序与降级：reranker 不可用时回退原排序）
- 复测：`python -m eval.run` 对比 rerank on/off，写入趋势

### Task B2：summary 类短板——摘要级 chunk
- 文件：`backend/app/services/processor.py`（分块逻辑）
- 每篇论文额外生成 1 个"摘要级 chunk"（abstract+结论段，metadata 标 `chunk_type: abstract`），summary 类查询天然命中
- 测试：新论文入库后 abstract chunk 存在且可被检索

### Task B3：factoid 类短板——QA 标注治理 + 多 chunk 标注
- 检查 5 条 factoid 低分题的 `relevant_chunks` 解析结果，过窄的放宽（同义段落多标）
- `eval/dataset.py` 支持 `relevant_chunks` 一对多解析（若已支持则只改种子数据）
- 复测入趋势

### Task B4：检索延迟评测
- `eval/metrics.py` 加 `latency_stats()`（P50/P95）；`eval/run.py` 输出检索耗时列
- 门控：recall@5 提升 ≥0.05（相对 Phase A 基线）→ commit `feat(retrieval): BGE-Reranker 重排 + 摘要级 chunk + 延迟指标` + push

---

## Phase C：Guardrails（防幻觉护栏）

**目标：** AI 回答的引用必须可溯源；检索不足时明确拒答而非编造。

### Task C1：引用忠实度校验（后置 guardrail 节点）
- 文件：`backend/app/services/agent_graph.py` 新增 `verify_citations` 节点（generate 之后、路由层落库之前调用）
- 规则：答案中每个 `[^n^]` 必须对应本次检索返回的第 n 个 chunk；无检索结果时禁止出现 `[^n^]`
- 违规处理：剔除无效引用标记 + 在 citations 字段标注 `verified: false`（不阻塞返回，先观测）
- 测试：`tests/test_guardrails.py`（伪造答案文本，断言校验行为）

### Task C2：检索不足拒答强化
- system prompt 增加硬约束（无 chunk 时必须声明"文献库中没有相关内容"）
- eval `--with-llm` 的负例拒答检查从此生效（Phase A 数据集已含负例）

### Task C3：引用忠实度进评测
- `eval/metrics.py` 的 `citation_coverage` 接入 `--with-llm` 流程，作为生成侧正式指标
- 门控：pytest 全绿 + 负例拒答率入趋势 → commit `feat(agent): Guardrails 引用校验与拒答强化` + push

---

## Phase D：Langfuse 自托管可观测性

**目标：** 所有 LLM 调用可追踪（prompt/延迟/token/成本），与 eval 互补。

### Task D1：Langfuse docker 服务
- `docker-compose.yml` 增加 langfuse-web + langfuse-worker + postgres + clickhouse（官方 compose 精简版）——本地开发用，不上 git 的部分写 `.env.example`
- 文档：`docs/DEPLOY.md` 增加 Langfuse 章节

### Task D2：后端接入
- `pip install langfuse`（先验证与 httpx 0.27.2/pydantic 2.7.4 兼容，冲突则锁版本）
- `services/llm.py`：用 langfuse OpenAI drop-in wrapper 或 `@observe` 装饰器包裹 `chat_stream` / `chat_completion_sync`；环境变量 `PAPERMIND_LANGFUSE_*` 未配置时零侵入跳过
- trace 关联 conversation_id / skill / 检索 chunk 数（metadata）
- 测试：mock langfuse client，断言 trace 字段；未配置时调用链无副作用
- 门控：本地 Langfuse UI 能看到真实对话 trace → commit `feat(obs): Langfuse 自托管接入` + push

---

## Phase E：MCP 客户端化

**目标：** PaperMind 从"只被外部调"升级为"也能消费外部 MCP 工具"（arXiv 检索、网页抓取等）。

### Task E1：MCP client 管理器
- 文件：`backend/app/services/mcp_client.py`（新建）
- config.yaml 新增 `mcp_servers:` 配置块（name/command/args 或 url）；用 `mcp` SDK 的 stdio/SSE client 连接，发现工具列表，统一封装为内部 `Tool` 协议
- 测试：起一个本地 echo MCP server（mcp SDK 5 行可写），断言发现与调用

### Task E2：接入 agent_graph
- `agent_graph.py` 新增 `external_tools` 节点（retrieve 之后）：用户问题命中"最新/ arXiv/ 未入库"等信号时调用外部 MCP 工具补充上下文
- 内置首个外部服务器：arXiv（`arxiv-mcp-server`，pip 安装，需验证依赖兼容）
- 测试：mock client，断言节点触发条件与结果注入
- 门控：pytest 全绿 + 实测"检索一篇库中不存在的 arXiv 论文"链路 → commit `feat(agent): MCP 客户端化 + arXiv 外部检索` + push

---

## Phase F：Deep Agents（文献综述长任务）

**目标：** 支持"帮我综述这个方向的 N 篇论文"类长任务（规划→分派→汇总）。

### Task F1：接入 deepagents
- `pip install deepagents`（LangGraph 官方生态，验证依赖兼容）
- 文件：`backend/app/services/deep_review.py`（新建）：规划节点把综述任务拆成子问题 → 逐子问题走现有 retrieve+generate → 汇总成结构化综述（带引用）
- 文件系统用 deepagents 的虚拟 FS 存中间产物

### Task F2：API 与前端入口
- `POST /api/chat/deep-review`（SSE 流式，复用现有事件契约 + 新增 `plan` 事件类型展示规划进度）
- 前端 ChatPanel 增加"深度综述"开关（Phase H 新 UI 中正式落地，本阶段先 API）
- 测试：mock 规划与生成，断言子任务拆分、综述结构、引用完整
- 门控：端到端跑一次真实综述任务（3 篇论文）→ commit `feat(agent): Deep Agents 文献综述长任务` + push

---

## Phase G：GraphRAG（引用图谱）

**目标：** 文献间引用关系成图，支持"这个方法的后续工作有哪些"多跳问答。

### Task G1：参考文献解析
- 文件：`backend/app/services/reference_parser.py`（新建）
- 从 PDF 末尾 References 段提取条目（正则 + 启发式，先不引 GROBID 重依赖）；标题模糊匹配库内文献，建立 `paper_citations` 表（citing_id → cited_id，ensure_schema 加迁移分支）
- 测试：构造含 References 的文本，断言解析条目数与匹配率

### Task G2：图谱检索节点
- `agent_graph.py` 新增 `graph_expand` 节点：对命中文献沿引用边扩展 1–2 跳，补充候选 chunk（与向量召回 RRF 融合）
- `GET /api/papers/{id}/citation-graph`：返回节点/边（前端 Phase H 可视化用）
- 测试：内存库造引用边，断言扩展结果
- 门控：eval 复测（graph on/off 对比入趋势）→ commit `feat(retrieval): GraphRAG 引用图谱检索` + push

---

## Phase H：UI 迁移（Figma Make TSX/shadcn）+ AG-UI

**目标：** 前端整体替换为新设计，并在其上集成 CopilotKit（AG-UI）。

### Task H1：新前端工程落地
- 解压 zip 代码到 `frontend-redesign/`（新目录，不动旧 frontend/ 直至切换）
- 补齐 package.json 依赖、接 `api.js` 等价层（指向 :8000）、vite 代理配置
- 逐页面对齐现有 8 个页面 + ChatPanel + PdfViewer 的功能（PDF 预览 react-pdf、SSE 解析逻辑从旧 ChatPanel 移植——P2 的 buffer 健壮解析必须带过来）

### Task H2：并行验证与切换
- 新旧前端并存跑（5173/5174），功能 checklist 逐项过（导入/检索/对话/标注/写作台/统计/导出）
- 切换：旧 `frontend/` 归档为 `frontend-legacy/`（或删除），新前端改名 `frontend/`，更新 electron/main.js 与打包配置、AGENTS.md
- 测试：lint + build + 实机全流程冒烟

### Task H3：AG-UI / CopilotKit 集成
- 新前端接 CopilotKit：`useCopilotAction` 把 MCP 工具（search_papers 等）暴露给 AI 直接驱动 UI（"把这篇标记已读"→ 界面自行动）
- 后端：CopilotKit runtime 接现有 `/api/chat` 或新增 `/api/copilotkit` 端点
- 测试：CopilotKit action 触发 → 后端工具调用 → UI 状态更新
- 门控：全流程冒烟 + 录屏验收 → commit `feat(ui): Figma 新 UI 迁移 + CopilotKit AG-UI 集成` + push

---

## Phase Z：部署到阿里云轻量服务器（最后做）

- 服务器已就绪（轻量应用服务器）；前置：Phase A–H 全部完成、域名/备案方案确认
- 按 `docs/DEPLOY.md`：docker-compose 起 backend + langfuse；nginx + HTTPS；数据卷与备份策略
- 上线 checklist：config.yaml（真实 Key 服务端持有）/ 安全组端口 / 快照策略 / health 探针
- 上线后跑一次 eval + 全流程冒烟作为验收

---

## 全局约定

- **门控**：每 Phase 结束必须 ①pytest 全绿 ②eval 复测入趋势（检索类 Phase 必须有 on/off 对比）③实机冒烟 ④commit+push
- **报告延续**：`backend/eval/reports/` 内所有历史报告与 trend.md 永久保留，评测报告每 Phase 追加版本章节
- **依赖纪律**：任何 pip install 后必须 `pip check` + pytest + 实机重启验证（mcp/starlette 冲突的教训）；新依赖锁版本进 requirements.txt + pyproject.toml + AGENTS.md 已知问题
- **测试纪律**：新增功能必带 pytest（mock LLM/embedding/外部服务）；新依赖引入的 deprecation warning 要收敛
- **环境铁律**：后端 python 一律 `env -u PYTHONPATH venv/bin/python`；git push 走 127.0.0.1:7892 代理
