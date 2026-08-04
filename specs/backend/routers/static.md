# routers/static.py（白名单受限静态文件服务）规格说明书

> 本文件描述 `backend/app/routers/static.py` 的**行为契约**（做什么），不描述实现细节。
> 依据源码实际内容反向工程整理（2026-08-04）。端点签名照抄代码。
> 本模块是宪法第 12 条（静态文件白名单）的直接实现。

## 1. 背景与目标

前端需要直接引用本地资源：PDF 预览（`papers/`）、Markdown 笔记（`notes/`）、大论文 Word（`my-thesis/`）、AI 概括（`summaries/`）。历史上整个项目根经 `StaticFiles` 挂载暴露，`config.yaml`（含 API Key）、`data/papers.db`、`backend/` 源码均可被 `GET /static/...` 直接下载，且存在 `../` 路径穿越面。本模块以**白名单 + `resolve()` 双重校验**取代整体挂载：只放行四个数据目录，拒绝一切穿越与软链接逃逸。

## 2. 范围

### 2.1 包含

- `GET /static/{file_path:path}`：按白名单提供单个文件下载
- `_resolve_static_path` 的三级校验契约（白名单 → 防穿越 → 存在性）
- 白名单目录集 `ALLOWED_DIRS` 与项目根定位 `PROJECT_ROOT`

### 2.2 非目标

- 目录列表 / 浏览：无 list 端点，请求目录本身一律 404
- 写操作（上传 / 删除）：上传走 `routers/papers.py` 等专用端点
- 缓存策略、Range 请求、Content-Type 细调：全部委托 Starlette `FileResponse` 默认行为，本模块不干预
- 前端构建产物 `frontend/dist` 的服务：开发走 Vite（:5173），生产由 Electron 壳加载，均不经本路由
- 新增白名单目录的运行时配置：`ALLOWED_DIRS` 是代码常量，改它必须改代码并过审（宪法第 12 条：新增敏感文件不得放入这四目录）

## 3. 行为契约

路由注册：`app.include_router(static.router, tags=["static"])`（`main.py`，**无前缀**，且必须排在 `/mcp` 挂载之后，避免抢先匹配）。

### 3.0 模块常量

- `PROJECT_ROOT = Path(__file__).resolve().parents[3]`：项目根（`backend/app/routers/` 上溯三级）。**不随 `PAPERMIND_DATA_DIR` 重定向**——Electron 生产包中数据目录被重定向到系统应用数据目录后，本常量仍指向安装包内路径（与 `services/backup.py::get_project_root` 同一已知遗留，见该规格第 8 节）。
- `ALLOWED_DIRS = ("papers", "notes", "my-thesis", "summaries")`：允许访问的一级子目录，与前端实际使用保持一致。

### 3.1 `_resolve_static_path(file_path: str) -> Path`

按顺序执行三级校验，任一失败即抛 `HTTPException`：

1. **白名单校验**：`parts = Path(file_path).parts`；`parts` 为空或 `parts[0]` 不在 `ALLOWED_DIRS` → **403** `detail="禁止访问该路径"`。
   - 由此直接拦下：`config.yaml`、`backend/...`、`data/...`、`logs/...`、`backups/...`、`../...`（`parts[0]` 为 `".."`）、`%2e%2e` 解码后的 `..` 等。
2. **防穿越校验**：`allowed_root = (PROJECT_ROOT / parts[0]).resolve()`；`target = allowed_root.joinpath(*parts[1:]).resolve()`（`len(parts)==1` 时 `target = allowed_root`）；`target != allowed_root and allowed_root not in target.parents` → **403** 同文案。
   - `resolve()` 同时归一化 `..` 段并**追随软链接**——`papers/../../config.yaml` 与白名单内指向外部文件的软链接（如 `papers/evil.txt -> ../../secret.yaml`）均因越出 `allowed_root` 被 403；
   - 指向白名单目录**内部**另一文件的软链接合法放行；
   - 仅请求目录本身（`/static/papers`）时 `target == allowed_root`，通过本级，留给第三级处理。
3. **存在性校验**：`target.is_file()` 为假（不存在、是目录、是悬空软链接）→ **404** `detail="文件不存在"`。

- **输出**：通过全部校验的绝对 `Path`
- **副作用**：无（仅文件系统元数据检查，不读文件内容）

### 3.2 `GET /static/{file_path:path}` → `async serve_static(file_path: str)`

- **输入**：路径参数 `file_path`（`path` 转换器，允许含 `/` 的多级相对路径）
- **输出**：`FileResponse(_resolve_static_path(file_path))`——Content-Type 按扩展名推断、支持条件请求等均为 Starlette 默认行为
- **异常**：403 / 404 见 3.1（HTTPException detail 原文返给客户端）；`FileResponse` 阶段异常 → 全局 500 通用文案
- **副作用**：读文件内容并下发；无写操作

## 4. 边界条件与错误处理

| 场景 | 期望行为 |
|------|----------|
| `/static/papers/xxx.pdf`（白名单内真实文件） | 200，`FileResponse` 正常下发 |
| `/static/papers/sub/xxx.pdf`（白名单内子目录） | 200（多级路径合法） |
| `/static/config.yaml`、`/static/backend/...`、`/static/data/...`、`/static/logs/...` | 403（一级目录非白名单） |
| `/static/../config.yaml` | 403 或 404（客户端/服务器规范化 `..` 后均不命中白名单；测试接受两者） |
| `/static/papers/../../config.yaml`（白名单内穿越） | 403（`resolve()` 后越出 `allowed_root`） |
| `/static/%2e%2e/config.yaml`（URL 编码穿越） | 403（解码后 `parts[0]==".."` 非白名单） |
| 白名单内软链接指向白名单**外**文件 | 403（`resolve()` 追随软链接后越界） |
| 白名单内软链接指向白名单**内**文件 | 200 放行 |
| `/static/papers`（只给目录不给文件） | 404（目录不是文件） |
| 不存在的文件 / 悬空软链接 | 404 |
| 新增敏感文件放入四个白名单目录 | **会被公开下载**——白名单是目录级授权，宪法第 12 条明令禁止此操作 |

## 5. 依赖

- **上游依赖**：`pathlib`；`fastapi.HTTPException`；`fastapi.responses.FileResponse`；`main.py` 的挂载顺序（`/mcp` 必须先于本路由）
- **下游消费者**：前端 PDF 预览 / 笔记 / 概括 / 大论文页面的资源 URL（经 Vite dev 代理 `/static` → :8000）；`test_security.py` 的安全回归

## 6. 验收标准（可测试）

- [ ] AC1：白名单四目录内的真实文件可 200 下载，内容一致
- [ ] AC2：非白名单一级目录（`config.yaml` / `backend/` / `data/` / `logs/`）一律 403/404，且响应体不含敏感文件内容
- [ ] AC3：`..` 穿越（含白名单内 `papers/../...`、URL 编码 `%2e%2e`）一律被拒
- [ ] AC4：指向白名单外的软链接被拒（403/404）；指向白名单内的软链接放行
- [ ] AC5：请求目录本身或不存在的文件 → 404

## 7. 现有测试覆盖与盲区

- **已覆盖**（`backend/tests/test_security.py`）：
  - `TestStaticTraversal`（7 个参数化路径）：`../config.yaml`、直接 `config.yaml`、`backend/`、`data/`、`logs/`、白名单内 `../..` 穿越、`%2e%2e` 编码穿越，断言 403/404 且响应体无 `api_key` → 覆盖 AC2、AC3
  - `TestStaticWhitelist`（monkeypatch `PROJECT_ROOT` 到 `tmp_path`，4 例）：白名单文件 200 且内容一致、缺失文件 404、白名单内穿越被拦、软链接逃逸被拦 → 覆盖 AC1、AC4（逃逸向）、AC5
- **盲区**：
  - 指向白名单**内部**的软链接放行行为（AC4 的放行向）无测试（**低**，属刻意放行但无固化）
  - `notes/`、`my-thesis/`、`summaries/` 三个白名单目录只测了 `papers/`（**低**，共用同一 `_resolve_static_path`，风险低）
  - `/static/papers`（请求目录本身）→ 404 无显式用例（**低**，被 `not-exist.txt` 用例间接覆盖 `is_file` 分支）
  - `PROJECT_ROOT` 不随 `PAPERMIND_DATA_DIR` 重定向的 Electron 生产包行为无测试（**中**，与 backup 同源的已知遗留；桌面包内数据被重定向后 `/static` 可能取不到真实文件）
  - 多级子目录（`papers/sub/file`）放行无显式用例（**低**）

## 8. 关键设计决策

- **白名单目录级授权 + `resolve()` 双重校验**（宪法第 12 条落地）：第一级按 `parts[0]` 粗筛（快、覆盖绝大多数攻击面），第二级 `resolve()` 后做祖先包含判断（兜住 `..` 归一化与软链接逃逸）——单靠字符串前缀匹配防不住软链接，单靠 `resolve()` 又无法表达「只允许这四个目录」，两者缺一不可
- **403 与 404 刻意分工**：「越权」与「不存在」语义分离；测试对穿越类接受 403/404 双值是因为客户端 URL 规范化可能让请求根本不命中本路由（落到 FastAPI 默认 404）
- **`FileResponse` 全委托**：不手写流式读取，Content-Type / 条件请求 / Range 均用 Starlette 默认，避免自造轮子引入新攻击面
- **替代整体 `StaticFiles` 挂载**：历史教训是项目根暴露导致 `config.yaml`（API Key）可下载；本模块是安全修复的直接产物，`test_security.py` 即其回归护栏——改动本模块前必须先跑该测试文件
- **挂载顺序敏感**：`/mcp` 子应用必须先于本路由挂载（`main.py` 注释明确），否则 `/mcp/...` 会被 `{file_path:path}` 抢先匹配——这是隐性契约，调整 `main.py` 路由顺序时需保持
