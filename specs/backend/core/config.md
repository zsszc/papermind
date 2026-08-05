# core/config.py（YAML 配置单例 Config）规格说明书

> 本文件描述 `backend/app/core/config.py` 的**行为契约**（做什么），不描述实现细节。
> 依据源码实际内容反向工程整理（2026-08-04）。

## 1. 背景与目标

PaperMind 是本地优先的单用户应用，开发模式配置集中存放在项目根 `config.yaml`，Electron 生产模式存放在应用数据目录。本模块提供全局唯一的配置访问入口，并统一解析配置路径、数据库目录与可变运行时文件根目录。

## 2. 范围

### 2.1 包含

- `Config` 单例的创建与首次加载时机
- 配置文件定位逻辑：`PAPERMIND_DATA_DIR` 分支、example 首次复制与开发模式回退
- 点分路径读取 `get()`、`config_path` 属性、`reload()`、`save()`、`data_dir` 属性
- 模块级 `config = Config()` 的导入期副作用

### 2.2 非目标

- `PAPERMIND_*` 环境变量对配置值的覆盖（归 `core/settings.py`）
- 启动时配置合法性校验（归 `core/settings.py` 的 `validate_startup_config`）
- 配置项 schema 定义与默认值表（各消费方自行传 default）
- 配置的并发写保护、变更通知、热更新广播

## 3. 行为契约

### 3.1 `class Config` / `__new__(cls)`

- **输入**：无（无参实例化）
- **输出**：`Config` 实例，全进程唯一
- **前置条件**：无
- **后置条件**：首次实例化时 `_instance` 被赋值、`_config_path` 初始化为 `None` 并立即调用一次 `_load()`；之后所有 `Config()` 调用返回同一实例，不再重复加载
- **副作用**：首次实例化触发 `_load()` 的全部文件 I/O（见 3.2）
- **异常**：`_load()` 中文件打不开或 YAML 解析失败时异常向上抛出（无兜底）

### 3.2 `Config._load(self)`

- **输入**：隐式读取环境变量 `PAPERMIND_DATA_DIR`、项目根（`Path(__file__).resolve().parents[3]`，即 `backend/app/core/config.py` 上溯 3 级 = 项目根）
- **输出**：无返回值；结果写入 `self._config_path`（`Path`）与 `self._config`（`dict`，`yaml.safe_load` 结果，空文件归一化为 `{}`）
- **前置条件**：无
- **后置条件**：`self._config` 为 dict；`self._config_path` 指向最终实际读取的文件
- **副作用**（文件 I/O，按分支）：
  - 设了 `PAPERMIND_DATA_DIR`（Electron 生产包）：
    1. `mkdir(parents=True, exist_ok=True)` 创建该数据目录；
    2. 目标为 `<数据目录>/config.yaml`；目标不存在且项目根 `config.yaml.example` 存在时复制 example；
    3. 安装包不携带、也绝不复制真实 `config.yaml`；已有用户配置（含占位符或自定义离线配置）不覆盖；
  - 未设 `PAPERMIND_DATA_DIR`（开发模式）：目标为项目根 `config.yaml`；
  - 若最终目标文件不存在，回退为项目根 `config.yaml.example`（只读回退，不复制）；
  - 以 UTF-8 打开目标文件并 `yaml.safe_load`。
- **异常**：最终路径文件不存在（example 也缺失）→ `FileNotFoundError`；YAML 语法错误 → `yaml.YAMLError`

#### 占位符判定（`_load` 内部 `_is_placeholder_config(path)`）

满足任一即视为占位符：

| 条件 | 说明 |
|------|------|
| 文件不存在 | 直接 True |
| 文本含 `sk-xxxx` 或（忽略大小写）`your-` | 模板残留 |
| YAML 解析后 `llm.api_key` 为空、含 `xxxx`、或以 `your-` 开头 | 未填真实 Key |
| 读取/解析过程抛任何异常 | 保守按占位符处理 |

### 3.3 `Config.get(self, key: str, default: Any = None) -> Any`

- **输入**：`key` 为点分路径（如 `"llm.api_key"`）；`default` 为缺失时返回值
- **输出**：路径命中返回对应值（类型取决于 YAML 内容）；任一层级缺失或当前值不是 dict 时返回 `default`
- **前置条件**：`_load()` 已成功执行
- **后置条件**：不修改 `_config`
- **副作用**：无
- **异常**：不抛异常（对缺失路径完全静默）

### 3.4 `Config.config_path`（property）`-> Path`

- **输出**：当前实际使用的配置文件路径
- **副作用**：无

### 3.5 `Config.reload(self)`

- **行为**：重新执行 `_load()`，从磁盘重读配置并整体替换 `_config` 与 `_config_path`
- **副作用**：与 `_load()` 相同的文件 I/O（含可能的目标目录创建与文件复制）
- **异常**：同 `_load()`

### 3.6 `Config.save(self)`

- **行为**：若 `_config_path` 为 `None` 则静默返回；若当前路径为 `config.yaml.example`，实际目标切换为同目录 `config.yaml`。配置先写入同目录临时文件，刷新并同步后以 `os.replace` 原子替换目标；成功后权限收紧为 `0600`，`_config_path` 更新为目标路径。
- **副作用**：全量重写私有配置文件（原注释丢失，因为只保留数据不保留 YAML 注释）；绝不写回 `.example` 模板。
- **异常**：序列化、同步、替换或权限设置失败时异常向上抛出；替换前失败保证原目标文件不变，并清理临时文件。
- **下游调用方**：`routers/settings.py` 的 `PUT /api/settings`

### 3.7 `Config.data_dir`（property）`-> Path`

- **行为**：
  - 设了 `PAPERMIND_DATA_DIR`：返回该路径（先 `mkdir -p`）；
  - 否则读 `app.data_dir` 配置（默认 `"./data"`），相对路径拼到项目根下；返回前 `mkdir -p`
- **输出**：已确保存在的目录 `Path`
- **副作用**：每次访问都可能创建目录
- **下游调用方**：`database.py`（拼 `papers.db` 的 `DATABASE_URL`）
- **异常**：无权限创建目录 → `OSError`

### 3.8 `Config.runtime_root`（property）`-> Path`

- 设置 `PAPERMIND_DATA_DIR` 时返回该目录，否则返回项目根；返回前自动创建。
- PDF、笔记、概括、论文、向量库、日志、备份和静态文件均从此根目录派生。

### 3.9 模块级 `config = Config()`

- **行为**：模块被导入时立即实例化单例，即首次 import 就完成配置文件定位与加载
- **副作用**：导入期文件 I/O（创建数据目录、可能复制 config.yaml、读 YAML）；因此**在测试里 monkeypatch 环境变量必须先于首次 import 本模块**

## 4. 边界条件与错误处理

| 场景 | 期望行为 |
|------|----------|
| 开发模式且项目根 `config.yaml` 缺失 | 回退读 `config.yaml.example`，`_config_path` 指向 example |
| `config.yaml` 为空文件 | `_config = {}`（`or {}` 归一化） |
| `config.yaml` 内容不是 dict（如纯列表/标量） | `get()` 第一级就因 `isinstance(value, dict)` 失败而返回 default |
| `get("")`（空 key） | 按单段 key 查找，通常返回 default |
| `PAPERMIND_DATA_DIR` 指向不存在/未建目录 | 自动 `mkdir -p` 创建 |
| 数据目录已有任意 config.yaml | 原样加载，不被安装包覆盖 |
| 目标不存在 | 复制 example 兜底 |
| 已有配置 YAML 损坏 | 抛 `yaml.YAMLError`，原文件保持不变 |
| `save()` 时从未成功 `_load`（`_config_path=None`） | 静默返回，不写文件 |
| 从 `.example` 回退后 `save()` | 新建同目录 `config.yaml`，模板不变 |
| `save()` 写回 | 原子替换，权限 `0600`；YAML 注释丢失、键顺序按原 dict 顺序 |
| 并发调用 `Config()` | 无锁，理论上竞争创建实例；实际项目单进程单线程启动期导入，风险可忽略 |

## 5. 依赖

- **上游依赖**：`os`（`PAPERMIND_DATA_DIR`）、`yaml`（PyYAML）、`pathlib`；项目根 `config.yaml` / `config.yaml.example` 文件
- **下游消费者**：`core/logger.py`（导入但未实际使用 config 值）、`database.py`（`config.data_dir`）、`main.py`、`routers/settings.py`（get/save）、`routers/export.py`、`services/llm.py`、`services/embedding.py`、`services/retrieval.py`、`services/backup.py`、`services/web_search.py`、`services/image_analyzer.py` 等几乎全部业务模块

## 6. 验收标准（可测试）

- [ ] AC1：开发模式（无 `PAPERMIND_DATA_DIR`）且项目根有 `config.yaml` 时，`config.config_path` 指向它
- [ ] AC2：项目根 `config.yaml` 缺失时回退到 `config.yaml.example`，`get("llm.model")` 返回 example 中的值
- [x] AC3：设置 `PAPERMIND_DATA_DIR` 且目标缺失时只复制 `config.yaml.example`
- [x] AC4：已有配置不覆盖；损坏配置显式报错且内容不变
- [ ] AC5：`get("a.b.c")` 在嵌套 dict 中逐层命中；任一层缺失返回传入的 default 且不抛异常
- [x] AC6：`save()` 后重新 `reload()`，修改过的键值持久存在；回退模板不被覆盖，失败不破坏原文件且权限为 `0600`
- [ ] AC7：`data_dir` 在设置 `PAPERMIND_DATA_DIR` 时返回该目录且目录被创建；未设置时返回 `<项目根>/data`（默认）并被创建
- [ ] AC8：重复调用 `Config()` 返回同一对象（`Config() is Config()`）

## 7. 现有测试覆盖与盲区

- **已覆盖**：
  - `tests/test_settings.py::test_get_settings_masks_api_key` 间接使用 `config.get("llm.api_key")` 做断言对比，属于消费方测试，并非针对本模块行为的直接测试
  - `tests/test_security.py::TestStaticTraversal` 验证 `/static/../config.yaml`、`/static/config.yaml` 不可经静态路由读取（保护的是配置文件不外泄）
- **盲区**：
  - `PAPERMIND_DATA_DIR` 分支全部逻辑（目录创建、bundled 覆盖、example 复制、占位符检测 `_is_placeholder_config` 四种判定路径）——**高**：Electron 生产包核心路径，一旦回归用户拿到的是占位符配置且无任何测试报警
  - 缺失 `config.yaml` 时回退 example 的行为——**高**：新机器/CI 首跑依赖此路径
  - `get()` 点分路径命中/缺失/中间层非 dict 的边界——**中**：全项目配置读取都走它
  - 配置目录失去写权限时的真实文件系统错误路径——**低**：序列化失败已覆盖原文件保护
  - `data_dir` 的相对路径拼接与自动建目录——**中**：`database.py` 启动依赖
  - `reload()` 重读行为、单例唯一性（`Config() is Config()`）——**低**
  - 测试环境下 `PAPERMIND_*` 环境变量受导入期加载时机影响，现有测试无任何隔离措施——**中**：一旦有人加相关测试容易互相污染

## 8. 关键设计决策

- **模块级单例 + 导入期加载**：`config = Config()` 在 import 时即完成文件定位与读取，保证任何模块 `from app.core.config import config` 后立即可用；代价是测试想改环境必须抢在首次 import 之前。
- **真实密钥不进入安装包**：生产首次启动只复制公开模板，用户通过设置界面或应用数据目录配置 Key；升级不得覆盖已有配置。
- **相对路径一律锚定 `parents[3]`**：满足宪法第 3 条「可移植」，禁止硬编码绝对路径；`data_dir` 同理。
- **原子私有保存**：先写同目录临时文件再替换，避免失败时截断配置；显式 `0600` 保护明文密钥。`yaml.dump` 仍不保留注释，属已知取舍。
- **`get()` 静默返回 default**：配置缺失不视为错误，由各消费方自行决定默认值与是否告警（启动期告警统一由 `core/settings.py` 负责），符合分层配置职责划分。
