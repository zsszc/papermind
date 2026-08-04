# core/config.py（YAML 配置单例 Config）规格说明书

> 本文件描述 `backend/app/core/config.py` 的**行为契约**（做什么），不描述实现细节。
> 依据源码实际内容反向工程整理（2026-08-04）。

## 1. 背景与目标

PaperMind 是本地优先的单用户应用，全部运行时配置（LLM Key、模型、检索参数等）集中存放在项目根 `config.yaml`。本模块提供全局唯一的配置访问入口 `config` 单例：负责在进程启动时定位并加载 YAML 配置文件（含 Electron 生产包的数据目录重定向与占位符 Key 检测），并向全项目提供点分路径读取、写回持久化、数据目录解析三类能力。它的存在让其余模块无需关心配置文件在哪里、是否存在。

## 2. 范围

### 2.1 包含

- `Config` 单例的创建与首次加载时机
- 配置文件定位逻辑：`PAPERMIND_DATA_DIR` 分支、占位符检测、bundled/example 复制、example 回退
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
    2. 目标为 `<数据目录>/config.yaml`；若项目根 `config.yaml`（bundled，打包进去的真实配置）存在，且目标文件不存在或是占位符配置，则将 bundled 复制覆盖到目标；
    3. 否则若目标不存在且项目根 `config.yaml.example` 存在，则复制 example 到目标；
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

- **行为**：若 `_config_path` 为 `None` 则静默返回；否则将 `_config` 全量写回该路径（UTF-8，`yaml.dump`，`allow_unicode=True`、`sort_keys=False`、`default_flow_style=False`）
- **副作用**：覆盖写配置文件（原注释丢失，因为只保留数据不保留 YAML 注释）
- **异常**：路径不可写 → `OSError` 向上抛出
- **下游调用方**：`routers/settings.py` 的 `PUT /api/settings`

### 3.7 `Config.data_dir`（property）`-> Path`

- **行为**：
  - 设了 `PAPERMIND_DATA_DIR`：返回该路径（先 `mkdir -p`）；
  - 否则读 `app.data_dir` 配置（默认 `"./data"`），相对路径拼到项目根下；返回前 `mkdir -p`
- **输出**：已确保存在的目录 `Path`
- **副作用**：每次访问都可能创建目录
- **下游调用方**：`database.py`（拼 `papers.db` 的 `DATABASE_URL`）
- **异常**：无权限创建目录 → `OSError`

### 3.8 模块级 `config = Config()`

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
| 数据目录已有真实 Key 的 config.yaml | 不被 bundled 覆盖（`_is_placeholder_config` 为 False 时不复制） |
| 数据目录 config.yaml 是占位符但 bundled 有真实 Key | bundled 覆盖复制过去 |
| bundled 不存在且目标不存在 | 复制 example 兜底 |
| `save()` 时从未成功 `_load`（`_config_path=None`） | 静默返回，不写文件 |
| `save()` 写回 | YAML 注释全部丢失、键顺序按原 dict 顺序（`sort_keys=False`） |
| 并发调用 `Config()` | 无锁，理论上竞争创建实例；实际项目单进程单线程启动期导入，风险可忽略 |

## 5. 依赖

- **上游依赖**：`os`（`PAPERMIND_DATA_DIR`）、`yaml`（PyYAML）、`pathlib`；项目根 `config.yaml` / `config.yaml.example` 文件
- **下游消费者**：`core/logger.py`（导入但未实际使用 config 值）、`database.py`（`config.data_dir`）、`main.py`、`routers/settings.py`（get/save）、`routers/export.py`、`services/llm.py`、`services/embedding.py`、`services/retrieval.py`、`services/backup.py`、`services/web_search.py`、`services/image_analyzer.py` 等几乎全部业务模块

## 6. 验收标准（可测试）

- [ ] AC1：开发模式（无 `PAPERMIND_DATA_DIR`）且项目根有 `config.yaml` 时，`config.config_path` 指向它
- [ ] AC2：项目根 `config.yaml` 缺失时回退到 `config.yaml.example`，`get("llm.model")` 返回 example 中的值
- [ ] AC3：设置 `PAPERMIND_DATA_DIR` 为临时目录、目标 config 缺失且 bundled 含真实 Key 时，加载后临时目录下出现 bundled 的拷贝
- [ ] AC4：目标 config 为占位符（含 `sk-xxxx`）时 bundled 覆盖；目标已含真实 Key 时不被覆盖
- [ ] AC5：`get("a.b.c")` 在嵌套 dict 中逐层命中；任一层缺失返回传入的 default 且不抛异常
- [ ] AC6：`save()` 后重新 `reload()`，修改过的键值持久存在
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
  - `save()` 的持久化往返（save→reload→值仍在）与注释丢失副作用——**中**：`PUT /api/settings` 依赖
  - `data_dir` 的相对路径拼接与自动建目录——**中**：`database.py` 启动依赖
  - `reload()` 重读行为、单例唯一性（`Config() is Config()`）——**低**
  - 测试环境下 `PAPERMIND_*` 环境变量受导入期加载时机影响，现有测试无任何隔离措施——**中**：一旦有人加相关测试容易互相污染

## 8. 关键设计决策

- **模块级单例 + 导入期加载**：`config = Config()` 在 import 时即完成文件定位与读取，保证任何模块 `from app.core.config import config` 后立即可用；代价是测试想改环境必须抢在首次 import 之前。
- **占位符保守判定**：`_is_placeholder_config` 对任何读取/解析异常一律返回 True（视为占位符），宁可被 bundled 覆盖也不让坏配置留在数据目录——这是为 Electron 首启场景设计的自愈逻辑。
- **相对路径一律锚定 `parents[3]`**：满足宪法第 3 条「可移植」，禁止硬编码绝对路径；`data_dir` 同理。
- **`save()` 不保留注释**：用 `yaml.dump` 全量重写是简单可靠的持久化方案；`config.yaml` 中的注释在首次 `PUT /api/settings` 后会丢失，属已知取舍。
- **`get()` 静默返回 default**：配置缺失不视为错误，由各消费方自行决定默认值与是否告警（启动期告警统一由 `core/settings.py` 负责），符合分层配置职责划分。
