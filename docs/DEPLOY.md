# PaperMind 部署文档（CI / Docker）

> 本文档说明 PaperMind 的持续集成（GitHub Actions）与 Docker 一键部署方式。
> 面向有一定命令行基础的部署者；开发环境搭建请见 README.md。

---

## 1. 持续集成（CI）

仓库包含两个 GitHub Actions 工作流（`.github/workflows/`）：

### 1.1 `ci.yml` — 每次 push / PR 自动运行

触发条件：`push` 或 `pull_request` 目标分支为 `main`。两个 job 并行执行：

| Job | 环境 | 步骤 |
|-----|------|------|
| **backend** | Python 3.12 | `pip install -r backend/requirements.txt` + `pytest`、`pydantic-settings` → 在 `backend/` 目录下运行 `python -m pytest tests/ -q`（PYTHONPATH 置空） |
| **frontend** | Node 20 | `npm ci`（严格按 `frontend/package-lock.json`）→ `npm run lint`（零警告）→ `npm run build` |

pip 与 npm 均开启依赖缓存，第二次起构建明显加速。

**依赖版本红线**：`backend/requirements.txt` 中 `httpx==0.27.2` 为固定版本，`openai==1.12.0` 与 `httpx>=0.28` 不兼容，升级依赖时务必保持该组合。

### 1.2 `eval.yml` — 检索质量评测（手动触发）

- 触发方式：GitHub 页面 → **Actions → Eval → Run workflow**（`workflow_dispatch`）。
- 运行 `python -m eval.run`（工作目录 `backend/`），为纯检索评测，**不调用 LLM，无需 API Key**。
- Embedding 模型走 `HF_ENDPOINT=https://hf-mirror.com` 镜像站下载。
- 评测报告目录 `backend/eval/reports/` 作为 artifact 上传（保留 30 天），无论评测成败均上传。

---

## 2. Docker 一键部署

### 2.1 前置条件

- 已安装 Docker 与 Docker Compose（v2）。
- 首次部署前准备配置文件（**含 Kimi API Key，切勿提交或打入镜像**）：

```bash
cp config.yaml.example config.yaml
# 编辑 config.yaml，填入 llm.api_key
```

### 2.2 构建与启动

```bash
# 构建镜像并后台启动（项目根目录执行）
docker compose up -d --build

# 查看启动日志（首次启动会下载 BGE-M3 模型，约 2GB，请耐心等待）
docker compose logs -f backend

# 健康检查
curl http://localhost:8000/api/health
# 期望返回 {"status":"ok", ...}；模型未下载完成前 llm_ready 可能为 false
```

### 2.3 前端访问

Compose 仅包含后端单服务（保持简单）。开发期前端在宿主机运行：

```bash
cd frontend
npm install
npm run dev    # http://localhost:5173，/api 与 /static 自动代理到 8000
```

### 2.4 停止与更新

```bash
docker compose down              # 停止并删除容器（数据保留在宿主机目录）
git pull && docker compose up -d --build   # 更新代码后重建
```

---

## 3. 数据持久化

所有数据目录通过 `docker-compose.yml` **绑定挂载**到宿主机项目根，容器删除/重建后数据不丢失：

| 宿主机目录 | 容器路径 | 内容 |
|-----------|---------|------|
| `./data` | `/app/data` | SQLite 数据库（papers.db） |
| `./papers` | `/app/papers` | 上传的 PDF |
| `./notes` | `/app/notes` | Markdown 笔记 |
| `./summaries` | `/app/summaries` | AI 概括输出 |
| `./my-thesis` | `/app/my-thesis` | 大论文 Word |
| `./vector_db` | `/app/vector_db` | ChromaDB 向量库 |
| `./logs` | `/app/logs` | 应用日志 |
| `./backups` | `/app/backups` | 每日自动备份 |
| `./config.yaml` | `/app/config.yaml`（只读） | 运行时配置（含 API Key） |
| `~/.cache/huggingface` | `/root/.cache/huggingface` | HuggingFace 模型缓存 |

Dockerfile 中同时声明了 `VOLUME`，即使不用 compose、直接 `docker run`，数据也会落在 Docker 管理的匿名卷中：

```bash
docker build -t papermind .
docker run -d --name papermind -p 8000:8000 \
  -v $(pwd)/config.yaml:/app/config.yaml:ro \
  -v papermind-data:/app/data \
  -v $(pwd)/papers:/app/papers \
  -v ~/.cache/huggingface:/root/.cache/huggingface \
  papermind
```

---

## 4. 模型下载注意事项（BGE-M3）

- Embedding 模型 **BGE-M3 不 baked 进镜像**，在**首次启动时**由 sentence-transformers 在线下载，体积约 **2GB**。
- 镜像与 compose 默认设置 `HF_ENDPOINT=https://hf-mirror.com`（国内镜像站），避免 huggingface.co 直连超时。
- **强烈建议挂载 HuggingFace 缓存目录**（compose 已配置 `~/.cache/huggingface`），否则容器每次重建都会重新下载 2GB 模型。
- 下载完成前，语义检索功能不可用（关键词检索 FTS5 不受影响），可在日志中看到模型加载进度。
- 若部署环境完全离线，可先在联网机器下载模型到 `~/.cache/huggingface`，再整体拷贝该目录到目标机器挂载。

---

## 5. 常见问题

**Q：镜像里没有 config.yaml 会怎样？**
A：后端配置加载会自动回退到 `config.yaml.example`（已拷贝进镜像），服务可正常启动，但 LLM 相关功能（对话/概括）不可用，`/api/health` 中 `llm_ready` 为 `false`。挂载真实 `config.yaml` 后恢复。

**Q：为什么镜像不含前端？**
A：PaperMind 定位为本地单用户应用，前端开发期用 Vite dev server，生产分发走 Electron 桌面包（`npm run electron:build`）。Docker 部署面向"后端服务化"场景，故仅打包后端。

**Q：端口冲突？**
A：修改 `docker-compose.yml` 中 `ports` 左侧的宿主机端口，如 `"8080:8000"`。
