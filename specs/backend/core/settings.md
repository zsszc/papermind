# core/settings.py（分层配置与启动校验）规格说明书

> 本文件描述 `backend/app/core/settings.py` 的**行为契约**（做什么），不描述实现细节。
> 依据源码实际内容反向工程整理（2026-08-04）。

## 1. 背景与目标

`core/config.py` 只负责读写 `config.yaml`，不理解「环境变量应优先于文件」这一分层语义，也不知道配置值合不合法。本模块补齐这两件事：一是把 `PAPERMIND_*` 环境变量在启动时覆盖进 `Config` 内存配置（优先级：环境变量 > config.yaml > 各消费方代码里的默认值）；二是在 lifespan 启动阶段对关键配置（LLM Key/模型/base_url）做体检，把问题以告警日志和返回值形式暴露，而不阻断启动。它让 Electron 包、Docker、CI 等环境可以不改 YAML 文件就注入真实配置。

## 2. 范围

### 2.1 包含

- `EnvOverrides`（pydantic-settings `BaseSettings`）对 `PAPERMIND_LLM_API_KEY` / `PAPERMIND_LLM_MODEL` / `PAPERMIND_LLM_BASE_URL` 的解析
- `EnvOverrides.apply_to(config)` 把非空值写入 `Config._config` 并返回被覆盖键列表
- 模块级 `_dotted_set()` 点分路径写入
- `validate_startup_config(config)` 启动校验与告警
- `apply_env_overrides(config)` 环境变量覆盖入口（含日志）
- 占位符判定常量 `_PLACEHOLDER_MARKERS`

### 2.2 非目标

- `config.yaml` 文件的定位、加载与回退（归 `core/config.py`）
- 配置项的全量 schema 校验（本模块只校验 llm 三项，其余配置项无校验）
- 校验失败时阻断启动（本模块只告警，永不抛异常）
- 把环境变量覆盖结果持久化回 `config.yaml`（只改内存，不落盘）
- 除 `llm.*` 外其他配置（embedding/retrieval/memory 等）的环境变量覆盖

## 3. 行为契约

### 3.1 `class EnvOverrides(BaseSettings)`

- **配置**：`model_config = SettingsConfigDict(env_prefix="PAPERMIND_", extra="ignore")`
- **字段**：`llm_api_key: str = ""`、`llm_model: str = ""`、`llm_base_url: str = ""`
- **输入**：进程环境变量 `PAPERMIND_LLM_API_KEY` / `PAPERMIND_LLM_MODEL` / `PAPERMIND_LLM_BASE_URL`；未设置时字段为空字符串
- **后置条件**：实例化即完成环境变量解析；空字符串语义为「不覆盖」
- **副作用**：无（pydantic-settings 只读环境）
- **异常**：环境变量值类型无法解析为 str 时理论上抛 pydantic 校验错；实际 str 字段几乎不会失败

### 3.2 `EnvOverrides.apply_to(self, config) -> list[str]`

- **输入**：`config` 为 `Config` 实例（鸭子类型，只需有 `_config: dict` 属性）
- **输出**：被实际覆盖的点分键列表，元素取自 `("llm.api_key", "llm.model", "llm.base_url")` 中值非空的项，顺序固定
- **前置条件**：`config._config` 已加载（dict）
- **后置条件**：非空环境变量值已通过 `_dotted_set` 写入 `config._config` 对应嵌套位置；值为空字符串的键不写入、不出现在返回列表
- **副作用**：修改传入 config 对象的内存配置（**不写回 config.yaml**）
- **异常**：`config._config` 非 dict 时行为未定义（`_dotted_set` 按 dict 操作）

### 3.3 `_dotted_set(cfg: dict, key: str, value: Any) -> None`

- **输入**：`cfg` 目标 dict；`key` 点分路径（如 `"llm.api_key"`）；`value` 任意值
- **输出**：无返回
- **后置条件**：`cfg` 中路径各中间层不存在时自动创建为 dict；末级键被赋为 `value`；路径上已有非 dict 值会被 `setdefault` 保留原值后索引赋值失败（见边界表）
- **副作用**：原地修改 `cfg`
- **异常**：中间层已存在且为非 dict（如 str）→ `TypeError`

### 3.4 `validate_startup_config(config) -> list[str]`

- **输入**：`config` 为 `Config` 实例（鸭子类型，需有 `get(key, default)` 方法）
- **输出**：告警字符串列表，可能为空；每条告警同时经 `logger.warning("[config] ...")` 输出
- **前置条件**：config 已完成加载（且通常已执行过 `apply_env_overrides`，校验的是覆盖后的最终值）
- **后置条件**：不修改 config；不抛异常
- **副作用**：每条告警写一条 WARNING 日志（前缀 `[config]`）
- **校验规则**（按顺序追加）：
  1. `llm.api_key` 取空或缺失 → `"llm.api_key 为空，LLM 功能不可用"`
  2. 非空但含 `_PLACEHOLDER_MARKERS = ("sk-xxxx", "your-", "xxxx")` 任一子串 → `"llm.api_key 仍是占位符，请在 config.yaml 中填入真实 Key"`
  3. `llm.model` 取空或缺失 → `"llm.model 未配置"`
  4. `llm.base_url` 非空且不以 `http://` 或 `https://` 开头 → `"llm.base_url 格式异常: {base_url}"`（base_url 为空不告警）
- **异常**：设计上不抛异常；但 `config.get` 返回非 str 时可比较行为依赖 `str(...)` 包装，已防御

### 3.5 `apply_env_overrides(config) -> list[str]`

- **输入**：`config` 为 `Config` 实例
- **输出**：被覆盖的键列表（同 `EnvOverrides.apply_to`）
- **后置条件**：实例化一次 `EnvOverrides`（读当前环境）并应用；每个被覆盖键写一条 INFO 日志 `"[config] 环境变量覆盖: {key}"`
- **副作用**：修改 config 内存配置；写 INFO 日志
- **异常**：同 3.2
- **调用时机**：`main.py` lifespan 第一步（在建表、LLM 健康检查之前）

## 4. 边界条件与错误处理

| 场景 | 期望行为 |
|------|----------|
| 三个 `PAPERMIND_*` 环境变量都未设置 | `apply_env_overrides` 返回 `[]`，无日志，config 不变 |
| 环境变量为空字符串 `PAPERMIND_LLM_API_KEY=""` | 视为未设置，不覆盖（空字符串不覆盖是显式设计） |
| 只设了 `PAPERMIND_LLM_MODEL` | 仅 `llm.model` 被覆盖，返回 `["llm.model"]` |
| config 中原本没有 `llm` 键 | `_dotted_set` 自动创建 `{"llm": {...}}` |
| config 中 `llm` 已是非 dict（异常情况） | `_dotted_set` 抛 `TypeError`（无防御） |
| api_key 为纯空白 `"  "` | `strip()` 后按空处理 → 告警「为空」 |
| api_key 含子串 `xxxx`（如真实 Key 碰巧含） | 误报占位符告警（子串匹配的已知粗糙点） |
| base_url 为 `""` | 不告警（允许走 llm.py 内部默认） |
| base_url 为 `api.moonshot.cn/v1`（缺协议头） | 告警「格式异常」，但不阻断启动 |
| 校验全部通过 | 返回 `[]`，零日志 |
| 环境变量覆盖后 | 不写回 `config.yaml`，进程重启后失效（除非再次设置环境变量） |

## 5. 依赖

- **上游依赖**：`pydantic-settings`（锁 2.5.2，见宪法第 16 条）；`app.core.logger.logger`；`os.environ`（经 pydantic-settings）
- **下游消费者**：仅 `app/main.py` lifespan（`apply_env_overrides(config)` → `validate_startup_config(config)`）；无其他模块调用
- **注意**：`backend/tests/conftest.py` 的 TestClient 不触发 lifespan，因此测试环境中这两个函数**从不执行**

## 6. 验收标准（可测试）

- [ ] AC1：无任何 `PAPERMIND_*` 环境变量时，`apply_env_overrides(config)` 返回 `[]` 且 config 不变
- [ ] AC2：设置 `PAPERMIND_LLM_API_KEY=sk-real` 后，`config.get("llm.api_key") == "sk-real"`，返回列表含 `"llm.api_key"`
- [ ] AC3：环境变量为空字符串时不覆盖原有 config.yaml 中的值
- [ ] AC4：config 无 `llm` 键时覆盖后自动创建嵌套结构
- [ ] AC5：api_key 缺失 → 返回的告警列表包含「为空」文案；含 `sk-xxxx` → 包含「占位符」文案；真实 Key → 无 api_key 相关告警
- [ ] AC6：`llm.model` 缺失时告警含「未配置」
- [ ] AC7：base_url 不以 http(s):// 开头时告警含「格式异常」；为空时不告警
- [ ] AC8：`validate_startup_config` 无论配置多坏都不抛异常，且每条告警产生一条 `[config]` 前缀 WARNING 日志

## 7. 现有测试覆盖与盲区

- **已覆盖**：
  - 无直接测试。`tests/test_settings.py::test_get_settings_masks_api_key` 测的是 `routers/settings.py` 的脱敏逻辑，与本模块无关
  - `tests/conftest.py` 不触发 lifespan，本模块两个入口函数在测试进程中从未运行
- **盲区**：
  - `apply_env_overrides` / `EnvOverrides.apply_to` 全部行为（覆盖、空串不覆盖、返回列表、自动建嵌套 dict）——**高**：环境变量注入是 Electron/Docker 部署的唯一配置通道，回归后线上无任何告警
  - `validate_startup_config` 四类告警规则（空 Key、占位符、model 缺失、base_url 协议头）及「只告警不阻断」契约——**高**：占位符 Key 流入生产的最后一道提示
  - 覆盖不落盘（只改内存不写 config.yaml）这一关键语义——**中**：容易被误改成持久化，需测试钉死
  - `_dotted_set` 中间层已存在非 dict 时抛 `TypeError` 的无防御行为——**低**：异常配置下的行为未定义，至少应文档化
  - 环境变量测试需要 monkeypatch `os.environ` 且注意 pydantic-settings 实例化时机，目前无任何夹具支持——**中**

## 8. 关键设计决策

- **只覆盖 llm 三项**：MVP 阶段只有 LLM 配置有跨环境注入需求（Electron 包 / Docker / CI 的 Key 各不相同）；embedding、retrieval 等保持 YAML 单一来源，避免覆盖面失控。
- **空字符串 = 不覆盖**：pydantic-settings 会把显式设置的空串也读进来，必须显式跳过，否则 `PAPERMIND_LLM_API_KEY=""` 会把真实 Key 清掉。
- **只改内存不落盘**：环境变量属于部署环境，写回 `config.yaml` 会把部署态泄漏进用户数据文件，且与 `routers/settings.py` 的 `config.save()` 叠加会产生意外持久化。
- **校验只告警不阻断**：本地优先单用户应用，LLM 不可用不应拖垮整个服务（文献管理、检索等功能仍可用）；告警文案全部中文并带 `[config]` 前缀，符合宪法第 7 条。
- **占位符检测与 `config.py` 各自实现一份**：`config.py` 的判定服务于「是否复制 bundled 配置」（文件级），本模块服务于「启动告警」（值级），两处规则近似但语义不同，未合并。
- **执行顺序固定**：lifespan 中先 `apply_env_overrides` 再 `validate_startup_config`，保证校验的是最终生效值。
