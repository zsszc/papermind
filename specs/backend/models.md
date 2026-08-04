# models.py（ORM 模型与 FTS5 全文检索）规格说明书

> 本文档描述 `backend/app/models.py` 的**行为契约**：11 张 ORM 表（含 `paper_tags` 关联表）、全部字段/默认值/关系/级联规则，以及 `papers_fts` FTS5 虚拟表、3 个同步触发器与建表事件。
> 依据源码全文反向工程（246 行，SQLAlchemy 2.0 声明式），字段定义与 DDL 照抄代码。

## 1. 背景与目标

`models.py` 定义 PaperMind 的全部持久化结构：文献（papers/chunks/tags）、对话（conversations/messages）、Skill 注册表（skills）、大论文（thesis_files/thesis_citations）、Agent 记忆（memory_summaries）、PDF 标注（paper_annotations），以及为关键词检索服务的 `papers_fts` FTS5 虚拟表。所有表共享 `app.database.Base`，启动时由 `Base.metadata.create_all` 建表；`papers_fts` 通过 `Paper.__table__` 的 `after_create` 事件与 lifespan 中的 `ensure_papers_fts()` 双保险创建，并靠 3 个触发器与 `papers` 表实时同步。

## 2. 范围

### 2.1 包含

- 11 张表：`papers`、`chunks`、`tags`、`paper_tags`（纯关联表）、`conversations`、`messages`、`skills`、`thesis_files`、`thesis_citations`、`memory_summaries`、`paper_annotations`。
- 关系与级联：`Paper.chunks` / `Paper.annotations` / `Paper.tags` / `Conversation.messages` / `ThesisFile.citations` / `ThesisCitation.paper` / `MemorySummary.conversation`。
- `papers_fts` 虚拟表 DDL、insert/update/delete 三个同步触发器、`ensure_papers_fts(engine)`、`after_create`/`after_drop` 两个表事件。

### 2.2 非目标

- 不描述引擎、会话、`ensure_schema()` 轻量迁移（归 database.py 规格）。
- 不描述各路由/服务如何读写这些模型（归各 router/service 规格）。
- 不描述 ChromaDB 中向量本体（`chunks` 表只存元数据，向量归 `services/embedding.py` 域）。

## 3. 行为契约

通用约定（适用于全部模型）：

- 主键均为 `id = Column(Integer, primary_key=True, index=True)`（SQLite 下即 `rowid` 别名）。
- `created_at` 默认 `datetime.datetime.utcnow`；带 `updated_at` 的表另有 `onupdate=datetime.datetime.utcnow`（ORM 层赋值，非数据库触发器）。注意 `utcnow` 在 Python 3.12 已弃用，当前行为是存**无时区的 UTC 时间**。
- SQLite 不强制 `String(N)` 长度与外键约束（未开 `PRAGMA foreign_keys`）：长度仅为声明性文档，孤儿清理由 ORM 级联完成。
- JSON 列在 SQLite 中以 TEXT 存储，由 SQLAlchemy 自动序列化/反序列化；`default=dict` / `default=list` 为 Python 侧默认值（新对象未入库即生效）。
- 列注释中的取值约定（如 `status` 的 unread/read/important/todo）**均不被数据库强制**。

### 3.1 `paper_tags`（关联表，非 ORM 类）

```python
paper_tags = Table(
    "paper_tags", Base.metadata,
    Column("paper_id", Integer, ForeignKey("papers.id"), primary_key=True),
    Column("tag_id", Integer, ForeignKey("tags.id"), primary_key=True),
)
```

- **语义**：`papers` ↔ `tags` 多对多关联；复合主键 `(paper_id, tag_id)` 天然去重。
- **无级联配置**：删除 Paper/Tag 时关联行的清理由 ORM relationship（`secondary`）在会话内处理；裸 SQL 删除会留孤儿关联行。

### 3.2 `class Paper(Base)`（`__tablename__ = "papers"`）

| 列 | 定义 | 说明 |
|---|---|---|
| `title` | `String(500), nullable=True` | 标题 |
| `authors` | `Text, nullable=True` | 作者（分号拼接串） |
| `year` | `Integer, nullable=True` | 年份 |
| `journal` | `String(500), nullable=True` | 期刊 |
| `abstract` | `Text, nullable=True` | 摘要 |
| `doi` | `String(200), nullable=True, index=True` | DOI（建索引） |
| `pages` | `Integer, nullable=True` | 页数 |
| `file_path` | `String(1000), nullable=False` | PDF 相对路径（**必填**） |
| `filename` | `String(500), nullable=False` | 文件名（**必填**） |
| `status` | `String(50), default="unread"` | 约定 unread / read / important / todo |
| `source` | `String(50), default="local"` | 约定 local / arxiv / crossref |
| `processed` | `String(50), default="pending"` | 约定 pending / processing / done / error |
| `last_read_page` | `Integer, nullable=True, default=1` | 最近阅读页 |
| `metadata_json` | `JSON, default=dict` | 扩展元数据 |
| `created_at` / `updated_at` | `DateTime` | 见通用约定 |

- **关系**：
  - `tags = relationship("Tag", secondary=paper_tags, back_populates="papers")`
  - `chunks = relationship("Chunk", back_populates="paper", cascade="all, delete-orphan")` —— 删除 Paper 级联删除其全部 Chunk
  - `annotations`（见 3.11，类定义之后追加赋值，同样 `cascade="all, delete-orphan"`）

### 3.3 `class Chunk(Base)`（`__tablename__ = "chunks"`）

| 列 | 定义 | 说明 |
|---|---|---|
| `paper_id` | `Integer, ForeignKey("papers.id"), nullable=False` | 所属文献 |
| `content` | `Text, nullable=False` | 分块文本（**必填**） |
| `page_number` | `Integer, nullable=True` | 页码 |
| `chunk_index` | `Integer, nullable=False, default=0` | 块序号；向量库 id 形如 `p{paper_id}_c{chunk_index}` |
| `section_title` | `String(500), nullable=True` | 章节标题 |
| `chunk_type` | `String(50), default="paragraph"` | 约定 abstract / intro / method / result / conclusion / paragraph |
| `token_count` | `Integer, nullable=True` | token 数 |
| `created_at` | `DateTime` | — |

- **关系**：`paper = relationship("Paper", back_populates="chunks")`（多对一）。

### 3.4 `class Tag(Base)`（`__tablename__ = "tags"`）

| 列 | 定义 | 说明 |
|---|---|---|
| `name` | `String(100), unique=True, nullable=False` | **全表唯一**，重名插入触发 `IntegrityError` |
| `color` | `String(20), default="#1890ff"` | 展示色 |
| `description` | `Text, nullable=True` | 描述 |

- **关系**：`papers = relationship("Paper", secondary=paper_tags, back_populates="tags")`。

### 3.5 `class Conversation(Base)`（`__tablename__ = "conversations"`）

| 列 | 定义 | 说明 |
|---|---|---|
| `title` | `String(500), nullable=True` | 会话标题 |
| `summary` | `Text, nullable=True` | 会话摘要（迁移列，见 database.py 3.4） |
| `paper_ids` | `JSON, default=list` | 关联文献 id 列表 |
| `message_count` | `Integer, default=0` | 消息计数（冗余字段，由路由层维护） |
| `created_at` / `updated_at` | `DateTime` | — |

- **关系**：`messages = relationship("Message", back_populates="conversation", cascade="all, delete-orphan")` —— 删除会话级联删除全部消息。

### 3.6 `class Message(Base)`（`__tablename__ = "messages"`）

| 列 | 定义 | 说明 |
|---|---|---|
| `conversation_id` | `Integer, ForeignKey("conversations.id"), nullable=False` | 所属会话 |
| `role` | `String(50), nullable=False` | 约定 user / assistant / system |
| `content` | `Text, nullable=False` | 消息正文 |
| `citations` | `JSON, default=list` | 引用文献列表（迁移列） |
| `skill_used` | `String(100), nullable=True` | 使用的 Skill id（迁移列） |
| `token_usage` | `Integer, nullable=True` | token 用量（迁移列） |
| `created_at` | `DateTime` | — |

- **关系**：`conversation = relationship("Conversation", back_populates="messages")`。

### 3.7 `class Skill(Base)`（`__tablename__ = "skills"`）

| 列 | 定义 | 说明 |
|---|---|---|
| `skill_id` | `String(100), unique=True, nullable=False` | 业务 id，**全表唯一** |
| `display_name` | `String(200), nullable=False` | 展示名 |
| `description` | `Text, nullable=True` | — |
| `trigger_words` | `JSON, default=list` | 触发词 |
| `parameters` | `JSON, default=dict` | 参数 schema |
| `prompt_template` | `Text, nullable=True` | Prompt 模板 |
| `icon` | `String(100), nullable=True` | 图标 |
| `enabled` | `Boolean, default=True` | 启用标记 |
| `created_at` | `DateTime` | — |

- **无关系**。注意：当前 Skill 运行时走 `services/skills.py` 的内存注册表，本表为持久化预留，**无任何路由读写它**。

### 3.8 `class ThesisFile(Base)`（`__tablename__ = "thesis_files"`）

| 列 | 定义 | 说明 |
|---|---|---|
| `title` | `String(500), nullable=True` | — |
| `file_path` / `filename` | `String(1000)/String(500), nullable=False` | **必填** |
| `chapter_structure` | `JSON, default=list` | `[{title, level, start_paragraph, end_paragraph}]` |
| `word_count` | `Integer, nullable=True` | — |
| `metadata_json` | `JSON, default=dict` | — |
| `created_at` / `updated_at` | `DateTime` | — |

- **关系**：`citations = relationship("ThesisCitation", back_populates="thesis", cascade="all, delete-orphan")`。

### 3.9 `class ThesisCitation(Base)`（`__tablename__ = "thesis_citations"`）

| 列 | 定义 | 说明 |
|---|---|---|
| `thesis_id` | `Integer, ForeignKey("thesis_files.id"), nullable=False` | 所属论文 |
| `paper_id` | `Integer, ForeignKey("papers.id"), **nullable=True**` | 匹配到的文献；未匹配为 NULL |
| `chapter_index` / `section_index` | `Integer, nullable=True` | 章节定位 |
| `context` | `Text, nullable=True` | 引用上下文 |
| `citation_text` | `String(500), nullable=True` | 如 `[1]` 或 `(Zhou et al., 2024)` |
| `detected_auto` | `Boolean, default=True` | 是否自动检测 |
| `created_at` | `DateTime` | — |

- **关系**：`thesis = relationship("ThesisFile", back_populates="citations")`；`paper = relationship("Paper")`（**单向**，Paper 侧无反向集合，删除 Paper 不级联清理本表）。

### 3.10 `class MemorySummary(Base)`（`__tablename__ = "memory_summaries"`）

| 列 | 定义 | 说明 |
|---|---|---|
| `memory_type` | `String(50), default="short_term"` | 约定 short_term / long_term / preference / fact |
| `content` | `Text, nullable=False` | 记忆内容（**必填**） |
| `source_conversation_id` | `Integer, ForeignKey("conversations.id"), nullable=True` | 来源会话；删除会话后本行**不会**被级联清理 |
| `importance` | `Integer, default=5` | 约定 1–10，模型层不校验范围 |
| `created_at` / `updated_at` | `DateTime` | — |

- **关系**：`conversation = relationship("Conversation")`（**单向**，无 back_populates）。

### 3.11 `class PaperAnnotation(Base)`（`__tablename__ = "paper_annotations"`）

| 列 | 定义 | 说明 |
|---|---|---|
| `paper_id` | `Integer, ForeignKey("papers.id"), nullable=False` | 所属文献 |
| `page_number` | `Integer, nullable=False` | 页码（**必填**） |
| `selected_text` | `Text, nullable=False` | 选中文本（**必填**） |
| `note` | `Text, nullable=True` | 备注 |
| `color` | `String(20), default="yellow"` | 高亮色 |
| `created_at` | `DateTime` | — |

- **关系**：`paper = relationship("Paper", back_populates="annotations")`。
- **特殊形态**：`Paper.annotations` 在类定义**之后**通过赋值追加：
  ```python
  Paper.annotations = relationship("PaperAnnotation", back_populates="paper", cascade="all, delete-orphan")
  ```
  行为与类内声明等价（删除 Paper 级联删除标注），但阅读代码时需翻到文件末尾。

### 3.12 `papers_fts`（FTS5 虚拟表，`_PAPERS_FTS_DDL`）

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS papers_fts USING fts5(
    title, authors, abstract,
    content='papers',
    content_rowid='id'
)
```

- **语义**：外部内容（external-content）FTS5 表，只索引 `papers` 的 `title/authors/abstract` 三列，`rowid` 对齐 `papers.id`；**不复制数据**，查询时回表取原文。
- **消费方式**：`routers/search.py` 以 `papers_fts MATCH :query`（绑定参数 + `_sanitize_fts_query` 清洗）`JOIN papers ON p.id = fts.rowid` 做关键词检索，`ORDER BY rank`。

### 3.13 三个同步触发器（`_PAPERS_FTS_TRIGGERS`）

```sql
CREATE TRIGGER IF NOT EXISTS papers_fts_insert AFTER INSERT ON papers BEGIN
    INSERT INTO papers_fts(rowid, title, authors, abstract)
    VALUES (new.id, new.title, new.authors, new.abstract);
END
```
```sql
CREATE TRIGGER IF NOT EXISTS papers_fts_update AFTER UPDATE ON papers BEGIN
    INSERT INTO papers_fts(papers_fts, rowid, title, authors, abstract)
    VALUES ('delete', old.id, old.title, old.authors, old.abstract);
    INSERT INTO papers_fts(rowid, title, authors, abstract)
    VALUES (new.id, new.title, new.authors, new.abstract);
END
```
```sql
CREATE TRIGGER IF NOT EXISTS papers_fts_delete AFTER DELETE ON papers BEGIN
    INSERT INTO papers_fts(papers_fts, rowid, title, authors, abstract)
    VALUES ('delete', old.id, old.title, old.authors, old.abstract);
END
```

- **语义**：`papers` 的任意 INSERT/UPDATE/DELETE（无论走 ORM 还是裸 SQL）自动同步 FTS 索引；UPDATE 采用「删旧插新」两段式。
- **只对三列建索引**：更新 `papers` 其他列也会触发删旧插新（无 `WHEN` 条件），行为正确但有少量无效重写。
- `IF NOT EXISTS` 保证重复执行幂等。

### 3.14 `ensure_papers_fts(engine)`

```python
def ensure_papers_fts(engine):
    """确保 papers_fts 虚拟表、触发器存在，并重建索引。"""
```

- **输入**：任意 SQLAlchemy `engine`（生产传模块级引擎，测试传内存引擎）。
- **输出**：无返回。
- **后置条件**：FTS 虚拟表存在、3 个触发器存在、且已执行 `INSERT INTO papers_fts(papers_fts) VALUES ('rebuild')`（全量重建索引，对齐 `papers` 当前内容）。
- **副作用**：DDL × 4 + rebuild × 1，`conn.commit()`；成功写 `[fts] papers_fts 虚拟表检查/重建完成`（INFO）。
- **异常**：**任何异常被捕获并吞掉**，写 `[fts] papers_fts 初始化失败: <e>`（WARNING，含 traceback）后返回——FTS 不可用时应用照常启动，关键词检索降级（由路由层判空/降级处理）。
- **幂等**：全部语句带 `IF NOT EXISTS` / rebuild 本身幂等，可反复调用（test_search.py 的检索接口用例夹具每次都显式调用一次）。

### 3.15 `_create_papers_fts_table` / `_drop_papers_fts_table`（表事件）

```python
@event.listens_for(Paper.__table__, "after_create")
def _create_papers_fts_table(target, connection, **kw): ...

@event.listens_for(Paper.__table__, "after_drop")
def _drop_papers_fts_table(target, connection, **kw): ...
```

- **after_create**：`papers` 表经 `create_all` 创建后立即执行同 3.14 的 DDL + 触发器 + rebuild（**无 try/except**，失败会向上抛）。因此测试 `db` 夹具每次 `create_all` 都会真实建出 FTS 结构。
- **after_drop**：`papers` 表被 drop 后执行 `DROP TABLE IF EXISTS papers_fts`（SQLite 中 `papers` 上的触发器随表自动删除，无需显式 drop）。
- **与 lifespan 的关系**：`create_all` 事件 + `ensure_papers_fts()` 双保险，重复执行靠 `IF NOT EXISTS` 保持幂等。

## 4. 边界条件与错误处理

| 场景 | 期望行为 |
|------|----------|
| 插入重名 `Tag.name` / 重名 `Skill.skill_id` | 数据库唯一约束触发 `IntegrityError`（全模型仅有的两个唯一约束） |
| 缺 `file_path`/`filename`（Paper）、`content`（Chunk/Message/MemorySummary）、`page_number`/`selected_text`（PaperAnnotation） | `nullable=False`，插入触发 `IntegrityError` |
| `Paper.tags.append(tag)` 重复添加同一标签 | 复合主键去重，重复提交触发 `IntegrityError` |
| 删除 Paper | ORM 级联删除其 chunks 与 annotations（delete-orphan）；**不**级联清理 thesis_citations.paper_id、memory_summaries 无关联、tags 本身保留 |
| 删除 Conversation | ORM 级联删除 messages；memory_summaries.source_conversation_id 成悬挂引用（nullable，不报错） |
| 裸 SQL（绕过 ORM）删除 Paper | 触发器仍同步 FTS；但 chunks/annotations 成孤儿行（未开 `PRAGMA foreign_keys`） |
| FTS 初始化失败（如 SQLite 无 FTS5 扩展） | `ensure_papers_fts` 吞异常记 WARNING，应用继续启动，关键词检索降级 |
| `papers` 表 UPDATE 非索引列（如 status） | 触发器仍执行删旧插新，索引内容不变、无错误 |
| `updated_at` 的 `onupdate` | 仅在 ORM `UPDATE` 时赋值；裸 SQL 更新不刷新该列 |
| `importance` 超出 1–10、`status` 填约定外取值 | 模型层一律接受，不校验 |
| `create_all` 时 FTS DDL 失败（after_create 事件） | **异常向上抛**，建表流程中断（与 `ensure_papers_fts` 的吞异常策略不同） |

## 5. 依赖

- **上游依赖**：`app.database.Base`（声明式基类）、`app.core.logger`（FTS 日志，函数内延迟导入）、SQLAlchemy 2.0、SQLite FTS5 扩展。
- **下游消费者**：
  - `app.main` lifespan：`ensure_papers_fts(engine)`；
  - 路由：`papers.py`（Paper/Tag/Chunk/PaperAnnotation/ThesisCitation/ThesisFile）、`chat.py`（Conversation/Message/Paper）、`thesis.py`（ThesisFile/ThesisCitation/Paper）、`memory.py`（MemorySummary）、`export.py`（Paper）、`search.py`（`papers_fts` MATCH 查询）；
  - 服务：`memory_manager.py`（MemorySummary/Message）、`processor.py`（Paper/Chunk）、`auto_tag.py`（Paper/Tag）、`agent_graph.py`（Message）、`mcp_server.py`（Paper）；
  - 测试：`conftest.py`（`create_all` 触发 after_create 事件）、`test_search.py`（`ensure_papers_fts`、Paper）、`test_mcp.py`（Paper/Tag）、`test_memory.py` / `test_agent_graph.py`（Conversation/Message/MemorySummary）、`test_dataset.py` / `test_generate_qa.py`（Chunk/Paper）。

## 6. 验收标准（可测试）

- [ ] AC1：`create_all` 后 11 张表与 `papers_fts` 全部存在；`drop_all` 后 `papers_fts` 被删除。
- [ ] AC2：插入/更新/删除 `papers` 行后，`papers_fts` 中对应 rowid 的索引内容同步（插入可命中、更新后旧词不命中新词命中、删除后不命中）。
- [ ] AC3：对已有数据的库手动清空 `papers_fts` 后调用 `ensure_papers_fts(engine)`，rebuild 恢复全部索引。
- [ ] AC4：重复调用 `ensure_papers_fts` 两次无异常（幂等）。
- [ ] AC5：删除 Paper 时其 chunks/annotations 被级联删除；删除 Conversation 时其 messages 被级联删除。
- [ ] AC6：重名 Tag 插入触发 `IntegrityError`。
- [ ] AC7：新 Paper 对象未显式赋值时 `status=="unread"`、`source=="local"`、`processed=="pending"`、`metadata_json=={}`。
- [ ] AC8：`ensure_papers_fts` 在无效引擎上只记 WARNING 不抛异常。

## 7. 现有测试覆盖与盲区

- **已覆盖**：
  - `tests/conftest.py`：每个用例 `create_all`/`drop_all`，**隐式**经过 after_create/after_drop 事件（FTS 结构随用例真实创建/删除，但无断言）。
  - `tests/test_search.py`：`paper` 夹具显式 `ensure_papers_fts(engine)`（幂等性被反复执行）+ 插入 Paper 后关键词命中——**insert 触发器同步被验证**（`test_keyword_hit`）。
  - `tests/test_mcp.py`：Paper/Tag 造数、`p.tags.append(tag)` 关联并经 `get_paper` 断言标签回读（`paper_tags` 关联可用）。
  - `tests/test_memory.py`、`test_agent_graph.py`：Conversation/Message/MemorySummary 作为夹具写入并查询（模型可建可写）。
  - `tests/test_dataset.py`、`test_generate_qa.py`：Chunk/Paper 造数（含 `chunk_index`、`section_title` 字段使用）。
- **盲区**：
  - 【高】FTS **update/delete 触发器**同步无测试：更新文献标题后旧词仍命中、删除文献后残留索引等回归不会被发现。
  - 【高】FTS **rebuild** 语义无测试：没有「索引损坏/清空后 rebuild 恢复」用例（AC3 未落地）。
  - 【高】`ensure_papers_fts` 与 after_create 事件的**异常/降级路径**（FTS5 不可用、DDL 失败）无测试；吞异常策略是否符合预期无保障。
  - 【中】级联删除（Paper→chunks/annotations、Conversation→messages、ThesisFile→citations）全部无测试。
  - 【中】唯一约束（`Tag.name`、`Skill.skill_id`）与 `nullable=False` 必填列的 `IntegrityError` 行为无测试。
  - 【中】字段默认值（`status`/`source`/`processed`/`importance=5`/`color`/`enabled`/`detected_auto`/JSON 默认值）无系统性断言，仅被个别用例隐式依赖。
  - 【中】`ThesisCitation.paper`、`MemorySummary.conversation` 单向关系删除 Paper/Conversation 后的悬挂引用行为无测试（孤儿行是否可接受未有结论）。
  - 【低】`updated_at` 的 `onupdate` 自动刷新无测试。
  - 【低】`skills` ORM 表整体无消费者、无测试（运行时走内存注册表），属于「建而不用」的预留表。
  - 【低】UPDATE 触发器无 `WHEN` 条件导致的无效删旧插新（性能微损）无基准测试。

## 8. 关键设计决策

- **外部内容 FTS 表 + 触发器同步**：`content='papers'` 避免三列文本在 FTS 中重复存储；触发器保证 ORM 与裸 SQL 写入都能同步索引，代价是每次 UPDATE 都删旧插新（无 `WHEN` 过滤）。rebuild 作为兜底修复手段在每次启动执行。
- **FTS 初始化吞异常、after_create 事件不吞**：启动期 `ensure_papers_fts` 失败只降级关键词检索；而建表事件失败直接中断，因为「表都建不起来」属于致命错误，两类失败严重度不同。
- **双保险建 FTS**（after_create 事件 + lifespan `ensure_papers_fts`）：事件覆盖 `create_all` 路径（含测试），函数覆盖「表已存在但 FTS 缺失/需重建」的旧库路径；`IF NOT EXISTS` 保证叠加执行幂等。
- **`Paper.annotations` 后置赋值**：`PaperAnnotation` 类定义在文件后部，其关系以 `Paper.annotations = relationship(...)` 追加，避免前向引用；行为与类内声明等价，改动时不要漏看这行。
- **单向关系不级联**：`ThesisCitation.paper`、`MemorySummary.conversation` 有意不加 back_populates 与级联——引用检测记录与记忆的生命周期独立于被引用对象，允许删除 Paper/Conversation 后保留记录（悬挂 id 由查询层容忍）。
- **不开 SQLite 外键约束**：级联完全交给 ORM（`cascade="all, delete-orphan"`），与 database.py 的 PRAGMA 策略一致；绕过 ORM 的维护脚本需自行处理孤儿行。
- **`skills` 表建而不用**：为 Skill 持久化/工具化预留，当前运行态在 `services/skills.py` 内存注册表（AGENTS.md 第 3 节）；不要误以为改此表会影响运行时 Skill 列表。
- **`utcnow` 存无时区 UTC**：全库时间列无 tz 信息，跨时区比较需调用方自行约定（Python 3.12 已弃用 `utcnow`，迁移到 `datetime.now(UTC)` 属于行为变更，需先补测试）。
