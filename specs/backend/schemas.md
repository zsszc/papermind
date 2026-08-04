# schemas.py（API 请求/响应契约模型）规格说明书

> 本文档描述 `backend/app/schemas.py` 的**行为契约**：全部 Pydantic 请求/响应模型及其字段、默认值与校验语义。
> 依据源码全文反向工程（277 行，Pydantic v2，`pydantic==2.7.4`），字段定义照抄代码。

## 1. 背景与目标

`schemas.py` 是后端所有 HTTP 接口的契约层：FastAPI 路由用它做请求体解析/校验（失败返回 422）与响应序列化。它集中定义了 papers / search / chat / thesis 等路由域的全部数据传输模型，保证前后端字段约定一致，并让响应模型能通过 `from_attributes = True` 直接从 SQLAlchemy ORM 对象序列化。

## 2. 范围

### 2.1 包含

- 全部 28 个 Pydantic 模型的字段、类型、默认值、可选性（按路由域分组：Tag / Paper / Chunk / Search / Chat / Conversation / Thesis 与标注）。
- 模型间的继承关系（Base → Create/Update/Response）与 `from_attributes` 配置。
- 校验语义的整体特征：无自定义 validator、无枚举/Literal 约束、无数值范围约束。

### 2.2 非目标

- 不描述各路由端点如何使用这些模型（归各 router 规格）。
- 不描述 ORM 模型（归 models.py 规格）与数据库迁移。
- 不描述 FastAPI 自动生成的 422 错误响应结构细节（框架行为）。

## 3. 行为契约

通用约定：

- 未标注默认值的 `Optional[...]` 字段默认 `None`。
- 标注 `class Config: from_attributes = True` 的模型可由 ORM 对象直接构建。
- **本文件没有任何自定义校验器**（无 `@field_validator`、无 `Literal`/枚举、无 `gt`/`le`/`min_length` 等约束，唯一 `Field(...)` 用法是 `SearchRequest.filters` 的 `default_factory=dict`）。因此注释中提到的取值约定（如 `action` 的 `add / remove`、`skill` 的五个候选值）**均不被模型层强制**，非法值可穿透到路由层。
- 请求模型中 `str` 类型字段不校验空串/超长；`int` 类型字段不校验范围。

### 3.1 Tag 域

#### `TagBase(BaseModel)`

```python
name: str
color: Optional[str] = "#1890ff"
description: Optional[str] = None
```

- **语义**：标签基础字段。`name` 必填；`color` 缺省为 `"#1890ff"`（Ant Design 主色）。

#### `TagCreate(TagBase)`

- 无新增字段。创建标签请求体。

#### `TagResponse(TagBase)`

- 新增 `id: int`；`from_attributes = True`。标签响应体。

### 3.2 Paper 域

#### `PaperBase(BaseModel)`

```python
title: Optional[str] = None
authors: Optional[str] = None
year: Optional[int] = None
journal: Optional[str] = None
abstract: Optional[str] = None
doi: Optional[str] = None
pages: Optional[int] = None
status: Optional[str] = "unread"
source: Optional[str] = "local"
processed: Optional[str] = "pending"
```

- **语义**：文献可编辑元数据。`status` 约定值 unread/reading/finished、`source` 约定值 local/import、`processed` 约定值 pending/processing/done/failed——均为约定，模型层不强制。

#### `PaperCreate(PaperBase)`

- 新增必填字段：

```python
file_path: str
filename: str
metadata_json: Optional[Dict[str, Any]] = None
```

- **语义**：登记文献（通常配合上传接口）的请求体。`file_path`/`filename` 是仅有的两个必填字段，其余元数据均可缺省。

#### `PaperUpdate(PaperBase)`

- 新增 `metadata_json: Optional[Dict[str, Any]] = None`。
- **语义**：更新文献元数据请求体。因全部字段有默认值，**空 JSON `{}` 也是合法请求体**，是否执行部分更新由路由层决定。

#### `PaperListItem(BaseModel)`（`from_attributes = True`）

```python
id: int
title: Optional[str]
authors: Optional[str]
year: Optional[int]
journal: Optional[str]
status: str
processed: str
filename: str
last_read_page: Optional[int]
created_at: datetime
tags: List[TagResponse] = []
```

- **语义**：文献列表项响应。`status`/`processed`/`filename` 为必填（依赖 ORM 列非空）；`tags` 缺省空列表。

#### `PaperDetail(PaperListItem)`（`from_attributes = True`）

- 新增：

```python
abstract: Optional[str]
doi: Optional[str]
pages: Optional[int]
file_path: str
source: str
metadata_json: Optional[Dict[str, Any]]
last_read_page: Optional[int]   # 重复声明，覆盖父类同名同型字段
updated_at: datetime
```

- **语义**：文献详情响应（列表项的超集）。注意 `last_read_page` 在子类中重复声明，类型与父类一致，无行为差异。

#### `PaperListResponse(BaseModel)`

```python
total: int
items: List[PaperListItem]
```

#### `PaperStatsResponse(BaseModel)`

```python
total: int
by_year: Dict[str, int]
by_status: Dict[str, int]
by_tag: Dict[str, int]
top_authors: List[Dict[str, Any]]
citation_graph: Dict[str, Any]
```

- **语义**：统计页聚合响应。字典键为字符串化的年份/状态/标签名；`top_authors` 与 `citation_graph` 内部结构不由模型约束。

#### 批量操作请求

```python
class PaperBatchDelete(BaseModel):
    ids: List[int]

class PaperBatchStatus(BaseModel):
    ids: List[int]
    status: str

class PaperBatchTags(BaseModel):
    ids: List[int]
    tag_names: List[str]
    action: str = "add"  # add / remove
```

- **语义**：`ids` 允许为空列表（模型层不拒绝）；`action` 注释约定 `add / remove` 但**无校验**，其他字符串可穿透。

### 3.3 Chunk 域

#### `ChunkResponse(BaseModel)`（`from_attributes = True`）

```python
id: int
paper_id: int
content: str
page_number: Optional[int]
chunk_index: int
section_title: Optional[str]
chunk_type: str
```

- **语义**：文本分块响应（分块元数据存 SQLite，向量本体在 ChromaDB）。

### 3.4 Search 域

#### `SearchRequest(BaseModel)`

```python
query: str
top_k: Optional[int] = 10
filters: Optional[Dict[str, Any]] = Field(default_factory=dict)
use_keyword: Optional[bool] = True
use_semantic: Optional[bool] = True
```

- **语义**：检索请求。`query` 必填（允许空串）；`top_k` 缺省 10 且无上限约束；`filters` 缺省为可变安全的新空 dict（`default_factory`）；两个开关缺省均开（双开时由路由层做 RRF 融合）。

#### `SearchResult(BaseModel)`

```python
paper_id: int
title: Optional[str]
authors: Optional[str]
year: Optional[int]
content: str
page_number: Optional[int]
chunk_type: str
score: float
source: str  # semantic / keyword / hybrid
```

- **语义**：单条检索命中。`source` 注释约定三值，模型层不强制。

#### `SearchResponse(BaseModel)`

```python
query: str
results: List[SearchResult]
```

### 3.5 Chat 域

#### `ChatMessage(BaseModel)`

```python
role: str
content: str
citations: Optional[List[Dict[str, Any]]] = None
```

#### `ChatRequest(BaseModel)`

```python
message: str
conversation_id: Optional[int] = None
paper_id: Optional[int] = None
stream: Optional[bool] = True
enable_web_search: Optional[bool] = False
skill: Optional[str] = None  # translator / proofreader / method_comparator / outline_generator / data_analyst
```

- **语义**：对话请求。`stream` 缺省 True（SSE 流式）；`skill` 注释列了 5 个候选值（注：注释未含实际默认注册的第 6 个 `writing_assistant`），模型层不校验取值，未知 skill 的处理归路由/服务层。

#### `ChatStreamChunk(BaseModel)`

```python
delta: str
finished: bool = False
citations: Optional[List[Dict[str, Any]]] = None
image_analysis: Optional[bool] = False
```

- **语义**：SSE 流式对话的事件载荷模型。与 `routers/chat.py` 实际推送的 `{delta}` / `{finished, citations}` / `{error}` 事件格式对应（`error` 事件不在本模型中表达）。

#### `ImageAnalysisRequest(BaseModel)`

```python
question: Optional[str] = "请描述这张图片的内容，并解释其在学术论文中可能的含义。"
```

- **语义**：图片分析（多模态）请求；`question` 缺省为通用中文学术图像解读提示语。

### 3.6 Conversation 域

#### `ConversationResponse(BaseModel)`（`from_attributes = True`）

```python
id: int
title: Optional[str]
summary: Optional[str]
message_count: int
created_at: datetime
updated_at: datetime
```

### 3.7 Thesis 与标注域

#### `ThesisChapter(BaseModel)`

```python
title: str
level: int
start_paragraph: int
end_paragraph: int
```

- **语义**：大论文章节结构项（段落索引为 Word 文档段落下标），作为 `ThesisFileResponse.chapter_structure` 的元素类型。

#### `ThesisCitationResponse(BaseModel)`（`from_attributes = True`）

```python
id: int
thesis_id: int
paper_id: Optional[int]
chapter_index: Optional[int]
section_index: Optional[int]
context: Optional[str]
citation_text: Optional[str]
detected_auto: bool
```

- **语义**：引用检测记录。`paper_id` 为 `None` 表示检测到的引用尚未匹配到库内文献。

#### `ThesisFileResponse(BaseModel)`（`from_attributes = True`）

```python
id: int
title: Optional[str]
filename: str
chapter_structure: List[ThesisChapter] = []
word_count: Optional[int]
metadata_json: Optional[Dict[str, Any]]
created_at: datetime
updated_at: datetime
```

#### `ChapterCitationMapItem(BaseModel)`

```python
chapter_index: int
chapter_title: str
level: int
paper_ids: List[int]
paper_titles: Dict[int, Optional[str]]
citation_count: int
```

- **注意**：`paper_titles` 的键类型为 `int`；JSON 序列化后对象键必为字符串，前端拿到的是字符串键。

#### `ThesisCitationMapResponse(BaseModel)`

```python
thesis_id: int
total_citations: int
matched_citations: int
chapters: List[ChapterCitationMapItem]
```

#### `ThesisCitationUpdate(BaseModel)`

```python
paper_id: Optional[int] = None
```

- **语义**：人工修正引用-文献匹配关系；`paper_id = None` 表示解除匹配。

#### 标注模型

```python
class PaperAnnotationBase(BaseModel):
    page_number: int
    selected_text: str
    note: Optional[str] = None
    color: Optional[str] = "yellow"

class PaperAnnotationCreate(PaperAnnotationBase):
    pass

class PaperAnnotationResponse(PaperAnnotationBase):
    id: int
    paper_id: int
    created_at: datetime
    # from_attributes = True
```

- **语义**：PDF 页面标注。`color` 缺省 `"yellow"`；`page_number` 无正数约束。

#### 其余

```python
class ThesisFileListResponse(BaseModel):
    total: int
    items: List[ThesisFileResponse]

class ThesisAnalyzeRequest(BaseModel):
    chapter_index: Optional[int] = None

class ThesisAnalyzeResponse(BaseModel):
    thesis_id: int
    chapter_title: Optional[str]
    suggestions: str
    citations: List[ThesisCitationResponse] = []
```

- `ThesisAnalyzeRequest.chapter_index = None` 表示分析整篇论文。

## 4. 边界条件与错误处理

| 场景 | 期望行为 |
|------|----------|
| 请求体缺失必填字段（如 `PaperCreate.file_path`、`SearchRequest.query`、`ChatRequest.message`） | FastAPI 返回 422 校验错误 |
| 字段类型不符（如 `year` 传非整数、`ids` 传非列表） | 422；Pydantic v2 对数字字符串等执行严格度依默认模式的 coercion |
| 可选字段整体省略（如 `PaperUpdate` 传 `{}`） | 合法，全部取默认值；是否产生实际变更由路由层决定 |
| `PaperBatchTags.action` 传 `add`/`remove` 以外的值 | 模型层放行（无校验）；行为由路由层决定，规格上属未定义 |
| `ChatRequest.skill` 传未注册 skill 名 | 模型层放行；由 chat 路由/Skill 服务决定（降级或报错） |
| `ids: []` 空批量 | 模型层合法；路由层预期返回零变更结果 |
| `top_k` 传 0、负数或超大值 | 模型层不约束；由检索服务层处理 |
| 请求体出现模型未声明的多余字段 | Pydantic 默认忽略（模型未设 `extra="forbid"`） |
| 响应模型从 ORM 构建（`from_attributes`） | 缺属性时按 Pydantic 默认行为报错（路由层保证 ORM 字段齐全） |

## 5. 依赖

- **上游依赖**：`pydantic`（BaseModel/Field）、`datetime`、`typing`；无项目内 import（纯契约层，零业务依赖）。
- **下游消费者**：`routers/papers.py`（Tag/Paper 域）、`routers/search.py`（Search 域）、`routers/chat.py`（Chat/Conversation 域）、`routers/thesis.py`（Thesis/标注域）；其余 router（memory/export/settings/static）不依赖本文件。

## 6. 验收标准（可测试）

- [ ] AC1：缺少必填字段的请求（`PaperCreate` 无 `file_path`、`SearchRequest` 无 `query`、`ChatRequest` 无 `message`）返回 422。
- [ ] AC2：缺省值生效——`PaperCreate` 只传 `file_path`/`filename` 时 `status="unread"`、`source="local"`、`processed="pending"`；`PaperBatchTags` 不传 `action` 时为 `"add"`；`ChatRequest` 不传 `stream`/`enable_web_search` 时为 `True`/`False`；`SearchRequest` 不传 `top_k` 时为 10、`filters` 为独立空 dict（跨实例不共享）。
- [ ] AC3：响应模型可由对应 ORM 对象构建（`from_attributes`），且缺省容器字段（`tags`、`chapter_structure`、`citations`）为空列表而非 None。
- [ ] AC4：模型未声明的多余请求字段被忽略而非报错（当前默认行为）。
- [ ] AC5：`ChapterCitationMapResponse` JSON 序列化后 `paper_titles` 的键为字符串。

## 7. 现有测试覆盖与盲区

- **已覆盖**：无直接针对 `schemas.py` 的测试文件。模型仅在路由测试中被动经过——`test_upload.py`（POST 文献登记/更新走 `PaperCreate`/`PaperUpdate`）、`test_search.py`（POST `/api/search` 走 `SearchRequest`）等；响应模型在断言响应 JSON 字段时被间接验证。
- **盲区**：
  - 全仓库无任何 422 校验失败用例（`grep 422` 零命中），必填缺失/类型错误路径未验证（中）。
  - 字段默认值（`status=unread`、`source=local`、`processed=pending`、`action=add`、`stream=True`、`top_k=10`、`filters` 独立空 dict）无直接断言（中）。
  - `PaperBatchTags.action` 注释约定 `add/remove` 但无枚举校验，非法值的端到端行为未定义也未测（中）。
  - `ChatRequest.skill` 注释候选值与实际默认 Skill 注册表不一致（缺 `writing_assistant`），未知 skill 的行为未测（低）。
  - `ChatStreamChunk` 与实际 SSE 事件格式（含 `error` 分支）的一致性无测试（低）。
  - `ChapterCitationMapItem.paper_titles` 的 `int` 键经 JSON 序列化后变字符串，前后端键类型约定无测试（低）。
  - 多余请求字段被静默忽略（未设 `extra="forbid"`）的行为无测试（低）。

## 8. 关键设计决策

- **零自定义校验**：本文件刻意保持「纯形状声明」，业务校验（如上传扩展名、50MB 上限、FTS 查询清洗）全部放在路由/服务层，模型层不做拦截——因此「注释中的取值约定」都不构成运行时约束。
- **Base/Create/Update/Response 继承分层**：Tag、Paper、Annotation 三个域用 `Base → Create/Update → Response` 继承，避免字段重复；响应模型统一 `from_attributes = True` 以直接吃 ORM 对象。
- **`filters` 用 `Field(default_factory=dict)`**：Pydantic v2 中可变默认值本身安全（模型会深拷贝），此处显式 `default_factory` 是防御性写法。
- **`PaperDetail` 重复声明 `last_read_page`**：与父类 `PaperListItem` 同型同名，属于冗余但无害的历史遗留，不构成行为差异。
- **skill 注释未含 `writing_assistant`**：注释落后于 `services/skills.py` 的实际注册表（6 个 Skill），属文档性偏差，因无校验故不影响运行。
