# database.py（SQLAlchemy 引擎与轻量 Schema 迁移）规格说明书

> 本文档描述 `backend/app/database.py` 的**行为契约**：全局 SQLite 引擎、会话工厂、声明式基类、WAL PRAGMA 监听、版本化轻量迁移登记表 `SCHEMA_MIGRATIONS` 与 `ensure_schema()`。
> 依据源码全文反向工程（92 行，SQLAlchemy 2.0 风格），函数签名与迁移分支照抄代码。

## 1. 背景与目标

`database.py` 是后端持久层的地基：它在**模块导入时**创建全局唯一的 SQLAlchemy 引擎（指向 `config.data_dir / 'papers.db'`）、会话工厂 `SessionLocal` 与声明式基类 `Base`，供全部 ORM 模型与路由/服务共享。项目**不使用 Alembic**（宪法第 9 条），schema 演进通过本文件的 `SCHEMA_MIGRATIONS` 登记表 + 启动时 `ensure_schema()` 手工 `ALTER TABLE ADD COLUMN` 完成，保证旧数据库文件在不删除数据的前提下补齐新列。

## 2. 范围

### 2.1 包含

- 模块级对象：`DATABASE_URL`、`engine`、`SessionLocal`、`Base` 的创建语义与配置。
- 引擎 `connect` 事件监听器 `_set_sqlite_pragma`（WAL / synchronous PRAGMA）。
- `SCHEMA_MIGRATIONS` 登记表的格式约定与全部 7 个迁移分支。
- `_apply_schema_migrations()`、`ensure_schema()`、`get_db()` 三个函数的行为契约。

### 2.2 非目标

- 不描述 ORM 表结构、FTS5 虚拟表与触发器（归 `models.py` 规格）。
- 不描述 `config.data_dir` 如何解析（归 `core/config.py` 规格）。
- 不描述调用方时序之外的启动流程（归 `main.py` 规格）；本文件只声明「`ensure_schema()` 必须在 `Base.metadata.create_all` 之后调用」这一前置条件。

## 3. 行为契约

### 3.1 `DATABASE_URL = f"sqlite:///{config.data_dir / 'papers.db'}"`

- **语义**：模块导入时根据配置单例 `config.data_dir` 拼接 SQLite 文件路径。**导入即定型**——之后修改 `PAPERMIND_DATA_DIR` 或 `config.data_dir` 不会改变已创建的引擎。
- **副作用**：读取 `app.core.config.config`（触发配置加载）。

### 3.2 `engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False}, echo=False)`

- **语义**：全局唯一引擎。`check_same_thread=False` 允许 SQLite 连接跨线程使用（FastAPI 线程池 + 后台处理线程必需）；`echo=False` 不打印 SQL。
- **副作用**：首次连接时真正打开/创建 `papers.db` 文件；每条新连接都会触发 3.3 的 PRAGMA 监听器。

### 3.3 `_set_sqlite_pragma(dbapi_conn, connection_record)`（`@event.listens_for(engine, "connect")`）

- **输入**：DBAPI 原生连接对象（由 SQLAlchemy 连接事件传入）。
- **输出**：无返回。
- **后置条件**：该连接上已执行 `PRAGMA journal_mode=WAL;` 与 `PRAGMA synchronous=NORMAL;`。
- **副作用**：对每条新建的数据库连接执行两条 PRAGMA。
- **异常**：无显式处理；PRAGMA 失败会以连接事件异常形式上抛。
- **注意**：**未**设置 `PRAGMA foreign_keys=ON`，因此 SQLite 数据库级外键约束不强制，孤儿行清理由 ORM 级联负责（见 models.py 规格）。内存数据库上 `journal_mode=WAL` 为静默无操作。

### 3.4 `SCHEMA_MIGRATIONS`（模块级常量）

- **格式**：`{表名: {列名: (SQL类型字符串, 默认值)}}`；`默认值` 为 `None` 表示 `ADD COLUMN` 不带 `DEFAULT` 子句，非 `None` 时按原样拼入 SQL（字符串默认值需自带引号，如 `"'{}'"`）。
- **当前登记的全部迁移分支**（7 个，逐一列出）：

| 表 | 列 | SQL 类型 | 默认值 | 生成的 SQL |
|---|---|---|---|---|
| `papers` | `last_read_page` | `INTEGER` | `1` | `ALTER TABLE papers ADD COLUMN last_read_page INTEGER DEFAULT 1` |
| `papers` | `metadata_json` | `JSON` | `'{}'` | `ALTER TABLE papers ADD COLUMN metadata_json JSON DEFAULT '{}'` |
| `conversations` | `paper_ids` | `JSON` | `'[]'` | `ALTER TABLE conversations ADD COLUMN paper_ids JSON DEFAULT '[]'` |
| `conversations` | `summary` | `TEXT` | `None` | `ALTER TABLE conversations ADD COLUMN summary TEXT` |
| `messages` | `citations` | `JSON` | `'[]'` | `ALTER TABLE messages ADD COLUMN citations JSON DEFAULT '[]'` |
| `messages` | `skill_used` | `VARCHAR(100)` | `None` | `ALTER TABLE messages ADD COLUMN skill_used VARCHAR(100)` |
| `messages` | `token_usage` | `INTEGER` | `None` | `ALTER TABLE messages ADD COLUMN token_usage INTEGER` |

### 3.5 `_apply_schema_migrations()`

```python
def _apply_schema_migrations():
    """执行轻量级 Schema 迁移：自动为旧表补齐模型中新增的列。"""
```

- **输入**：无（读取模块级 `engine` 与 `SCHEMA_MIGRATIONS`）。
- **输出**：无返回。
- **前置条件**：`Base.metadata.create_all` 已执行（否则新库上表结构已由 ORM 建全，本函数应为无操作）。
- **后置条件**：`SCHEMA_MIGRATIONS` 中登记且目标表已存在、但尚未存在的列，全部通过 `ALTER TABLE ... ADD COLUMN` 补齐并提交。
- **副作用**：
  1. 对每张登记表执行 `SELECT name FROM sqlite_master WHERE type='table' AND name=?`（参数化）判断表是否存在，**表不存在则整张表跳过**；
  2. 对存在的表执行 `PRAGMA table_info(<表名>)` 取已有列集合；
  3. 对缺失列执行 `ALTER TABLE <表名> ADD COLUMN <列名> <类型> [DEFAULT <默认值>]`（DDL 由登记表常量拼接，不接收运行时输入）；
  4. 每迁移一列写日志 `[schema] 已迁移 <表>.<列>`（INFO）；
  5. 全部完成后 `conn.commit()`。
- **异常**：**任何异常被捕获并吞掉**，仅写 `[schema] 轻量级迁移失败: <e>`（WARNING，含 traceback），不向上抛出——迁移失败不会阻止应用启动（但后续 ORM 查询缺列时会失败）。

### 3.6 `ensure_schema()`

```python
def ensure_schema():
    """在 Base.metadata.create_all 之后调用，保证旧数据库也能对齐新列。"""
```

- **语义**：对外暴露的唯一迁移入口，当前实现仅委托 `_apply_schema_migrations()`。
- **前置条件**：必须在 `Base.metadata.create_all(bind=engine)` 之后调用（`main.py` lifespan 中的实际顺序为 `create_all` → `ensure_schema()` → `ensure_papers_fts(engine)`）。
- **后置条件 / 副作用 / 异常**：同 3.5。

### 3.7 `SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)`

- **语义**：全局会话工厂。`autocommit=False`、`autoflush=False`，事务边界完全由调用方控制（显式 `commit()`/`rollback()`）。

### 3.8 `Base = declarative_base()`

- **语义**：全部 ORM 模型的声明式基类；`Base.metadata` 汇集所有表定义，供 `create_all`/`drop_all` 使用。

### 3.9 `get_db()`

```python
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
```

- **输出**：生成器，产出 `SessionLocal()` 会话（FastAPI 依赖注入用）。
- **后置条件**：请求结束（或依赖清理）时**保证 `db.close()`**，无论是否异常。
- **副作用**：每次调用新建一个会话；不自动 commit/rollback。
- **注意**：测试中通过 `app.dependency_overrides[get_db]` 整体替换为内存库会话（见 `tests/conftest.py`），本函数自身逻辑在测试中不被执行。

## 4. 边界条件与错误处理

| 场景 | 期望行为 |
|------|----------|
| 全新数据库（无任何表） | `create_all` 已建全列；`ensure_schema()` 中各表要么不存在被跳过、要么列已存在被跳过——整体无操作 |
| 旧库缺某列 | 仅对该缺失列执行 `ADD COLUMN`；已有列跳过（幂等，可重复启动） |
| 登记表中的表在库里不存在 | 整张表跳过，不报错、不建表（建表是 `create_all` 的职责） |
| 默认值为 `None` 的分支 | `ADD COLUMN` 不带 `DEFAULT`，存量行该列为 `NULL` |
| 默认值为字符串（如 `"'{}'"`） | 原样拼入 DDL，存量行获得该默认值 |
| 迁移过程中任何异常（如 DDL 语法错误、数据库损坏） | 捕获并写 WARNING 日志后**吞掉**，应用继续启动；缺列问题延后到查询时暴露 |
| 多线程同时使用会话 | 由 `check_same_thread=False` + WAL 支持；会话本身非线程安全，靠「每请求一会话」约定隔离 |
| 测试环境（内存 SQLite + StaticPool） | 模块级 `engine` 不被使用；PRAGMA 监听器只挂在模块级 `engine` 上，不影响测试引擎 |

## 5. 依赖

- **上游依赖**：`app.core.config.config`（`data_dir`）、`app.core.logger.logger`、SQLAlchemy 2.0、SQLite（需支持 WAL）。
- **下游消费者**：
  - `app.main`（lifespan：`engine`、`Base`、`ensure_schema()`）；
  - `app.models`（`Base`）；
  - 路由 `papers.py` / `chat.py` / `thesis.py` / `memory.py` / `export.py` / `search.py`（`get_db` 依赖注入；`papers.py`、`chat.py` 还在后台线程中直接用 `SessionLocal`）；
  - 服务 `mcp_server.py`（`SessionLocal`）；
  - `tests/conftest.py`（导入 `Base`、`get_db` 做依赖覆盖）。

## 6. 验收标准（可测试）

- [ ] AC1：对一个用旧 schema（缺 `papers.metadata_json` 等列）预建的数据库文件执行 `create_all` + `ensure_schema()` 后，`PRAGMA table_info` 显示 7 个登记列全部存在，且存量数据保留。
- [ ] AC2：对全新数据库连续调用两次 `ensure_schema()`，结果与调用一次一致（幂等，无异常、无重复列）。
- [ ] AC3：删除某张登记表对应的表后调用 `ensure_schema()`，该表被静默跳过且其他表正常迁移。
- [ ] AC4：模块级 `engine` 新建连接的 `PRAGMA journal_mode` 返回 `wal`、`PRAGMA synchronous` 返回 `1`（NORMAL）。
- [ ] AC5：`get_db()` 产出的会话在依赖清理后处于关闭状态（再使用会抛/重新建立连接）。
- [ ] AC6：迁移函数抛错路径（如对损坏库执行）只写 WARNING 日志，不向上抛异常。

## 7. 现有测试覆盖与盲区

- **已覆盖**：
  - `tests/conftest.py` 导入 `Base` 与 `get_db`：每个用例经 `Base.metadata.create_all/drop_all` 间接验证「`Base` 能汇集全部表定义」；`get_db` 作为依赖注入锚点被 `dependency_overrides` 替换（仅验证其「可覆盖」角色，不验证其自身逻辑）。
  - 除此之外**没有任何测试直接引用** `ensure_schema` / `SCHEMA_MIGRATIONS` / `_apply_schema_migrations` / `SessionLocal` / PRAGMA 监听器。
- **盲区**：
  - 【高】7 个迁移分支（3.4 全表）全部无测试：没有用「旧 schema 库」验证 `ADD COLUMN` 真正补齐列且保留数据，登记错误（类型写错、表名写错）不会被 CI 发现。
  - 【高】迁移失败「吞异常继续启动」路径（3.5 异常分支）无测试，无法保证降级行为符合预期而非静默带伤运行。
  - 【中】WAL / synchronous=NORMAL PRAGMA 未在文件数据库上断言（测试全走内存库，WAL 在内存库上是无操作）。
  - 【中】`get_db()` 的 `finally: db.close()` 语义无直接测试。
  - 【低】「表不存在则跳过」分支无测试。
  - 【低】`check_same_thread=False` 的跨线程使用约定无并发测试。

## 8. 关键设计决策

- **无 Alembic，登记表 + ADD COLUMN**：项目为单用户本地应用，schema 演进只有「加列」一种形态；登记表把「新列 = 模型 + 迁移 + schemas 三处同步」简化为一次登记，刻意不引入迁移框架（宪法第 9 条、AGENTS.md 第 5 节）。新增字段时必须同步改 `models.py` + 本表 + `schemas.py`。
- **迁移异常只警告不抛出**：保证「数据库有轻微问题时应用仍能启动」；代价是迁移失败会被静默降级，问题延后暴露——排查启动后查询异常时应先搜日志 `[schema]`。
- **引擎/会话工厂/Base 全部模块级单例**：导入即定型，与 `Config` 单例一致；测试通过自建引擎 + `dependency_overrides` 隔离，不复用模块级引擎。
- **只设 WAL/synchronous，不设 foreign_keys=ON**：外键完整性依赖 ORM 级联（`cascade="all, delete-orphan"`）而非数据库约束，绕过 ORM 的裸 SQL 删除可能留下孤儿行。
- **`check_same_thread=False`**：FastAPI 同步路由在线程池执行、PDF 处理等在后台线程执行，均需跨线程使用连接；安全性靠「每请求/每任务独立会话」约定保障。
