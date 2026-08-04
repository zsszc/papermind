# services/auto_tag.py（自动打标 AutoTagService）规格说明书

> 本文件描述 `backend/app/services/auto_tag.py` 的**行为契约**（做什么），不描述实现细节。
> 触发链路的调用方语义（后台任务编排、锁、状态机）已在 `specs/backend/services/processor.md` 定义，本规格只描述打标模块自身的契约与其被触发时机的衔接点。
> 依据源码实际内容反向工程整理（2026-08-04）。

## 1. 背景与目标

论文导入后，用户希望不打断手头工作就能获得可筛选、可统计的领域标签。`AutoTagService` 采用「关键词规则 + LLM 抽取」双通道：规则通道对标题/摘要/期刊做离线子串匹配，保证零成本、永远可用；LLM 通道从 30 个内置候选标签池（可少量池外补充）中选出 3–5 个最相关标签，提升召回与贴合度。两路结果合并去重后写入 `tags` 表并关联到论文。

打标是「锦上添花」型任务：它被刻意设计为失败可降级的旁路——LLM 挂掉时退化为纯规则打标，整体失败时不影响论文的检索可用性（`processed` 状态机归 processor 规格管，打标不参与）。

## 2. 范围

### 2.1 包含

- 触发时机：打标在导入流水线中的位置、与元数据增强的先后/连锁关系（衔接语义，编排细节归 processor 规格）
- 规则通道 `_rule_based_tags` 的关键词匹配契约（内置 20 组规则、子串匹配语义）
- LLM 通道的消息构造、调用方式（`llm_service` 唯一入口）、输出解析与清洗契约
- 合并/去重/截断契约（LLM 优先、总数上限 5）
- `Tag` 行查重/新建/颜色分配与落库事务边界（flush 不 commit）
- 失败降级路径（LLM 异常 → 纯规则；整体异常 → 调用方兜底）
- 异步入口 `generate_tags` 与同步入口 `generate_tags_sync` 的双入口契约

### 2.2 非目标

- 后台任务编排、并发锁、`processed` 状态机（归 processor 规格）
- LLM 元数据增强 `_enhance_metadata_with_llm_sync` 的内部行为（归 pdf_parser / papers 路由规格；本规格只描述它对打标的连锁影响）
- `llm_service.chat_completion(_sync)` 的重试/截断/temperature 处理（归 llm 规格）
- 手动标签 CRUD 接口、`/api/papers/{id}/tags` 等路由行为（归 papers 路由规格）
- 标签池内容的产品合理性评审（本规格只记录现状，不评判选词）

## 3. 行为契约

### 3.1 触发时机（模块级契约）

- **唯一生产调用点**：`routers/papers.py::_enhance_paper_metadata(paper_id)` 第 204 行 `auto_tag_service.generate_tags_sync(paper, db, timeout=60)`。
- **触发链**：`POST /api/papers/import` → BackgroundTasks 跑 `_process_paper_background` → 核心处理标 `done` → 启动独立守护线程 `_enhance_paper_metadata` → **先** LLM 元数据增强并 commit → **后** 自动打标。
- **关键推论**：
  1. 打标读到的 `paper.title/abstract/journal` 是**增强后**的值（增强 commit 在前），规则与 LLM 两通道都基于增强后文本工作；
  2. **连锁跳过**：元数据增强抛异常时 `_enhance_paper_metadata` 直接 `return`，打标**不会执行**——LLM 故障不仅丢增强，也丢标签（见第 4 节）；
  3. 调用方拿到返回的 `List[Tag]` 后自行 `paper.tags.append(tag)` 并 `db.commit()`；本模块**不负责关联与提交**；
  4. 手动端点 `POST /api/papers/{paper_id}/process`（重处理）**不触发**打标；
  5. 打标线程无独立锁，串行性依赖上游「同一 paper 只有一个核心处理任务」的保证（见 processor 规格 §3.3）。

### 3.2 `class AutoTagService` / `__init__(self)`

- **输出**：`AutoTagService` 实例；模块级单例 `auto_tag_service` 在导入时构造。
- **后置条件**：持有三份模块级常量引用——`tag_pool`（30 个候选标签）、`keyword_rules`（20 组关键词规则）、`colors`（10 个十六进制颜色）。无 I/O、无网络、无线程，构造永远成功。
- **标签池与规则集的现状**：池 30 项，规则仅覆盖其中 20 项；`胃癌`/`肝癌`/`肺癌`/`乳腺癌`/`生存分析`/`迁移学习`/`联邦学习`/`数据增强`/`注意力机制` 等 10 项**只能经 LLM 通道产出**，规则通道永远不会命中它们。

### 3.3 `_rule_based_tags(self, paper: Paper) -> List[str]`

- **输入**：`paper`（`Paper` ORM 对象；读 `title`/`abstract`/`journal`，`None` 按空串处理）。
- **输出**：命中的标签名列表。**来源是 `set`，顺序不确定**（调用方不得依赖次序）。
- **匹配语义**：三字段以空格拼接、整体转小写后，对每个标签的关键词列表做**子串包含**判断（`kw in text`），任一关键词命中即取该标签；关键词字面量本身即小写。`MRI`/`CT` 规则写成 `" mri "`/`" ct "` 带两侧空格的形式，**词边界靠空格约定**，紧挨标点或行首/行尾的缩写会漏匹配（如 `"MRI."` 不命中 `" mri "`）。
- **副作用**：无（纯函数）。
- **异常**：无显式抛出路径；`paper` 缺属性时抛 `AttributeError`（不属于契约内场景）。

### 3.4 `_clean_tag_name(self, name: str) -> str`

- **输入**：任意字符串（LLM 输出的一行或合并阶段的标签名）；空/None 输入返回 `""`。
- **输出**：清洗后的标签名，可能为 `""`（调用方须丢弃空结果）。
- **清洗步骤（顺序固定）**：
  1. **乱码还原防御**：尝试 `name.encode("latin-1")`，若字节中含 `\xc3`/`\xe4`/`\xe5`/`\xe6` 则按 UTF-8 重新解码——针对「UTF-8 被误以 latin-1 解码」的双层错码；编码/解码异常静默忽略；
  2. `strip()`；
  3. 去除行首列表符号与编号前缀（正则 `^[-\d•*·]+[.\\s]*`，如 `1.`、`- `、`• `）；
  4. 去除首尾双/单引号；
  5. 剔除不可打印字符（保留空格与制表符）；
  6. 最终 `strip()`。
- **副作用**：无。

### 3.5 `_build_llm_messages(self, paper: Paper) -> Optional[List[Dict[str, str]]]`

- **输入**：`paper`（读 `title`/`authors`/`journal`/`abstract`）。
- **输出**：`[{system}, {user}]` 两条消息；**四个字段拼接后整体为空（strip 后为空串）时返回 `None`**——这是「是否发起 LLM 调用」的判空闸门，两个 LLM 通道都先过它。
- **消息契约**：system 固定为「你是专业的学术文献分类助手，只输出标签名列表。」；user 包含完整 30 项标签池、允许 1–2 个池外新标签但总数 ≤5、要求 2–6 字或 1–3 个英文单词、按相关性降序每行一个；论文信息部分**截断到 1500 字符**（`context[:1500]`）。
- **副作用**：无。

### 3.6 `_parse_llm_tags_result(self, result: str) -> List[str]`

- **输入**：LLM 原始输出字符串。
- **输出**：逐行过 `_clean_tag_name`、丢弃空行后的标签列表，**截断到前 5 个**（`tags[:5]`）。不做池内校验——LLM 返回的池外标签原样保留。
- **副作用**：无。

### 3.7 `async _llm_tags(self, paper: Paper) -> List[str]` / `_llm_tags_sync(self, paper: Paper, timeout: Optional[int] = None) -> List[str]`

- **行为**：`_build_llm_messages` 返回 `None` 时不发起调用直接返回 `[]`；否则经 `llm_service.chat_completion` / `chat_completion_sync(messages, timeout=timeout)`（宪法第 8 条唯一入口）调用，结果过 `_parse_llm_tags_result`。
- **失败降级（核心契约）**：LLM 调用与解析中的**一切异常**被捕获，记 `logger.warning("[AutoTag] LLM 生成标签失败[（同步）]", exc_info=True)`，**返回 `[]`**——绝不向外抛。降级后果由 `_collect_tags` 兜底为纯规则打标。
- **超时语义**：`timeout` 原样透传给 llm_service（其内部默认 120 秒）；生产调用方固定传 60 秒。本模块不做超时管理。

### 3.8 `_collect_tags(self, paper: Paper, llm_tags: List[str], db) -> List[Tag]`

- **输入**：`paper`、LLM 通道产出的标签名列表（可为空）、调用方持有的 SQLAlchemy Session。
- **输出**：`List[Tag]`——**已落库（flush）但尚未关联到 paper、尚未 commit** 的 Tag 对象；`paper.tags` 中已关联的同名 Tag 不出现在返回列表（幂等保护：重复打标不会重复关联）。
- **合并去重契约**：
  1. 顺序为 `llm_tags + rule_tags`——**LLM 标签优先**，规则标签垫底；
  2. 按 `name.strip()` 后的**精确字符串**去重——无大小写/全半角归一化，`Transformer` 与 `transformer` 视为两个标签（quirks，见第 4 节）；
  3. 合并序列**截断到前 5 个**——若 LLM 给满 5 个，规则命中全部丢弃；
  4. 每个名字再过一次 `_clean_tag_name`，清洗为空的丢弃。
- **落库契约**：按 `Tag.name == name` 精确查重；不存在则新建 `Tag(name=name, color=random.choice(self.colors))`——**颜色为 10 色池随机，不可复现**；`db.add` + `db.flush` 立即分配主键，**绝不 commit**（事务边界归调用方，若调用方后续异常回滚，新建 Tag 一并消失）。
- **副作用**：DB 写入（`tags` 表查/插 + flush）；读 `paper.tags` 关系。
- **异常**：DB 层异常（如唯一约束竞态）不兜底，向外抛给调用方。

### 3.9 `async generate_tags(self, paper: Paper, db) -> List[Tag]` / `generate_tags_sync(self, paper: Paper, db, timeout: Optional[int] = None) -> List[Tag]`

- **行为**：模块对外双入口，均为「LLM 通道 → `_collect_tags`」的薄封装；同步版供无事件循环的后台线程使用（避免线程内 `asyncio.run` 复用 async client 的跨事件循环问题）。
- **现状**：生产代码**仅使用 `generate_tags_sync`**（papers.py:204）；`generate_tags` 当前无调用者，属预留入口，行为契约与同步版等价。
- **后置条件**：返回列表中的 Tag 已在 Session 中 flush；关联与 commit 是调用方责任。

## 4. 边界条件与错误处理

| 场景 | 期望行为 |
|------|----------|
| 论文四字段（标题/作者/期刊/摘要）全空 | 不发起 LLM 调用；规则通道对空文本也无命中；返回 `[]`，paper 无标签（调用方 commit 无实质变更） |
| LLM 超时/报错/返回空串 | 记 warning 降级为纯规则打标；规则也无命中则返回 `[]` |
| LLM 输出超过 5 行 | 解析阶段截断到前 5 行 |
| LLM 返回池外标签 | 照收：清洗后落库为新 `Tag` 行（`tags.name` 有唯一约束，同名复用已有行） |
| LLM 标签与规则标签同名 | 去重只保留一次（LLM 位置在前） |
| 大小写变体（如 LLM 返回 `transformer`，池内为 `Transformer`） | **视为不同标签**：可能同时入库并关联（精确匹配、无归一化所致，已知 quirk） |
| 元数据增强失败 | `_enhance_paper_metadata` 提前 return，打标被**连锁跳过**（本次无标签，无重试机制） |
| 打标过程 DB 异常 | 本模块不兜底；调用方 catch 记 `logger.error`，paper 保持已有标签/无标签，**不影响 `processed="done"`** |
| 同一 paper 重复打标 | 已关联的 Tag 被过滤，返回列表只含新关联候选；`Tag` 行按名复用不产生重复行 |
| 关键词位于词边界无空格处（如 `"MRI."`、`"CT-"`） | `" mri "`/`" ct "` 规则漏匹配（空格约定的已知局限） |
| 乱码输入（UTF-8 被 latin-1 误解码） | `_clean_tag_name` 尝试还原；还原失败静默按原样继续清洗 |
| 标签颜色 | 新 Tag 颜色随机取自 10 色池；同一标签多次创建（理论仅首次）颜色不可复现 |

## 5. 依赖

- **上游依赖**：`app.services.llm.llm_service`（`chat_completion` / `chat_completion_sync`，宪法第 8 条唯一入口）、`app.models`（`Paper` / `Tag` / `paper_tags` 关联表）、`app.core.logger`、标准库 `re` / `random`。
- **下游消费者**：`routers/papers.py::_enhance_paper_metadata`（唯一生产调用方，负责关联 `paper.tags` 与 commit）；间接消费者为所有展示/筛选标签的前端页面与统计接口。

## 6. 验收标准（可测试）

- [ ] AC1：构造 `paper.title="Deep learning for colorectal cancer T staging"`（其余字段为空），`_rule_based_tags` 命中集合包含 `深度学习`、`结直肠癌`、`T分期`，且不包含 `胃癌` 等纯 LLM 通道标签
- [ ] AC2：mock `llm_service.chat_completion_sync` 返回 `"医学影像\n深度学习\n1. Transformer\n- 放射组学\n综述\n方法学\n可解释性"`，`generate_tags_sync` 返回的 Tag 名列表恰为前 5 个清洗后名字（编号/列表符号已去除，第 6、7 行被截断）
- [ ] AC3：mock LLM 抛异常时，`generate_tags_sync` 不向外抛，返回纯规则打标结果；mock LLM 断言被调用一次
- [ ] AC4：四字段全空的 paper，`generate_tags_sync` 返回 `[]` 且 mock LLM **未被调用**
- [ ] AC5：对已有 `Tag(name="深度学习")` 行的库重复打标，`tags` 表行数不增；已关联到该 paper 的标签不出现在返回列表中
- [ ] AC6：`generate_tags_sync` 返回后未 commit 时回滚 Session，新建 Tag 行随之消失（验证 flush-不-commit 事务边界）

## 7. 现有测试覆盖与盲区

- **已覆盖**：`backend/tests/` 中**无任何针对 `AutoTagService` 的测试**（15 个测试文件 grep 验证）。唯一沾边的是 `test_mcp.py` 手工 `Tag(name="MIL")` 验证 MCP 工具透传标签名，与自动打标无关；`test_upload.py` 用 `monkeypatch.setattr(papers_router, "_process_paper_background", lambda paper_id: None)` 把整条后台链（含打标）桩掉。
- **盲区**：
  - **高**：规则通道匹配语义（子串、转小写、`" mri "` 空格边界、命中集合无序）无测试（AC1）
  - **高**：LLM 输出解析与清洗（编号/符号/引号去除、5 个截断、池外标签照收）无测试（AC2）
  - **高**：LLM 失败降级为纯规则、空上下文不发起调用两条降级路径无测试（AC3、AC4）——降级是本模块的核心设计承诺
  - **高**：合并去重与「LLM 优先、规则垫底、总数截断 5」的顺序语义无测试
  - **中**：Tag 查重复用 / 新建 / flush-不-commit 事务边界无测试（AC5、AC6）
  - **中**：大小写变体产生重复语义标签的 quirk（`Transformer` vs `transformer`）无测试、无防护
  - **中**：触发链路的「元数据增强失败 → 打标连锁跳过」行为无测试（属 papers 路由侧，但与打标可得性直接相关）
  - **低**：`_clean_tag_name` 的 latin-1→UTF-8 乱码还原分支无测试
  - **低**：`timeout` 参数透传（60 秒）与随机颜色分配无测试
  - **低**：异步入口 `generate_tags` 无生产调用者亦无测试（死代码风险）

## 8. 关键设计决策

- **规则 + LLM 双通道而非纯 LLM**：规则通道零成本、离线可用，保证 LLM 故障/未配 Key 时仍有基础标签；LLM 通道负责召回规则覆盖不到的语义。LLM 标签排在合并序列前部，体现「语义判断优先于关键词」的取舍。
- **候选标签池内置硬编码**：30 项紧贴用户研究方向（结直肠癌 T 分期 / MIL / 病理影像），允许 LLM 池外补充 1–2 个以兼顾开放性；池与规则均为模块级常量，改词表即改代码——单用户场景下可接受，但新增池标签时须注意规则集只覆盖 20 项的鸿沟。
- **失败一律降级为 `[]` 而非向外抛**：打标是旁路增强，任何失败都不应传导到调用方事务与 `processed` 状态；代价是失败只留一条 warning 日志，用户无感知（无 UI 反馈、无重试入口）。
- **flush 不 commit、关联留给调用方**：保持模块无事务边界，让调用方把「新建 Tag + 关联 paper」与自身业务放进同一 commit；副作用是模块单独调用时极易留下未提交状态，属调用纪律而非模块防护。
- **去重仅精确字符串匹配**：实现最简单，但把「同义不同形」的治理推给了 LLM 输出习惯；历史上未出问题故未加归一化，修订前须先补 AC5 类测试。
- **同步/异步双入口**：后台守护线程无事件循环，复用 async client 会触发跨事件循环错误（见 eval 报告记载的历史事故），故打标与元数据增强在后台链全部走 `_sync` 入口；异步版保留给未来请求作用域内的调用场景。
