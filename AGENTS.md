# PaperMind 项目说明

> 本文档面向 AI 编程助手，基于对当前代码库的实际探查整理（2026-07-20）。PaperMind 已完成 Phase 1–3 的全部规划功能并打包发布过 1.0.0 桌面版，代码真实可运行。

---

## 1. 项目概述

**PaperMind** 是一个面向个人研究生（单用户）的本地化文献知识库与 AI 辅助写作系统。用户正在撰写「结直肠癌 T 分期预测」毕业论文，研究方向涉及多实例学习（MIL）、视觉 Transformer、病理图像分析（WSI）。

核心解决三大痛点：文献管理混乱、知识检索低效、大论文写作辅助缺失。

核心设计原则：

- **本地优先**：PDF、笔记、SQLite、ChromaDB 全部存本地，无任何云端依赖（LLM 调用除外）。
- **单用户零权限**：没有登录/注册/权限/协作等概念。
- **可移植**：路径全部相对化；Electron 生产包通过 `PAPERMIND_DATA_DIR` 环境变量把数据重定向到系统应用数据目录。
- **单进程架构**：一个 FastAPI 进程同时提供 API 与静态文件服务，前端独立开发、构建后由 Electron 壳加载。

---

## 2. 技术栈（实际使用，以锁定的依赖文件为准）

### 后端（`backend/requirements.txt`）

- Python 3.12（`backend/venv` 中为 3.12.2）
- FastAPI 0.110 + Uvicorn 0.27 + SQLAlchemy 2.0 + Pydantic 2.7
- ChromaDB 0.4.24（本地向量库，`vector_db/`）
- sentence-transformers 2.3 + transformers 4.39.3 + torch 2.2.2（本地跑 BGE-M3 Embedding）
- pdfplumber 0.10（PDF 文本提取）、python-docx 1.1（Word 解析）、PyPDF2（备用）
- openai 1.12（调用 Kimi API，OpenAI 兼容协议）+ httpx 0.27.2（**已固定版本，见「已知问题」**）
- langgraph 1.2.9（对话编排 Agent 图）+ mcp 1.3.0（MCP Server，**版本锁定原因见「已知问题」**）
- pandas / openpyxl（导出 Excel）、tiktoken（token 计数）

### 前端（`frontend/package.json`）

- React 18 + Vite 8 + Ant Design 5 + @ant-design/icons（Vite 8 要求 Node `^20.19.0 || >=22.12.0`）
- react-pdf 10（PDF 预览，worker 从当前 pdfjs-dist 同源打包）、react-markdown 9 + remark-gfm（Markdown 渲染）
- ECharts 6 + echarts-for-react（统计可视化）
- zustand 4（客户端状态）、react-window（虚拟列表）、axios、dayjs
- **注意**：没有使用 React Query、TypeScript、react-virtualized（旧设计文档中的规划，实际未采用）。路由用的是 `App.jsx` 内部 view 状态切换；未使用的 react-router-dom 已移除。

### 桌面端（`electron/`）

- Electron 43 + electron-builder 26
- `main.js` 在生产模式生成随机回环端口、256-bit 能力令牌与实例 ID，再拉起后端子进程并加载 `frontend/dist`；开发模式仍使用 8000

---

## 3. 运行时架构

```
浏览器/Electron 渲染进程 (React, :5173 dev)
        │  HTTP (/api, /static)   SSE 流式对话
        ▼
FastAPI 后端 (:8000, 单 worker)
  ├─ routers/   papers / search / chat / thesis / memory / export / settings / static
  ├─ services/  pdf_parser, docx_parser, embedding, retrieval, llm,
  │             skills, memory_manager, web_search, image_analyzer,
  │             auto_tag, backup, cache, processor,
  │             agent_graph（LangGraph 对话编排）, mcp_server（MCP 工具）
  ├─ eval/      RAG 评测（dataset / metrics / run，详见「测试与评测」）
  ├─ /mcp       MCP Server（SSE 传输，FastMCP 子应用挂载）
  └─ SQLite (data/papers.db, WAL) + ChromaDB (vector_db/) + 本地文件
        ▼
Kimi API (kimi-k2.6) —— 对话 / 概括 / 联网搜索 / 图片分析
```

关键机制：

- **启动流程**（`backend/app/main.py` lifespan）：`Base.metadata.create_all` → `ensure_schema()` 轻量迁移 → `ensure_papers_fts()` 建 FTS5 虚拟表与触发器 → LLM 健康检查（结果存 `app.state.llm_ready`，暴露在 `/api/health`）→ 启动每日凌晨 3 点自动备份线程。
- **静态服务**：`/static` 为白名单静态路由（`routers/static.py`），仅放行 `papers/`、`notes/`、`my-thesis/`、`summaries/` 四个目录，`resolve()` 防 `../` 穿越与软链接逃逸；项目根不再整体暴露。
- **配置加载**（`backend/app/core/config.py`，单例 `Config`）：开发模式优先读项目根 `config.yaml`，缺失时回退 `config.yaml.example`；若设了 `PAPERMIND_DATA_DIR`，只在首次启动复制公开模板，真实配置保存在应用数据目录且升级不覆盖。`runtime_root` 统一重定向所有可变数据。
- **检索**（`backend/app/routers/search.py`）：语义检索（ChromaDB cosine，Embedding 用 BGE-M3）与关键词检索（SQLite FTS5 `papers_fts` 表）可独立开关，同时开启时用 RRF（Reciprocal Rank Fusion）融合；语义检索结果有 60 秒内存缓存（`services/cache.py`）。`config.yaml` 里 `retrieval.rerank` 默认为 `false`，BGE-Reranker 相关代码是预留。
- **Skill 系统**（`backend/app/services/skills.py`）：`SkillRegistry` 可注册注册表（Skill-as-Tool 基础），`Skill` dataclass 预留 `tools` 字段供后续工具化；模块级 `build_skill_prompt()` / `list_skills()` 保持原签名。现有 6 个默认 Skill：translator、proofreader、method_comparator、outline_generator、data_analyst、writing_assistant。根目录 `skills/` 目录为空，属预留。
- **对话**（`routers/chat.py`）：`POST /api/chat` 为 SSE 流式。LLM 调用前的编排（记忆加载 → 向量检索 → 消息组装）由 `services/agent_graph.py` 的 LangGraph StateGraph 完成；流式生成与 SSE 事件格式（`{delta}` / `{finished, citations}` / `{error}`）由路由层保持。另有会话 CRUD、消息删除/重新生成、`/analyze-image`（多模态）、`/skills` 列表。
- **MCP Server**（`services/mcp_server.py`）：挂载于 `/mcp`（SSE 握手 `/mcp/sse`），暴露 4 个只读工具 `search_papers` / `list_papers` / `get_paper` / `get_library_stats`，供任意 MCP 客户端连接使用。
- **备份**（`services/backup.py`）：每日凌晨 3 点自动备份到 `backups/`，保留最近 10 份；也可经 `POST /api/export/backup` 手动触发。

---

## 4. 目录结构（实际）

```
个人知识库/（项目根，即 PaperMind）
├── backend/
│   ├── app/
│   │   ├── core/           # config.py（YAML 配置单例）、settings.py（环境变量覆盖/启动校验）、logger.py
│   │   ├── routers/        # papers/search/chat/thesis/memory/export/settings/static
│   │   ├── services/       # 见上文运行时架构（含 agent_graph、mcp_server）
│   │   ├── database.py     # SQLAlchemy 引擎 + ensure_schema() 轻量迁移
│   │   ├── models.py       # ORM 模型 + FTS5 虚拟表 DDL
│   │   ├── schemas.py      # Pydantic 请求/响应模型
│   │   └── main.py         # FastAPI 入口（lifespan、CORS、/mcp 挂载、/static 白名单）
│   ├── eval/               # RAG 评测：公开 fixture、稳定/私人 QA、metrics.py、run.py
│   ├── tests/              # pytest 套件（511 用例，内存 SQLite + TestClient）
│   ├── venv/               # Python 3.12 虚拟环境（会被 electron-builder 打包）
│   ├── pyproject.toml      # 依赖声明 + pytest/ruff 配置
│   └── requirements.txt    # 锁定依赖（与 pyproject 保持一致）
├── .github/workflows/      # ci.yml（pytest+lint+build）、eval.yml（公开离线评测）
├── Dockerfile / docker-compose.yml / .dockerignore   # 一键部署（见 docs/DEPLOY.md）
├── docs/                   # DEPLOY.md 等运维文档
├── frontend/
│   ├── src/
│   │   ├── pages/          # PaperList, PaperDetail, SearchPage, ThesisList,
│   │   │                   # ThesisDetail, WritingDesk, DataExport, StatsPage
│   │   ├── components/     # ChatPanel, PdfViewer, ResizablePanels,
│   │   │                   # ResizableVertical, SettingsModal
│   │   ├── App.jsx         # 主布局 + view 状态切换（非 URL 路由）
│   │   ├── api.js          # axios 封装
│   │   ├── theme.js        # 配色与组件样式常量
│   │   └── utils/apiUrl.js
│   ├── vite.config.js      # dev 代理 /api 与 /static → 127.0.0.1:8000
│   └── package.json
├── electron/               # main.js / preload.js / electron-builder.yml
├── papers/                 # 上传的 PDF（数据，勿提交）
├── notes/                  # 每篇文献的 Markdown 笔记（数据）
├── summaries/              # AI 概括输出
├── my-thesis/              # 上传的大论文 Word（数据）
├── data/                   # SQLite 数据库（数据）
├── vector_db/              # ChromaDB 数据（数据）
├── logs/                   # app.log 及按日期轮转的历史日志
├── skills/                 # 空目录，Skill 系统预留
├── config.yaml             # 运行时配置（含 API Key，已 gitignore）
├── config.yaml.example     # 配置模板
└── README.md / 若干设计文档（PaperMind_*.md）
```

仓库根目录还混有用户个人内容（`面试.md`、`thesis (1).docx` 等），不属于应用代码，改动代码时不要动它们。

---

## 5. 数据模型（`backend/app/models.py`，共 11 张表 + 1 张 FTS 虚拟表）

- `papers`：文献主表（标题、作者、年份、期刊、摘要、PDF 路径、阅读状态等）。
- `chunks`：文本分块元数据（向量本体在 ChromaDB，chunk id 形如 `p{paper_id}_c{i}`）。
- `tags` + 文献-标签关联（在 `papers.py` 路由中处理）。
- `conversations` / `messages`：对话与消息。
- `skills`：Skill 注册表（DB 表存在，当前 Skill 走 `services/skills.py` 的内存 Prompt）。
- `thesis_files` / `thesis_citations`：大论文文件与引用检测。
- `memory_summaries`：Agent 记忆（`memory_type` ∈ short_term / long_term / preference / fact）。
- `paper_annotations`：PDF 页面标注（页码、选中文本、颜色、备注）。
- `papers_fts`：FTS5 虚拟表（title/authors/abstract），由 insert/update/delete 触发器与 `papers` 表同步，启动时 `rebuild`。

**没有 Alembic**：schema 演进靠 `database.py` 里的 `ensure_schema()` 手工 `ALTER TABLE` 轻量迁移。新增字段时应同样走这条路，不要引入迁移框架。

---

## 6. 构建与运行命令

### 后端

```bash
cd backend
source venv/bin/activate          # 虚拟环境已存在；重建则 python -m venv venv && pip install -r requirements.txt
# 首次：cp ../config.yaml.example ../config.yaml 并填入 Kimi API Key
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000        # 开发（热重载）
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1     # 稳定模式
```

### 前端

```bash
cd frontend
npm install
npm run dev        # http://localhost:5173，/api 与 /static 代理到 8000
npm run build      # 产物在 frontend/dist
npm run lint       # eslint（js/jsx，--max-warnings 0）
npm run electron:dev    # 开发模式 Electron 壳（加载 :5173）
npm run electron:build  # 先 build 前端再用 electron-builder 打包
```

### 桌面端打包

```bash
cd frontend && npm run build
cd ../electron && npm run build    # 产物在 frontend/out/（dmg/zip/exe）
```

打包逻辑（`electron/electron-builder.yml` + `main.js`）：仅将 `frontend/dist`、`backend/`（**含 venv，包体 500MB+**）与 `config.yaml.example` 作为 extraResources 打入；真实配置和个人数据严禁进入安装包。运行时通过 `PAPERMIND_DATA_DIR` 把全部可变数据写到系统应用数据目录。

---

## 7. 代码风格与开发约定

- **注释与文档语言为中文**。后端 docstring、日志前缀（如 `[startup]`、`[fts]`、`[backup]`）、提交到仓库的文档均用中文；代码标识符用英文。
- 后端：FastAPI 路由按资源拆文件，业务逻辑下沉到 `services/`；ORM 用 SQLAlchemy 2.0 风格；所有文件路径经 `Path(__file__).resolve().parents[N]` 定位项目根，保持相对路径与可移植性。
- 单例模式很常见：`Config`、`VectorStore`（`get_vector_store()`，带锁懒加载）、`EmbeddingService`（后台线程加载模型）、`cache`。
- 前端：JSX（非 TS）；页面组件放 `pages/`，可复用组件放 `components/`；非首屏页面用 `React.lazy` 懒加载；样式以内联 style + `theme.js` 常量为主，没有 CSS 模块体系。
- LLM 调用统一走 `services/llm.py`（`llm_service`），内含重试、消息截断、错误格式化，不要绕过它直接调 openai。
- 日志统一写 `logs/app.log`（`core/logger.py`），排查问题先看这里；Electron 主进程日志在数据目录 `logs/electron-main.log`。

## 8. 测试与评测

### 单元/集成测试（pytest，601 个用例）

```bash
cd backend
env -u PYTHONPATH venv/bin/python -m pytest tests/ -v   # 注意必须 env -u PYTHONPATH
```

测试栈：内存 SQLite + FastAPI TestClient（不触发 lifespan，离线快速）。覆盖：health/settings、安全（静态穿越/CORS/异常脱敏）、FTS5 清洗、上传限制、Skill 注册表、Memory 统一 API、评测数据集、指标计算、Agent 图编排、MCP 工具。约定：不触发真实 LLM/embedding 调用（mock），后台线程 mock 掉。

### 前端与 Electron Harness

```bash
cd frontend && npm test          # Vitest + jsdom + Testing Library（SSE / ErrorBoundary）
cd ../electron && npm test       # node:test（health / wait / restart / kill 生命周期）
```

前端测试依赖包含 MSW，新增网络交互测试不得连接真实后端；Electron 生命周期与安全策略纯模块不得
`require('electron')`，确保 CI 无 GUI 也能运行。当前后端 601 个测试、前端 39 个测试、Electron 26 个测试。

### RAG 评测（backend/eval/）

```bash
cd backend
env -u PYTHONPATH venv/bin/python -m eval.run                 # 检索评测（默认关键词降级也可用）
env -u PYTHONPATH venv/bin/python -m eval.run --with-llm      # 含生成侧（真实调用 Kimi，慎用）
env -u PYTHONPATH venv/bin/python -m eval.run \
  --fixture eval/fixtures/rag_public_v1.json \
  --dataset eval/dataset/qa_public_v1.jsonl \
  --keyword-only --lexical-profile bm25 --threshold 0.85      # 公开离线 Gate
```

- `eval/dataset/qa_seed.jsonl`：25 条种子 QA（含 3 条幻觉负例），schema 见 `eval/dataset/README.md`
- `eval/dataset/qa_public_v1.jsonl` + `eval/fixtures/rag_public_v1.json`：原创 CC0 合成公开基准，12 条 QA/3 篇论文，稳定 DOI + evidence quote qrels
- `eval/metrics.py`：recall@k / MRR / NDCG@k / citation precision-recall-F1 / keyword_hit_rate
- 报告写入 `eval/reports/`（已 gitignore）；recall@5 低于阈值（默认 0.5）退出码非 0，供 CI 门禁
- **公开稳定基线**：count 与 BM25 Recall@5 均为 0.900；MRR 分别 0.775/0.783，NDCG@5 分别 0.806/0.813；CI Gate 为 Recall@5 ≥ 0.85
- `eval/private/` 为已忽略的真实语料评测目录；v1 共 72 条已审 QA / 18 篇论文，train/dev/holdout 各 24 条，证据 72/72 唯一解析
- **真实库留出基线**：BM25 Recall@5/MRR/NDCG@5 为 0.542/0.308/0.365；中英术语扩展为 0.583/0.353/0.410
- **真实库 dev 当前有效 hybrid**：重建后的 464 条 BGE-M3 向量经显式快照评测，Recall@5/MRR/NDCG@5 为 0.625/0.394/0.452；该结果只用于开发诊断，不替代 holdout
- **生产聊天对齐 dev 基线**：`semantic-production` 只复刻聊天的语义 top5，Recall@5/MRR/NDCG@5 为 0.500/0.268/0.324，P95=245.6ms；factoid Recall=0，是后续优先弱项
- 私有真实库不可与公开基准混算趋势；公开集用于链路正确性和回归，不替代真实论文质量评测

### 改动后至少应验证

1. 后端 pytest 全绿，且能启动、`curl http://127.0.0.1:8000/api/health` 返回 `status: ok`、`llm_ready: true`；
2. 前端 `npm run lint` 零警告 + `npm run build` 通过；
3. 涉及接口改动时用 curl 或前端实际点一遍。

## 9. 安全与隐私

- `config.yaml` 含 Kimi API Key，**已加入 `.gitignore`，严禁提交**；同样被忽略的还有 `data/`、`papers/`、`notes/`、`vector_db/`、`logs/`、`cache/`、模型缓存与 venv。
- Electron 安装包只携带 `config.yaml.example`；真实 Key 必须由用户在应用数据目录或设置界面填写，不得随分发包提供。
- CORS 已严格化：显式 origin 白名单（`http://localhost:5173`、`http://127.0.0.1:5173`、Electron `file://` 的 `"null"`），`allow_credentials=False`——仍不要将后端暴露到公网。
- `/static` 为白名单静态路由（仅 papers/notes/my-thesis/summaries），`resolve()` 防路径穿越与软链接逃逸；新增敏感文件不要放进这四个目录。
- 全局异常处理不向前端返回异常原文（仅通用文案 + error_code），详情只写 `logs/app.log`。
- 上传限制：PDF 单文件 50MB 上限（413），扩展名白名单（400）。
- 数据库操作全部走 SQLAlchemy ORM / 参数化查询（FTS 检索用 `MATCH :query` 绑定参数，且查询串先经 `_sanitize_fts_query()` 清洗），不要拼接 SQL。

## 10. 已知问题与注意事项

- **mcp/langgraph 版本锁定**：`mcp` 必须锁 1.3.0、`sse-starlette` 锁 1.8.2——更高版本的 mcp 依赖 `starlette>=0.49`/`pydantic>=2.11`，与 FastAPI 0.110（starlette<0.37）硬冲突；同理 `sse-starlette` 必须 <2。为此 `pydantic` 从 2.6.0 升到 2.7.4、`pydantic-settings` 锁 2.5.2（langgraph 1.2.9 要求 pydantic>=2.7.4）。升级 mcp/langgraph 前必须跑 `pip check` 验证零冲突。
- **httpx 版本**：`openai==1.12.0` 与 `httpx>=0.28` 不兼容，已固定 `httpx==0.27.2`，升级 openai 前必须验证。
- **transformers/torch**：当前 macOS x86_64 + Python 3.12 环境下 torch 最高可用 2.2.2，故 `transformers` 固定 4.39.3。
- **BGE-M3 首次下载**：约 2GB，走 HuggingFace 镜像（`hf-mirror.com`），需网络畅通；Embedding 模型在后台线程加载，`available()` 为假时检索会降级。
- **Kimi API**：当前模型 `kimi-k2.6`（`config.yaml`）；复杂问题响应可达 60–120 秒，优先用 SSE 流式；遇 `429 engine_overloaded_error` 可稍后重试；该模型只支持 `temperature=1`，`llm.py` 已自动处理。
- **ChromaDB telemetry 警告**：启动时 `Failed to send telemetry event` 可忽略（已设置 `anonymized_telemetry=False`，残余警告无害）。
- **Chroma 已完成原子重建**：当前库与 SQLite 的 464 个 chunk ID 完全一致，Embedding 为 1024 维并通过 query smoke；旧失配库保留在已忽略的 `vector_db.backup-*` 目录。后续重建必须继续使用显式 stage/activate CLI，不得原地修补。
- **真实 SQLite 历史孤儿**：主库 `quick_check=ok`，但仍有 4 条 `paper_tags` 外键孤儿；Batch 18 已生成并验证 FK=0 的修复候选副本，未自动覆盖源库。切换前必须再次备份并由用户明确确认。
- **Kimi 已恢复但私有生成烟测待授权**：2026-08-14 最小健康检查返回 `ok=true`、模型 `kimi-k2.6`。真实论文固定四题生成烟测会把 QA 与 top-5 证据发送到外部 Kimi，必须获得用户明确的内容出站授权后执行。
- **本地 BGE-Reranker 不满足延迟 Gate**：2.1GiB 模型可正常加载，但 CPU 上首题超过约 4 分钟未完成，已安全中止；生产 `retrieval.rerank` 继续保持 `false`。
- **`backend/=2.6.0` 文件**：是历史上 `pip install 包名=2.6.0`（少写一个 `=`）误生成的空文件，可删。
- **旧设计文档**（`PaperMind_需求规格说明书_技术设计文档.md` 等）描述的是规划态，与实现有出入时以代码为准（例如 React Query、YAML Skill 注册表、Alembic 均未落地）。

## 11. 给 AI 助手的操作建议

1. 改动前先读相关 router/service，本项目规模不大（后端约 5000 行），直接读代码比查文档可靠。
2. 保持最小改动：这是单用户本地应用，不要引入 auth、多租户、消息队列等无用复杂度。
3. 新增数据库字段：改 `models.py` + 在 `database.py` 的 `ensure_schema()` 加对应迁移分支 + 同步 `schemas.py`。
4. 新增 Skill：在 `services/skills.py` 的默认注册区（`_register_default_skills`）加一项 `Skill(...)` 即可，前端会自动列出；`tools` 字段留给后续 Agent 工具化。
5. 新增 API：在对应 router 文件中加端点并更新 `schemas.py`；前端在 `api.js` 加封装。新增后跑 `env -u PYTHONPATH venv/bin/python -m pytest tests/ -q` 确认全绿。
6. 不要提交 `config.yaml` 和任何数据目录；不要把项目根的用户个人文件当作代码资产处理。
7. 修改了本文档涉及的命令、结构或约定时，同步更新本文件。

---

> 最后更新：2026-08-14，Batch 19 前端可靠性与生产语义评测完成后同步。
