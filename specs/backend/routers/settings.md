# routers/settings.py（运行时设置读取 / 更新）规格说明书

> 本文件描述 `backend/app/routers/settings.py` 的**行为契约**（做什么），不描述实现细节。
> 依据源码实际内容反向工程整理（2026-08-04）。端点签名照抄代码。

## 1. 背景与目标

用户需要在前端「设置」弹窗里查看并修改 LLM 接入信息（API Key、模型、base_url），而不必手工编辑 `config.yaml`。本路由提供两个端点：读取当前设置（API Key **必须脱敏**返回，宪法第 14 条密钥纪律）与保存修改（持久化回 `config.yaml` 并立即生效于内存单例）。Embedding 模型只读展示，不可经 API 修改。

## 2. 范围

### 2.1 包含

- `GET /api/settings`：读取当前设置，API Key 脱敏
- `PUT /api/settings`：更新 `llm.api_key` / `llm.model` / `llm.base_url` 并写回 YAML
- `_mask_key` 脱敏规则
- 脱敏值回传保护（含 `*` 的 key 不落盘）

### 2.2 非目标

- 配置文件的加载策略、占位符检测、`PAPERMIND_DATA_DIR` 重定向（归 `core/config.py` 规格）
- `PAPERMIND_*` 环境变量覆盖与启动校验（归 `core/settings.py`，lifespan 执行）
- `embedding.local_model`、`retrieval.*`、`export.*` 等其余配置项的 API 修改（不支持，只能手编 YAML）
- 多配置 profile、配置版本历史、修改审计

## 3. 行为契约

路由注册：`app.include_router(settings.router, prefix="/api/settings", tags=["settings"])`（`main.py`）。两端点路径均为空串，即 `GET /api/settings` 与 `PUT /api/settings`。

### 3.0 数据模型

- `SettingsResponse(BaseModel)`：`llm_api_key: str` / `llm_model: str` / `llm_base_url: str` / `embedding_model: str`，四字段必填。
- `SettingsUpdate(BaseModel)`：`llm_api_key: str`（**必填**，无默认值）/ `llm_model: str | None = None` / `llm_base_url: str | None = None`。
- `_mask_key(key: str) -> str` 脱敏规则：
  - 空 / None → `""`；
  - `strip()` 后长度 ≤ 8 → 全长度的 `*`（连头尾都不露）；
  - 长度 > 8 → 前 4 字符 + `(len-8)` 个 `*` + 后 4 字符。

### 3.1 `GET ""` → `get_settings()`（response_model=SettingsResponse）

- **输入**：无参数
- **输出**：`SettingsResponse`：
  - `llm_api_key` = `_mask_key(config.get("llm.api_key", ""))`——**绝不返回原始 Key**；
  - `llm_model` = `config.get("llm.model", "moonshot-v1-8k")`；
  - `llm_base_url` = `config.get("llm.base_url", "https://api.moonshot.cn/v1")`；
  - `embedding_model` = `config.get("embedding.local_model", "BAAI/bge-m3")`。
- **行为**：实时读内存单例 `config`——PUT 之后的再次 GET 反映新值（按新 key 重新脱敏）
- **副作用**：无（只读，不写盘）
- **异常**：无显式处理

### 3.2 `PUT ""` → `update_settings(payload: SettingsUpdate)`

- **输入**：JSON body（本路由唯一用 body 的端点）：
  - `llm_api_key` 必填但**允许空串**；含 `*` 的值（即前端把 GET 到的脱敏值原样回传）→ **忽略不写入**，防止把 `****` 覆盖成真 Key；
  - `llm_model` / `llm_base_url` 可选；空串 / None → 忽略；
  - 三项写入前均 `strip()`。
- **输出**：`{"ok": true}`
- **行为**：直接改内存单例 `config._config` 的 `llm` 节（`llm` 键不存在时先建空 dict），然后 `config.save()` 把**整个配置 dict** dump 回 `config.config_path`（`yaml.dump`，`allow_unicode=True`，`sort_keys=False`）——**全量重写配置文件**，YAML 注释与键序不保留；内存与磁盘同步生效，**无需重启**，后续 LLM 调用立即用新值
- **副作用**：
  - 磁盘写入：覆盖式重写 `config.yaml`（明文含 API Key——该文件已 gitignore，宪法第 14 条）；
  - **边界**：若用户从未创建 `config.yaml`（`Config` 回退加载了 `config.yaml.example`），`save()` 会**覆盖写 `config.yaml.example`**（config_path 指向谁就写谁）；
  - 日志：`logger.info("[settings] 配置已更新")`。
- **异常**：任何异常（磁盘只读、YAML dump 失败等）→ `logger.error` + `HTTPException(500, detail=f"保存配置失败: {e}")`——**注意 detail 含异常原文**，HTTPException 不经全局脱敏处理，异常文本会返给客户端（与宪法第 13 条异常脱敏存在张力，现状记录）；此时内存中的 `_config` **可能已被部分修改但未落盘**，内存与磁盘短暂不一致（重启后按磁盘恢复）。
- **校验缺失**：`llm_api_key` 无法通过本端点**删除**（空串被忽略）；`llm_model` / `llm_base_url` 同理只能改不能清；对 key 格式（`sk-` 前缀等）无任何校验。

## 4. 边界条件与错误处理

| 场景 | 期望行为 |
|------|----------|
| `config.yaml` 无 `llm.api_key` | GET 返回 `llm_api_key: ""` |
| Key 长度 ≤ 8（如短测试 key） | 全 `*` 返回，头尾均不露 |
| Key 长度 > 8 | 仅露前 4 + 后 4，中段 `*` 数 = len-8 |
| PUT body 缺 `llm_api_key` | 422（Pydantic 必填校验） |
| PUT `llm_api_key` 为脱敏值（含 `*`） | 静默忽略，真 Key 不被覆盖；其余字段照常保存，返回 `{"ok": true}` |
| PUT `llm_api_key: ""` | 忽略（无法经 API 清空 Key） |
| PUT `llm_model: ""` / `llm_base_url: ""` | 忽略，保留旧值 |
| PUT 值首尾带空白 | `strip()` 后入库与落盘 |
| 磁盘写失败（只读、权限） | 500，detail 含异常原文；内存已改、磁盘未改，不一致至重启 |
| `config.yaml` 不存在（用了 example 回退） | PUT 成功后 `config.yaml.example` 被覆盖写入真实配置 |
| 并发 PUT | 无锁；后到的 save 全量覆盖先到的（单用户场景视为可接受） |

## 5. 依赖

- **上游依赖**：`app.core.config.config`（单例，含 `_config` 直改与 `save()`）；`app.core.logger`；Pydantic
- **下游消费者**：前端 `api.js` 的 `getSettings` / `updateSettings`（`SettingsModal.jsx`）；间接影响 `services/llm.py` 后续调用的 Key/模型/base_url（同读 `config` 单例）

## 6. 验收标准（可测试）

- [ ] AC1：GET 返回四字段；`llm_api_key` 为空串或含 `*`，绝不等于原始 Key；>8 长度时仅露前 4 后 4
- [ ] AC2：PUT 传新 key（不含 `*`）+ 新 model/base_url → 200 `{"ok": true}`，再次 GET 时 model/base_url 为新值、key 按新值脱敏
- [ ] AC3：PUT 把 GET 到的脱敏 key 原样回传 → 配置中的真实 Key 不被 `*` 覆盖
- [ ] AC4：PUT 缺 `llm_api_key` → 422
- [ ] AC5：PUT 成功后 `config.config_path` 文件内容与内存一致（`llm` 节三项为新值）

## 7. 现有测试覆盖与盲区

- **已覆盖**（`backend/tests/test_settings.py`，1 例）：
  - `test_get_settings_masks_api_key`：GET 200、masked 为空或含 `*`、masked ≠ 真实 Key → 覆盖 AC1 的核心断言
- **盲区**：
  - PUT 全链路（AC2、AC3、AC5）零测试：脱敏值回传保护、「含 `*` 不覆盖真 Key」这一关键安全行为无固化（**高**，一旦被重构破坏，前端保存设置即把真 Key 洗成 `****` 且无任何告警）
  - `_mask_key` 的边界（空、≤8 全星、>8 露前 4 后 4）无参数化测试（**中**）
  - PUT 422（缺必填 key）、空串忽略语义、strip 行为无测试（**中**）
  - 保存失败路径（500 + detail 含异常原文、内存/磁盘不一致）无测试（**中**；detail 泄露异常原文与宪法第 13 条的张力亦无断言记录现状）
  - 「`config.yaml` 缺失时覆盖写 `config.yaml.example`」的边界无测试（**低**）
  - 全量重写 YAML 丢注释/键序的行为无测试（**低**）

## 8. 关键设计决策

- **GET 必脱敏、PUT 拒收脱敏值**：配套闭环——前端表单可以安全地把 GET 到的 masked key 回填进输入框原样提交，真 Key 不会被 `*` 覆盖；这是本模块最重要的安全契约（宪法第 14 条）
- **直改单例 + 全量 `yaml.dump` 回写**：实现最简、改后无需重启即生效；代价是配置文件注释与键序全部丢失，且 `config.yaml` 缺失时会污染 `config.yaml.example`——引入保留注释的 YAML 库（如 ruamel）前，以现状为准
- **只开放 `llm` 三项**：embedding 模型等其余配置涉及本地模型加载等重资产，刻意不开放 API 修改；`SettingsUpdate` 加字段即可扩展，但须同步补 PUT 测试（当前零覆盖）
- **异常 detail 直给原文**：`HTTPException` 绕过全局脱敏属已知现状（宪法第 13 条与「可操作错误信息」的取舍）；收紧时应先把 detail 改通用文案并补 AC 测试
