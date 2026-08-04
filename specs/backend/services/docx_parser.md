# docx_parser 模块规格说明书

> 本文件描述 `backend/app/services/docx_parser.py`（`DocxParser` 类）的**行为契约**。
> 依据 2026-08-04 时点的源码反向工程整理；规格与代码冲突时以代码为准并立即修订本文件。

## 1. 背景与目标

`DocxParser` 是「大论文（毕业论文）辅助写作」链路的解析入口，负责把用户上传的 Word 版学位论文解析为结构化数据：论文章节结构（标题、级别、段落区间）、全文字数、正文引用标记（`[1]`、`(Zhou et al., 2024)`）。产出供 `thesis` 路由落库（`thesis_files.chapter_structure`、`thesis_citations`）及按章节取文本送 LLM 评审。

设计取向是**启发式 + 样式双轨**：既识别 Word 标准标题样式（Heading 1–4 / 标题 1–4），也识别中文学位论文常见的编号文本（`第X章`、`1.1`、`1.1.1`），以适配样式不规范的真实论文文档。

## 2. 范围

### 2.1 包含

- python-docx 段落级提取：文本、样式名、主要字号（段内最大 run 字号）、标题判定；
- 目录/页眉页脚/题注等噪声样式过滤（`IGNORE_STYLES`）；
- 论文章节结构识别：标题行 → `{title, level, start_paragraph, end_paragraph}` 扁平列表；
- 封面论文标题提取（字号优先 + 文本特征兜底双策略）；
- 中文字数统计；
- 数字括号型 `(1)` / 作者年型引用标记检测及章节归属；
- 按章节区间取全文。

### 2.2 非目标

- **不提取 Word 表格**（`doc.tables` 完全不读；表格内的文字、引用、字数均不进入任何输出）；
- 不提取文本框、脚注/尾注、批注、图片、页眉页脚部件（`doc.paragraphs` 本身不含这些）；
- 不支持 `.doc`（OLE 复合文档）格式，仅支持 `.docx`；
- 不校验章节层级父子嵌套关系（输出为扁平列表，`level` 仅标注）；
- 不做引用与文献库的智能匹配（归 `routers/thesis.py::find_paper_by_citation`）；
- 不写数据库/文件（纯读取；落库由路由层负责）。

## 3. 行为契约

### 3.1 `def parse(self, file_path: str) -> Dict[str, Any]:`

一次性完整解析，返回固定 5 键字典。

- **输入**：`file_path: str`，本地 `.docx` 路径。
- **输出**：`Dict[str, Any]`：
  - `title: Optional[str]`——封面标题（提取失败为 `None`，路由层兜底为文件名 stem）；
  - `paragraphs: List[Dict]`——每项 `{index, text, style, font_size, is_heading}`：
    - `index` 是**原始 `doc.paragraphs` 枚举序号**（被跳过的空段/噪声段不占位，故序号有空洞但不重排）；
    - `text` 已 `strip()`，空段被剔除；
    - `style` 为样式名，无样式时为 `"Normal"`；命中 `IGNORE_STYLES`（`toc 1–9`、`Table of Contents`、`Header`、`Footer`、`Caption`、`题注`，大小写不敏感）的段被剔除；
    - `font_size` 为段内显式字号的最大值（pt，float），无任何显式字号时为 `None`；
    - `is_heading` 判定规则：样式名含 `Heading` 或 `标题`；或文本匹配 `^第[一二三四五六七八九十0-9]+章`；或匹配 `^\d+(\.\d+)+\s+`（**要求编号后必须紧跟空白**，`1.1.1 概述` 是标题、`1.1.1概述` 不是）。
  - `chapters: List[Dict]`——扁平章节列表，每项 `{title, level, start_paragraph, end_paragraph}`：
    - 仅由 `is_heading` 段生成；`title` 经 `_clean_heading_text` 清理（去掉制表符后页码、尾部 `... 7` / 尾部数字）；
    - `level` 判定：`第X章` → 1（**先于样式判断**，`第1章` 即使是 Heading 2 样式也记 1）；`Heading/标题 1–4` → 1–4；数字编号 `1.1.1.1`/`1.1.1`/`1.1` → 4/3/2；其余标题一律 → 2；
    - 文本恰为 `目录 / 目  录 / 目 录 / Table of Contents / Contents` 的标题被剔除；
    - 区间：`end_paragraph` = 下一标题的 `index − 1`，最后一个章节 = 最后一个保留段的 `index`；章节间允许间隙（被剔除段不属于任何章节）。
  - `word_count: int`——全部保留段以 `\n` 拼接后**去除空格的字符数**（CJK 友好；**换行符也计入**，每段约多计 1）。
  - `citations: List[Dict]`——见 3.3。
- **前置条件**：文件是合法 `.docx`。
- **后置条件**：返回结构固定；`paragraphs/chapters/citations` 可为空列表；无任何写入。
- **副作用**：文件只读；无日志。
- **异常**：**不捕获任何异常**。文件不存在/损坏/非 docx（如 `.doc`、伪 zip）时 `Document(file_path)` 抛 `PackageNotFoundError`/`BadZipFile` 等向上传播。当前 `routers/thesis.py` 上传端点未对其 try/except——损坏文件会得到全局兜底的 500，且已落盘的上传文件不清理。

### 3.2 `def extract_chapter_text(self, paragraphs: List[Dict[str, Any]], chapter: Dict[str, Any]) -> str:`

- **输入**：`paragraphs`（3.1 的产出）、`chapter`（3.1 产出的单个章节，含 `start_paragraph/end_paragraph`）。
- **输出**：`str`——`index` 落在闭区间内的段落文本以 `"\n"` 拼接；区间内无段落返回 `""`。
- **前置条件**：`paragraphs` 与 `chapter` 来自**同一次** `parse`（序号体系一致；跨次解析因文档未变动时亦一致，路由层每次请求都重新 `parse` 保证对齐）。
- **后置条件/副作用/异常**：纯函数；无异常（`chapter` 缺键则抛 `KeyError`）。

### 3.3 引用检测（`_extract_citations`，`parse` 内部行为）

对每个保留段（**含标题段**）执行两类正则，每个命中生成一条 `{paragraph_index, citation_text, raw_numbers, context, chapter_index}`：

- 数字括号型：`\[(\d+(?:\s*[,-]\s*\d+)*)\]`——`[1]`、`[1,2]`、`[1-3]`、`[1, 2-3]` 均命中，`raw_numbers` 为括号内原文；
- 作者年型：`\(([A-Z][a-zA-Z\s,\.]+(?:et al\.)?,\s*\d{4}[a-z]?)\)`——`(Zhou et al., 2024)`、`(Zhang and Li, 2023)` 命中；**要求大写字母开头且含逗号 + 四位年份**，中文作者年型（`（张三等, 2024）`）、无括号年份、四位以上年份均不命中；此型 `raw_numbers` 字段实际装的是作者+年份文本（命名沿用，语义偏差）；
- `context` 为段落全文（未截断）；`chapter_index` 为首个满足 `start ≤ index ≤ end` 的章节下标，无归属为 `None`；
- 同一段落多种标记按「先全部数字型、后全部作者年型」的顺序输出。

### 3.4 标题提取（`_extract_title`，`parse` 内部行为）

- **策略 1（字号优先）**：扫描前 30 个保留段，跳过含噪声词（`作者/学院/指导教师/学位论文/论文题目/摘要/本文/本章/University/Thesis/By/...`，子串匹配）的段落；取 `font_size ≥ 16` 的段，**原始序号相邻的连续段合并**为一个候选；最终候选中长度 ∈ [12, 120] 的最长者胜出。
- **策略 2（文本特征兜底）**：策略 1 无结果时，扫描前 25 个保留段，跳过含噪声词或任何 `: ： / \ 。 ！ ？ . , ， =` 的段落，取长度 ∈ [15, 80] 且样式 ∈ (`Normal`, `Title`, `标题`) 的段落，最长者胜出。
- 均无结果返回 `None`。

## 4. 边界条件与错误处理

| 场景 | 期望行为 |
|------|----------|
| 文件不存在 / 损坏 / 非 docx（含 `.doc`、伪 zip） | `Document()` 抛异常向上传播；上传端点表现为全局兜底 500，已写盘文件残留 |
| 空文档 / 全空段 | 正常返回：`paragraphs=[]`、`chapters=[]`、`word_count=0`、`citations=[]`、`title=None` |
| 全文无标题样式与编号 | `chapters=[]`；章节文本/分析接口因 `chapters` 为空分别返回 400 或按 `chapter=None` 走空文本分支（路由层行为） |
| 样式不规范（标题用 Normal + 手打编号） | 依赖文本编号正则识别；编号后无空白的（`1.1概述`）不识别 |
| 标题样式层级与文本编号冲突 | 文本 `第X章` 优先记 level 1；其余按样式，数字编号兜底 |
| 目录页 | 样式命中 `toc *` 的条目直接剔除；误入的 `目录/Contents` 标题按文本剔除 |
| 含大量表格的论文 | 表格内容完全不进入 paragraphs/word_count/citations/chapters（结构性缺失，非异常） |
| 字号全部继承样式（run 无显式字号） | `font_size=None` → 策略 1 标题提取失效，依赖策略 2 |
| 引用在表格/文本框中 | 不检测（不读取这些部件） |
| 同一引用多次出现 | 每次出现各记一条（不去重） |

## 5. 依赖

- **上游依赖**：`python-docx 1.1`（`Document`）。无 logger、无配置项、无 LLM。
- **下游消费者**（均在 `routers/thesis.py`）：
  - `POST /api/thesis/upload`：`parse` → 落 `thesis_files`（title/chapter_structure/word_count）+ `thesis_citations`；
  - `GET /api/thesis/{id}/chapters/{idx}/text`：重新 `parse` + `extract_chapter_text`；
  - `POST /api/thesis/{id}/analyze`：重新 `parse` + `extract_chapter_text`（截断 6000 字送 LLM 评审）。

## 6. 验收标准（可测试）

- [ ] AC1：构造含 `Heading 1`/`标题 2`/`第1章`/`1.1 概述`（编号后带空格）段落的 docx，`chapters` 级别分别为 1/2/1/2，`1.1概述`（无空格）不识别为章节。
- [ ] AC2：`toc 1`/`Caption` 样式段落不出现在 `paragraphs`；文本恰为 `目录` 的标题不出现在 `chapters`。
- [ ] AC3：章节区间闭合——第 N 章 `end_paragraph` = 第 N+1 章 `start_paragraph − 1`；末章 `end_paragraph` = 最后保留段 `index`；`extract_chapter_text` 恰好覆盖区间内全部段落。
- [ ] AC4：封面含 ≥16pt 标题段时 `title` 取之（连续相邻段合并）；无显式字号时回退策略 2；均无则 `None`。
- [ ] AC5：`word_count` = 全部保留段拼接后去空格字符数（含换行符）；空文档为 0。
- [ ] AC6：`[1]`、`[1,2]`、`[1-3]` 各生成一条数字型引用；`(Zhou et al., 2024)` 生成作者年型引用；`（张三等, 2024）` 不命中；引用的 `chapter_index` 与段落所属章节一致，文前部分为 `None`。
- [ ] AC7：损坏 docx 调用 `parse` 抛异常（契约：不静默吞错）。
- [ ] AC8：含表格的 docx，表格文字不出现在 `paragraphs`/`word_count`/`citations` 中。

## 7. 现有测试覆盖与盲区

- **已覆盖**：无真实覆盖。`backend/tests/test_upload.py` 的 3 个 thesis 用例将 `DocxParser.parse` 整体 mock（`thesis_env` 夹具），仅验证上传链路的大小限制/扩展名/落库字段；`conftest.py` 无 parser 相关夹具。
- **盲区**：
  - 章节识别全部规则（样式判定、`第X章` 优先、数字编号尾随空白要求、目录剔除、区间闭合）——**高**（大论文章节识别是本模块存在价值，零测试）；
  - 标题提取双策略（≥16pt 合并、噪声词、长度窗口、策略回退）——**中**；
  - 引用检测两型正则与 `chapter_index` 归属、重复计数——**中**；
  - 损坏/非 docx 文件的异常传播与上传 500 + 文件残留——**中**；
  - 表格/文本框内容结构性缺失、`word_count` 含换行符——**低**；
  - `IGNORE_STYLES` 大小写不敏感匹配——**低**。

## 8. 关键设计决策

- **每次请求重新解析**：章节文本/评审接口不落库段落本体，而是实时重跑 `parse` 再用上传时存的章节下标对齐——实现简单、无 schema 演进（符合宪法第 9 条），代价是每次 O(文档) 解析与「文件被替换后下标错位」的隐含假设。
- **章节用扁平列表 + 原始段落序号区间**：不构建树、不做层级校验；`paragraphs` 保留原始 `doc.paragraphs` 枚举序号（含空洞），使 `start/end_paragraph` 与 `extract_chapter_text` 天然对齐，也让「被剔除段不属于任何章节」的语义显式化。
- **样式 + 中文编号双轨启发式**：真实学位论文常用 Normal 样式手打 `第1章`、`1.1` 编号，仅靠 Heading 样式会漏掉整章；故 `第X章` 判定先于样式、编号正则要求尾随空白以降低正文误伤。已知取舍：对 `1.1概述` 这类无空格编号漏检。
- **表格完全不读**：`doc.paragraphs` 不含表格，代码也未遍历 `doc.tables`——三线表数据、表内引用均不可见。对大论文属已知盲区（数据表不进评审上下文），如未来需要须同时改 `parse` 输出契约与路由落库。
- **`word_count` 用「去空格字符数」**：中文字数统计惯例（Word「字数」口径近似）；实现上将 `\n` 也计入，每段多计约 1 字符，量大时偏差 <1%，不修正。
- **不捕获解析异常**：与 `PDFParser.parse_metadata` 的静默兜底相反，docx 解析失败直接向上抛——上传链路依赖全局异常脱敏（宪法第 13 条）返回通用 500；已落盘文件残留属已知小瑕疵。
