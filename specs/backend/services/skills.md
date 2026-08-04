# Skill 注册与提示词组装服务（skills）规格说明书

> 本规格由 `backend/app/services/skills.py`（167 行）反向工程而来，描述 Skill 系统的行为契约。

## 1. 背景与目标

Skill 系统采用轻量级 Prompt 路由：前端通过 `skill` 字段触发，后端在 system prompt 中注入对应的角色设定与输出要求，使同一对话接口可切换「学术翻译 / 论文校对 / 方法对比 / 大纲生成 / 数据分析 / 写作助手」等专家模式。

本模块在保持模块级公开函数（`build_skill_prompt` / `list_skills`）原签名与返回结构不变的前提下，实现了可注册的 `SkillRegistry`（Skill-as-Tool 第一步）：Skill 以 dataclass 描述，注册表支持动态注册/查询，`tools` 字段为后续 LangGraph 工具化预留扩展点。调用方（`chat.py` / `agent_graph.py`）无需感知注册表存在。

## 2. 范围

### 2.1 包含

- `Skill` dataclass 定义（含预留的 `tools` 字段）
- `SkillRegistry` 的注册/查询/列表/prompt 构建契约（线程安全）
- 6 个内置默认 Skill 的注册与内容
- 全局单例 `get_skill_registry()`（带锁懒加载）
- 模块级公开函数 `build_skill_prompt()` / `list_skills()` 的输入输出契约

### 2.2 非目标

- 不做 Skill 的持久化（DB 中 `skills` 表存在但当前未使用，Skill 全走内存注册表）
- 不从文件加载 Skill（项目根 `skills/` 目录为空，属预留；YAML 注册表未落地）
- 不实现工具调用（`tools` 字段当前始终为空，LangGraph 工具化未启用）
- 不调用 LLM（本模块只产出 prompt 字符串）

## 3. 行为契约

### 3.1 `@dataclass class Skill`

| 字段 | 类型 | 语义 |
|------|------|------|
| `skill_id` | `str` | 唯一标识（注册表键），如 `"translator"` |
| `display_name` | `str` | 前端展示名，如 `"学术翻译"` |
| `description` | `str` | 前端展示的描述文本 |
| `role` | `str` | 角色设定（prompt 第一段） |
| `instruction` | `str` | 输出要求（prompt 第二段） |
| `tools` | `List[str]` | **预留**，默认 `field(default_factory=list)`，当前始终为空 |

- **后置条件**：`tools` 不传时每个实例持有独立空列表（`default_factory` 语义，不共享）。

### 3.2 `class SkillRegistry`

线程安全的注册表（内部 `threading.Lock` 保护 `_skills: Dict[str, Skill]`）。

#### `def register(self, skill: Skill) -> None`

- **输入**：`skill` Skill 实例
- **后置条件**：以 `skill.skill_id` 为键写入注册表；**同 ID 重复注册为覆盖语义（后写覆盖先写），不报错**
- **副作用**：修改注册表内部状态（加锁）

#### `def get(self, skill_id: str) -> Optional[Skill]`

- **输入**：`skill_id`
- **输出**：命中的 Skill 实例；**不存在返回 `None`（不抛异常）**

#### `def list(self) -> List[Skill]`

- **输出**：全部已注册 Skill 的列表，**按注册顺序**（dict 插入序）

#### `def build_prompt(self, skill_id: Optional[str], user_message: str) -> Optional[str]`

- **输入**：`skill_id` 可为 `None`/空串；`user_message` 用户输入原文
- **输出**：`skill_id` 为空或未注册时返回 `None`（表示无需注入）；命中时返回如下格式字符串（注意尾部带换行）：

  ```
  {skill.role}
  
  {skill.instruction}
  
  当前用户输入：
  {user_message}
  ```

- **副作用**：无（只读 + 字符串拼装）
- **异常**：无显式处理

### 3.3 `def _default_skills() -> List[Skill]`（内部）

- 返回 6 个内置 Skill，内容与重构前硬编码版本保持一致：

| skill_id | display_name | role 要点 |
|----------|--------------|-----------|
| `translator` | 学术翻译 | 中英文学术论文互译专家 |
| `proofreader` | 论文校对 | 严谨的学术论文校对专家 |
| `method_comparator` | 方法对比 | 计算机视觉/医学图像分析领域专家 |
| `outline_generator` | 大纲生成 | 学术论文写作导师 |
| `data_analyst` | 数据分析 | 医学图像/机器学习实验数据分析专家 |
| `writing_assistant` | 写作助手 | 学术写作助手（润色与扩展） |

- 每个 Skill 的 `instruction` 为编号条目式输出要求，`tools` 均为空。
- 新增默认 Skill 的约定：在此函数中追加一项 `Skill(...)`，前端经 `/api/chat/skills` 自动列出（AGENTS.md 第 11 节）。

### 3.4 `def get_skill_registry() -> SkillRegistry`

- **输出**：全局单例 `SkillRegistry`
- **后置条件**：双检锁（`_skill_registry_lock`）懒加载；首次调用时创建注册表并按序注册 `_default_skills()` 的全部 6 个 Skill；多次调用返回同一对象，线程安全
- **副作用**：首次调用时写入模块级 `_skill_registry_instance`

### 3.5 `def build_skill_prompt(skill: Optional[str], user_message: str) -> Optional[str]`

- **输入**：`skill` Skill ID（可空）；`user_message` 用户输入
- **输出**：委托 `get_skill_registry().build_prompt(skill, user_message)`；`None` / 空串 / 未注册 ID 均返回 `None`
- **后置条件**：返回 prompt 必含对应 Skill 的 `role`、`instruction` 与 `user_message` 原文
- **副作用**：首次调用触发全局单例初始化
- **消费者**：`agent_graph.build_messages` 节点（注入为最后一条 system 消息）

### 3.6 `def list_skills() -> list`

- **输入**：无
- **输出**：dict 列表，每项**恰好** 3 个键：`skill_id` / `display_name` / `description`（**不含 `role` / `instruction` / `tools`**——prompt 内容不暴露给列表接口）
- **后置条件**：顺序与注册顺序一致；默认情况下恰为 6 项
- **消费者**：`routers/chat.py` 的 `GET /api/chat/skills`（原样返回给前端）

## 4. 边界条件与错误处理

| 场景 | 期望行为 |
|------|----------|
| `skill=None` | `build_skill_prompt` 返回 `None`，不注入 |
| `skill=""`（空串） | 同上，返回 `None`（falsy 短路） |
| 未注册的 skill_id | 返回 `None`，不抛异常 |
| 同 ID 重复 register | 后者覆盖前者，不报错 |
| 并发 register/get/list | 内部锁保证线程安全 |
| `user_message` 含任意字符（含换行/注入样文本） | 原样拼入 prompt（纯字符串拼装，无转义——prompt 注入防护非本模块职责） |
| 空注册表（独立实例未注册任何 Skill） | `list()` 返回 `[]`，`build_prompt` 恒返回 `None` |

## 5. 依赖

- **上游依赖**：仅标准库（`threading` / `dataclasses` / `typing`），无任何项目内依赖——是依赖图叶节点
- **下游消费者**：
  - `app.services.agent_graph.build_messages`（`build_skill_prompt`，Skill 注入为消息列表最后一条）
  - `app.routers.chat`（`GET /api/chat/skills` → `list_skills()`）

## 6. 验收标准（可测试）

- [ ] AC1：`list_skills()` 恰返回 6 个默认 Skill，ID 集合为 {translator, proofreader, method_comparator, outline_generator, data_analyst, writing_assistant}
- [ ] AC2：每个列表项恰含 `skill_id` / `display_name` / `description` 三键且均为非空字符串；display_name 与约定中文名一致
- [ ] AC3：`build_skill_prompt("translator", msg)` 返回非空，且含该 Skill 的 role、instruction 与 msg 原文
- [ ] AC4：`build_skill_prompt` 对未注册 ID / `None` / 空串均返回 `None`
- [ ] AC5：独立 `SkillRegistry` 实例可 register 新 Skill 并 get 取回同一对象；`tools` 默认 `[]`；`build_prompt` 含 role/instruction/user_message
- [ ] AC6：`get` / `build_prompt` 对缺失 ID 返回 `None`
- [ ] AC7：`get_skill_registry()` 多次调用返回同一实例（单例）

## 7. 现有测试覆盖与盲区

- **已覆盖**：`backend/tests/test_skills.py`（10 用例）
  - `TestDefaultSkills`：6 个默认 Skill 齐全、display_name 匹配、列表项三键完整且非空
  - `TestBuildSkillPrompt`：已知 Skill 返回含 role/instruction/用户输入；未知 ID、`None`、空串均返回 `None`
  - `TestSkillRegistry`：独立实例 register→get、register→build_prompt、缺失 ID 返回 None、`tools` 默认空列表、全局单例共享
  - 纯内存测试，不调任何 LLM
- **盲区**：
  - 同 ID 重复注册的覆盖语义未测 —— **中**
  - `build_prompt` 返回字符串的精确格式（`role\n\ninstruction\n\n当前用户输入：\n{msg}\n`，含尾部换行）仅做包含断言，未做全等匹配 —— 低
  - 注册表的线程安全性（并发 register/get）未测 —— 低
  - `list()` 的注册顺序保证未断言 —— 低
  - 向全局单例 register 新 Skill 后 `list_skills()` / `build_skill_prompt()` 的集成路径未测（注册测试只用独立实例，避免污染单例） —— 低
  - 默认 6 个 Skill 的 `tools` 恒为空这一预留契约未逐一断言（仅自定义 Skill 断言了默认值） —— 低

## 8. 关键设计决策

- **Prompt 路由而非工具调用**：Skill 现阶段只是 system prompt 注入，不进入 LangGraph 工具循环；`Skill.tools` 字段与 DB `skills` 表均为「Skill-as-Tool」演进的预留，启用前不得假定其已生效。
- **模块级函数签名冻结**：`build_skill_prompt(skill, user_message)` / `list_skills()` 保持重构前签名与返回结构，使 `chat.py` / `agent_graph.py` 零改动；变更签名属破坏性变更，须同步修改调用方与规格。
- **注册表覆盖语义**：`register` 同 ID 覆盖而非报错，为运行时动态替换内置 Skill（如用户自定义同名 Skill）留路；调用方若需防误覆盖应自行先 `get` 检查。
- **list_skills 不暴露 prompt 内容**：列表接口只给 `skill_id` / `display_name` / `description` 三键，`role` / `instruction` 属内部实现；前端展示与 prompt 构建解耦。
- **内容与硬编码版本逐字一致**：6 个默认 Skill 的 role/instruction 文本与重构前一致，保证模型行为零漂移；修改任一文案属行为变更，须先改规格并补测试。
- **内存注册表、不做持久化**：单用户本地应用，Skill 集合在代码中定义即可；DB `skills` 表与根目录 `skills/` 目录是历史规划残留，不要误以为 Skill 从这两处加载。
