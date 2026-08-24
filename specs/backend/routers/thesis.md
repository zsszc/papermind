# routers/thesis.py（大论文 HTTP API，挂载前缀 `/api/thesis`）规格说明书

> 本文件描述 `backend/app/routers/thesis.py` 的**行为契约**（做什么），不描述实现细节。
> 依据 2026-08-04 时点的源码反向工程整理；规格与代码冲突时以代码为准并立即修订本文件。

## 1. 背景与目标

`thesis.py` 是「大论文（毕业论文）写作辅助」的全部 HTTP 入口：上传 Word（.docx）→ 解析章节结构与引用标记 → 章节-文献引用映射（发现引用盲区）→ 章节正文读取 → AI 章节评审 → 段落级引用推荐。目标是让用户在本地完成「论文引用治理 + 写作质检」，无任何云端依赖（LLM 调用除外）。

上传安全契约与 papers 路由对齐：单文件 50MB 上限（413）、`.docx` 扩展名白名单（400）、文件名去目录、`_1/_2` 重名改名、失败清理残留（`_save_upload_file` 为 papers.py 同名 helper 的**独立副本**，刻意避免路由间互相依赖）。

## 2. 范围

### 2.1 包含

- `POST /upload`：上传 + 解析 + 建档 + 引用检测落库的完整契约
- 引用-文献自动匹配 `find_paper_by_citation` / `_extract_surnames` 的匹配语义
- 列表/详情/删除、引用列表、引用手动关联（`PUT /{thesis_id}/citations/{citation_id}`）
- 章节-文献映射 `GET /{thesis_id}/citation-map`
- 章节正文 `GET /{thesis_id}/chapters/{chapter_index}/text`
- AI 评审 `POST /{thesis_id}/analyze` 与引用推荐 `POST /{thesis_id}/suggest-citations`

### 2.2 非目标

- docx 解析内部（标题样式识别、章节边界、引用正则、字数统计，归 `services/docx_parser.py` 规格）
- 向量检索与 RRF（归 retrieval/search 规格）；本模块只用 `store.search(query, top_k=5)`
- `llm_service` 重试/截断/temperature（归 llm 规格）
- 大论文文件的静态访问 `/static/my-thesis/...`（归 routers/static.py）
- 前端 ThesisList/ThesisDetail/WritingDesk 展示逻辑

## 3. 行为契约

端点签名照抄代码（`router = APIRouter()`，挂载时加前缀 `/api/thesis`）。

### 3.1 `POST /upload` — `upload_thesis(file: UploadFile = File(...), db: Session = Depends(get_db))`

- **输入**：multipart 单文件 `file`。
- **行为（顺序固定）**：
  1. 扩展名白名单：`filename` 为空或不以 `.docx` 结尾（`lower()`）→ 400「只支持 .docx 文件」；
  2. 声明大小快筛：`file.size > MAX_UPLOAD_SIZE`（50MB）→ 413；
  3. `safe_name = Path(file.filename).name`；**重名改名** `{stem}_1.docx`、`{stem}_2.docx`……；
  4. `_save_upload_file` 分块（1MB）写盘，按实际字节数兜底 50MB；失败（含超限）清理残留后抛错；
  5. `DocxParser().parse` 放线程池执行（同步 CPU/IO）；
  6. 建 `ThesisFile`：`title` 取解析标题或文件 stem；`chapter_structure = parsed["chapters"]`；`word_count`；`metadata_json = {"citations_detected": len(citations)}`；`db.flush()` 分配 id；
  7. 对每条检测到的引用：调 `find_paper_by_citation` 尝试匹配文献（§3.2）；按 `chapters[i].start_paragraph <= citation.paragraph_index <= end_paragraph` 确定 `chapter_index`（首个命中章，无命中为 None）；落 `ThesisCitation(detected_auto=True)`；
  8. 统一 `db.commit()` + refresh。
- **输出**：`ThesisFileResponse`（含 chapter_structure、word_count、metadata_json）。
- **副作用**：文件 I/O（`my-thesis/`）；DB 写入（thesis_files + thesis_citations）。**无后台任务**：解析在请求内同步完成。
- **异常**：400 / 413；`DocxParser.parse` 抛异常（损坏 docx 等）**不兜底**——向外传播经全局脱敏为 500，**已落盘的 docx 文件残留**（DB 未 commit，无记录指向它）。
- **多次上传同一文件**：因改名机制产生多个独立 ThesisFile 记录与多份文件，无去重。

### 3.2 `find_paper_by_citation(citation_text: str, db: Session) -> Optional[Paper]` 与 `_extract_surnames(authors_text: str) -> List[str]`

引用标记 → 文献的自动反查（仅上传时执行一次）。

- **数字引用 `[N]`**：恒返回 `None`——代码注释明示「序号与 paper.id 通常不对应，需要后续用户手动关联」。即数字引用文献**永不自动匹配**。
- **作者-年份引用**：正则 `\(?([A-Za-z\s,\.&]+),?\s*(\d{4})` 提取作者段与年份；`_extract_surnames` 去掉 `et al.`/`et al`，按逗号/`\band\b`/`&` 切分，取每段首词、剥非 `\w` 字符、转小写得姓氏列表（空列表 → None）。
- **匹配**：先按 `Paper.year == year` 全量取出，逐一检查**所有姓氏都是 `paper.authors.lower()` 的子串**，返回第一个命中者（query 无序，多命中时结果不确定）。
- **局限**：中文作者名（正则只认 `[A-Za-z\s,\.&]`）不匹配；姓氏子串匹配可能误中（如 "Li" 命中 "Liu" 不存在——"li" 是 "liu" 的子串，会误中）；年份错误的引用全部落空。

### 3.3 `GET ""` — `list_thesis(db)`

- **输出**：`ThesisFileListResponse{total, items}`，按 `created_at desc` 全量返回（无分页参数）。

### 3.4 `GET /{thesis_id}` — `get_thesis(thesis_id: int, db)`

- **输出**：`ThesisFileResponse`。**异常**：不存在 → 404「Thesis not found」。

### 3.5 `DELETE /{thesis_id}` — `delete_thesis(thesis_id: int, db)` → 204

- **行为（顺序固定）**：404 检查 → 显式删 `thesis_citations` 记录（ORM 上另有 cascade delete-orphan，双保险）→ 删 ThesisFile → commit → **最后**删本地 docx 文件（`os.remove` 失败静默吞掉）。
- **后置条件**：DB 记录必删；文件删除失败时留孤儿文件（仅理论，无日志）。

### 3.6 `GET /{thesis_id}/citations` — `get_thesis_citations(thesis_id: int, db)`

- **输出**：该论文全部 `ThesisCitation` 列表，**无排序**（DB 返回序）；未声明 response_model，依赖 FastAPI `jsonable_encoder`（`sqlalchemy_safe`）直接序列化 ORM——响应体为模型全部列。
- **异常**：404。

### 3.7 `PUT /{thesis_id}/citations/{citation_id}` — `update_thesis_citation(thesis_id, citation_id, payload: ThesisCitationUpdate, db)`

手动关联/取消关联引用标记对应的文献。

- **行为**：citation 按 `id + thesis_id` **双条件**匹配（防跨论文改），不匹配 → 404「Citation not found」；`payload.paper_id` 非 None 时校验目标 paper 存在（404「Paper not found」）；`paper_id = None` 表示**取消关联**（显式传 null）。
- **输出**：更新后的 citation（同 §3.6 的裸序列化）。
- **副作用**：DB 更新。不改 `detected_auto`（手动改关联后该字段仍反映检测来源）。

### 3.8 `GET /{thesis_id}/citation-map` — `get_citation_map(thesis_id: int, db)`

章节-文献映射视图，用于发现引用盲区。

- **输出**：`ThesisCitationMapResponse{thesis_id, total_citations, matched_citations（paper_id 非空计数）, chapters[ChapterCitationMapItem]}`。
- **chapter item 语义**：按 `chapter_structure` 顺序逐章产出 `{chapter_index, chapter_title（缺省 第N章）, level（缺省 1）, paper_ids（升序去重）, paper_titles{paper_id: title}（预加载防 N+1；title 可为 None）, citation_count}`。
- **盲区语义**：`paper_ids` 为空的章 = 无已关联文献的章；`chapter_index IS NULL` 的引用（不属于任何章）**不出现在任何 chapter item 中**，但计入 `total_citations`。

### 3.9 `GET /{thesis_id}/chapters/{chapter_index}/text` — `get_chapter_text(thesis_id, chapter_index, db)`

- **行为**：404（thesis）；**每次调用重新完整解析 docx**（无缓存）；文件缺失 → 404「Word file not found」；`chapter_index < 0 或 >= len(chapters)` → 400「章节索引超出范围」（此处**有负数检查**）；`parser.extract_chapter_text(paragraphs, chapter)` 提取正文。
- **输出**：`{"thesis_id", "chapter_index", "title", "text"}`。
- **副作用**：文件只读；无 DB 写入。

### 3.10 `POST /{thesis_id}/analyze` — `analyze_thesis(thesis_id, request: ThesisAnalyzeRequest = ThesisAnalyzeRequest(), db)`

AI 章节评审。

- **输入**：`ThesisAnalyzeRequest{chapter_index: Optional[int] = None}`（默认实例作缺省——**不传 body 也可调用**）。
- **行为**：
  1. 404（thesis / docx 文件）；重新解析 docx；
  2. 章节选择：指定 `chapter_index` 时 **`>= len(chapters)` → 400，但不检查负数**（负数按 Python 负索引取倒数第 N 章——与 §3.9 不一致的怪癖）；未指定时**默认第一章**（通常是绪论），无章节时 `chapter_text=""`、`chapter_title=thesis.title`；
  3. 文本截断 6000 字符；`strip` 后 < 30 字符 → 400「章节内容为空或过短，无法生成评审意见」；
  4. 调 `llm_service.chat_completion(messages)`（system 写死「医学图像分析和深度学习领域」，prompt 要求四段式 Markdown 评审）；
  5. 返回 `ThesisAnalyzeResponse{thesis_id, chapter_title, suggestions, citations}`；citations 按 `chapter_index` 过滤（未指定时为全论文引用）。
- **异常（已修复 Batch7b-F8，宪法第 13 条）**：LLM 调用抛异常，或返回串以 `[调用 LLM 出错` 开头（llm_service 错误格式化产物，`_format_error` 兜底透传异常原文）→ 均 500 通用文案「AI 评审失败，请稍后再试」，原文仅入日志；修复前抛异常路 detail 直给 `f"AI 评审调用失败: {e}"`，错误串路则以 200 把错误串带进 `suggestions` 字段。响应序列化失败 → 500。
- **副作用**：一次 LLM 调用；**不写库、不写文件**（评审意见不落盘，前端刷新即失）。

### 3.11 `POST /{thesis_id}/suggest-citations` — `suggest_citations(thesis_id, request: ThesisSuggestRequest, db)`

段落级引用推荐。

- **输入**：JSON body `ThesisSuggestRequest{paragraph}`；入口 trim，纯空白或超过 20,000 字符返回 422，正文不进入 URL/访问日志。
- **行为**：404（thesis）；经生产 `RetrievalPipeline` 的 shared hybrid、top_k=5、filters={} 取候选，语义不可用时允许同范围关键词降级；最终零候选则**跳过 LLM**并返回明确的本地证据不足提示。非空候选拼 prompt 要求推荐 3–5 篇，再调 `llm_service.chat_completion`。
- **输出**：`{"thesis_id", "suggestions"（LLM Markdown 或零证据提示）, "citations": retrieved}`。
- **异常（已修复 Batch7b-F8，宪法第 13 条）**：LLM 调用抛异常，或返回串以 `[调用 LLM 出错` 开头 → 均 500 通用文案「引用推荐失败，请稍后再试」，原文仅入日志；修复前抛异常路由全局异常处理器兜底 500（文案不含路由语义），错误串路则以 200 把错误串带进 `suggestions` 字段。
- **注意**：`paragraph` 仅用于检索与 prompt，**不校验它是否真属于该 thesis**；thesis_id 只作存在性检查与回填。

## 4. 边界条件与错误处理

| 场景 | 期望行为 |
|------|----------|
| 上传非 .docx / 无文件名 | 400；不写盘 |
| 声明或实际大小 > 50MB | 413；清理残留文件 |
| 同名 docx 重复上传 | 改名 `_1/_2`，各自独立建档，无去重 |
| 损坏 docx（如假 PK 头） | `DocxParser.parse` 抛异常 → 500（脱敏）；文件残留磁盘、无 DB 记录 |
| docx 无章节结构 | `chapter_structure=[]` 正常建档；`analyze` 默认章退化为 `chapter_text=""` → 400 文本过短 |
| 数字引用 `[1]` | 自动匹配恒落空（paper_id=None），等手动关联（§3.7） |
| 中文作者引用 `(张三等, 2024)` | 正则不匹配，恒落空 |
| 引用落在所有章节范围外 | `chapter_index=None`；计入 total，不进任何章的 citation-map |
| `chapter_index` 负数 | `/chapters/{i}/text` 400；`/analyze` **放行**（负索引取倒数章，怪癖） |
| 章节文本 < 30 字符 | `/analyze` 400 |
| LLM 评审/引用推荐失败 | 500 通用文案（「AI 评审失败，请稍后再试」/「引用推荐失败，请稍后再试」），异常原文仅入日志；LLM 错误串不再以 200 混入 `suggestions`（已修复 Batch7b-F8） |
| 向量库不可用 | `/suggest-citations` 先尝试关键词降级；最终零候选则 200、`citations=[]` 且不调用 LLM |
| 取消引用关联 | `PUT citations/{id}` 传 `{"paper_id": null}` → 解除；传不存在的 paper_id → 404 |
| 删除 thesis 后 | citations 一并删除；`paper_stats` 的 citation_graph 自动消失（按现存记录生成） |

## 5. 依赖

- **上游依赖**：`app.database.get_db`、`app.models`（ThesisFile/ThesisCitation/Paper）、`app.schemas`、`services/docx_parser.DocxParser`、`services/llm.llm_service`（宪法第 8 条唯一入口）、`services.retrieval_pipeline.RetrievalPipeline` + `services/retrieval.get_vector_store`（函数内惰性 import）、`aiofiles`、`app.core.logger`。
- **下游消费者**：前端 ThesisList / ThesisDetail / WritingDesk 页面；`routers/papers.py` 的 `/stats/overview` 读取本模块写入的 `thesis_citations` 生成引用关系图。

## 6. 验收标准（可测试）

- [ ] AC1：上传 > 50MB（monkeypatch 阈值）返回 413 且无残留；非 .docx 返回 400
- [ ] AC2：mock `DocxParser.parse` 上传成功：返回 title/chapter_structure/word_count，文件落盘，`metadata_json.citations_detected` 等于引用数
- [ ] AC3：mock parse 返回含作者-年份引用（如 `(Zhou et al., 2024)`）且库中存在 year=2024、authors 含 Zhou 的 paper 时，落库的 ThesisCitation.paper_id 指向它；`[1]` 数字引用恒为 None
- [ ] AC4：同名二次上传生成 `_1.docx` 与独立 ThesisFile 记录
- [ ] AC5：`PUT citations/{id}`：`paper_id` 指向不存在 paper → 404；跨 thesis_id 的 citation → 404；`paper_id=null` 成功解除关联
- [ ] AC6：citation-map：章内引用聚合计数正确、`chapter_index=None` 的引用不进任何章但计入 total、matched 计数只算 paper_id 非空
- [ ] AC7：`/chapters/{i}/text` 越界（含负数）→ 400；`/analyze` 负数索引按负索引语义取章（锁定现状怪癖）或显式拒绝——二选一并固化
- [ ] AC8：`/analyze`：mock LLM 返回评审文本 → 200 且 citations 按章过滤；章节文本 < 30 字符 → 400；LLM 抛异常 → 500
- [x] AC9：`/suggest-citations`：共享 hybrid 候选字段/顺序正确；最终零证据时 200、`citations=[]` 且 LLM 调用次数为 0

## 7. 现有测试覆盖与盲区

- **已覆盖**：`backend/tests/test_upload.py` 覆盖上传门禁与成功路径；`tests/test_routes_sanitize.py` 覆盖 analyze/suggest 的输入校验、异常脱敏、共享检索和零证据；引用映射等其余端点仍有下列盲区。
- **盲区**：
  - **高**：引用自动匹配 `find_paper_by_citation`（数字引用落空、作者-年份子串匹配、年份过滤、多命中不确定性）无测试（AC3）——引用治理的核心正确性
  - **高**：~~`/analyze` LLM 失败路径~~ 与 ~~`/suggest-citations` LLM 异常传播/零证据生成~~ **已修复**：`tests/test_routes_sanitize.py::TestAnalyzeSanitize` / `TestSuggestCitationsSanitize` 固化异常脱敏、JSON body 校验、共享 hybrid 候选与零证据不调用 LLM；`/analyze` 的章节选择/30 字符下限/6000 截断/citations 按章过滤其余链路仍无测试（AC8 部分）
  - **中**：citation-map 聚合语义（chapter_index=None 排除、matched 计数、paper_titles 预加载）无测试（AC6）
  - **中**：引用手动关联/解除（双条件 404、paper 校验）无测试（AC5）
  - **中**：同名 `_1/_2` 改名与重复上传不去重无测试（AC4）
  - **中**：`DELETE /{thesis_id}` 的 citations 级联与文件清理无测试
  - **低**：`/chapters/{i}/text` 越界 400 与「每次重解析无缓存」行为无测试（AC7）
  - **低**：`/analyze` 负数索引怪癖与 `/chapters` 负数拒绝的不一致无测试、无防护
  - **低**：损坏 docx 的「500 + 文件残留」语义无测试
  - **低**：无响应 model，`citations` 字段仍依赖检索管线字典结构

## 8. 关键设计决策

- **`_save_upload_file` 独立副本而非共享**：注释明示「两处独立，避免路由间互相依赖」——路由层保持扁平，宁可重复 30 行也不抽公共模块；修订上传安全逻辑时必须**同步两处**（papers.py / thesis.py）。
- **上传即解析、无后台任务**：docx 解析远快于 PDF 向量化（秒级），请求内同步完成换「上传结果立即可见」；代价是大文件会阻塞该请求数秒。
- **数字引用放弃自动匹配**：`[N]` 序号是论文参考文献列表的局部编号，与 paper.id 无对应关系，自动匹配必然张冠李戴——宁可全部落空交给用户手动关联（§3.7 配套入口），也不做错误关联。
- **作者-年份用子串匹配**：实现最简单（不解析 authors 结构），代价是中文作者不匹配、短姓氏误中长姓氏；单用户库规模小，误配可由手动关联修正。
- **章节正文/analyze 每次重解析 docx**：无缓存换实现简单与「文件被外部替换后行为一致」；docx 解析开销可接受。
- **评审意见不落盘**：定位为即时辅助，历史评审无管理需求；citations 随响应回传供前端并排展示。
- **`ThesisAnalyzeRequest()` 默认实例作参数缺省**：让「不传 body 分析绪论」成为最简调用路径；FastAPI 对缺省 body 模型按可选处理（现状可用，升级 FastAPI 时需回归验证）。
- **~~LLM 失败 detail 透传异常原文~~ 已统一脱敏（Batch7b-F8）**：`/analyze`、`/suggest-citations` 与 papers `/summarize` 同按宪法第 13 条收口——LLM 抛异常与 `[调用 LLM 出错` 错误串两路均改通用文案（原文仅入日志）；papers `/process` 的 `f"处理失败: {e}"` 仍为脱敏例外（归 processor.md §3.4，后续批次处理）。
