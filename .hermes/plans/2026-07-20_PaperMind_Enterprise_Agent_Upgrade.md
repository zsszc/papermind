# PaperMind 企业化 + Agent 2.0 升级实施计划

> **For Hermes:** 这是一个综合整改与技术升级计划。执行时请先修复基础问题，再逐步引入新框架，避免一次性改动过大导致无法回滚。

**Goal:** 在 6-8 周内把 PaperMind 从「个人工具」升级为「可落地、可评测、可演示」的 AI 研究助手：修复安全/稳定性/工程化缺口，引入 Skill-as-Tool、LangGraph 编排、MCP Server、Multi-Agent 协作，并建立完整 RAG 评测体系。

**Architecture:** 保持「FastAPI + React + Electron + SQLite + ChromaDB」本地优先架构不变；在 Service 层抽象出 `AgentOrchestrator` 与 `SkillRegistry`，将原硬编码 Skill 替换为可注册工具；通过 LangGraph 编排 RAG/多 Agent 流程；通过 MCP 将 PaperMind 知识库暴露给外部客户端；通过 `backend/eval/` 持续评估检索与生成质量。

**Tech Stack:** Python 3.11+ / FastAPI 0.110+ / SQLAlchemy 2.0 / ChromaDB / SentenceTransformers / React 18 / Ant Design 5 / Vite / Electron 29 / LangGraph / LangChain Core / MCP SDK / pytest / GitHub Actions / Docker

---

## 当前已知关键问题（执行前提）

1. **Git 未初始化**，根目录有 1.6GB 打包产物未忽略。
2. **后端安全**：`main.py` 将 `/static` 挂载到项目根目录；CORS `allow_origins=["*"]`。
3. **后端性能**：异步路由里同步写文件、后台线程里 `asyncio.run()` 复用 async client、FTS5 查询未清洗。
4. **前端/桌面端**：SSE 裸 fetch 解析脆弱、Electron 后端启动/kill 不健壮、无错误边界、大列表无虚拟滚动。
5. **工程化**：0 测试、0 CI、0 Docker、`langchain` 在 requirements.txt 但代码未使用。
6. **Skill 系统**：硬编码 prompt 字典，无参数模式、无工具化。
7. **无 RAG 评测**：`模型性能评估方案.md` 只有方案，无 `backend/eval/` 实现。

---

## Phase 0：工程底座与仓库治理（第 1-2 周）

**目标**：让项目处于可维护、可回滚、可协作的状态。

### Task P0.1：初始化 Git 并补全 .gitignore

**Objective:** 建立版本控制，隔离源码与数据/构建产物。

**Files:**
- Modify: `.gitignore`

**Steps:**
1. 进入项目根目录执行 `git init`（如未初始化）。
2. 将 `.gitignore` 补全为以下内容：

```gitignore
# 敏感配置
config.yaml
config.yml
*.env
.env

# Python
__pycache__/
*.py[cod]
*$py.class
*.so
.Python
backend/venv/
backend/.venv/
backend/env/
backend/.env
*.egg-info/
dist/
build/

# 数据目录（本地运行生成）
data/
papers/
notes/
summaries/
my-thesis/
vector_db/
logs/
cache/
backups/

# 模型缓存
*.bin
*.safetensors
models/

# Node.js
frontend/node_modules/
frontend/dist/
frontend/.env
frontend/.env.local
npm-debug.log*
yarn-debug.log*
yarn-error.log*

# IDE
.vscode/
.idea/
*.swp
*.swo
*~

# OS
.DS_Store
Thumbs.db

# Electron 打包产物
frontend/out/
frontend/release/
*.dmg
*.zip
*.exe
```

3. 删除（或移出仓库）根目录的 `PaperMind-1.0.0-arm64.dmg` 和 `.zip`（已生成的 release 应走 GitHub Releases 或网盘，不走 Git）。
4. 提交第一次 commit：

```bash
git add .gitignore README.md AGENTS.md backend/ electron/ frontend/ config.yaml.example
# 不要提交 data/ papers/ notes/ summaries/ my-thesis/ vector_db/ logs/ backups/
git commit -m "chore: init repo and ignore local data/build artifacts"
```

**Verification:**
- `git status` 只剩未跟踪的数据目录（已忽略则不显示）。
- `git log --oneline` 有至少 1 条 commit。

---

### Task P0.2：锁定依赖与配置 Python 工具链

**Objective:** 让后端依赖可复现，统一格式化/检查工具。

**Files:**
- Create: `backend/pyproject.toml`
- Create: `backend/.python-version`（可选，写 `3.11`）
- Modify: `backend/requirements.txt`

**Steps:**
1. 在 `backend/pyproject.toml` 中定义：

```toml
[project]
name = "papermind-backend"
version = "1.0.0"
requires-python = ">=3.11"
dependencies = [
    "fastapi==0.110.0",
    "uvicorn[standard]==0.27.0",
    "sqlalchemy==2.0.0",
    "pydantic==2.6.0",
    "python-multipart==0.0.9",
    "python-docx==1.1.0",
    "pdfplumber==0.10.0",
    "pypdf2==3.0.1",
    "cryptography==42.0.8",
    "chromadb==0.4.24",
    "sentence-transformers==2.3.0",
    "openai==1.12.0",
    "httpx==0.27.2",
    "aiofiles==23.2.0",
    "pyyaml==6.0.1",
    "requests==2.31.0",
    "numpy==1.26.0",
    "pandas==2.2.0",
    "openpyxl==3.1.2",
    "python-dateutil==2.8.2",
    "tiktoken==0.6.0",
    "torch==2.2.2",
    # 新增（企业化/Agent）
    "langchain-core>=0.2.0,<0.3",
    "langgraph>=0.0.50",
    "mcp>=1.0.0",
    "slowapi>=0.1.9",
    "structlog>=24.0.0",
    "prometheus-client>=0.20.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "httpx>=0.27.0",
    "ruff>=0.4.0",
    "mypy>=1.9",
]
```

2. 保留 `requirements.txt` 作为低门槛入口，但同步更新为 `pyproject.toml` 的基础依赖（去掉未用的 `langchain==0.1.0` 和 `langchain-community==0.0.20`）。
3. 在 `backend/` 创建虚拟环境并安装：

```bash
cd backend
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

4. 生成锁定文件：

```bash
pip freeze > requirements-lock.txt
```

**Verification:**
- `python -c "import app.main"` 在 `backend/` 下可运行。
- `pytest --version` 可用。

---

### Task P0.3：建立后端测试骨架

**Objective:** 让后续重构有回归保障。

**Files:**
- Create: `backend/pytest.ini`
- Create: `backend/tests/conftest.py`
- Create: `backend/tests/test_health.py`
- Create: `backend/tests/test_settings.py`

**Steps:**
1. `backend/pytest.ini`：

```ini
[pytest]
asyncio_mode = auto
testpaths = tests
pythonpath = .
```

2. `backend/tests/conftest.py`：

```python
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.main import app

SQLITE_URL = "sqlite:///:memory:"
engine = create_engine(SQLITE_URL, connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

@pytest.fixture(scope="function")
def db():
    Base.metadata.create_all(bind=engine)
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)

@pytest.fixture(scope="function")
def client(db):
    def override_get_db():
        yield db
    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()
```

3. `backend/tests/test_health.py`：

```python
def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
```

4. `backend/tests/test_settings.py`：

```python
def test_get_settings_masks_key(client):
    r = client.get("/api/settings")
    assert r.status_code == 200
    key = r.json()["llm_api_key"]
    assert "*" in key or key == ""
```

**Verification:**

```bash
cd backend
pytest tests/test_health.py tests/test_settings.py -v
# Expected: 2 passed
```

---

### Task P0.4：配置管理与环境变量分离

**Objective:** 把配置从单一 YAML 升级为「环境变量 + YAML + 校验」三层结构。

**Files:**
- Create: `backend/app/core/settings.py`
- Modify: `backend/app/core/config.py`（保留兼容，但内部调用 Pydantic Settings）
- Modify: `config.yaml.example`

**Steps:**
1. 新建 `backend/app/core/settings.py`：

```python
from pydantic_settings import BaseSettings
from pathlib import Path

class LLMSettings(BaseSettings):
    provider: str = "moonshot"
    api_key: str = ""
    base_url: str = "https://api.moonshot.cn/v1"
    model: str = "kimi-k2-7"
    max_tokens: int = 4096
    temperature: float = 0.3

class AppSettings(BaseSettings):
    name: str = "PaperMind"
    version: str = "1.0.0"
    data_dir: Path = Path("./data")

class Settings(BaseSettings):
    app: AppSettings = AppSettings()
    llm: LLMSettings = LLMSettings()

    class Config:
        env_prefix = "PAPERMIND_"
        env_nested_delimiter = "__"
```

2. 保留 `config.yaml` 作为持久化用户配置，但启动时先用 Pydantic Settings 做 schema 校验。
3. 在 `app.main.py` 的 `lifespan` 中增加配置校验：若 `llm.api_key` 为空或占位符，记录 warning。

**Verification:**

```bash
PAPERMIND_LLM__API_KEY=sk-test pytest tests/test_settings.py -v
```

---

## Phase 1：后端安全与稳定性加固（第 2-4 周）

**目标**：修复所有严重/中等问题，使后端达到可发布水平。

### Task P1.1：修复 `/static` 路径穿越 + 严格化 CORS

**Objective:** 消除信息泄露和任意文件读取风险。

**Files:**
- Modify: `backend/app/main.py`
- Create: `backend/app/routers/static.py`（可选）

**Steps:**
1. 移除 `main.py` 里的全局 `StaticFiles(directory=project_root)` 挂载。
2. 新增 `papers/` 静态资源访问路由，只允许读取 `papers/`、`notes/`、`my-thesis/` 目录内的白名单文件，且必须验证 `paper_id` 存在。
3. 修改 CORS：生产环境只允许 Electron 的 `file://` 或 `http://localhost:5173`。

```python
# main.py
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import config

origins = ["http://localhost:5173"]
if config.is_packaged():
    origins.append("file://")  # Electron 生产环境

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=False,  # 与 allow_origins=["*"] 不能同时用
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)
```

4. 在 `papers.py` 中保持 `get_pdf_file` 的校验逻辑，不再通过 `/static` 暴露 PDF。

**Verification:**
- 测试 `GET /static/backend/app/core/config.yaml` 返回 404 或 403。
- 测试 `GET /api/papers/{id}/pdf` 仍可正常读取 PDF。
- 新增测试 `backend/tests/test_security.py`：

```python
def test_static_path_traversal_blocked(client):
    r = client.get("/static/backend/app/core/config.py")
    assert r.status_code in (403, 404)
```

---

### Task P1.2：异步路由去阻塞

**Objective:** 防止上传/写盘阻塞事件循环。

**Files:**
- Modify: `backend/app/routers/papers.py`（`/import`）
- Modify: `backend/app/routers/thesis.py`（上传接口）

**Steps:**
1. 把 `shutil.copyfileobj(file.file, f)` 改为 `aiofiles` 或 `run_in_threadpool`：

```python
from starlette.concurrency import run_in_threadpool

async def save_upload(file: UploadFile, target_path: Path):
    with open(target_path, "wb") as f:
        await run_in_threadpool(shutil.copyfileobj, file.file, f)
```

2. 增加文件大小限制（例如单文件 50MB）和扩展名白名单：

```python
MAX_PDF_SIZE = 50 * 1024 * 1024
if file.size and file.size > MAX_PDF_SIZE:
    raise HTTPException(status_code=413, detail="PDF 超过 50MB")
```

3. 对 `thesis.py` 上传接口同样处理。

**Verification:**
- 新增测试上传大文件返回 413。
- 运行原 `/import` 功能仍正常。

---

### Task P1.3：统一异常处理与日志结构化

**Objective:** 不再向前端暴露 `str(exc)`，使用结构化日志。

**Files:**
- Modify: `backend/app/main.py`（全局异常处理）
- Modify: `backend/app/core/logger.py`

**Steps:**
1. 全局异常处理改为：

```python
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled API error", path=request.url.path, method=request.method)
    return JSONResponse(
        status_code=500,
        content={
            "detail": "服务器内部错误",
            "error_code": "internal_error",
            "path": request.url.path,
        },
    )
```

2. 使用 `structlog` 或标准 logging 添加 request_id 字段；`logger.py` 添加 JSON formatter 选项。

**Verification:**
- 新增测试触发 500 的接口，确认返回中不包含原始异常信息。

---

### Task P1.4：FTS5 查询清洗与检索安全

**Objective:** 防止 FTS5 语法错误和注入风险。

**Files:**
- Modify: `backend/app/routers/search.py`

**Steps:**
1. 新增 `_sanitize_fts_query(query: str) -> str`：

```python
import re

_FTS_SPECIAL = re.compile(r'[\\"*\-\:]')

def _sanitize_fts_query(query: str) -> str:
    """把用户输入转义成 FTS5 安全短语查询。"""
    cleaned = _FTS_SPECIAL.sub(" ", query.strip())
    tokens = [t for t in cleaned.split() if t]
    return " ".join(f'"{t}"' for t in tokens)
```

2. 在 `_keyword_search` 中使用 `query = _sanitize_fts_query(query)` 后再传给 `MATCH`。

**Verification:**
- 新增测试：查询 `"`、`*` 等特殊字符不再抛异常，返回空列表或正常结果。

---

### Task P1.5：后台任务与事件循环治理

**Objective:** 消除后台线程中 `asyncio.run()` 复用 client 的问题。

**Files:**
- Modify: `backend/app/routers/papers.py`（`_enhance_paper_metadata`、`_process_paper_background`）
- Modify: `backend/app/services/llm.py` 和 `auto_tag.py`

**Steps:**
1. 把后台任务改为 FastAPI `BackgroundTasks` 或线程池调用同步封装函数，而不是在线程内新跑事件循环。
2. 在 `services/llm.py` 中提供同步阻塞版 `chat_completion_sync` 与流式版 `chat_stream`；后台线程统一调用同步版。
3. 将 `auto_tag_service.generate_tags` 也提供同步入口。

**Verification:**
- 新增测试：上传 PDF 后，后台任务能正常完成不抛 `RuntimeError: Event loop is closed`。

---

### Task P1.6：限流与基础指标

**Objective:** 防止 LLM/embedding 接口被刷爆。

**Files:**
- Create: `backend/app/core/limiter.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/routers/chat.py`、`papers.py`

**Steps:**
1. 使用 `slowapi` 添加基于内存的限流：

```python
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_middleware(limiter)
```

2. 对 `/api/chat` 限制 10/min，对 `/api/papers/import` 限制 5/min。

**Verification:**
- 测试快速调用 `/api/chat` 两次以上返回 429。

---

## Phase 2：前端与 Electron 稳定性（第 3-5 周）

**目标**：让前端更稳、更流畅、更可维护。

### Task P2.1：SSE 改用健壮库并支持断线重连

**Objective:** 解决裸 fetch 解析脆弱问题。

**Files:**
- Modify: `frontend/src/components/ChatPanel.jsx`
- Modify: `frontend/package.json`

**Steps:**
1. 安装 `@microsoft/fetch-event-source`：

```bash
cd frontend
npm install @microsoft/fetch-event-source
```

2. 替换 `readSSEStream` 为 `fetchEventSource`：

```javascript
import { fetchEventSource } from '@microsoft/fetch-event-source'

const handleSend = async () => {
  // ... 准备消息
  await fetchEventSource(`${getApiBaseUrl()}/api/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ... }),
    signal: abortCtrlRef.current.signal,
    onmessage(msg) {
      const data = JSON.parse(msg.data)
      // update messages
    },
    onerror(err) {
      // 返回 0 表示不抛异常，库会自动重连
      return 0
    },
  })
}
```

3. 组件卸载时 abort 请求，避免内存泄漏。

**Verification:**
- 手动测试：关闭浮窗后重新打开，不再出现重复请求或异常 toast。

---

### Task P2.2：添加 React Error Boundary

**Objective:** 防止单个组件崩溃拖垮整个应用。

**Files:**
- Create: `frontend/src/components/ErrorBoundary.jsx`
- Modify: `frontend/src/main.jsx`

**Steps:**
1. 创建 ErrorBoundary 类组件，捕获渲染错误，显示 fallback UI 和重置按钮。
2. 在 `main.jsx` 中包裹 `<App />`：

```jsx
<ErrorBoundary>
  <App />
</ErrorBoundary>
```

**Verification:**
- 在 Development 下故意 throw，确认 fallback 显示。

---

### Task P2.3：Electron 后端进程健壮性

**Objective:** 解决后端启动失败、kill 不彻底、端口占用问题。

**Files:**
- Modify: `electron/main.js`

**Steps:**
1. 添加端口占用检测和备选端口逻辑：

```javascript
async function findAvailablePort(startPort = 8000) {
  const net = require('net')
  return new Promise((resolve) => {
    const server = net.createServer()
    server.listen(startPort, '127.0.0.1', () => {
      const port = server.address().port
      server.close(() => resolve(port))
    })
    server.on('error', () => resolve(startPort + 1))
  })
}
```

2. 根据平台动态选择 venv Python 路径：

```javascript
const venvPython = process.platform === 'win32'
  ? path.join(projectRoot, 'backend', 'venv', 'Scripts', 'python.exe')
  : path.join(projectRoot, 'backend', 'venv', 'bin', 'python')
```

3. `killBackend` 改为 SIGTERM + 超时 SIGKILL：

```javascript
function killBackend() {
  if (!backendProcess || backendProcess.killed) return
  backendProcess.kill('SIGTERM')
  setTimeout(() => {
    if (!backendProcess.killed) backendProcess.kill('SIGKILL')
  }, 5000)
}
```

**Verification:**
- 在 8000 端口已有服务时启动 Electron，确认使用备选端口并正常加载。

---

### Task P2.4：大列表虚拟滚动与状态管理

**Objective:** 优化性能，引入 Zustand 和 react-window。

**Files:**
- Modify: `frontend/src/pages/PaperList.jsx`
- Create: `frontend/src/stores/chatStore.js`
- Modify: `frontend/src/components/ChatPanel.jsx`

**Steps:**
1. 用 `react-window` 替换 PaperList 的普通 Table 渲染（或至少用 AntD 的 `virtual` 属性）。
2. 创建 `chatStore.js` 管理 `conversations`、`currentId`、`messages`、`citations`、`loading`。
3. 将 ChatPanel 的流式状态、会话列表迁移到 store，避免 props drilling 和闭包过时问题。

**Verification:**
- 列表加载 200 条数据时 FPS 保持 50+。

---

## Phase 3：Agent 2.0 与技术升级（第 5-7 周）

**目标**：引入 Skill-as-Tool、LangGraph 编排、MCP Server、多 Agent 协作。

### Task P3.1：重构 Skill 为可注册工具（Skill-as-Tool）

**Objective:** 替代硬编码 `SKILL_PROMPTS` 字典。

**Files:**
- Create: `backend/app/skills/__init__.py`
- Create: `backend/app/skills/base.py`
- Create: `backend/app/skills/registry.py`
- Create: `backend/app/skills/translator.py` 等示例
- Modify: `backend/app/services/skills.py`（最终替换为 registry）
- Modify: `backend/app/routers/chat.py`

**Steps:**
1. 定义 `Skill` 基类：

```python
from typing import Callable, Optional, Any
from pydantic import BaseModel

class SkillParams(BaseModel):
    pass

class Skill:
    name: str
    description: str
    params_schema: Optional[type[BaseModel]] = None
    handler: Optional[Callable[..., Any]] = None

    def build_prompt(self, user_message: str, params: dict | None = None) -> str:
        raise NotImplementedError
```

2. 实现 `SkillRegistry`，通过装饰器注册：

```python
registry = SkillRegistry()

@registry.register()
class TranslatorSkill(Skill):
    name = "translator"
    description = "将学术文本进行中英互译，保持术语准确。"
    params_schema = TranslatorParams

    def build_prompt(self, user_message, params=None):
        return f"你是学术翻译专家。请翻译：\n\n{user_message}"
```

3. 在 `chat.py` 中把 `build_skill_prompt(request.skill, request.message)` 替换为 `registry.get(request.skill).build_prompt(...)`。

**Verification:**
- 新增测试：所有注册 Skill 都有非空 `name` 和 `description`。
- 测试 `/api/chat` 带 `skill=translator` 仍能正常返回。

---

### Task P3.2：接入 LangGraph 编排 RAG 流程

**Objective:** 用图节点替代 `chat.py` 中的手工流程。

**Files:**
- Create: `backend/app/agent/__init__.py`
- Create: `backend/app/agent/graph.py`
- Create: `backend/app/agent/nodes.py`
- Create: `backend/app/agent/state.py`
- Modify: `backend/app/routers/chat.py`

**Steps:**
1. 定义 `AgentState`：

```python
from typing import TypedDict, List, Optional

class AgentState(TypedDict):
    query: str
    retrieved: List[dict]
    memory: str
    messages: List[dict]
    response: Optional[str]
    citations: List[dict]
```

2. 实现节点：
   - `retrieve_node`: 调用 `get_vector_store().search()`
   - `build_context_node`: 组装 RAG prompt
   - `llm_node`: 调用 `llm_service.chat_completion`
   - `postprocess_node`: 保存消息、提取引用

3. 构建 LangGraph：

```python
from langgraph.graph import StateGraph, END

builder = StateGraph(AgentState)
builder.add_node("retrieve", retrieve_node)
builder.add_node("build_context", build_context_node)
builder.add_node("generate", llm_node)
builder.add_node("postprocess", postprocess_node)
builder.set_entry_point("retrieve")
builder.add_edge("retrieve", "build_context")
builder.add_edge("build_context", "generate")
builder.add_edge("generate", "postprocess")
builder.add_edge("postprocess", END)
graph = builder.compile()
```

4. 在 `chat.py` 中当 `request.stream is False` 时调用 `graph.invoke(...)`；流式路径暂时保留原实现，后续再图化。

**Verification:**
- 新增测试：调用 `graph.invoke` 返回非空 `response` 和 `citations`。

---

### Task P3.3：实现 MCP Server

**Objective:** 让外部客户端能访问 PaperMind 知识库。

**Files:**
- Create: `backend/mcp_server.py`
- Create: `backend/app/mcp/__init__.py`
- Create: `backend/app/mcp/tools.py`
- Modify: `README.md`（补充 MCP 使用说明）

**Steps:**
1. 使用 `mcp` SDK 创建 server：

```python
from mcp.server import Server
from mcp.server.stdio import stdio_server
from app.services.retrieval import get_vector_store
from app.database import SessionLocal

server = Server("papermind")

@server.tool()
def search_papers(query: str, top_k: int = 5) -> list:
    """在 PaperMind 知识库中检索相关论文片段。"""
    store = get_vector_store()
    if not store.available():
        return []
    return store.search(query, top_k=top_k)

@server.tool()
def get_paper_notes(paper_id: int) -> str:
    with SessionLocal() as db:
        paper = db.query(Paper).filter(Paper.id == paper_id).first()
        if not paper:
            return ""
        note_path = Path(...) / f"{paper_id}.md"
        return note_path.read_text(encoding="utf-8") if note_path.exists() else ""
```

2. 提供 stdio 启动入口：

```python
async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.get_capabilities())

if __name__ == "__main__":
    asyncio.run(main())
```

**Verification:**
- 在 Claude Desktop 或 Cline 配置 MCP 后，能成功调用 `search_papers`。

---

### Task P3.4：多 Agent 协作（Multi-Agent）

**Objective:** 实现 Planner + Retriever + Critic + Writer 协作流程。

**Files:**
- Create: `backend/app/agent/agents.py`
- Modify: `backend/app/agent/graph.py`

**Steps:**
1. 定义 Agent 角色：
   - `PlannerAgent`: 分析意图，决定是否需要检索、需要调用哪些工具
   - `RetrieverAgent`: 执行检索
   - `SynthesizerAgent`: 综合生成回答
   - `CriticAgent`: 检查引用准确性和幻觉

2. 在 LangGraph 中定义条件边：
   - `plan → retrieve`（若需要检索）
   - `retrieve → synthesize`
   - `synthesize → critic`
   - `critic → revise` 或 `END`

3. 先用单步流式接口调用 `PlannerAgent` 做路由，降低首次响应延迟。

**Verification:**
- 测试比较型问题能触发两次检索并给出对比回答。

---

## Phase 4：RAG 评测体系（第 6-8 周）

**目标**：建立可量化、可持续的评估体系。

### Task P4.1：构建 QA 评估数据集

**Objective:** 准备 50-100 个真实问题对。

**Files:**
- Create: `backend/eval/data/qa_pairs.json`
- Create: `backend/eval/data/README.md`

**Steps:**
1. 从你自己的研究问题中挑选 30-50 个真实问题，例如：
   - "TransMIL 和 CLAM 的主要区别是什么？"
   - "多实例学习中的空间抑制问题是什么？"
2. 每个问题标注：
   - `query`
   - `expected_answer`
   - `relevant_paper_ids`
   - `ground_truth_chunk_ids`
   - `difficulty` / `type`

**Verification:**
- JSON 格式通过 `jsonschema` 校验。

---

### Task P4.2：实现检索评估脚本

**Objective:** 计算 Recall@K、Precision@K、MRR、NDCG。

**Files:**
- Create: `backend/eval/metrics/retrieval.py`
- Create: `backend/eval/eval_retrieval.py`

**Steps:**
1. 实现 `compute_recall_at_k`、`compute_precision_at_k`、`compute_mrr`、`compute_ndcg`。
2. 运行语义检索、关键词检索、RRF 融合三种模式，对比指标。

**Verification:**

```bash
cd backend
python -m eval.eval_retrieval
# Expected: 输出 Recall@5 / Precision@5 / MRR / NDCG@5
```

---

### Task P4.3：LLM-as-Judge 与 RAGAS

**Objective:** 评估生成质量。

**Files:**
- Create: `backend/eval/metrics/generation.py`
- Create: `backend/eval/eval_end2end.py`

**Steps:**
1. 用 Kimi API 作为 Judge，对回答在相关性、事实准确性、完整性、引用准确性、流畅度上 1-5 分打分。
2. 接入 `ragas` 计算 Faithfulness、Answer Relevance、Context Precision/Recall。

**Verification:**
- 运行 `python -m eval.eval_end2end`，输出基线指标表格。

---

### Task P4.4：消融实验

**Objective:** 找到最优配置。

**Experiments:**
- 语义 vs 关键词 vs 混合
- chunk_size: 256 / 512 / 1024
- top_k: 5 / 10 / 20
- 是否加查询前缀（bge-m3）
- 是否加 BGE-Reranker（可选，若模型支持本地运行）

**Verification:**
- 输出 `backend/eval/reports/baseline.md`。

---

## Phase 5：CI/CD、Docker 与打包（第 7-9 周）

**目标**：实现自动化、可分发。

### Task P5.1：GitHub Actions CI

**Objective:** 自动测试、构建、打包。

**Files:**
- Create: `.github/workflows/ci.yml`

**Steps:**
1. 定义 workflow：
   - Python 3.11 环境
   - 安装 backend dev 依赖
   - 运行 `pytest`
   - 运行 `ruff check .`
   - 安装 frontend 依赖并 `npm run build`

**Verification:**
- push 后 GitHub Actions 全部绿灯。

---

### Task P5.2：Docker 化后端

**Objective:** 提供可复现的运行环境。

**Files:**
- Create: `backend/Dockerfile`
- Create: `docker-compose.yml`

**Steps:**
1. `backend/Dockerfile` 多阶段构建：
   - 阶段 1：安装 Python 依赖
   - 阶段 2：复制源码并设置 entrypoint
2. `docker-compose.yml` 挂载 `./data`、 `./papers`、 `./vector_db`。

**Verification:**

```bash
docker compose up -d
# 访问 http://localhost:8000/api/health 返回 ok
```

---

### Task P5.3：Electron 签名与自动更新

**Objective:** 发布可安装的桌面应用。

**Files:**
- Modify: `electron/electron-builder.yml`
- Create: `electron/build/entitlements.mac.plist`

**Steps:**
1. 配置 macOS notarization（需 Apple Developer ID 和 app-specific password）。
2. 配置 Windows 代码签名证书（可选，测试阶段可跳过）。
3. 接入 `electron-updater`：
   - 发布到 GitHub Releases
   - 应用启动时检查更新

**Verification:**
- 在干净 macOS 上双击 `.dmg` 安装后不报 Gatekeeper 拦截。

---

## 面试与论文输出物

每完成一个 Phase，都应同步产出以下可写进简历/论文的材料：

| Phase | 面试可讲点 | 论文可写点 |
|---|---|---|
| P0-P1 | 安全加固、CORS、路径穿越、异步阻塞、限流 | 系统稳定性优化 |
| P2 | React 18 并发、SSE、Electron 进程模型 | 桌面端架构优化 |
| P3 | Skill-as-Tool、Function Calling、LangGraph、MCP、Multi-Agent | 面向学术写作的 Agent 架构 |
| P4 | RAG 评测、RRF、Embedding、LLM-as-Judge、RAGAS | RAG 系统评估方法 |
| P5 | Docker、CI/CD、Electron 签名、自动更新 | 工程化部署 |

---

## 执行建议

1. **先 P0-P1，再做 P3**：安全与工程化不解决，后续加 Agent 只会让系统更难调试。
2. **P3 中先 Skill Registry，再 LangGraph，再 MCP，最后 Multi-Agent**：每一步都保持原接口兼容，可回滚。
3. **每完成一个 Task 就提交一次 commit**：方便面试时展示 commit history。
4. **保留一份 `CHANGELOG.md`**：记录每个 Phase 的变更和原因。

---

## 风险评估

| 风险 | 影响 | 缓解措施 |
|---|---|---|
| LangGraph/MCP 依赖体积大 | Electron 包体可能超过 1GB | 后端 Docker 化，Electron 可远程/本地连接 |
| 多 Agent 增加延迟 | 用户体验下降 | 先做 Planner 路由，非必要不跑多 Agent |
| 测试不足导致重构回退 | 功能 regressions | 每改一个模块先补测试 |
| 文档与代码再次脱节 | 后续维护困难 | 每个 Phase 更新 README/AGENTS.md |
