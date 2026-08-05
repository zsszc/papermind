# PaperMind

PaperMind 是一款面向个人研究生（单用户）的本地化文献知识库与 AI 辅助写作系统。核心解决三大痛点：

1. **文献管理混乱**：100+ 篇论文的 PDF、笔记、引用关系难以管理。
2. **知识检索低效**：传统文件夹无法基于语义快速定位内容。
3. **写作辅助缺失**：大论文写作时难以快速找到"该引用哪篇文章的哪段话"。

> **核心原则**：本地优先、单用户零权限、可移植、Agent 记忆连续、模块化 Skill、渐进式复杂。

---

## 技术栈

| 层级 | 技术 |
|------|------|
| 后端 | Python 3.10+、FastAPI、SQLAlchemy、ChromaDB、Sentence-Transformers |
| 前端 | React 18、Ant Design 5、Vite、react-pdf、react-markdown、ECharts |
| 桌面端 | Electron（可选打包） |
| LLM | Kimi API（支持联网搜索、多模态图片分析） |
| Embedding | BAAI/bge-m3（本地） |

---

## 功能特性

### 文献管理
- 批量导入 PDF，自动解析元数据（标题、作者、年份、期刊、DOI 等）
- 文献列表/详情、阅读状态、标签管理、批量操作
- PDF 预览与阅读进度记忆
- 个人笔记（Markdown）自动保存

### 知识检索
- 语义检索（RAG）：基于向量相似度检索论文片段
- 关键词检索：SQLite FTS5 全文检索
- 混合检索：语义 + 关键词 RRF 融合
- 搜索结果可跳转至 PDF 对应页面

### Agent 对话
- 基于文献库的 SSE 流式对话
- 联网搜索（Kimi web_search tool）
- 截图/图片多模态分析
- 引用来源显示文献标题并支持跳转

### 大论文集成
- Word 文件上传与章节结构解析
- 引用检测与反向关联
- 章节-文献映射视图（发现引用盲区）
- 引用标记手动关联/校正
- 章节点评 AI 评审

### Skill 插件
- 学术翻译
- 论文校对
- 方法对比
- 大纲生成
- 数据分析
- 写作助手

### 统计可视化
- 文献年份分布
- 阅读状态分布
- 标签分布
- 高频作者
- 章节-文献引用关系图

### 数据安全与备份
- SQLite WAL 模式
- 每日凌晨自动备份（保留最近 10 份）
- 手动全量备份/导出
- CSV/Excel/引用格式导出

---

## 快速开始

### 1. 克隆与准备

```bash
git clone <repository-url>
cd PaperMind
```

### 2. 后端启动

```bash
cd backend
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt

# 复制配置文件模板
cp ../config.yaml.example ../config.yaml
# 编辑 ../config.yaml，填入你的 Kimi API Key

# 开发模式（热重载）
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

### 3. 前端启动

```bash
cd frontend
npm install
npm run dev
```

浏览器访问 `http://localhost:5173`。

### 4. 生产构建

```bash
cd frontend
npm run build
```

构建产物位于 `frontend/dist`，可由后端 `/static` 或 Nginx 直接托管。

### 5. 打包为桌面应用（无需单独启动前后端）

已在 `electron/` 目录配置好 Electron + electron-builder，打包后会自动启动后端和前端。

```bash
# 先确保前端已构建
cd frontend && npm run build

# 再执行打包（macOS 示例）
cd ../electron && npm run build
```

打包产物位于 `frontend/out/`：
- macOS: `PaperMind-1.0.0-arm64.dmg` / `PaperMind-1.0.0-arm64-mac.zip`
- Windows: `PaperMind Setup 1.0.0.exe` / `PaperMind-1.0.0-win.zip`

双击安装/运行后，应用会自动启动后端服务并加载前端页面，无需再手动运行 `uvicorn` 或 `npm run dev`。

> **注意**：
> - 当前 `electron-builder.yml` 会把 `backend/venv` 一并打包，因此安装包较大（约 500MB+），但无需目标机器安装 Python 环境。
> - 数据默认保存在系统应用数据目录（macOS: `~/Library/Application Support/PaperMind/PaperMindData`；Windows: `%APPDATA%/PaperMind/PaperMindData`），不会随应用升级丢失。
> - 桌面包不会携带开发机的 API Key 或个人数据；首次运行后请在设置界面填写 Kimi API Key。

---

## 目录结构

```
PaperMind/
├── backend/              # FastAPI 后端
│   ├── app/
│   │   ├── core/         # 配置、日志
│   │   ├── routers/      # API 路由
│   │   ├── services/     # 业务服务
│   │   ├── models.py     # SQLAlchemy ORM
│   │   ├── schemas.py    # Pydantic 模型
│   │   └── main.py       # 入口
│   ├── venv/             # Python 虚拟环境
│   └── requirements.txt
├── frontend/             # React 前端
│   ├── src/
│   │   ├── components/   # ChatPanel、PdfViewer 等
│   │   ├── pages/        # 页面组件
│   │   ├── App.jsx
│   │   └── api.js
│   ├── package.json
│   └── vite.config.js
├── electron/             # Electron 桌面壳（可选）
├── papers/               # 上传的 PDF
├── notes/                # Markdown 笔记
├── summaries/            # AI 概括
├── my-thesis/            # 上传的大论文
├── data/                 # SQLite 数据库
├── vector_db/            # ChromaDB 向量库
├── backups/              # 自动备份文件
├── skills/               # Skill 配置目录（预留）
├── logs/                 # 运行日志
├── config.yaml           # 运行时配置（需手动创建）
├── config.yaml.example   # 配置模板
└── README.md
```

---

## 配置文件

参考 `config.yaml.example`：

```yaml
app:
  name: "PaperMind"
  version: "1.0.0"
  data_dir: "./data"

llm:
  provider: "moonshot"
  api_key: "sk-xxxxxxxx"          # 替换为你的 Kimi API Key
  base_url: "https://api.moonshot.cn/v1"
  model: "kimi-k2.6"
  max_tokens: 4096
  temperature: 0.3

embedding:
  provider: "local"
  local_model: "BAAI/bge-m3"
  device: "auto"
  chunk_size: 512
  chunk_overlap: 50

retrieval:
  top_k: 10
  rerank: false

memory:
  enabled: true
  session_limit: 50
  summary_interval: 5
  max_short_term: 10

skills:
  auto_load: true
  skills_dir: "./skills"

ui:
  theme: "light"
  language: "zh-CN"
  page_size: 20

export:
  citation_format: "GB/T 7714"
```

> ⚠️ `config.yaml` 已加入 `.gitignore`，请勿提交 API Key。

---

## API 接口

主要接口前缀均为 `/api`：

| 模块 | 接口 |
|------|------|
| 文献 | `POST /api/papers/import`、`GET /api/papers`、`GET /api/papers/{id}` |
| 检索 | `POST /api/search` |
| 对话 | `POST /api/chat`、`POST /api/chat/analyze-image`、`GET /api/chat/skills` |
| 大论文 | `POST /api/thesis/upload`、`GET /api/thesis/{id}/citation-map` |
| 记忆 | `GET /api/memory/memories` |
| 导出 | `GET /api/export/papers/csv`、`POST /api/export/backup` |

完整 API 文档可在后端启动后访问 `http://localhost:8000/docs`。

---

## 已知问题与注意事项

- **Embedding 模型**：首次使用 `BAAI/bge-m3` 时会从 HuggingFace 镜像自动下载，约 2GB。
- **Kimi API**：`kimi-k2.6` 对复杂问题响应可能较慢（60-120 秒），请耐心等待。
- **ChromaDB Telemetry**：启动时可能出现 telemetry 警告，不影响功能，可忽略。
- **数据库迁移**：后端启动时会自动检查并补齐旧表缺失列。

---

## 开发路线图

- ✅ Phase 1：文献管理、语义检索、Agent 对话
- ✅ Phase 2：大论文集成、引用建议、记忆系统、写作台
- ✅ Phase 3：可视化、更多 Skill、图片分析、联网搜索、性能优化、自动备份
- ⏳ 待办：Electron 桌面端打包发布、用户手册视频、团队协作（二期）

---

## 许可

本项目为个人学术工具，仅供学习和研究使用。
