# eval.generate_qa LLM 辅助 QA 候选集生成 规格说明书

## 1. 背景与目标

RAG 评测种子集（`eval/dataset/qa_seed.jsonl`，25 条）全部基于 1 篇示例论文手工编写，随真实论文入库需要扩充到 50–100 条。纯手工编写成本高，故用 LLM（经 `llm_service`，Kimi，json_mode）基于论文原文自动生成候选 QA，再通过**严格校验 + 人工审稿门禁**保证质量：机器负责「出题 + 自证答案出处」，人负责终审合并。

核心质量闸：LLM 必须给出从原文**逐字复制**的英文摘录（excerpts/locators），校验端用大小写不敏感、空白折叠的包含匹配确认摘录真实命中该论文 chunk 语料；未命中的条目整条丢弃。产出物带 `source="llm_generated"` / `reviewed=false` 审稿标记，**不能直接进评测**，人工审稿通过后才能改为 `imported_paper` 合并进种子集。

## 2. 范围

### 2.1 包含

- 素材构造：论文标题 + 摘要 + 前若干 chunk，受字符预算（默认 9000）控制。
- Prompt 构造：单篇 QA 生成（system + user 模板）与跨论文 comparison 生成（含 locators 结构）。
- LLM 输出解析：`parse_llm_json` 容错 ```json 围栏与首尾杂质，半截 JSON 必须报错。
- 条目校验与构造：schema 过滤、`ALLOWED_TYPES` 白名单（拒绝 `out_of_scope`）、摘录逐字命中校验、`relevant_chunks`（keywords 定位）生成、`qa_id` 编号。
- 单篇生成 `generate_for_paper` 与跨论文生成 `generate_cross_paper`：失败自动重试、全部失败降级为空、绝不抛异常、绝不写半截结果。
- 全量入口 `generate_all`：逐篇生成、**逐篇落盘（每篇后 flush）**、单篇失败不阻塞、跨论文题追加、`--resume` 断点续跑。
- CLI：`python -m eval.generate_qa`（`--paper-ids / --per-paper / --output / --max-attempts / --retry-sleep / --material-chars / --no-cross-paper / --cross-n / --dry-run / --resume`）。
- 审稿门禁辅助：`normalize_for_validation` 模拟人工合并动作，供测试断言 schema 兼容。

### 2.2 非目标

- 不生成负例（`out_of_scope`）：负例需人工构造，LLM 生成的 `out_of_scope` 条目一律过滤。
- 不做人工审稿本身：不修改种子集 `qa_seed.jsonl`，只产出候选集 `qa_candidates.jsonl`。
- 不评测生成质量：候选集未经人工改 source 前，`validate_dataset` 不接受（`SOURCES` 不含 `llm_generated`），`eval.run` 不会消费它。
- 不修改数据库：对 SQLite 只读（Paper / Chunk 查询）。
- 不做并发/批量 API 优化：逐篇串行调用，429 限流靠 `retry_sleep` 与 `--resume` 人工重跑对冲。
- 非增量去重：不检查「同内容问题是否已存在于种子集」，去重属于人工审稿环节。

## 3. 行为契约

### 3.1 模块常量

- `DEFAULT_OUTPUT_PATH = Path(__file__).resolve().parent / "dataset" / "qa_candidates.jsonl"`
- `DEFAULT_PAPER_IDS = list(range(4, 20))`（id=1 已被种子集覆盖，2/3 为早期导入，跳过）
- `MATERIAL_CHAR_BUDGET = 9000`（每篇送 LLM 的素材字符预算；**`generate_all` 会用 CLI 值覆写此全局变量**）
- `ALLOWED_TYPES = {"method_detail", "experiment_data", "factoid", "summary", "comparison"}`
- `CANDIDATE_SOURCE = "llm_generated"`

### 3.2 `def build_material(paper: Any, chunks: List[Any], budget: Optional[int] = None) -> str:`

- **输入**：Paper ORM 对象（用 `title` / `abstract`）、按 `chunk_index` 升序的 Chunk 列表、字符预算（`None` 时用全局 `MATERIAL_CHAR_BUDGET`）。
- **输出**：素材文本：`论文标题: <标题或(无标题)>` + 可选 `摘要: <abstract>` + 各 chunk 内容，段间 `\n\n` 连接。
- **后置条件**：跳过空白 chunk；逐个追加时若 `已用 + 当前chunk长度 > budget` 则**停止**（后续 chunk 不再追加，不截断单个 chunk）；标题与摘要不计超预算（先放入再累计）。
- **副作用**：无（纯函数）。

### 3.3 `def build_messages(title: str, material: str, n: int) -> List[Dict[str, str]]:` 与 `def build_cross_messages(overviews: str, n_papers: int, n: int) -> List[Dict[str, str]]:`

- **输出**：`[{"role": "system", ...}, {"role": "user", ...}]` 两条消息。
- **单篇 prompt 契约**（`_USER_PROMPT_TEMPLATE`）：素材包裹于 `---论文内容开始/结束---`；要求输出 `n` 条 QA；`question_type` 限 `method_detail / experiment_data / factoid / summary`（前两者优先，`factoid`、`summary` 各至多 1 条；仅当内容明确对比多种方法时才可用 `comparison`）；`ground_truth` 为「、」分隔的短小关键短语、≤120 字；`excerpts` 为 2~3 个**逐字原样复制**的英文片段（每段 3~10 词）；只输出 `{"items": [{question, question_type, ground_truth, excerpts}]}`。
- **跨论文 prompt 契约**（`_CROSS_USER_TEMPLATE`）：每题对比恰好两篇论文；`question_type` 恒为 `comparison`；`ground_truth` ≤150 字；`locators` 对每篇涉论文给 `paper_id` 与 2 个逐字英文片段；只输出 `{"items": [{question, question_type, ground_truth, locators: [{paper_id, excerpts}, ...]}]}`。
- **副作用**：无（纯函数）。

### 3.4 `def parse_llm_json(text: str) -> Any:`

- **输入**：LLM 原始输出文本。
- **输出**：解析后的 JSON 值（dict 或 list）。
- **解析规则**：空串/全空白 → `ValueError("LLM 返回为空")`；先去一处 ``` 或 ```json 代码围栏（正则 ```(?:json)?\s*(.*?)```，DOTALL）；再截取第一个 `{`/`[` 到最后一个 `}`/`]` 之间的子串做 `json.loads`（丢弃首尾杂质）；找不到起始符、起止错位、`json.JSONDecodeError` 一律转抛 `ValueError`。
- **核心保证**：**半截 JSON 必须报错**（由调用方重试），绝不静默通过；`llm_service` 的错误串（如 `[调用 LLM 出错: ...]`）因无 JSON 起始符而报 `ValueError`。
- **副作用**：无。

### 3.5 `def _verify_excerpts(excerpts: Any, corpus_norm: str, max_keep: int = 3) -> List[str]:`（模块私有，行为关键）

- **输入**：LLM 给的摘录列表、已归一化（`_norm`：小写化 + 连续空白折叠为单空格 + strip）的论文语料。
- **输出**：逐字命中原文的摘录列表（**保留 LLM 原始大小写**）。
- **过滤规则**：非 list 输入返回 `[]`；非 str 元素跳过；strip 后长度 < 8 跳过（太短无定位价值）；归一化后是 `corpus_norm` 的子串才保留；结果去重；最多保留 `max_keep`（3）条。
- **副作用**：无。

### 3.6 `def build_items_from_payload(payload: Any, paper_id: int, corpus_norm: str, qa_id_prefix: str, start_seq: int = 1) -> List[dict]:`

- **输入**：`parse_llm_json` 的结果、论文 id、归一化语料、qa_id 前缀、起始序号。
- **输出**：候选条目列表（可为空）。条目提取（`_extract_items`）兼容三种形态：`{"items": [...]}`、顶层 list、dict 中第一个 list 类型值；非 dict 元素丢弃。
- **过滤规则**（任一不满足即丢弃该条）：`question` / `ground_truth` 为非空字符串；`question_type ∈ ALLOWED_TYPES`（`out_of_scope` 被拒绝）；经 3.5 校验后至少 1 条摘录命中。
- **产出条目结构**（与种子集 schema 对齐 + 审稿标记）：
  ```json
  {"qa_id": "<prefix>-<seq:03d>", "question": "...", "ground_truth": "...",
   "relevant_chunks": [{"paper_id": <int>, "keywords": [<命中摘录原文>]}],
   "question_type": "...", "source": "llm_generated", "has_answer": true, "reviewed": false}
  ```
  `qa_id` 从 `start_seq` 起按合格条目顺序编号（被过滤条目不占号）。
- **副作用**：无（纯函数）。

### 3.7 `def normalize_for_validation(item: dict) -> dict:`

- **语义**：模拟人工审稿合并动作——移除 `reviewed` 键、`source` 改为 `imported_paper`，其余字段原样保留。仅供测试断言「除审稿标记外 schema 与种子集一致」（合并动作本身由人手工完成）。

### 3.8 `def generate_for_paper(paper: Any, chunks: List[Any], per_paper: int = 4, max_attempts: int = 3, call_llm: Optional[Callable[[List[Dict[str, str]]], str]] = None, retry_sleep: float = 0.0) -> Tuple[List[dict], str]:`

- **输入**：Paper 对象、该论文全部 chunk、`per_paper` 目标条数（仅写入 prompt，不作硬校验）、最大尝试次数、LLM 调用注入点（默认 `_call_llm` → `llm_service.chat_completion_sync(messages, json_mode=True)`，延迟导入）、重试前休眠秒数。
- **输出**：`(条目列表, 错误信息)`；成功时错误信息为 `""`。
- **行为**：
  - 语料取**全部 chunk**（非预算截断后的素材）做摘录校验；`qa_id` 前缀 `gen-p{paper.id:02d}`；
  - 每次尝试：`call_llm(messages)` → `parse_llm_json` → `build_items_from_payload`；
  - LLM 抛异常或解析失败 → 记录 `第 {attempt} 次调用/解析失败: {e}` 并重试；解析成功但条目全部被过滤 → 记录 `第 {attempt} 次生成全部被过滤（schema/摘录校验未过）` 并重试；
  - 第 2 次及以后尝试前 `time.sleep(retry_sleep)`（>0 时，应对 429）；
  - 任一尝试产出非空条目即返回；全部失败返回 `([], 最后一次错误)`。
- **核心保证**：**绝不抛异常、绝不返回/写出半截结果**。
- **副作用**：网络调用（默认实现）；`time.sleep`。

### 3.9 `def generate_cross_paper(papers_with_chunks: List[Tuple[Any, List[Any]]], n: int = 4, max_attempts: int = 3, call_llm: Optional[Callable[[List[Dict[str, str]]], str]] = None, retry_sleep: float = 0.0) -> Tuple[List[dict], str]:`

- **输入**：`(Paper, chunks)` 对列表（≥2 才有意义）、目标条数、重试参数、LLM 注入点。
- **输出**：`(comparison 条目列表, 错误信息)`。
- **行为**：
  - 每篇简介 = 标题 + 首个非空 chunk 的前 600 字符，以 `[paper_id=N] 标题\n简介` 拼入 prompt；每篇语料（全部 chunk 归一化）存入 `corpus_by_id`；
  - 条目过滤：`question_type` 必须为 `comparison`；`question` / `ground_truth` 非空；`locators` 中每个定位须 `paper_id` 为 int 且属于输入论文集合、且经 3.5 至少 1 条摘录命中；**合格 locators < 2 时整条丢弃**（跨论文对比必须两篇都能定位）；
  - `qa_id` 为 `gen-cross-{seq:03d}`，`relevant_chunks` 即合格 locators（`{paper_id, keywords}`）；
  - 重试/降级语义与 3.8 相同（异常、解析失败、全部被过滤均重试，全败返回 `([], 错误)`）。
- **副作用**：网络调用；`time.sleep`。

### 3.10 `def generate_all(db: Any, paper_ids: List[int], per_paper: int, output_path: Path, include_cross: bool = True, cross_n: int = 4, max_attempts: int = 3, material_budget: int = MATERIAL_CHAR_BUDGET, retry_sleep: float = 0.0, call_llm: Optional[Callable[[List[Dict[str, str]]], str]] = None, resume: bool = False) -> Dict[str, Any]:`

- **输入**：SQLAlchemy Session、论文 id 列表、每篇目标条数、输出 JSONL 路径、跨论文开关与条数、重试参数、素材预算、LLM 注入点、`resume` 断点续跑开关。
- **输出**：汇总 dict：
  ```python
  {"total": int,                # 本次运行新写入的条目总数（不含 resume 跳过的历史条目）
   "type_counts": {qtype: n},   # 本次新写入条目的题型分布
   "per_paper": [{"paper_id": int, "ok": bool, "n": int, "error": str(仅失败时)}],
   "n_ok": int, "n_fail": int,  # 以 per_paper 计；不存在/无 chunk 被跳过的论文不计入
   "output": str(output_path)}
  ```
- **行为流程**：
  1. **全局副作用**：`MATERIAL_CHAR_BUDGET = material_budget`（修改模块全局变量，影响后续 `build_material` 默认值）；
  2. **断点续跑**（`resume=True` 且输出文件存在）：逐行解析已有 JSONL（坏行跳过），`qa_id` 匹配 `gen-p(\d+)-` 的论文 id 记入 `done_paper_ids`，任一条目 `question_type == "comparison"` 则 `done_cross=True`；有任一完成记录时以**追加模式（"a"）**打开输出并打印 `[gen] 续跑：跳过已完成论文 [...]`；无完成记录时仍为覆盖模式（"w"）；
  3. 逐 id 查询：在 `done_paper_ids` 中→静默跳过；`Paper` 不存在→打印 `[gen] 论文 id=N 不存在，跳过`；无 chunk→打印跳过；**被跳过的论文不进 `per_paper_status`、不算失败**；
  4. `output_path.parent` 自动创建；逐篇调用 3.8：成功→**逐行写 JSONL 并立即 `f.flush()`（逐篇落盘，中途崩溃不丢已完成部分）**，打印生成条数；失败→打印错误并记入 `per_paper`（`ok=False`），**不阻塞后续论文**；
  5. 跨论文题：仅当 `include_cross and not done_cross and len(papers_with_chunks) >= 2` 时调用 3.9；失败仅打印不记状态；成功则同样写入 + flush，`type_counts["comparison"]` 累计。
- **副作用**：数据库只读查询；**写/追加输出 JSONL**；模块全局变量覆写；stdout 进度输出；网络调用。
- **注意**：resume 时 `total` / `type_counts` 只统计**本次新写入**，已跳过的历史条目不重复计数；同一论文的条目要么整批写入要么没有（单篇内不产生半截）。

### 3.11 `def main(argv: Optional[List[str]] = None) -> int:`

- **CLI 参数**：`--paper-ids`（默认 4..19）、`--per-paper`（4）、`--output`（候选集默认路径）、`--max-attempts`（3）、`--retry-sleep`（10.0，0 表示不休眠）、`--material-chars`（9000）、`--no-cross-paper`、`--cross-n`（4）、`--dry-run`、`--resume`。
- **`--dry-run`**：不调用 LLM，仅连接数据库逐篇打印 `[dry-run] id=N chunks=M 素材字符数=L 标题《...》`（不存在的论文打印「不存在」），返回 0。
- **正常路径**：`SessionLocal()` 连接真实 SQLite（只读）→ `generate_all` → 打印生成汇总（成功/失败篇数、候选总数、题型分布、输出路径、耗时）与**审稿提示**（候选集带 `llm_generated` 标记，人工审稿后合并时改 `imported_paper`）。
- **退出码**：`total > 0` 返回 0；**total == 0（全部失败或无产出）返回 1**，供脚本化调用方感知。

## 4. 边界条件与错误处理

| 场景 | 期望行为 |
|------|----------|
| LLM 返回 ```json 围栏包裹/首尾有杂质 | 正常解析 |
| LLM 返回半截 JSON / 纯文本 / llm_service 错误串 / 空串 | `parse_llm_json` 抛 `ValueError` → 重试 |
| LLM 调用抛异常（网络、429 等） | 捕获并重试；`retry_sleep>0` 时重试前休眠 |
| 重试 max_attempts 次全失败 | 返回 `([], "第 N 次...")`，不抛异常、不写文件 |
| 条目全部被过滤（schema/摘录/out_of_scope） | 视为本次失败并重试；全败后错误含「全部被过滤」 |
| 摘录未逐字命中原文 / 长度 < 8 / 非字符串 | 该摘录丢弃；条目无有效摘录则整条丢弃 |
| LLM 生成 `out_of_scope` 条目 | 整条过滤（负例不允许 LLM 生成） |
| 跨论文题某篇 locator 摘录未命中 / paper_id 不在输入集合 | 该 locator 丢弃；合格 locator < 2 时整条丢弃 |
| 论文 id 不存在 / 无 chunk | 打印跳过；不算失败、不进 per_paper |
| 单篇生成失败 | 记录失败状态，继续后续论文（不阻塞） |
| 只有 1 篇有效论文 | 不生成跨论文题（需 ≥2 篇） |
| resume 且输出文件已有完成记录 | 跳过已完成论文与跨论文题，追加写入 |
| resume 且输出文件存在但无有效记录 | 覆盖模式重写（不追加） |
| resume 后所有论文均已完成 | 不生成任何条目，`total=0`，`main` 返回 1 |
| 追加模式下 qa_id 序号 | 每篇内部从 1 重新编号；同一 prefix 不会在已完成的论文上复用（该论文整体被跳过） |
| 素材超预算 | 停止追加 chunk（不截断单 chunk）；标题摘要必含 |
| 输出目录不存在 | 自动 `mkdir(parents=True, exist_ok=True)` |
| 全部失败（total=0） | `main` 返回 1 |

## 5. 依赖

- **上游依赖**：`app.models`（`Paper` / `Chunk`，延迟导入）、`app.database.SessionLocal`（仅 CLI 路径）、`app.services.llm.llm_service.chat_completion_sync(json_mode=True)`（经 `_call_llm` 延迟导入，遵守宪法第 8 条 LLM 唯一入口）、`eval.dataset`（schema 对齐目标：`REQUIRED_FIELDS` / `QUESTION_TYPES` / `SOURCES`；测试中用 `validate_dataset` 断言）。
- **下游消费者**：人工审稿流程（候选集 `qa_candidates.jsonl` → 人工改 `source=imported_paper`、去 `reviewed` → 合并进 `qa_seed.jsonl` → `eval.run` 消费）。无代码调用方直接 import 本模块（测试除外）。

## 6. 验收标准（可测试）

- [ ] AC1：`parse_llm_json` 接受裸 JSON、```json 围栏、首尾杂质；对半截 JSON、纯垃圾文本、LLM 错误串抛 `ValueError`。
- [ ] AC2：单篇 happy path 产出条目带 `gen-p{id:02d}-NNN` 编号、`source=llm_generated`、`reviewed=false`、`has_answer=true`，且 `relevant_chunks[0].keywords` 为逐字命中的摘录；经 `normalize_for_validation` 后通过 `validate_dataset`。
- [ ] AC3：JSON 解析失败与 LLM 异常均触发重试，成功时调用次数 = 失败次数 + 1；`max_attempts` 次全败返回 `([], 错误)` 且不抛异常。
- [ ] AC4：摘录未命中原文的条目被整条过滤；`out_of_scope` 条目被过滤；全部被过滤时重试并最终降级为空。
- [ ] AC5：跨论文题要求 ≥2 个合格 locator（两篇均可定位），非 `comparison` 类型被过滤。
- [ ] AC6：`generate_all` 端到端写 JSONL，逐行合法 JSON；不存在的论文跳过且不计失败；单篇失败不阻塞其他论文；汇总 `total / n_ok / n_fail / per_paper` 正确。
- [ ] AC7：成功条目在所属论文完成后立即 flush 落盘（逐篇落盘语义）。
- [ ] AC8（暂无测试，见盲区）：`resume=True` 时跳过已有输出中的论文与跨论文题并以追加模式写入。
- [ ] AC9（暂无测试，见盲区）：`main` 在 `total == 0` 时返回 1，`--dry-run` 不调 LLM 并返回 0。

## 7. 现有测试覆盖与盲区

- **已覆盖**：`backend/tests/test_generate_qa.py`（17 个用例，全部经 `call_llm` 注入 fake，绝不触真实 API）——
  - `parse_llm_json` 六例：裸 JSON / 围栏 / 顶层 list / 半截 / 垃圾 / LLM 错误串（AC1）；
  - `generate_for_paper` 七例：happy path（编号、keywords 定位、schema 兼容）、坏 JSON 重试恰好 2 次、全败降级、异常重试、未命中摘录过滤、`out_of_scope` 过滤、全过滤触发重试（AC2、AC3、AC4）；
  - `generate_cross_paper` 两例：两篇均可定位才保留、非 comparison 过滤（AC5）；
  - `generate_all` 两例：内存库端到端（逐行合法 JSON、id=999 跳过不算失败、汇总正确）、单篇失败不阻塞（AC6）。
- **盲区**：
  - **`--resume` 断点续跑零测试**：跳过已完成论文、`done_cross` 判定、追加模式、续跑后 `total` 只计新增、全部已完成时 `total=0` → 退出码 1——新功能无任何用例 —— **高**。
  - 逐篇落盘 flush 语义（崩溃不丢已完成论文）未用故障注入验证 —— 中。
  - `main` 层未测：`total==0 → 退出码 1`、`--dry-run` 路径、审稿提示输出 —— 中。
  - `build_material` 预算截断（超预算停止追加、空 chunk 跳过、abstract 拼接）未直接测 —— 中。
  - `_verify_excerpts` 边界：长度 < 8 过滤、`max_keep=3` 截断、大小写/空白折叠匹配、去重 —— 低（happy path 隐含覆盖一部分）。
  - 跨论文题在 `generate_all` 端到端的成功路径未覆盖（现有端到端仅 1 篇有效论文不触发；另一例 `include_cross=False`）—— 中。
  - `generate_all` 覆写全局 `MATERIAL_CHAR_BUDGET` 的副作用未测（测试间可能串扰）—— 低。
  - `retry_sleep` 实际休眠行为未测（真实等待不宜入单测，可用 mock `time.sleep` 断言调用次数）—— 低。
  - resume 追加模式下 qa_id 与既有文件冲突的防御（如手工删了部分行）未定义 —— 低。

## 8. 关键设计决策

- **摘录逐字命中作为唯一机器可验的质量锚**：LLM 会编造答案，但「摘录必须逐字存在于原文」是可确定性校验的硬约束；命中后才写入 `relevant_chunks[].keywords`，保证 `eval.dataset.resolve_relevant_chunks` 评测时一定能解析出候选 chunk。校验用全部 chunk 语料（非截断素材），防止 LLM 引用了素材外但真实存在的原文而被误杀。
- **`out_of_scope` 不让 LLM 生成**：负例质量依赖「库中确实无答案」的人工判断，LLM 无从保证，故 `ALLOWED_TYPES` 直接排除。
- **审稿门禁靠 schema 排斥实现**：`dataset.SOURCES` 故意不收录 `llm_generated`，候选集无法被 `validate_dataset` 接受、不会误入评测；合并动作（改 `imported_paper`、去 `reviewed`）必须经人手，`normalize_for_validation` 只是把这一步显式化供测试断言。
- **重试 + 降级而非抛错**：批量跑 16 篇论文遇 429/抖动是常态，单篇失败返回空并记录原因、绝不阻塞批处理；配合 `--retry-sleep`（默认 10s）与 `--resume` 支持「反复重跑直至收敛」的使用模式。
- **逐篇 flush**：单篇全部条目写完立即 `f.flush()`，保证进程被杀时只损失当前这篇的 LLM 调用费用，已落盘论文不重跑——与 `--resume` 的 `gen-p(\d+)-` 前缀解析共同构成断点语义。
- **延迟导入 app 模块**：`app.models` / `app.database` / `app.services.llm` 全部在函数内导入，保证 `import eval.generate_qa` 本身不读配置、不连库、不加载模型（测试可纯内存构造 Paper/Chunk 对象）。
- **`call_llm` 注入点**：默认实现 `_call_llm` 是模块级函数而非直接内联，测试 monkeypatch 注入 fake，遵守宪法第 10 条「不发起真实 LLM 调用」。
- **退出码语义**：`total==0` 返回 1 让脚本化调用方（CI/定时任务）能感知「白跑一趟」，但不细分失败原因（原因在 stdout 日志中）。
