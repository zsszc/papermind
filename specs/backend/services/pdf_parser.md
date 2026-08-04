# pdf_parser 模块规格说明书

> 本文件描述 `backend/app/services/pdf_parser.py`（`PDFParser` 类）的**行为契约**。
> 依据 2026-08-04 时点的源码反向工程整理；规格与代码冲突时以代码为准并立即修订本文件。

## 1. 背景与目标

`PDFParser` 是文献导入链路的第一个环节，负责从本地 PDF 文件中提取两类信息：

1. **元数据**（标题、作者、年份、期刊、摘要、DOI、页数）：供 `papers` 表建档与后续 LLM 增强；
2. **正文文本**（按页结构，含双栏版面处理）：供 `TextChunker` 分块后向量化入库（ChromaDB）。

设计目标是**对任何 PDF 都不阻断导入**：规则解析路径全程容错，提取失败仅记日志并降级到文件名兜底；更精确的元数据交给 LLM 增强路径异步补全。

## 2. 范围

### 2.1 包含

- PyPDF2 XMP 元数据读取（标题/作者/页数/从 Subject+Keywords 找 DOI）；
- PyPDF2 与 pdfplumber 双引擎提取前 3 页文本并启发式推断标题、作者、年份、DOI，打分合并；
- pdfplumber 全文本提取（按页返回页码/宽高/文本），竖版页面自动双栏检测与分栏重排；
- LLM 增强元数据提取（异步，经 `llm_service`）；
- 各类失败（损坏 PDF、加密 PDF、扫描件）的降级行为。

### 2.2 非目标

- 不做 OCR：扫描件（纯图像 PDF）只得到空文本，不引入 OCR 引擎；
- 不提取参考文献列表、图表、表格结构；
- 不写数据库、不写文件：本模块纯读取，落库由 `routers/papers.py` 与 `services/processor.py` 负责；
- 不实现同步版 LLM 增强：`routers/papers.py::_enhance_metadata_with_llm_sync` 是路由层的同步镜像，不在本模块；
- 不负责分块与向量化（归 `embedding.TextChunker` / `retrieval.VectorStore`）。

## 3. 行为契约

### 3.1 `def parse_metadata(self, file_path: str) -> Dict[str, Any]:`

规则路径元数据提取，导入建档时同步调用。

- **输入**：`file_path: str`，本地 PDF 路径。
- **输出**：`Dict[str, Any]`，固定 8 个键：
  `title / authors / year / journal / abstract / doi / pages / filename`。
  - `pages` 初始为 0，其余初始为 `None`；`filename` 恒为 `Path(file_path).name`。
  - **`journal` 与 `abstract` 在本函数中没有任何提取逻辑，返回值恒为 `None`**（见第 8 节）。
- **前置条件**：无（文件不存在/损坏/加密均被内部吞掉，见下）。
- **后置条件**：
  - 不抛异常（两个提取阶段各自 `except Exception` 兜底，仅 `logger.warning`）；
  - `title` 恒非空：所有途径失败时兜底为 `path.stem.replace("_", " ")`；
  - 非空字符串字段（title/authors/journal/abstract/doi）经 `\s+ → 单空格` 折叠并去首尾空白。
- **副作用**：文件只读；失败时写 `logs/app.log`（前缀 `[PDFParser]`）。无 DB/网络。
- **异常**：设计上不向外抛（调用方 `routers/papers.py` 仍另包一层 try/except，失败时记 `{"parse_error": ...}`）。

提取流程（顺序固定）：

1. **PyPDF2 XMP 阶段**：`PdfReader.metadata` 取 `/Title`（或 `Title`）、`/Author`（或 `Author`）；
   `pages = len(reader.pages)`；在 `/Subject` + `/Keywords` 拼接文本中按 DOI 正则找 DOI。整段异常即放弃本阶段。
2. **双引擎前页阶段**：分别用 PyPDF2、pdfplumber 提取前 3 页文本，各自跑 `_extract_from_front_text`
   得到候选 `{doi, year, authors, authors_line_idx, title, title_line_idx}`，再经 `_merge_metadata_candidates` 合并。
   整段异常即放弃本阶段（保留阶段 1 结果）。
3. **合并规则**（`_merge_metadata_candidates`）：
   - DOI、Year：base（XMP）优先，空缺时先 PyPDF2 候选、后 pdfplumber 候选补；
   - Authors：比较两候选逗号分隔的作者数，多者胜（平局取 PyPDF2 候选）；**只要任一候选非空就覆盖 XMP 作者**；
   - Title：候选得分 = `_title_quality(t) − 标题首行行号 × 0.01`，高分者胜；仅当 base 无标题或新标题质量分严格更高时替换。
4. **启发式细节**：
   - `_extract_doi`：正则 `10\.\d{4,9}/[-._;()/:A-Za-z0-9]+`（忽略大小写）取首个匹配；**字符类含 `.`，尾随句点会被一并捕获**（真实样例：`10.1109/TMI.2022.3202759.`）。
   - `_extract_year`：优先 `Copyright/(c) YYYY`；否则取前页文本中第一个 `1900–2099` 的独立四位数（可能误中参考文献年份）。
   - `_infer_title_with_index`：取前页非空行中「10 < 长度 < 300、不含噪声词、非纯数字/标点」的首个候选行；若下一候选行紧邻且首行不以 `. : ? !` 结尾则两行合并。噪声词含 `abstract/arxiv/journal/university/figure/copyright/medical image analysis` 等（子串匹配，可能误伤真实标题）。
   - `_infer_authors_with_index`：先按模式提取（`Authors:` 前缀行；或标题行与 `Abstract/摘要/Introduction/Received/Keywords` 行之间的连续作者行块，多行合并去连字符断词），命中即止；否则用机构邮箱前缀反推（仅 `.edu/.ac./university/institute/hospital/lab/org` 域，去重，至多 10 个）。作者行判定要求 5 < 长度 < 200、不含机构/邮箱/出版商标记、至少 2 个大写开头人名（或过分隔符校验），并剥离上标字母/数字/星号。
   - `_title_quality`：学术指示词每个 +0.5、负面短语（`we propose` 等）每个 −1.0、长度 20–150 加 1.0、以 `The/This/These/In/However/Therefore` 开头 −2.0。

### 3.2 `async def enhance_metadata_with_llm(self, file_path: str) -> Dict[str, Any]:`

LLM 增强路径：`abstract`、`journal` 的唯一来源。

- **输入**：`file_path: str`，本地 PDF 路径。
- **输出**：`Dict[str, Any]`。成功时固定 9 键：
  `title / authors / year(int 或 None) / journal / abstract / doi / authors_list / confidence / source_lines`；
  前页文本为空或 JSON 解析/规整失败时返回 `{}`。
- **前置条件**：`app.services.llm.llm_service` 可用（函数内惰性 import，避免循环依赖）。
- **后置条件**：成功时 `year` 已转为 `int`；各字段空串归一为 `None`；`confidence/source_lines` 缺省为 `{}`。
- **副作用**：发起一次真实 LLM 调用（`llm_service.chat_completion(messages, json_mode=True)`，prompt 文本截断至前 4000 字符）；失败写日志。
- **异常**：
  - `json.loads` 及字段规整（含 `int(data["year"])`）在同一 try 块内，**任一失败整体返回 `{}`**——例如 LLM 把 year 返回成 `"2024a"` 会连 title/abstract 一起丢弃；
  - **`llm_service.chat_completion` 自身抛出的异常（网络/API/超时）不在本函数捕获范围，向调用方传播**（`routers/papers.py` 的两个调用点均有外层兜底）。

### 3.3 `def extract_text(self, file_path: str) -> List[Dict[str, Any]]:`

全文提取，按页返回，供分块向量化。

- **输入**：`file_path: str`。
- **输出**：`List[Dict]`，每页一项：`{"page_number": int（1 起）, "text": str, "width": float, "height": float}`；0 页返回 `[]`。
- **前置条件**：文件可被 pdfplumber 打开。
- **后置条件**：列表长度等于页数，页码连续从 1 开始；扫描件页面 `text` 为空串（不报错）。
- **副作用**：文件只读。**无日志、无异常吞噬**。
- **异常**：`pdfplumber.open` 或页面解析的异常（文件不存在、加密 `PDFPasswordIncorrect`、结构损坏）**原样向上传播**；调用方 `PaperProcessor.process` 不捕获，由 `_process_paper_background` 重试 1 次后将 `paper.processed` 置为 `"error"`。

### 3.4 `def extract_text_full(self, file_path: str) -> str:`

- **输入/副作用/异常**：同 3.3。
- **输出**：全部页面文本以 `"\n\n"` 拼接的单一字符串（页内同样走 3.5 的双栏处理）。

### 3.5 `def _extract_page_text(self, page, x_tolerance: float = 1.5) -> str:`（核心内部行为）

单页文本提取与双栏自动检测，`extract_text / extract_text_full / _extract_front_text_plumber` 共用：

1. 以 `x_tolerance=1.5` 整页提取并 strip；空 → 返回 `""`；
2. **横版或方形页（width ≥ height）或整页文本 < 200 字符** → 直接返回整页文本；
3. 竖版长文本页：按宽度中点裁左右两半分别提取；**任一侧为空视为单栏** → 返回整页文本；
4. 双栏判据：分栏后总行数 ≥ 整页行数 × 1.45（分栏解除了左右拼行，行数显著增加）→ 返回 `左栏 + "\n\n" + 右栏`；否则返回整页文本。

即阅读顺序为「先左栏到底、再右栏」，跨页不拼接。

## 4. 边界条件与错误处理

| 场景 | 期望行为 |
|------|----------|
| 文件不存在 | `parse_metadata`：两阶段均失败 → 返回文件名兜底标题、`pages=0`、其余 None；`extract_text`：`pdfplumber.open` 抛异常向上传播 |
| 损坏/非真实 PDF（如测试用 MINIMAL_PDF） | `parse_metadata` 不抛，返回兜底结果；路由层再容错为 `{"parse_error": ...}`，导入继续 |
| 加密 PDF | 阶段 1/2 各自失败被吞 → 元数据为文件名兜底；`extract_text` 抛异常 → 后台处理重试 1 次后 `processed="error"` |
| 扫描件（无文本层） | `extract_text` 正常返回、每页 `text=""`；`parse_metadata` 全空走文件名兜底；`enhance_metadata_with_llm` 因 `front_text` 为空直接返回 `{}`；下游 chunker 产出 0 个 chunk，`processed` 仍可置 `done` |
| 横版页面 | 不做分栏，整页返回（避免把横向图表页误切） |
| 双栏误检 | 任一侧裁剪文本为空、或分栏行数增幅 < 45% 时回退整页文本 |
| DOI 尾随标点 | 正则字符类含 `.` `;` `()` 等，尾随句点/括号会被带入结果（不清洗） |
| 年份歧义 | 无版权年时取前 3 页首个 1900–2099，可能命中正文/参考文献中的年份 |
| LLM 返回非法 JSON / 字段类型错误 | `enhance_metadata_with_llm` 返回 `{}`（含 year 无法转 int 的情形，全部字段丢弃） |
| LLM 调用本身失败 | 异常向外传播，由调用方兜底；本函数不兜底 |
| 极大 PDF | 无页数/大小防御；`parse_metadata` 只读前 3 页文本（PyPDF2 仍会加载全文件页表），`extract_text` 全量逐页提取，耗时随页数线性增长 |

## 5. 依赖

- **上游依赖**：`pdfplumber 0.10`（文本提取主力）、`PyPDF2`（XMP + 备用前页提取）、`app.core.logger`、`app.services.llm.llm_service`（仅 3.2，惰性 import）。
- **下游消费者**：
  - `routers/papers.py`：`POST /api/papers/import`（3.1）、`POST /api/papers/{id}/extract-metadata`（3.2）、后台线程 `_enhance_paper_metadata`（经同步镜像复用 `_extract_front_text`）；
  - `services/processor.py::PaperProcessor.process`（3.3 → `TextChunker.chunk_pages` → SQLite chunks + ChromaDB）。

## 6. 验收标准（可测试）

- [ ] AC1：对任意损坏/加密/不存在文件，`parse_metadata` 不抛异常，返回 8 键字典且 `title` 非空（文件名兜底）、`pages` 为 0 或真实页数。
- [ ] AC2：`parse_metadata` 对任意输入返回值的 `journal`、`abstract` 恒为 `None`。
- [ ] AC3：构造含 `/Title`、`/Author` XMP 的 PDF，结果 title/authors 取自 XMP；构造前页文本含 `Copyright 2023` 的 PDF，`year=2023`。
- [ ] AC4：双栏竖版页面（左栏文 + 右栏文、行数增幅 ≥45%）提取结果为「左栏全文 + `\n\n` + 右栏全文」；横版页不分栏。
- [ ] AC5：扫描件 PDF `extract_text` 返回与页数等长的列表且每项 `text == ""`；`enhance_metadata_with_llm` 对其返回 `{}`。
- [ ] AC6：`enhance_metadata_with_llm` 在 LLM 返回合法 JSON 时规整出 9 键字典（year 为 int）；返回非法 JSON 或 year 不可转 int 时返回 `{}`；LLM 调用抛异常时异常向外传播。
- [ ] AC7：加密 PDF 调用 `extract_text` 抛出异常（供后台重试并置 `processed="error"`）。
- [ ] AC8：DOI 正则命中 `10.xxxx/...` 首个匹配；无 DOI 文本返回 `None`。

## 7. 现有测试覆盖与盲区

- **已覆盖**：仅 `backend/tests/test_upload.py::test_import_small_pdf_success` 间接经过 `parse_metadata`——对最小坏 PDF 走真实解析并依赖其不抛异常；无针对本模块的单元测试（全库 grep `pdfplumber/PdfReader/parse_metadata/extract_text/chunk_pages` 在 tests/ 下零命中）。
- **盲区**：
  - 双栏检测启发式（200 字符下限、横版排除、1.45 行数阈值、单栏回退）——**高**；
  - 加密 PDF / 扫描件的降级路径（3.1 静默兜底 vs 3.3 异常传播、`processed="error"` 链路）——**高**；
  - 标题/作者启发式与双引擎合并打分（噪声词误伤、作者数择优、行号衰减）——**中**（真实库已见误检：paper 5 作者被识别为标题片段）；
  - DOI/年份正则（版权年优先、首匹配、尾随句点保留）——**中**；
  - `enhance_metadata_with_llm` 的 JSON 失败返回 `{}`、year 转换失败吞全部字段、LLM 异常传播——**中**；
  - XMP 与前页候选冲突时的覆盖优先级（作者候选非空必覆盖 XMP）——**低**。

## 8. 关键设计决策

- **规则路径全程吞异常**：导入体验优先——任何 PDF 都能建档（文件名兜底标题），解析细节失败只写日志；代价是错误对用户不可见，需查 `logs/app.log`。
- **`abstract` / `journal` 规则路径零提取**：`parse_metadata` 从初始化到合并全程不触碰这两个字段，唯一来源是 LLM 增强（3.2 或路由层同步镜像）。**这就是「16 篇真实论文 abstract 全空」的直接根源**：实测 `data/papers.db` 19/19 篇 `abstract` 与 `journal` 全 NULL，且所有 `metadata_json` 均不含 LLM 增强必然写入的 `confidence` / `source_lines` 键——说明 LLM 增强从未成功落库（导入时 LLM 不可用/调用失败/JSON 解析失败均有可能；增强失败仅记日志、不影响 `processed` 状态，用户无感知）。
- **双引擎 + 质量分合并**：PyPDF2 与 pdfplumber 对同一 PDF 的文本序差异很大，各自启发式独立跑再按「作者数 / 标题质量分 − 行号衰减」择优，牺牲确定性换鲁棒性。
- **双栏用行数增幅而非版面分析**：不引入版面模型，用「分栏后行数 ≥ 1.45×」这一廉价信号；已知对三栏、跨栏通栏标题会误判为单栏回退，属可接受损耗。
- **`extract_text` 故意不吞异常**：与 `parse_metadata` 相反——向量化是核心链路，失败必须让上层重试并置 `processed="error"`，而不是静默产出空库。
- **关联发现（chunks.section_title 全 NULL 的根源）**：`processor.py` 以 `cd.get("section_title")` 读取分块结果，但 `embedding.py::TextChunker._make_chunk` 产出的字典**只有 `content / page_number / chunk_type / token_count` 四个键，从不产生 `section_title`**。该列因此是结构性恒 NULL（实测 445/445  chunks 全 NULL），属「字段预留、生产端未实现」，而非数据质量问题；`eval/dataset.py` 已按「section_title 为 NULL 时回退 content 匹配」兼容。
