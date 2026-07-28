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
- pandas / openpyxl（导出 Excel）、tiktoken（token 计数）

### 前端（`frontend/package.json`）

- React 18 + Vite 5 + Ant Design 5 + @ant-design/icons
- react-pdf 7（PDF 预览）、react-markdown 9 + remark-gfm（Markdown 渲染）
- ECharts 6 + echarts-for-react（统计可视化）
- zustand 4（客户端状态）、react-window（虚拟列表）、axios、dayjs
- **注意**：没有使用 React Query、TypeScript、react-virtualized（旧设计文档中的规划，实际未采用）。路由用的是 `App.jsx` 内部 view 状态切换，react-router-dom 已安装但主界面未走 URL 路由。

### 桌面端（`electron/`）

- Electron 29 + electron-builder 24
- `main.js` 负责拉起后端子进程（`backend/venv/bin/python -m uvicorn app.main:app --port 8000 --workers 1`）并加载 `frontend/dist`

---

## 3. 运行时架构

```
浏览器/Electron 渲染进程 (React, :5173 dev)
        │  HTTP (/api, /static)   SSE 流式对话
        ▼
FastAPI 后端 (:8000, 单 worker)
  ├─ routers/   papers / search / chat / thesis / memory / export / settings
  ├─ services/  pdf_parser, docx_parser, embedding, retrieval, llm,
  │             skills, memory_manager, web_search, image_analyzer,
  │             auto_tag, backup, cache, processor
  └─ SQLite (data/papers.db, WAL) + ChromaDB (vector_db/) + 本地文件
        ▼
Kimi API (kimi-k2.6) —— 对话 / 概括 / 联网搜索 / 图片分析
```

关键机制：

- **启动流程**（`backend/app/main.py` lifespan）：`Base.metadata.create_all` → `ensure_schema()` 轻量迁移 → `ensure_papers_fts()` 建 FTS5 虚拟表与触发器 → LLM 健康检查（结果存 `app.state.llm_ready`，暴露在 `/api/health`）→ 启动每日凌晨 3 点自动备份线程。
- **静态服务**：整个项目根目录挂载在 `/static`，前端通过它访问 PDF 等本地资源。
- **配置加载**（`backend/app/core/config.py`，单例 `Config`）：优先读项目根 `config.yaml`，缺失时回退 `config.yaml.example`；若设了 `PAPERMIND_DATA_DIR`（Electron 生产包），则从该目录读/复制配置，并自动检测占位符 API Key。
- **检索**（`backend/app/routers/search.py`）：语义检索（ChromaDB cosine，Embedding 用 BGE-M3）与关键词检索（SQLite FTS5 `papers_fts` 表）可独立开关，同时开启时用 RRF（Reciprocal Rank Fusion）融合；语义检索结果有 60 秒内存缓存（`services/cache.py`）。`config.yaml` 里 `retrieval.rerank` 默认为 `false`，BGE-Reranker 相关代码是预留。
- **Skill 系统**（`backend/app/services/skills.py`）：**不是** YAML 插件注册表，而是轻量 Prompt 路由——前端传 `skill` 字段，后端往 system prompt 里注入角色设定。现有 6 个：translator、proofreader、method_comparator、outline_generator、data_analyst、writing_assistant。根目录 `skills/` 目录为空，属预留。
- **对话**（`routers/chat.py`）：`POST /api/chat` 为 SSE 流式；另有会话 CRUD、消息删除/重新生成、`/analyze-image`（多模态）、`/skills` 列表。
- **备份**（`services/backup.py`）：每日凌晨 3 点自动备份到 `backups/`，保留最近 10 份；也可经 `POST /api/export/backup` 手动触发。

---

## 4. 目录结构（实际）

```
个人知识库/（项目根，即 PaperMind）
├── backend/
│   ├── app/
│   │   ├── core/           # config.py（YAML 配置单例）、logger.py
│   │   ├── routers/        # papers/search/chat/thesis/memory/export/settings
│   │   ├── services/       # 见上文运行时架构
│   │   ├── database.py     # SQLAlchemy 引擎 + ensure_schema() 轻量迁移
│   │   ├── models.py       # ORM 模型 + FTS5 虚拟表 DDL
│   │   ├── schemas.py      # Pydantic 请求/响应模型
│   │   └── main.py         # FastAPI 入口（lifespan、CORS、/static 挂载）
│   ├── venv/               # Python 3.12 虚拟环境（会被 electron-builder 打包）
│   └── requirements.txt
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

打包逻辑（`electron/electron-builder.yml` + `main.js`）：`frontend/dist`、`backend/`（**含 venv，包体 500MB+**）、`config.yaml`、各数据目录作为 extraResources 打入；运行时主进程 spawn `backend/venv/bin/python -m uvicorn`，并通过 `PAPERMIND_DATA_DIR` 把数据写到系统应用数据目录（macOS: `~/Library/Application Support/PaperMind/PaperMindData`）。项目根已有打包产物 `PaperMind-1.0.0-arm64.dmg` / `PaperMind-1.0.0-arm64-mac.zip`。

---

## 7. 代码风格与开发约定

- **注释与文档语言为中文**。后端 docstring、日志前缀（如 `[startup]`、`[fts]`、`[backup]`）、提交到仓库的文档均用中文；代码标识符用英文。
- 后端：FastAPI 路由按资源拆文件，业务逻辑下沉到 `services/`；ORM 用 SQLAlchemy 2.0 风格；所有文件路径经 `Path(__file__).resolve().parents[N]` 定位项目根，保持相对路径与可移植性。
- 单例模式很常见：`Config`、`VectorStore`（`get_vector_store()`，带锁懒加载）、`EmbeddingService`（后台线程加载模型）、`cache`。
- 前端：JSX（非 TS）；页面组件放 `pages/`，可复用组件放 `components/`；非首屏页面用 `React.lazy` 懒加载；样式以内联 style + `theme.js` 常量为主，没有 CSS 模块体系。
- LLM 调用统一走 `services/llm.py`（`llm_service`），内含重试、消息截断、错误格式化，不要绕过它直接调 openai。
- 日志统一写 `logs/app.log`（`core/logger.py`），排查问题先看这里；Electron 主进程日志在数据目录 `logs/electron-main.log`。

## 8. 测试

**当前没有任何测试代码**（无 pytest、无前端测试）。验证方式为手动跑通：导入 PDF → 检索 → 对话 → 生成概括。改动后至少应：

1. 后端能启动且 `curl http://127.0.0.1:8000/api/health` 返回 `status: ok`、`llm_ready: true`；
2. 前端 `npm run build` 通过；
3. 涉及接口改动时用 curl 或前端实际点一遍。

## 9. 安全与隐私

- `config.yaml` 含 Kimi API Key，**已加入 `.gitignore`，严禁提交**；同样被忽略的还有 `data/`、`papers/`、`notes/`、`vector_db/`、`logs/`、`cache/`、模型缓存与 venv。
- `config.py` 会识别占位符 Key（`sk-xxxx` / `your-` 开头）， Electron 打包时只有填了真实 Key 的 `config.yaml` 才会被采用。
- CORS 当前 `allow_origins=["*"]`——仅限本机单用户场景，不要将此后端暴露到公网。
- `/static` 挂载整个项目根目录，注意新增敏感文件时不要放到会被静态服务暴露的位置（本机使用场景下风险可控，但要有意识）。
- 数据库操作全部走 SQLAlchemy ORM / 参数化查询（FTS 检索用 `MATCH :query` 绑定参数），不要拼接 SQL。

## 10. 已知问题与注意事项

- **mcp/langgraph 版本锁定**：`mcp` 必须锁 1.3.0、`sse-starlette` 锁 1.8.2——更高版本的 mcp 依赖 `starlette>=0.49`/`pydantic>=2.11`，与 FastAPI 0.110（starlette<0.37）硬冲突；同理 `sse-starlette` 必须 <2。为此 `pydantic` 从 2.6.0 升到 2.7.4、`pydantic-settings` 锁 2.5.2（langgraph 1.2.9 要求 pydantic>=2.7.4）。升级 mcp/langgraph 前必须跑 `pip check` 验证零冲突。
- **httpx 版本**：`openai==1.12.0` 与 `httpx>=0.28` 不兼容，已固定 `httpx==0.27.2`，升级 openai 前必须验证。
- **transformers/torch**：当前 macOS x86_64 + Python 3.12 环境下 torch 最高可用 2.2.2，故 `transformers` 固定 4.39.3。
- **BGE-M3 首次下载**：约 2GB，走 HuggingFace 镜像（`hf-mirror.com`），需网络畅通；Embedding 模型在后台线程加载，`available()` 为假时检索会降级。
- **Kimi API**：当前模型 `kimi-k2.6`（`config.yaml`）；复杂问题响应可达 60–120 秒，优先用 SSE 流式；遇 `429 engine_overloaded_error` 可稍后重试；该模型只支持 `temperature=1`，`llm.py` 已自动处理。
- **ChromaDB telemetry 警告**：启动时 `Failed to send telemetry event` 可忽略（已设置 `anonymized_telemetry=False`，残余警告无害）。
- **`backend/=2.6.0` 文件**：是历史上 `pip install 包名=2.6.0`（少写一个 `=`）误生成的空文件，可删。
- **旧设计文档**（`PaperMind_需求规格说明书_技术设计文档.md` 等）描述的是规划态，与实现有出入时以代码为准（例如 React Query、YAML Skill 注册表、Alembic 均未落地）。

## 11. 给 AI 助手的操作建议

1. 改动前先读相关 router/service，本项目规模不大（后端约 5000 行），直接读代码比查文档可靠。
2. 保持最小改动：这是单用户本地应用，不要引入 auth、多租户、消息队列等无用复杂度。
3. 新增数据库字段：改 `models.py` + 在 `database.py` 的 `ensure_schema()` 加对应迁移分支 + 同步 `schemas.py`。
4. 新增 Skill：在 `services/skills.py` 的 `SKILL_PROMPTS` 和 `list_skills()` 里各加一项即可，前端会自动列出。
5. 新增 API：在对应 router 文件中加端点并更新 `schemas.py`；前端在 `api.js` 加封装。
6. 不要提交 `config.yaml` 和任何数据目录；不要把项目根的用户个人文件当作代码资产处理。
7. 修改了本文档涉及的命令、结构或约定时，同步更新本文件。

---

> 最后更新：2026-07-20，基于实际代码探查重写（替代原规划摘要版）。
