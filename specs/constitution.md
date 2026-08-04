# PaperMind 项目宪法

> 本文件是 PaperMind 的最高工程准则。所有规格（spec）、计划（plan）、任务（tasks）与代码实现不得违反本宪法。
> 如确需偏离，必须先修改本文件、说明理由，并在提交信息中引用本文件章节号。
>
> 版本：v1.0（2026-08-04 初版）

---

## 一、产品原则（不可妥协）

1. **本地优先**：PDF、笔记、SQLite、ChromaDB、日志全部存本地。除 LLM API 调用外，不得引入任何云端依赖。
2. **单用户零权限**：不得引入登录、注册、权限、角色、协作、多租户等概念。发现即删。
3. **可移植**：所有路径相对化，经 `Path(__file__).resolve().parents[N]` 或 `config.data_dir` 定位；Electron 生产包经 `PAPERMIND_DATA_DIR` 重定向数据目录。禁止硬编码绝对路径。
4. **单进程架构**：一个 FastAPI 进程提供 API + 静态文件；不引入消息队列、Celery、独立 worker 进程。

## 二、工程原则

5. **TDD 铁律**：`NO PRODUCTION CODE WITHOUT A FAILING TEST FIRST`。新功能/缺陷修复/行为变更必须先写失败测试（RED），亲眼看到它因正确原因失败，再写最小实现（GREEN），最后重构（REFACTOR）。例外仅限：纯配置、生成代码、一次性原型（且需在提交信息中声明）。
6. **最小改动**：只碰任务需要的代码。禁止顺手重构、改名、重排版。
7. **中文注释与文档**：docstring、日志前缀（如 `[startup]`、`[fts]`）、提交说明、spec 文档一律中文；标识符用英文。
8. **LLM 调用唯一入口**：所有 OpenAI 兼容调用必须经 `services/llm.py`（`llm_service`），不得绕过它直接实例化 openai client。重试、截断、错误格式化由它统一负责。
9. **数据库演进**：无 Alembic。新增字段 = 改 `models.py` + 在 `database.py` 的 `ensure_schema()` 加迁移分支 + 同步 `schemas.py`。
10. **测试纪律**：测试使用内存 SQLite + TestClient，不触发 lifespan，不发起真实 LLM/embedding 调用（mock），后台线程 mock 掉。测试命令固定为 `env -u PYTHONPATH venv/bin/python -m pytest tests/ -q`。

## 三、安全纪律

11. **SQL 参数化**：一切数据库操作走 SQLAlchemy ORM 或绑定参数；FTS 查询串必须先经 `_sanitize_fts_query()` 清洗。禁止字符串拼接 SQL。
12. **静态文件白名单**：`/static` 仅放行 `papers/`、`notes/`、`my-thesis/`、`summaries/`，`resolve()` 防穿越。新增敏感文件不得放入这四目录。
13. **异常脱敏**：全局异常处理不向前端返回异常原文（通用文案 + error_code），详情只写 `logs/app.log`。
14. **密钥与数据不入库**：`config.yaml`、`data/`、`papers/`、`notes/`、`vector_db/`、`logs/`、`cache/`、`venv/` 永不提交 git。文档与日志中出现密钥一律 `[REDACTED]`。
15. **不暴露公网**：CORS 维持显式 origin 白名单；后端绑定与部署文档不得引导用户把服务暴露到公网。

## 四、依赖纪律

16. **锁定版本不得擅自升级**，已知硬约束：
    - `mcp==1.3.0`、`sse-starlette==1.8.2`（更高版依赖 starlette≥0.49/pydantic≥2.11，与 FastAPI 0.110 冲突）
    - `httpx==0.27.2`（openai 1.12 与 httpx≥0.28 不兼容）
    - `transformers==4.39.3`、`torch==2.2.2`（macOS x86_64 + Py3.12 上限）
    - `pydantic==2.7.4`、`pydantic-settings==2.5.2`（langgraph 1.2.9 要求）
17. **新增/升级依赖**必须先 `pip check` 零冲突，并在提交信息中说明动机与验证结果。
18. **requirements.txt 与 pyproject.toml 保持同步**。

## 五、SDD 工作流（规格驱动开发）

19. **新功能四件套**：任何新功能必须先有 `spec.md`（要什么/为什么）→ `plan.md`（怎么做/技术方案）→ `tasks.md`（TDD 任务分解）→ 实现。模板见 `specs/_templates/`。
20. **存量改动同步规格**：修改已有行为时，必须同步更新对应 `specs/` 文档；规格与代码冲突时以代码为准并立即修订规格。
21. **规格是行为契约**：spec 只描述"做什么、边界、错误处理、副作用"，不描述实现细节；实现细节归 plan。
22. **验收标准可测试**：spec 中的每条验收标准必须能映射到至少一个自动化测试。

---

> 本宪法随代码库演进。修订历史见 git log（本文件）。
