# core/logger.py（全局日志器）规格说明书

> 本文件描述 `backend/app/core/logger.py` 的**行为契约**（做什么），不描述实现细节。
> 依据源码实际内容反向工程整理（2026-08-04）。

## 1. 背景与目标

PaperMind 排查问题依赖单一日志文件 `logs/app.log`（见 AGENTS.md：「排查问题先看这里」）。本模块提供项目统一的日志器 `logger`：同时输出到控制台（开发期可读）和按天轮转的文件（长期留存），格式带中文日志前缀约定（如 `[startup]`、`[fts]`）。它的存在让全部业务模块以 `from app.core.logger import logger` 一行获得行为一致的日志通道，避免各处自行 `logging.basicConfig` 造成的重复输出与格式分裂。

## 2. 范围

### 2.1 包含

- `setup_logger(name)` 的创建、幂等与 handler 配置契约
- 日志文件位置、轮转策略、两种输出格式
- 模块级 `logger = setup_logger()` 全局实例
- 日志目录的自动创建

### 2.2 非目标

- 日志级别按环境区分（固定 INFO，无 DEBUG/生产分级）
- 日志内容脱敏（依赖调用方自律：API Key 等敏感值不应写入日志，见宪法第 14 条）
- 第三方库（uvicorn/sqlalchemy/chromadb）的日志接管
- 结构化/JSON 日志、日志采集
- Electron 主进程日志（在数据目录 `logs/electron-main.log`，归 electron/main.js 管）

## 3. 行为契约

### 3.1 `setup_logger(name: str = "papermind") -> logging.Logger`

- **输入**：`name` 日志器名称，默认 `"papermind"`
- **输出**：配置完成的 `logging.Logger`
- **前置条件**：无
- **后置条件**：
  - 返回 `logging.getLogger(name)` 对应的日志器；
  - **幂等**：若该日志器已有任何 handler，直接原样返回，不重复添加（重复调用不产生重复行）；
  - 首次配置时：日志器级别 INFO；日志目录 `<项目根>/logs/` 被创建（`parents=True, exist_ok=True`）；挂两个 handler（见 3.2、3.3）
- **副作用**：创建 `logs/` 目录；首次调用后日志文件 `logs/app.log` 由 handler 在首条日志时创建/追加
- **异常**：日志目录无权限创建 → `OSError`（模块导入即失败）

### 3.2 文件 handler（`TimedRotatingFileHandler`）

- **文件**：`<项目根>/logs/app.log`（项目根 = `Path(__file__).resolve().parents[3]`）
- **轮转**：`when="midnight"`、`interval=1`、`backupCount=7`——每天零点轮转，最多保留 7 个历史文件（`app.log.YYYY-MM-DD`）
- **编码**：UTF-8
- **级别**：INFO
- **格式**：`%(asctime)s [%(levelname)s] %(name)s - %(message)s`，时间格式 `%Y-%m-%d %H:%M:%S`
  - 示例：`2026-08-04 03:00:01 [INFO] papermind - [startup] 数据库表结构检查完成`

### 3.3 控制台 handler（`StreamHandler`）

- **输出**：stderr（`StreamHandler` 默认）
- **级别**：INFO
- **格式**：`[%(levelname)s] %(message)s`（不含时间与 logger 名，保持终端简洁）
  - 示例：`[INFO] [startup] 数据库表结构检查完成`

### 3.4 模块级 `logger = setup_logger()`

- **行为**：模块导入时即以默认名 `"papermind"` 创建全局日志器；全项目统一 `from app.core.logger import logger` 使用
- **副作用**：导入期创建 `logs/` 目录；与 `config.py` 一样属于「导入即生效」模块
- **注意**：`logger.propagate` 未显式设置（默认 True），若 root logger 被第三方配置（如 uvicorn），记录会向上传播、可能在终端出现第三方格式化的重复行

### 3.5 日志内容约定（消费方契约）

- 日志消息使用中文并带模块前缀，如 `[startup]`、`[fts]`、`[backup]`、`[config]`、`[settings]`（宪法第 7 条）
- 全局异常详情只写本日志、不回前端（宪法第 13 条），因此本文件是唯一异常详情落点
- API Key 等敏感值不得写入日志；文档与日志样例中一律 `[REDACTED]`（宪法第 14 条）

## 4. 边界条件与错误处理

| 场景 | 期望行为 |
|------|----------|
| 重复调用 `setup_logger()` | 返回同一 logger，handler 数量不增加，日志不重复 |
| 调用方先给 logger 加了自定义 handler | `setup_logger` 检测到已有 handler，直接返回，不再附加文件/控制台 handler（静默采用调用方配置） |
| `logs/` 目录不存在 | 自动创建（含多级父目录） |
| `logs/` 存在但无写权限 | 模块导入期抛 `OSError`，应用无法启动（显式失败，不降级为纯控制台） |
| 跨零点长时间运行 | 自动轮转，`app.log` 始终是当天文件，历史文件最多 7 份，更老的在下次轮转时删除 |
| `PAPERMIND_DATA_DIR` 已设置（Electron 生产包） | **日志仍写项目根 `logs/`**，不跟随数据目录重定向（见第 8 节决策） |
| 同一进程内测试多次 import | Python 模块缓存保证只配置一次 |
| 子 logger（如 `logging.getLogger("papermind.x")`） | 未在项目代码中使用；按 logging 语义会 propagate 到 `papermind` 的 handler |

## 5. 依赖

- **上游依赖**：Python 标准库 `logging`、`logging.handlers`、`pathlib`；`app.core.config`（导入 config 单例，但当前代码**未实际使用其任何值**，属冗余耦合，导入即触发 config 加载副作用）
- **下游消费者**：全项目——`main.py`、`database.py`、`models.py`（ensure_papers_fts 内部）、`core/settings.py`、全部 routers 与大部分 services（web_search / retrieval / pdf_parser / memory_manager / mcp_server / llm / image_analyzer / embedding / backup / auto_tag / agent_graph 等）
- **被保护关系**：`tests/test_security.py::TestStaticTraversal` 验证 `/static/logs/app.log` 经静态路由不可读（403/404），保证日志不外泄

## 6. 验收标准（可测试）

- [ ] AC1：`setup_logger()` 返回的 logger 名为 `papermind`，级别 INFO，恰好挂 2 个 handler（1 个 TimedRotatingFileHandler + 1 个 StreamHandler）
- [ ] AC2：连续调用两次 `setup_logger()`，handler 数量不变（幂等）
- [ ] AC3：写一条 INFO 日志后，`logs/app.log` 出现匹配 `时间 [INFO] papermind - 消息` 格式的行；stderr 出现 `[INFO] 消息`
- [ ] AC4：文件 handler 的轮转参数为 midnight/interval=1/backupCount=7，编码 UTF-8
- [ ] AC5：项目根无 `logs/` 目录时导入本模块会自动创建
- [ ] AC6：DEBUG 级别消息不进入任何 handler（级别 INFO 过滤）

## 7. 现有测试覆盖与盲区

- **已覆盖**：
  - 无直接测试。`tests/test_security.py::TestStaticTraversal` 的 `/static/logs/app.log` 用例间接保护日志文件不经 HTTP 外泄
  - 全项目测试运行时 logger 处于激活状态（导入链必然触发），但无断言针对日志行为本身
- **盲区**：
  - `setup_logger` 的幂等性（重复调用不重复加 handler）——**中**：一旦回归，所有日志行成倍出现，污染排查
  - 文件 handler 的轮转参数（midnight/7 天）与 UTF-8 编码——**中**：日志无限增长或中文乱码属静默劣化
  - 日志格式串（文件带时间戳、控制台不带）——**低**：格式错误只影响可读性
  - `logs/` 目录自动创建——**低**：失败会在导入期直接抛错，容易暴露
  - DEBUG 消息被过滤、propagate 默认 True 与 uvicorn root logger 叠加的重复输出现象——**低**：属已知现象无断言
  - Electron 场景日志不随 `PAPERMIND_DATA_DIR` 重定向——**中**：生产包日志写在 resources 内（只读卷上可能失败），无任何测试守护

## 8. 关键设计决策

- **模块级全局 logger + 幂等 setup**：`logger = setup_logger()` 在导入时执行，所有模块共享同一实例；`if logger.handlers: return logger` 的提前返回是最小成本的幂等实现，顺带允许测试预置 handler 来捕获日志。
- **双 handler 分工**：文件要全量上下文（时间/级别/logger 名）供事后排查；控制台只留级别和消息，保证开发期终端清爽。两种格式是有意的不一致。
- **固定 INFO 级别**：单用户本地应用没有多环境日志分级需求，DEBUG 噪音默认关闭；需要时改源码一处即可。
- **按天轮转保留 7 天**：与「每日凌晨 3 点备份、保留 10 份」的运维节奏一致；本地磁盘占用可预期。
- **日志路径锚定项目根而非 `config.data_dir`**：历史实现如此——`data_dir` 管数据库与向量库，日志独立放项目根 `logs/`；副作用是 Electron 生产包中日志不随 `PAPERMIND_DATA_DIR` 迁移（AGENTS.md 另记 electron-main.log 在数据目录）。改动此处需同时评估打包场景写权限。
- **对 `config.py` 的冗余 import**：`from app.core.config import config` 未被使用，但使 logger 导入连带触发 config 的导入期加载；删除它是安全的最小改动，但目前保持原样（宪法第 6 条最小改动）。
