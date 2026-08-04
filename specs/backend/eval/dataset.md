# eval/dataset.py（评测数据集加载与校验）规格说明书

> 本文件描述 `backend/eval/dataset.py` 的**行为契约**（做什么），不描述实现细节。
> 依据源码实际内容反向工程整理（2026-08-04）。

## 1. 背景与目标

本模块是 RAG 评测体系（P4）的数据层，解决三个问题：

1. **加载**：把 JSONL 格式的 QA 数据集读入内存（每行一条样本）；
2. **校验**：在评测前对数据集做 schema 完整性校验，坏样本一次性汇总报错，避免「跑到一半才发现标注错了」；
3. **定位解析**：数据集中不直接标注 chunk id，而是标注「paper_id + section/keywords」定位信息，评测时用 `resolve_relevant_chunks()` 实时解析为真实 chunk id——分块策略演进时标注不失效（见 `eval/dataset/README.md` 的设计说明）。

另承担一个**数据级审稿门禁**：`SOURCES` 枚举刻意不收录 `llm_generated`，使 `eval/generate_qa.py` 产出的 LLM 候选集（`qa_candidates.jsonl`，带 `source="llm_generated"` / `reviewed=false`）无法通过 `validate_dataset`，必须人工审稿、改 `source="imported_paper"` 并去掉 `reviewed` 标记后才能合并进种子集。

## 2. 范围

### 2.1 包含

- 常量：`DEFAULT_SEED_PATH`、`REQUIRED_FIELDS`、`QUESTION_TYPES`、`SOURCES`
- `load_dataset()`：JSONL 加载与逐行 JSON 解析
- `validate_dataset()` / `_validate_locator()`：schema 与枚举校验（含审稿门禁语义）
- `resolve_relevant_chunks()`：定位信息 → 真实 chunk id 列表的打分解析

### 2.2 非目标

- 候选集的 LLM 生成与摘录校验（归 `eval/generate_qa.py`）
- 指标计算（归 `eval/metrics.py` 规格）、评测执行与报告（归 `eval/run.py` 规格）
- `Chunk` 表结构与分块策略（归 `models.py` / processor 侧）
- 数据集内容本身（`qa_seed.jsonl` 的构成见 `eval/dataset/README.md`，本规格只约束其 schema）

## 3. 行为契约

### 3.1 模块常量

| 常量 | 值 | 语义 |
|------|----|------|
| `DEFAULT_SEED_PATH` | `Path(__file__).resolve().parent / "dataset" / "qa_seed.jsonl"` | 内置种子集路径（`backend/eval/dataset/qa_seed.jsonl`） |
| `REQUIRED_FIELDS` | `{"qa_id", "question", "ground_truth", "relevant_chunks", "question_type", "source", "has_answer"}` | 每条样本的必填字段集 |
| `QUESTION_TYPES` | `{"factoid", "summary", "comparison", "method_detail", "experiment_data", "out_of_scope"}` | 合法问题类型 |
| `SOURCES` | `{"demo_paper", "synthetic", "imported_paper"}` | 合法来源；**刻意不含 `llm_generated`**（审稿门禁，见 3.4） |

### 3.2 `load_dataset(path: Optional[Union[str, Path]] = None) -> list[dict]`

- **输入**：JSONL 文件路径；**falsy 值（None / 空串等）一律回退 `DEFAULT_SEED_PATH`**（`Path(path) if path else DEFAULT_SEED_PATH`）
- **输出**：`list[dict]`，每非空行一个 JSON 对象；空行（strip 后为空）跳过；文件可以只有 0 条样本（返回空列表）
- **前置条件**：无（不依赖 app 任何模块，可离线单独使用）
- **后置条件**：仅读取文件，不校验 schema（校验是 `validate_dataset` 的职责）
- **副作用**：文件 I/O（只读）
- **异常**：
  - `FileNotFoundError(f"数据集文件不存在: {path}")`：路径不存在
  - `ValueError(f"{path.name} 第 {lineno} 行不是合法 JSON: {e}")`：某行 JSON 解析失败（`raise ... from e`，报错带 1 起行号）
  - `ValueError(f"{path.name} 第 {lineno} 行不是 JSON 对象")`：某行是合法 JSON 但不是对象（如数组、标量）

### 3.3 `validate_dataset(items: list[dict]) -> None`

- **输入**：样本字典列表（通常来自 `load_dataset`）
- **输出**：无返回值；校验通过即正常返回
- **校验规则**（逐条累积错误，最后一次性抛出）：
  1. 样本必须是 dict，否则记错并跳过后续检查；
  2. **缺任一必填字段**：记错（列出缺失字段名排序列表）并 `continue`——**跳过后续类型校验，避免级联误报**；
  3. `qa_id`：非空字符串且**全局唯一**（重复记错；错误定位串 `where` 在 qa_id 合法时带 `(qa_id=...)`）；
  4. `question` / `ground_truth`：必须是非空字符串（`str` 且 `strip()` 后非空——纯空白串不合法）；
  5. `question_type ∈ QUESTION_TYPES`、`source ∈ SOURCES`（报错信息附合法值排序列表）；
  6. `has_answer`：必须是 `bool`；
  7. `relevant_chunks`：必须是 `list`；**`has_answer is False` 时必须为空列表**（负例不得标注定位）；
  8. 每个定位对象经 `_validate_locator` 校验（见 3.4）。
- **异常**：`ValueError("数据集校验失败:\n  - <错误1>\n  - <错误2>...")`——全部错误汇总为一条多行消息一次抛出
- **注意**：**不检查未知多余字段**——候选集的 `reviewed` 等额外键被静默容忍，这正是 `generate_qa.normalize_for_validation()` 只需删 `reviewed`、改 `source` 即可通过校验的原因。

### 3.4 `_validate_locator(locator: Any, where: str) -> list[str]`

- **输入**：单个 `relevant_chunks` 元素；`where` 为错误定位前缀（如 `第 3 条 (qa_id=recomil-003)`）
- **输出**：错误串列表（空列表 = 合法），由调用方汇总
- **规则**：
  1. 必须是 dict；
  2. `paper_id` 必须是 `int` 且**不能是 `bool`**（`isinstance(paper_id, bool)` 显式排除——Python 中 `True` 是 `int` 子类）；
  3. `section is None` 且 `keywords` 为 falsy（None/空列表/空串）→ 记错「至少需要 section 或 keywords 之一」；即两者至少提供其一；
  4. `section` 非 None 时必须为 `str`；
  5. `keywords` 非 None 时必须为 `list` 且元素全部为 `str`。

### 3.5 数据级审稿门禁（`llm_generated` 不得直接进评测）

- **机制**：`SOURCES` 枚举（3.1）刻意不含 `llm_generated`；`eval/generate_qa.py` 写出的候选条目带 `source="llm_generated"` + `reviewed=false`，原样喂给 `validate_dataset` 必因「非法 source」被拒。
- **合法合并路径**：人工审稿后将 `source` 改为 `"imported_paper"` 并删除 `reviewed` 键（`generate_qa.normalize_for_validation()` 即该动作的代码化，供测试断言「除审稿标记外 schema 与种子集一致」）。
- **注意**：门禁完全由「枚举成员资格」实现，`dataset.py` 中没有独立的审稿检查函数；`generate_qa.py` 自身**不导入**本模块，两模块仅靠数据文件与枚举约定耦合。

### 3.6 `resolve_relevant_chunks(db, entry: dict) -> list[str]`

- **输入**：`db` 为 SQLAlchemy Session；`entry` 为一条 QA 样本（应已通过 `validate_dataset`——函数内直接 `locator["paper_id"]` 索引，缺键抛 `KeyError`，不做防御）
- **输出**：去重后的 chunk id 列表，id 形如 `f"p{paper_id}_c{chunk_index}"`；**按得分降序，同分按 `(paper_id, chunk_index)` 升序**（确定性排序）；负例（`relevant_chunks=[]`）或无命中时返回空列表
- **打分规则**（对每条 locator，先按 `paper_id` 过滤 `chunks` 表全量行，再逐行打分）：
  1. `locator.section`（strip 后非空才参与）与 `Chunk.section_title` 做**大小写不敏感的子串包含**匹配：命中 **+2**；
  2. 同一 section 串同时作为关键词在 `Chunk.content` 中做大小写不敏感子串匹配：命中 **+1**——兼容 `section_title` 为 NULL 的库（当前示例论文即如此），此时 section 退化为普通关键词；
  3. `locator.keywords` 中每个关键词（strip 后非空才参与）在 content 中大小写不敏感命中 **+1**（逐关键词累加）；
  4. 得分 > 0 的 chunk 进入候选；同一 `(paper_id, chunk_index)` 被多条 locator 命中时**取最高分**（`max` 合并）。
- **副作用**：数据库只读查询（每条约 `len(relevant_chunks)` 次按 paper_id 的 `chunks` 全行扫描）
- **异常**：不抛业务异常；`entry["relevant_chunks"]` 缺失时按 `[]` 处理（`.get(..., [])`）
- **注意**：匹配是**纯子串**语义，无词边界概念（关键词 `"MIL"` 会命中含 `"MIL"` 子串的任何词）；`row.content or ""` / `row.section_title or ""` 对 NULL 做了兜底（尽管 models.py 中 `content` 为 `nullable=False`）。

## 4. 边界条件与错误处理

| 场景 | 期望行为 |
|------|----------|
| 数据集文件不存在 | `load_dataset` 抛 `FileNotFoundError`（消息含路径） |
| 某行非法 JSON | `ValueError`，消息带文件名与 1 起行号 |
| 某行是 JSON 数组/标量 | `ValueError`「不是 JSON 对象」 |
| 空行 / 纯空白行 | 跳过，不计入样本 |
| 文件为空（0 条样本） | 返回 `[]`；`validate_dataset([])` 正常通过（无样本即无错误） |
| `path` 传 None / 空串 | 回退内置种子集（falsy 语义） |
| 缺必填字段 | 记错并跳过后续类型校验（避免级联误报） |
| `qa_id` 重复 / 非字符串 / 空串 | 记错（重复判定只对首个之后的样本报） |
| `question`/`ground_truth` 为纯空白 | 记错（`strip()` 后为空即非法） |
| `question_type`/`source` 非法（含 `llm_generated`） | 记错并附合法值列表 |
| `has_answer` 非 bool（如字符串 `"false"`） | 记错 |
| 负例（`has_answer is False`）带非空 `relevant_chunks` | 记错「负例不应标注」 |
| `paper_id` 为 `True`/`False` | 记错（bool 显式排除） |
| locator 缺 `section` 且 `keywords` 为空/缺失 | 记错「至少需要 section 或 keywords 之一」 |
| 样本带未知多余字段（如 `reviewed`） | 静默容忍，不报错 |
| `resolve_relevant_chunks` 的 entry 未过校验（缺 `paper_id` 键） | 抛 `KeyError`（无防御，前置条件是已校验数据） |
| 多条 locator 命中同一 chunk | 取最高分，返回列表去重 |
| `section_title` 为 NULL 的库 | section 串退化为 content 关键词（+1），仍可命中 |

## 5. 依赖

- **上游依赖**：标准库 `json` / `pathlib` / `typing`；`app.models.Chunk`（**函数内延迟导入**——`load_dataset`/`validate_dataset` 可在不加载 app、不连库的环境下单独使用，只有 `resolve_relevant_chunks` 需要 ORM）
- **下游消费者**：
  - `eval/run.py`（评测主流程：`load_dataset` → `validate_dataset` → 逐条 `resolve_relevant_chunks`）
  - `backend/tests/test_dataset.py`（直接单测）
  - `backend/tests/test_generate_qa.py`（`validate_dataset` + `normalize_for_validation` 的审稿门禁链路）
  - `eval/generate_qa.py`：**无代码导入**，仅数据级耦合（其产出的候选集受 `SOURCES` 门禁约束）

## 6. 验收标准（可测试）

- [ ] AC1：种子集可加载、全部为 dict、条数 >= 20，且 `validate_dataset` 不抛异常
- [ ] AC2：种子集 `qa_id` 全局唯一；负例存在（`has_answer is False` 且 `relevant_chunks == []` 且 `question_type == "out_of_scope"`）
- [ ] AC3：缺必填字段 / `qa_id` 重复 / 非法 `question_type` 与 `source` / 负例带定位 / locator 缺 section 与 keywords，均能抛出含对应关键词的 `ValueError`
- [ ] AC4：非法 JSON 行报错带行号；文件不存在抛 `FileNotFoundError`
- [ ] AC5：`resolve_relevant_chunks` 对空库与负例返回 `[]`；对造数库按 section/keywords 命中正确 chunk、排除他论文 chunk，返回 `p{paper_id}_c{i}` 形式 id；`section_title` 为 NULL 时 section 串回退 content 匹配
- [ ] AC6：审稿门禁——`source="llm_generated"` 的候选条目原样不能通过 `validate_dataset`（**当前仅由「非法 source」通用用例隐式覆盖，无直接断言**）

## 7. 现有测试覆盖与盲区

- **已覆盖**（`backend/tests/test_dataset.py`，16 个用例；夹具为 conftest 的内存 SQLite `db`）：
  - 种子集加载与规模、`validate_dataset` 通过、`qa_id` 唯一、负例形态、问题类型覆盖（AC1/AC2，5 例）
  - 坏样本检出：缺字段 / `qa_id` 重复 / 非法枚举 / 负例带定位 / 坏 locator / 非法 JSON 带行号 / 文件不存在（AC3/AC4，7 例）
  - `resolve_relevant_chunks`：空库、负例、造数命中与他论文排除、NULL section_title 回退（AC5，3 例）
  - `DEFAULT_SEED_PATH` 文件存在（1 例）
  - 另 `test_generate_qa.py` 间接验证：候选条目带 `source="llm_generated"`/`reviewed=false`，经 `normalize_for_validation` 改写后能通过 `validate_dataset`
- **盲区**（按严重程度标注）：
  - **中**：`resolve_relevant_chunks` 的打分权重（section+2 / section 串作 content 关键词 +1 / 每关键词 +1）、多 locator 的 `max` 合并、排序规则（得分降序 + `(paper_id, chunk_index)` 升序决胜）均无显式断言——现有用例只断言成员资格，不断言顺序与去重合并
  - **中**：`_validate_locator` 的类型细分（`paper_id` 为 bool、`section` 非 str、`keywords` 非列表或含非 str 元素）无测试
  - **低**：审稿门禁的反向断言（`llm_generated` 原样被拒）无专属用例，仅靠 `source="nowhere"` 的通用非法枚举例隐式覆盖同一代码路径
  - **低**：`load_dataset` 对「合法 JSON 但非对象」行、空行跳过、空文件返回 `[]`、`path` 传空串回退种子集，无测试
  - **低**：`validate_dataset` 的错误聚合形态（多错误一次抛出、`(qa_id=...)` 定位串）、多余字段容忍、`question`/`ground_truth` 纯空白判定，无测试
  - **低**：`resolve_relevant_chunks` 对未校验输入抛 `KeyError` 的行为无测试

## 8. 关键设计决策

- **标注定位信息而非 chunk id**：分块策略演进（chunk 大小/重叠调整）会使全部 chunk id 重排，直接标注 id 的数据集会整体失效；标注「paper_id + section/keywords」并在评测时实时解析，使标注与分块解耦（README 明示的动机）。代价是解析依赖启发式打分，可能漏标/多标——README 建议关键词直接用原文术语以提高 content 命中率
- **section 双通道匹配（title +2 / content +1）**：当前示例论文入库时 `section_title` 全为 NULL，若只匹配 title 则 section 标注完全失效；让 section 串同时在 content 中作关键词匹配（+1），保证旧库可用、新库（有 section_title）权重更高
- **审稿门禁用「枚举排除」实现**：不给 `generate_qa` 写专门的拦截代码，而是让 `SOURCES` 天然不含 `llm_generated`——候选集想过校验必须经人工改 `source`，把「必须审稿」编码进数据结构而非流程约定
- **缺字段即 `continue`**：避免同一坏样本因缺字段引发一连串类型错误噪音，每条样本至多一类根因错误
- **错误聚合一次抛出**：数据集是人工维护的标注文件，一次列全所有问题比「改一个错再跑一遍」效率高
- **延迟导入 `app.models`**：保证加载/校验两个纯数据函数可脱离 app 环境使用（CI、本地快速校验）；与 `eval/metrics.py` 的「纯标准库」约定同向
