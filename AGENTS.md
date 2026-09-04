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
  ├─ routers/   papers / search / chat / thesis / memory / export / readiness / settings / static
  ├─ services/  pdf_parser, docx_parser, embedding, retrieval, llm,
  │             skills, memory_manager, web_search, image_analyzer,
  │             auto_tag, backup, cache, processor,
  │             agent_graph（LangGraph 对话编排）, mcp_server（MCP 工具）,
  │             corpus_readiness（真实语料 v2 只读聚合核心）
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
- **检索**：`services/retrieval_pipeline.py` 是聊天与 eval 共用的 chunk 级管线，生产默认 BGE-M3 semantic + `bm25-bilingual` + chunk RRF；`paper_id/year` 同时限制两路，运行异常显式诊断并按相同范围降级。`routers/search.py` 保留论文级 FTS/RRF 适配层。语义结果缓存 60 秒且读写复制隔离；限制性 Chroma where 失败必须返回空，禁止降级为无过滤。`retrieval.rerank` 默认 `false`。
- **Skill 系统**（`backend/app/services/skills.py`）：`SkillRegistry` 可注册注册表（Skill-as-Tool 基础），`Skill` dataclass 预留 `tools` 字段供后续工具化；模块级 `build_skill_prompt()` / `list_skills()` 保持原签名。现有 6 个默认 Skill：translator、proofreader、method_comparator、outline_generator、data_analyst、writing_assistant。根目录 `skills/` 目录为空，属预留。
- **对话**（`routers/chat.py`）：`POST /api/chat` 为 SSE 流式。LLM 调用前的编排（记忆加载 → 向量检索 → 消息组装）由 `services/agent_graph.py` 的 LangGraph StateGraph 完成；流式生成与 SSE 事件格式（`{delta}` / `{finished, citations}` / `{error}`）由路由层保持。另有会话 CRUD、消息删除/重新生成、`/analyze-image`（多模态）、`/skills` 列表。
- **MCP Server**（`services/mcp_server.py`）：挂载于 `/mcp`（SSE 握手 `/mcp/sse`），暴露 4 个只读工具 `search_papers` / `list_papers` / `get_paper` / `get_library_stats`，供任意 MCP 客户端连接使用。
- **备份**（`services/backup.py`）：每日凌晨 3 点自动备份到 `backups/`，保留最近 10 份；也可经 `POST /api/export/backup` 手动触发。
- **语料就绪度**（`services/corpus_readiness.py` + `routers/readiness.py`）：`GET /api/readiness/benchmark-v2` 实时只读审计当前 DB/PDF，并以严格白名单返回 PASS/WAIT/UNAVAILABLE 聚合状态；响应 `no-store`，异常时未知计数为 null。核心位于可打包的 `app`，`eval.benchmark_v2` 复用同一覆盖函数；Electron 包继续排除私有 `backend/eval`。

---

## 4. 目录结构（实际）

```
个人知识库/（项目根，即 PaperMind）
├── backend/
│   ├── app/
│   │   ├── core/           # config.py（YAML 配置单例）、settings.py（环境变量覆盖/启动校验）、logger.py
│   │   ├── routers/        # papers/search/chat/thesis/memory/export/readiness/settings/static
│   │   ├── services/       # 见上文运行时架构（含 agent_graph、mcp_server、corpus_readiness、generation_guardrails）
│   │   ├── database.py     # SQLAlchemy 引擎 + ensure_schema() 轻量迁移
│   │   ├── models.py       # ORM 模型 + FTS5 虚拟表 DDL
│   │   ├── schemas.py      # Pydantic 请求/响应模型
│   │   └── main.py         # FastAPI 入口（lifespan、CORS、/mcp 挂载、/static 白名单）
│   ├── eval/               # RAG/生成 Guardrail 评测：公开 fixture、私有 QA、run.py、generation_guardrails.py
│   ├── tests/              # pytest 套件（1091 用例，内存 SQLite + TestClient）
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
npm run check:chunks   # 普通 JS ≤600KiB，PDF worker ≤1100KiB
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

### 单元/集成测试（pytest，1091 个用例）

```bash
cd backend
env -u PYTHONPATH venv/bin/python -m pytest tests/ -v   # 注意必须 env -u PYTHONPATH
```

测试栈：内存 SQLite + FastAPI TestClient（不触发 lifespan，离线快速）。覆盖：health/settings、安全（静态穿越/CORS/异常脱敏）、FTS5 清洗、上传限制、Skill 注册表、Memory 统一 API、评测数据集、指标计算、Agent 图编排、MCP 工具。约定：不触发真实 LLM/embedding 调用（mock），后台线程 mock 掉。

### 前端与 Electron Harness

```bash
cd frontend && npm test          # Vitest + jsdom + Testing Library（SSE / ErrorBoundary）
cd ../electron && npm test       # node:test（health / wait / restart / kill 生命周期）
# 真实发布 E2E：需后端依赖与回环监听权限，CI 在 backend job 显式执行
cd ../electron
PAPERMIND_RELEASE_E2E=1 PAPERMIND_PYTHON=../backend/venv/bin/python \
  node --test test/release-flow.test.js test/data-dir-migration.test.js
```

前端测试依赖包含 MSW，新增网络交互测试不得连接真实后端；Electron 生命周期与安全策略纯模块不得
`require('electron')`，确保 CI 无 GUI 也能运行。当前后端 1091 个测试、前端 66 个测试；
Electron 默认纯测试为 26 passed + 2 个显式 skipped，真实发布 E2E 单独为 10/10 passed。

### RAG 评测（backend/eval/）

```bash
cd backend
env -u PYTHONPATH venv/bin/python -m eval.run                 # 检索评测（默认关键词降级也可用）
env -u PYTHONPATH venv/bin/python -m eval.run --with-llm      # 含生成侧（真实调用 Kimi，慎用）
env -u PYTHONPATH venv/bin/python -m eval.run \
  --fixture eval/fixtures/rag_public_v1.json \
  --dataset eval/dataset/qa_public_v1.jsonl \
  --keyword-only --lexical-profile bm25 --threshold 0.85      # 公开离线 Gate
env -u PYTHONPATH venv/bin/python -m eval.generation_guardrails \
  --report-dir eval/reports/public-generation                 # 生成引用/拒答离线 Gate
# 构建不改 embedding 内容的确定性 HNSW 评测副本（目标必须不存在）
env -u PYTHONPATH venv/bin/python -m eval.deterministic_vector_snapshot \
  --source eval/private/source-vector --target eval/private/deterministic-vector \
  --expected-vector-sha256 <已审计的64位embedding内容SHA>
# 仅分析完整 train 评测报告；输出为去标识化聚合，显式拒绝 dev/holdout
env -u PYTHONPATH venv/bin/python -m eval.train_failure_diagnostics \
  --report eval/private/<train-report>.json \
  --output eval/private/<new-diagnostics>.json
```

- `eval/dataset/qa_seed.jsonl`：25 条种子 QA（含 3 条幻觉负例），schema 见 `eval/dataset/README.md`
- `eval/dataset/qa_public_v1.jsonl` + `eval/fixtures/rag_public_v1.json`：原创 CC0 合成公开基准，12 条 QA/3 篇论文，稳定 DOI + evidence quote qrels
- `eval/metrics.py`：recall@k / MRR / NDCG@k / citation precision-recall-F1 / keyword_hit_rate
- 报告写入 `eval/reports/`（已 gitignore）；recall@5 低于阈值（默认 0.5）退出码非 0，供 CI 门禁
- **公开稳定基线**：count 与 BM25 Recall@5 均为 0.900；MRR 分别 0.775/0.783，NDCG@5 分别 0.806/0.813；CI Gate 为 Recall@5 ≥ 0.85
- `eval/private/` 为已忽略的真实语料评测目录；v1 共 72 条已审 QA / 18 篇论文，train/dev/holdout 各 24 条，证据 72/72 唯一解析
- **真实库留出基线**：BM25 Recall@5/MRR/NDCG@5 为 0.542/0.308/0.365；中英术语扩展为 0.583/0.353/0.410
- **生产聊天当前 shared hybrid（private dev）**：显式 464-chunk 快照，Recall@5/MRR/NDCG@5 为 0.625/0.39375/0.4517186825，factoid Recall=0.333，P95=275.7ms、零降级；聊天与 eval 有逐项排序 parity Harness。该结果只用于开发诊断，不替代 holdout
- **Batch 22H 确定性 HNSW 候选未晋级**：隔离候选 train 独立双跑 24/24 top-5 完全一致，Recall/factoid/MRR/NDCG=`0.667/0.500/0.424/0.485`、P95=344/365ms；同一次问题遍历的 dev 基线/候选质量完全相同（`0.625/0.333/0.39375/0.45172`），候选 P95=244.6ms。因没有任何严格质量提升，按 Gate 未激活，生产 `vector_db/` 仍保持默认 HNSW。Chroma 0.4.24 打开默认 HNSW 会重写 `length.bin`，首次查询还可能重写 `data_level0.bin` 运行时区域；比较必须在 client 打开前冻结原始文件指纹，打开后以 ID/维度/embedding SHA/双层元数据/query smoke 判定语义完整性
- **Batch 22I factoid 锚点候选未晋级**：新增查询数字/缩写/ASCII 单位锚点和等权第三路 BM25，每题只计算一次共享 semantic/BM25 并派生生产/候选排序。真实 train 基线与候选 Recall/factoid 均为 `0.667/0.500`，候选 MRR/NDCG 由 `0.424/0.485` 回退到 `0.399/0.465`，method_detail Recall 回退 1/6；P95=435.9ms。Gate 失败后未运行 dev/holdout，未调权重，生产默认未变。
- **Batch 22J 盲化基准 readiness 未通过**：`papers/` 的 36 个物理 PDF 仅有 19 份唯一内容，17 个是重复副本；v1 已覆盖 18 份，只有 1 篇可进入 v2，低于预注册的 12 篇下限。已增加 PDF/UID 双重覆盖、论文 split 预冻结、证据唯一解析、固定路径一次性 claim 与通用 `eval.run` holdout 禁止；未生成 v2 QA、未运行盲化基线、未消费 holdout。
- **Batch 22K 就绪度可观测性完成**：共享覆盖核心同时服务 CLI 与应用 API；统计页显示 PASS/WAIT/不可用三态并独立重试。真实核心/API 均为 WAIT，36 个物理 PDF、19 份唯一内容、17 个副本、v1 覆盖 18、合格 1、缺 11。API 严格排除路径/标题/DOI/UID/SHA，manifest 自哈希、幽灵论文、根/PDF 软链接和快照变化均 fail closed；本批未打开 QA/holdout，未调用 Kimi。
- **Batch 22L Benchmark v2 既有报告**：已提交报告记录 readiness 34/12、40 条已审 QA、train/dev
  span coverage@5=`0.452/0.750`，train Gate 失败且 holdout 封存。本轮 Batch 25 接班审计未读取
  `eval/private/` 或论文，不能把这些私有结果视为本轮独立复验。QA 生成器现要求显式内容出站确认，
  并在调用 LLM 前校验冻结 PDF SHA、严格 JSONL/0600/symlink 与逐类型断点续跑状态。
- **Batch 23A 生成 Guardrail 离线 Harness 完成**：生产 `[^n^]` 解析、实际引用子集、
  stream/non-stream/regenerate 清洗与 SSE finished 原子终态已统一。公开 CC0 合成
  Gate 的 citation P/R/F1 与负例拒答率均为 1.000，越界/畸形/重复/负例引用
  均为 0；执行阶段无网络、子进程、私有路径或禁止模块。本批未读取私有 QA/论文，
  未调用 Kimi/Embedding。
- **Batch 23C 生成失败事务闭环完成**：流式 LLM 首 token 后失败不再重试拼接；错误串、
  异常、空白与 Guardrail 清洗后为空均转脱敏 error，失败不落 assistant、不发 finished，
  `message_count` 等于真实行数；regenerate 失败保留原正文/引用。前端引用改为消息级，
  文本/图片 error、取消、EOF 与缺失最终正文均丢弃 provisional。生成相关日志不记录
  问题、主题、异常原文或非法引用 token，CI 仅在 Gate 成功后上传报告。
- **Batch 23D 生成并发与深度综述事务完成**：deep-review 新任务在成功终态前不创建
  Conversation，规划/汇总/空输出/Guardrail 清空/取消失败均无孤儿；成功时会话、两条
  消息和真实计数一次提交。messages 新增 `revision` 轻量迁移，regenerate 要求
  `expected_revision`，以进程内 active-set 避免重复 Kimi 调用，并用 revision 条件更新
  防跨进程覆盖；冲突、目标删除、断流或取消后前端从 history 对账且有会话 epoch 门禁。
- **Batch 23E 独立失败事务 Harness 完成**：新增 `eval.failure_transactions`，在干净子进程
  中先重定向临时 runtime、安装网络/子进程/私有路径审计和 fake 服务，再导入真实 chat
  router。七个公开合成场景逐项使用 NullPool 文件 SQLite/WAL，request、finalizer、verify
  跨连接；commit 场景要求写事务、调用和异常注入均恰好一次，再以新连接验证回滚。
  报告严格白名单、无正文/路径/异常，绑定 fixture/runner/生产事务代码 SHA；CI 另以
  `python -S`、零依赖 job 结构性证明生成 Guardrail 未引入模型栈。
- **Batch 23F 并发事务矩阵 v2 完成**：失败事务场景扩为 11 个，覆盖双 Client 409、外部
  revision 冲突、目标删除与取消后重试；公开报告双跑字节一致，Gate 为 PASS。
- **Batch 24/25 发布候选与接班审计完成**：发布 E2E、a11y 契约、包体预算和旧数据目录兼容
  Harness 已建立。Batch 25 将真实 E2E 移到具备后端依赖的 CI job，默认 Electron 套件保持纯
  Node；同时加固安装包路径归一化敏感文件扫描。旧配置测试证明升级兼容，不等同于旧二进制回滚；
  E2E 的外网隔离来自离线环境配置，不是系统调用级审计。
- **Batch 26 v2 train 失败归因 Harness 完成**：新增纯本地只读分析器，将完整 train 报告的
  逐题结果互斥归为跨论文召回、同论文定位、部分覆盖、空结果或完整覆盖；输出只含聚合计数、
  比例、指标与指纹，CLI 限定 `eval/private/` 并显式拒绝 dev/holdout、symlink 和路径逃逸。
  主导失败映射到单一预注册候选，完整 train 未通过前禁止运行 dev，holdout 始终禁止。本批
  只用公开合成报告验证 Harness，未读取真实 v2 报告、QA/PDF，未调用 Kimi/Embedding。
- **Batch 27/27B 诊断准入与论文优先候选完成**：历史 dirty 报告只允许显式 selection-only，
  必须校验 Git 祖先和完整语料/向量指纹且永不具备晋级资格。审计发现 Batch 22L 的既有
  “hybrid” train 报告实际为 `lexical_profile=count`，故在 clean Git `5802d80` 以生产
  `bm25-bilingual` 重跑 13 题：Recall/MRR/NDCG/span/P95=`0.346/0.338/0.296/0.452/883.6ms`。
  主导失败仍为同论文定位 6/13；唯一候选 `paper-first-evidence-rerank-v1` 的 span/Recall/factoid
  Recall 回退至 `0.375/0.308/0.313`，配对 Gate FAIL，未运行 dev/holdout，生产默认未变。
- **Batch 28 证据深度归因完成**：clean Git `23759fd` 的真实 v2 train 聚合将 13 题归为
  baseline full/deep-route recoverable/correct-paper-only/paper-absent=`5/4/4/0`。双路并集
  any-hit@5/10/20=`0.538/0.538/0.769`，span coverage=`0.529/0.529/0.760`；semantic
  与 BM25 单路 span@20 分别为 `0.683/0.606`。按预注册规则只冻结下一候选
  `paper-preserving-deep-route-v1`，本批未实现候选、未运行 dev/holdout、未调用 Kimi，
  生产默认未变。
- **Batch 29 保论文深层候选未晋级**：候选先以生产两路前 10 的 legacy RRF 冻结 top-5
  论文 slot，再只在同论文内用两路 top-20 RRF 换块。真实 train Recall/MRR/NDCG 从
  `0.346/0.338/0.296` 提升到 `0.385/0.362/0.326`，factoid Recall 从 `0.375` 提升到
  `0.438`，但 span coverage 保持 `0.4522`，未达到至少 `+1/13` 的主 Gate；P95=969.8ms。
  因此未运行 dev/holdout，生产默认未变。下一候选转向在已选正确论文的全量 chunk 中按查询定位。
- **Batch 30 论文内全块 BM25 候选未晋级**：`within-paper-query-rerank-v1` 冻结生产论文
  slot，以一次批量 SQL 在已选论文全部 chunk 中执行双语 BM25；正分 incumbent 原位锁定，
  只替换同论文零分 slot。真实 train 仅改变 2/65 个 slot、2/13 个问题，Recall/MRR/NDCG/
  span 与全部分型指标均零变化；P95=833.6ms、零降级。主 Gate 因 span 增益为 0 失败，
  未运行 dev/holdout，生产默认未变。下一步先测量论文内 semantic evidence rank 与增量延迟。
- **Batch 31 论文内语义深度可行性通过**：clean Git `2854b8c` 的完整 v2 train 诊断每题
  只生成一次 query embedding，并由全局 semantic 与已选论文过滤查询复用；13 题共 27 次
  过滤查询，额外 embedding 为 0。基线/已选论文语义全集 span coverage=`0.4522/0.9231`，
  7/13 题可恢复，过滤查询总增量 P50/P95=`34.7/58.4ms`，通过预注册可行性 Gate。该结果
  只是穷举上限，不代表 top-5 已提升；本批未实现候选、未运行 dev/holdout、未调用 Kimi。
- **Batch 32 论文内语义候选因 dev 延迟未晋级**：固定候选在 train 将 Recall/MRR/NDCG/span
  从 `0.346/0.338/0.296/0.452` 提升至 `0.500/0.392/0.374/0.606`，factoid Recall
  从 `0.375` 提升至 `0.625`，全部 Gate 通过。经 train Gate、Git/公式和原子 claim 授权的
  唯一 dev 也将四项质量从 `0.667/0.392/0.459/0.667` 提升至
  `0.708/0.419/0.485/0.750`，但候选 P95=`1081ms` 超过 `<1000ms` 门槛，故最终 FAIL。
  未重跑 dev、未运行 holdout、未调用 Kimi，生产默认仍为 `hybrid`。
- **Batch 33 前端 chunk 性能收尾完成**：StatsPage 改用 ECharts core 与显式图表/组件注册，
  并只向 React 适配层暴露 `init/getInstanceByDom/dispose`；移除 Ant Design 全量 `ui` 聚合。
  StatsPage 原始 chunk 从 `1115.2KiB` 降至 `574.8KiB`（约 -48.5%），`1096.9KiB` 的旧
  `ui` chunk 消失。新增确定性 Gate 要求普通 JS ≤600KiB、PDF worker ≤1100KiB，并已接入
  CI；三端、发布 E2E 与公开离线 Gate 全绿。本批未读取私有语料、未调用 Kimi。
- **Batch 21 邻域候选未晋级**：`hybrid-local-neighbor`（semantic top20、同论文 ±2、固定 rank-distance 衰减）dev 为 0.625/0.36389/0.43005，factoid 仍 0.333、P95=270.1ms；MRR/NDCG/factoid Gate 失败，生产默认保持 shared hybrid。候选仅供显式复现
- **Batch 22 双语 v2 未进入 dev**：`bm25-bilingual-v2` 仅新增四条病理术语映射，train 质量与 v1 完全相同（0.66667/0.42361/0.48529，factoid=0.50），未达到至少新增 1 题的 Gate，因此按预案跳过 dev；生产继续使用 `bm25-bilingual`
- **Batch 22B 消费者已收敛**：聊天、重新生成、深度综述、论文引用推荐和 eval 的 chunk RAG 都经共享 `RetrievalPipeline`；论文引用零证据时跳过 LLM，显式单篇论文范围禁止 graph 越界，论文发现页语义异常保留 FTS 结果
- **真实 chunk 质量基线**：19 篇/464 chunks 中 437 条超过 512 字符、415 条超过 1024 字符、211 条超过 2048 字符，正文中位 1872 字符、P95 约 5.75k、最大 9776；`section_title` 为 0/464，19 条摘要缺 `token_count`。private train 的 24 条证据中 23 条落在 >512 字符块，下一批优先隔离硬分块重建
- **Batch 22C 细粒度候选未晋级**：512/50 候选为 2904 chunks（正文 2885，P50/P95/最大 452/510/512），DB 隔离与完整性通过；但 train evidence qrel 仅 16/24 唯一解析，8 条 328–480 字符证据跨块。按 Gate 在向量构建前停止，未跑 train 排序/dev/holdout/LLM，生产仍为 464 chunks
- **Batch 22D 已修正跨块评测并正式拒绝 512/50 候选**：新增页内半开坐标、原页唯一 quote resolver、字符并集 `span_coverage@5` 与配对 Gate；train 24/24 evidence 在旧/新候选均唯一解析，旧基线 coverage/any-hit/MRR/NDCG 为 0.667/0.667/0.422/0.483，新候选为 0.453/0.500/0.344/0.316，候选质量明显回退，按预案未看 dev。两套 SQLite/Chroma 均为私有 stage，生产仍为 464 chunks/1024 维向量
- **Batch 22E Parent-Child 候选未晋级**：同一 `parent-child-v1` 下，旧粗粒度/512 child 的 train span coverage@5 为 `0.667/0.324`，MRR 为 `0.394/0.222`；候选五个质量 Gate 失败，按协议未运行 dev/holdout/Kimi。Parent 只用于分组排序，未向 LLM 注入 parent 正文；生产继续使用 464-chunk shared hybrid
- **Batch 22F 严格去重 Weighted-RRF 在网格前停止**：新等权函数仅 11/24 条 train 与生产 hybrid top-5 完全同序，Recall/factoid/MRR/NDCG 为 `0.625/0.333/0.392/0.452`，低于生产控制的 `0.667/0.500/0.424/0.485`；按 parity Gate 跳过三组权重、dev/holdout/Kimi。下一批只保留旧重复/tie 语义来隔离权重变量
- **候选评测隔离**：`eval.run` 支持显式 `--database` + `--corpus-root` 只读候选库；`vector_rebuild` 支持 `--database` 并默认强制 1024 维。候选路径不得依赖 `PAPERMIND_DATA_DIR` 隐式切库
- **历史纯语义对齐基线**：`semantic-production` 为 0.500/0.268/0.324，P95=245.6ms、factoid=0；保留为改进起点，不再是生产默认
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
- **Kimi 已恢复但 Batch 23B 私有生成烟测仍未执行**：2026-08-24 实际启动 `/api/health` 返回
  `status=ok`、`llm_ready=true`，模型 `kimi-k2.6`。现有提交只有规格/计划/任务，没有实跑报告；
  固定四题会把问题与 top-5 证据发送到外部 Kimi，执行前必须取得当前轮次的明确内容出站确认。
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

> 最后更新：2026-09-04，Batch 33 前端 chunk 性能收尾并接入 CI 后同步。
