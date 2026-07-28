# PaperMind 升级链路：从个人工具到可落地/可演示/可评测的 AI 研究助手

> 目标：6–8 周内完成工程化、安全加固、Agent 2.0 升级、RAG 评测体系与首次 Git 推送。当前已有第一版总体计划（`.hermes/plans/2026-07-20_PaperMind_Enterprise_Agent_Upgrade.md`），本文聚焦**执行链路**与**多 Agent 并行拆分**。

---

## 1. 当前已完成的底座（可直接复用）

- ✅ Git 初始化并首次 push 到 `https://github.com/zsszc/papermind.git`
- ✅ `.gitignore` 已覆盖数据、构建产物、安装包、文档等
- ✅ 已删除旧大文件（dmg/zip）并清理 electron/node_modules 等误提交
- ✅ 演示脚本已提交：`scripts/seed_demo.py` + `scripts/start-demo.sh`
- ✅ 后端可启动（`llm_ready: true`），前端 dev server 可访问，示例论文已导入

---

## 2. 总体升级链路（按阶段推进）

```
P0 工程底座 → P1 后端安全/稳定 → P2 前端/桌面稳定 → P3 Agent 2.0 → P4 RAG 评测 → P5 CI/CD 与发布
```

| 阶段 | 周期 | 目标 |
|---|---|---|
| **P0** 工程底座 | 第 1 周 | 依赖锁定、测试骨架、配置分层、仓库整洁 |
| **P1** 后端安全/稳定 | 第 2–3 周 | 静态文件/路径安全、CORS、异步去阻塞、异常/日志、FTS5 清洗、后台任务治理、限流 |
| **P2** 前端/桌面稳定 | 第 3–4 周 | SSE 健壮、错误边界、虚拟滚动、Electron 子进程管理、构建 lint 通过 |
| **P3** Agent 2.0 | 第 4–6 周 | Skill-as-Tool、LangGraph 编排、Memory 统一、MCP Server、多 Agent 协作 |
| **P4** RAG 评测体系 | 第 6–7 周 | 评测数据集、检索指标、生成指标、CI 集成 |
| **P5** CI/CD 与首次发布 | 第 7–8 周 | GitHub Actions、Docker Compose、Electron 打包、Release |

---

## 3. 各阶段详细任务与可并行性

### P0 工程底座（剩余任务）

当前已做完仓库治理，剩余：

| 任务 | 文件/目录 | 依赖 | 能否并行 |
|---|---|---|---|
| P0.2 依赖锁定 + `pyproject.toml` | `backend/pyproject.toml`、`requirements.txt` | 无 | ✅ 独立 |
| P0.3 测试骨架 | `backend/tests/`、`pytest.ini` | P0.2 完成 | ⚠️ 依赖 P0.2 安装 pytest |
| P0.4 配置分层 | `backend/app/core/settings.py`、`config.yaml.example` | 无（向后兼容） | ✅ 可与 P0.2 并行 |
| P0.5 基础 lint/format 工具 | `pyproject.toml` 中 ruff/mypy | P0.2 | ⚠️ 依赖 P0.2 |

**并行建议**：
- Agent A：P0.2 + P0.5（依赖 + 工具链）
- Agent B：P0.4 + P0.3（配置 + 测试骨架，P0.3 需等 A 完成后跑通）

**P0 验收**：`pytest tests/test_health.py tests/test_settings.py` 通过，后端能启动。

---

### P1 后端安全与稳定性

| 任务 | 文件/目录 | 依赖 | 能否并行 |
|---|---|---|---|
| P1.1 `/static` 路径穿越修复 + CORS 严格化 | `backend/app/main.py`、`routers/static.py` | 无 | ✅ 独立 |
| P1.2 异步路由去阻塞（上传、写文件） | `backend/app/routers/papers.py`、`thesis.py` | 无 | ✅ 独立 |
| P1.3 统一异常处理 + 结构化日志 | `backend/app/main.py`、`core/logger.py` | 无 | ✅ 独立，但会和所有 router 交互 |
| P1.4 FTS5 查询清洗 | `backend/app/routers/search.py` | 无 | ✅ 独立 |
| P1.5 后台任务与事件循环治理 | `backend/app/services/llm.py`、`auto_tag.py`、`routers/papers.py` | 无 | ⚠️ 与 P1.2 可能共享 `papers.py` |
| P1.6 限流与基础指标 | `backend/app/main.py`、`middleware` | 无 | ✅ 独立 |

**并行建议**：
- Agent A：P1.1（安全）
- Agent B：P1.2 + P1.5（异步 + 后台任务）
- Agent C：P1.3（日志/异常）
- Agent D：P1.4 + P1.6（检索 + 限流）

**注意**：P1.2 和 P1.5 都改 `papers.py`，需要最后合并时协调，或让同一个 Agent 负责。

**P1 验收**：新增 `tests/test_security.py`、`tests/test_upload.py`、`tests/test_search.py` 全部通过；`pytest` 绿。

---

### P2 前端与桌面端稳定

| 任务 | 文件/目录 | 依赖 | 能否并行 |
|---|---|---|---|
| P2.1 SSE 流式解析健壮化 | `frontend/src/components/ChatPanel.jsx`、`api.js` | 无 | ✅ 独立 |
| P2.2 错误边界 + 加载状态 | 各 pages/components | 无 | ✅ 独立 |
| P2.3 Electron 子进程管理 | `electron/main.js` | 后端启动稳定 | ✅ 独立 |
| P2.4 大列表虚拟滚动 | `frontend/src/pages/PaperList.jsx` | 无 | ✅ 独立 |
| P2.5 构建与 lint 通过 | `frontend/package.json` | P2.1–P2.4 | ❌ 必须在最后 |

**并行建议**：
- Agent A：P2.1 + P2.2（对话 + 全局体验）
- Agent B：P2.3（Electron 壳）
- Agent C：P2.4（列表性能）
- 最后统一跑 `npm run lint` 和 `npm run build`。

**P2 验收**：`npm run build` 通过，Electron dev 可正常拉起后端，手动测试上传/聊天/搜索无崩溃。

---

### P3 Agent 2.0 升级

| 任务 | 文件/目录 | 依赖 | 能否并行 |
|---|---|---|---|
| P3.1 Skill 抽象为 Tool Registry | `backend/app/services/skills.py`、`models.py` | 无 | ✅ 独立 |
| P3.2 LangGraph 编排 RAG/对话流程 | `backend/app/services/agent/` | P3.1、P3.3 | ❌ 依赖 |
| P3.3 Memory 统一模块 | `backend/app/services/memory_manager.py` | 无 | ✅ 可与 P3.1 并行 |
| P3.4 MCP Server 暴露知识库 | `backend/app/mcp/` | 无 | ✅ 可与 P3.2 并行 |
| P3.5 多 Agent 协作（Planner/Retriever/Writer/Critic） | `backend/app/services/multi_agent/` | P3.2 | ❌ 依赖 |

**并行建议**：
- Agent A：P3.1 + P3.3（Skill + Memory，两者都是基础服务）
- Agent B：P3.4（MCP Server，独立协议层）
- 等 A 完成后，Agent C：P3.2（LangGraph 编排）
- 等 C 完成后，Agent D：P3.5（多 Agent 协作）

**P3 验收**：新的 Skill 可被注册、被 LangGraph 调用；MCP 客户端能查询 PaperMind 知识库；多 Agent 能完成“检索 → 总结 → 校对”流程。

---

### P4 RAG 评测体系

| 任务 | 文件/目录 | 依赖 | 能否并行 |
|---|---|---|---|
| P4.1 构建 50–100 条 QA 评测集 | `backend/eval/dataset/` | 无 | ✅ 独立 |
| P4.2 检索指标脚本（Recall@K、MRR、NDCG） | `backend/eval/retrieval.py` | 无 | ✅ 独立 |
| P4.3 生成指标脚本（相关性、引用准确率） | `backend/eval/generation.py` | 需要 LLM 可用 | ✅ 独立 |
| P4.4 一键评测 + CI 集成 | `backend/eval/run.py`、GitHub Actions | P4.1–P4.3 | ❌ 最后集成 |

**并行建议**：
- Agent A：P4.1（数据集标注）
- Agent B：P4.2（检索指标）
- Agent C：P4.3（生成指标）
- 最后 A/B/C 合并后由 Agent D 做 P4.4。

**P4 验收**：`python -m eval.run` 能输出检索和生成指标报告。

---

### P5 CI/CD、Docker 与首次发布

| 任务 | 文件/目录 | 依赖 | 能否并行 |
|---|---|---|---|
| P5.1 GitHub Actions：pytest、lint、build | `.github/workflows/` | P0–P2 | ✅ 独立 |
| P5.2 Docker Compose 一键启动 | `docker-compose.yml`、`Dockerfile` | 无 | ✅ 独立 |
| P5.3 Electron 打包 CI | `.github/workflows/package.yml` | P2.3 | ✅ 可与 P5.1 并行 |
| P5.4 首次 Release 与推送 | GitHub Releases | P5.1–P5.3 | ❌ 最后 |

**并行建议**：
- Agent A：P5.1 + P5.3（Actions 工作流）
- Agent B：P5.2（Docker Compose）
- 最后统一验证并发布 v1.1.0。

---

## 4. 多 Agent 并行分配方案（推荐 3 个并行子代理）

虽然链路有先后顺序，但大量任务是**同阶段内可并行**的。建议按“后端/前端/评测基建”三条线分 Agent：

| Agent | 负责范围 | 典型阶段任务 |
|---|---|---|
| **Agent 1：后端稳定性** | 安全、异步、日志、测试、依赖 | P0.2、P0.3、P0.5、P1.1–P1.6、P3.1、P3.3 |
| **Agent 2：前端与桌面** | UI、SSE、Electron、构建 | P0.4（前端相关）、P2.1–P2.5、P3.4 的 MCP 客户端（可选） |
| **Agent 3：Agent & 评测** | LangGraph、MCP、多 Agent、评测体系 | P3.2、P3.4、P3.5、P4.1–P4.4、P5.1–P5.4 |

### 跨 Agent 协调原则

1. **文件隔离优先**：尽量让不同 Agent 改不同文件。例如 P1.1 改 `main.py`，P1.4 改 `search.py`，P1.3 改 `logger.py`，天然可并行。
2. **共享文件合并控制**：如果两个 Agent 都要改 `papers.py`（如 P1.2 和 P1.5），优先让同一个 Agent 负责，或约定“先小改接口、再合并”的顺序。
3. **阶段门控**：每阶段结束前必须有统一验收（pytest / npm run lint / 手动点一遍）。不要跨阶段并行，否则后端 Agent 在改 `services/llm.py` 时，前端 Agent 无法稳定测试聊天。
4. **分支策略**：每个 Agent 在独立分支工作，阶段末合并到 `main`，跑一次完整 CI。

---

## 5. 关键风险与串行依赖

以下任务**不能并行**，必须串行：

- P3.2（LangGraph）依赖 P3.1（Skill Tool）和 P3.3（Memory）完成。
- P3.5（多 Agent）依赖 P3.2（编排框架）完成。
- P4.4（CI 集成）依赖 P4.1–P4.3 脚本稳定。
- P5.1（CI）依赖 P0–P2 测试和 lint 能跑通。
- P2.3（Electron）依赖后端启动逻辑稳定，最好等 P1 完成后再大规模改。

以下任务可以**提前独立启动**：

- P4.1 评测数据集标注：只要有当前 RAG 流程和示例 PDF，就可以开始设计 QA 对。
- P5.2 Docker Compose：与代码改动相对独立，可早期定义。
- MCP Server（P3.4）：可以在 P1 稳定后、P3.2 完成前并行设计协议层。

---

## 6. 推荐的执行顺序（最小可验证单元）

为了让每一阶段都有“能跑、能演示”的版本，建议按这个节奏发布：

1. **MVP（P0 完成后）**：`pytest` 通过，后端能启动，仓库 clean。
2. **安全版（P1 完成后）**：上传/聊天/搜索稳定，异常不泄露，静态文件安全。
3. **可用版（P2 完成后）**：Electron 能打包，前端能流畅使用。
4. **Agent 版（P3 完成后）**：新 Skill、LangGraph 流程、MCP 可演示。
5. **可评测版（P4 完成后）**：能运行 `python -m eval.run` 输出指标。
6. **发布版（P5 完成后）**：GitHub Release + Docker Compose + Electron 安装包。

---

## 7. 立即下一步（建议本周做）

1. 锁定 P0：合并 `pyproject.toml`、测试骨架、配置分层。
2. 启动 3 个 Agent 并行进入 P1：后端安全、前端稳定、Electron 子进程管理。
3. 同时让第 3 个 Agent 开始准备 P4.1 评测数据集（可提前做）。

---

> 该计划与 `2026-07-20_PaperMind_Enterprise_Agent_Upgrade.md` 互为补充：前者偏重技术细节和代码实现，本文偏重执行顺序与并行策略。两个文档都保留在 `.hermes/plans/` 下。
