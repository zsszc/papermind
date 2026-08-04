# eval.trend 评测报告趋势追踪 规格说明书

## 1. 背景与目标

`eval.run` 每次评测都会在 `eval/reports/` 落一份 `<时间戳>.json` 报告，但单次报告无法回答「指标在变好还是变差」。`eval.trend` 扫描该目录下全部历史报告，在控制台打印总体指标趋势表与分题型纵向对比表，并生成 `eval/reports/trend.md` 供人工归档与填写备注，支撑评测指标的趋势追踪与调参决策。

本模块刻意只依赖 Python 标准库（不导入 `app.*` 任何模块），保证加载与运行不触发 Embedding 模型加载、数据库连接或 LLM 调用。

## 2. 范围

### 2.1 包含

- 扫描 `eval/reports/*.json`（或 `--report-dir` 指定目录），按文件名升序加载并提取趋势摘要（`ReportSummary`）。
- 控制台输出两张纯文本表：总体指标趋势表（含与上一次的差值列）、分 `question_type` 的 recall 纵向变化表。
- 生成/覆盖 `trend.md`（中文 Markdown，两张表 + 每次报告备注位），并保留上次生成后人工填写的备注。
- 边界处理：目录不存在、目录为空、仅 1 份报告、单份报告字段缺失、单份报告 JSON 损坏。
- CLI 入口 `python -m eval.trend [--report-dir X]`。

### 2.2 非目标

- 不运行评测本身（评测由 `eval.run` 负责），不计算任何检索指标。
- 不读取数据库、ChromaDB，不调用 LLM/Embedding。
- 不做趋势告警、阈值门禁或图表可视化（trend.md 为纯文本 Markdown 表格）。
- 不修改/删除任何历史报告 JSON；除 `trend.md` 外不写任何文件。
- 不解析报告中的 `items` 明细（只消费 `timestamp` / `retrieval_mode` / `overall` / `by_question_type` 四层字段）。

## 3. 行为契约

### 3.1 `class ReportSummary`（dataclass）

- **语义**：单份评测报告的趋势摘要。`stem`（文件名去扩展名，如 `20260728_160433`，兼作报告唯一标识）与 `path` 必填；其余字段（`timestamp` / `retrieval_mode` / `n_positive` / `n_negative` / `recall` / `mrr` / `ndcg` / `k`）在源报告缺失或类型不符时为 `None`；`by_type` 为 `{question_type: {"n", "recall", "mrr", "ndcg"}}` 字典。
- **property `n_total`**：返回 `n_positive + n_negative`；任一缺失按 0 计；两者都缺失时返回 `None`。

### 3.2 `def summarize_report(path: Path) -> ReportSummary:`

- **输入**：单份报告 JSON 的路径。
- **输出**：填充好的 `ReportSummary`。
- **前置条件**：`path` 指向可读文件。
- **后置条件**：返回对象字段按下述规则提取——
  - `timestamp` / `retrieval_mode` 仅当源字段为 `str` 时采纳，否则置 `None`；
  - `recall` / `ndcg` 的键名从 `overall` 中正则匹配 `^recall@(\d+)$` / `^ndcg@(\d+)$` 获得（k 可变）；`k` 取两者中第一个可解析出的整数；
  - 指标值仅当为 `int/float` 时采纳（转 `float`），否则 `None`；
  - `by_question_type` 列表中非 dict 元素、`question_type` 非 str 的元素直接跳过；`n`/`recall`/`mrr`/`ndcg` 类型不符置 `None`；
  - `overall` 不是 dict、`by_question_type` 不是 list 时对应部分整体留空，不抛异常。
- **副作用**：文件读取（只读）。
- **异常**：文件不可读抛 `OSError`；JSON 损坏抛 `json.JSONDecodeError`；JSON 顶层不是对象抛 `ValueError`。（均由调用方 `load_summaries` 捕获处理。）

### 3.3 `def load_summaries(report_dir: Path) -> List[ReportSummary]:`

- **输入**：报告目录路径。
- **输出**：`ReportSummary` 列表，按文件名（`p.name`）字典序升序——报告文件名即时间戳，故等同时间升序。
- **后置条件**：仅匹配 `*.json`（`trend.md`、txt 等非 JSON 文件天然忽略）；解析失败（`OSError` 或 `ValueError`，含 `json.JSONDecodeError`）的报告打印 `[trend] [warn] 跳过无法解析的报告 <文件名>: <原因>` 到 **stderr** 并跳过，不影响其余报告。
- **副作用**：目录遍历 + 文件读取；stderr 警告输出。
- **异常**：无（目录不存在时不抛错，由 `main` 先行判断；对不存在的目录调用本函数会因 `glob` 返回空迭代而得到空列表）。

### 3.4 `def compute_deltas(summaries: List[ReportSummary]) -> List[Dict[str, Optional[float]]]:`

- **输入**：按时间升序的摘要列表。
- **输出**：与输入等长的差值列表，每项为 `{"recall": ..., "mrr": ..., "ndcg": ...}`；第 0 份报告三项均为 `None`；第 i 份为当前值减第 i-1 份对应值，**任一侧为 `None` 时差值为 `None`**（展示层显示 `-`）。
- **副作用**：无（纯函数）。

### 3.5 `def format_overall_table(summaries: List[ReportSummary]) -> str:`

- **输入**：摘要列表（假定非空）。
- **输出**：控制台纯文本表格（列宽按内容自适应、两空格分隔、表头下一条 `-` 分隔线）。表头为 `时间 / 检索模式 / QA总数 / recall@{k} / MRR / NDCG@{k}`；**仅当报告数 ≥ 2 时**追加三个差值列 `Δrecall@{k} / ΔMRR / ΔNDCG@{k}`。
- **格式化规则**：
  - 表头 k 取列表中最后一份 `k` 非 `None` 的报告的 k，全部缺失时默认 5（`_metric_label`）；
  - `时间` 列取 `timestamp`，缺失回退 `stem`；`检索模式` 缺失显示 `-`；
  - `QA总数` 形如 `5 (正4/负1)`；`n_total` 为 `None` 显示 `-`，单边缺失时对应位置显示 `-`（如 `3 (正3/负-)`）；
  - 指标值保留三位小数（`0.400`），缺失显示 `-`；差值为 `{+.3f}` 格式（`+0.100` / `-0.020`），首份报告与缺失差值显示 `-`。
- **副作用**：无（纯函数）。

### 3.6 `def format_type_table(summaries: List[ReportSummary]) -> str:`

- **输入**：摘要列表（假定非空）。
- **输出**：分题型纵向对比表：首列 `question_type (recall@{k})`，其余每列为一份报告的 `stem`；行取所有报告 `by_type` 键的并集、按字母序排序。单元格格式 `0.667 (n=5)`；该次报告无此题型或 recall 缺失显示 `-`；`n` 缺失显示 `n=?`。
- **后置条件**：若所有报告均无 `by_question_type` 数据，返回固定文案 `（所有报告均缺少 by_question_type 数据）`（不渲染表格）。
- **副作用**：无（纯函数）。

### 3.7 `def load_existing_notes(trend_path: Path) -> Dict[str, str]:`

- **输入**：既有 `trend.md` 的路径。
- **输出**：`{报告 stem: 备注文本}` 字典；逐行匹配 `- **<stem>**（<任意>）：<备注>`（模块级 `_NOTE_LINE_RE`），备注取捕获组 `.strip()`。
- **后置条件**：文件不存在返回空字典；读取发生 `OSError` 按无历史备注处理（返回已解析部分，通常为 `{}`）；不匹配格式的行静默忽略。
- **副作用**：文件读取（只读）。

### 3.8 `def render_markdown(summaries: List[ReportSummary], notes: Dict[str, str]) -> str:`

- **输入**：摘要列表（假定非空）+ 历史备注字典。
- **输出**：`trend.md` 全文，固定结构：
  1. 标题 `# 评测趋势报告`；
  2. 两行引用块说明（生成时间 `time.strftime('%Y-%m-%d %H:%M:%S')`、备注保留提示）；
  3. `## 总体指标趋势`：Markdown 表格，比控制台表多一列 `报告文件`（stem），其余列与 3.5 相同，差值列规则相同；
  4. `## 分题型 recall@{k} 趋势`：Markdown 表格，单元格规则同 3.6；无数据时写 `（所有报告均缺少 by_question_type 数据）`；
  5. `## 各次报告备注`：每份报告一行 `- **{stem}**（{timestamp 或 stem}）：{备注}`；备注从 `notes` 取，缺失或空白时写 `（暂无）`。
- **转义规则**：`retrieval_mode` 与 `question_type` 中的 `|` 转义为 `\|`（`_md_escape`），防止破坏表格结构；指标值、stem 不转义。
- **副作用**：无（纯函数，不写文件）。

### 3.9 `def build_parser() -> argparse.ArgumentParser:`

- **输出**：仅含一个参数的解析器：`--report-dir`（默认 `str(DEFAULT_REPORT_DIR)`，即 `backend/eval/reports/`，经 `Path(__file__).resolve().parent / "reports"` 定位）。

### 3.10 `def main(argv: Optional[List[str]] = None) -> int:`

- **输入**：CLI 参数列表（`None` 时取 `sys.argv`）。
- **输出**：退出码。**本命令在任何「无数据」边界下都返回 0**（趋势查看是只读辅助操作，不构成 CI 门禁）。
- **行为流程**：
  1. `report_dir` 不是目录 → 打印 `[trend] 报告目录 ... 不存在；请先运行 python -m eval.run 生成评测报告。`，返回 0，不写任何文件；
  2. 加载后无任何 JSON 报告 → 打印 `[trend] 报告目录 ... 中没有任何 JSON 报告；...`，返回 0，**不生成 trend.md**；
  3. 正常路径：打印 `[trend] 共加载 N 份报告（<目录>）`，依次打印总体表、空行、分题型表；随后读取既有 `trend.md` 的备注（3.7），渲染并**覆盖写入** `report_dir / "trend.md"`，打印 `[trend] 趋势报告已写入 <路径>`，返回 0。
- **副作用**：控制台 stdout 输出；**覆盖写 `trend.md`**（仅备注区内容经 3.7 保留，其余全部重写）。
- **异常**：`trend.md` 写入失败（如目录只读）不兜底，`OSError` 直接上抛。

## 4. 边界条件与错误处理

| 场景 | 期望行为 |
|------|----------|
| 报告目录不存在 | 打印友好提示（含「不存在」），退出码 0，不写文件 |
| 目录存在但无 `*.json` | 打印「没有任何 JSON 报告」，退出码 0，不生成 trend.md |
| 目录中混有 `trend.md` / `.txt` 等非 JSON 文件 | 自动忽略（glob 只匹配 `*.json`） |
| 某份报告 JSON 损坏 / 顶层非对象 / 不可读 | stderr 打 `[warn] 跳过无法解析的报告`，跳过该份，其余正常 |
| 仅 1 份报告 | 正常输出与生成 trend.md，总体表无差值列（无 `Δ` 列） |
| 报告缺 `overall` / 指标键 / 字段类型不符 | 对应字段显示 `-`，不中断整体输出 |
| 报告缺 `by_question_type` | 分题型表显示「（所有报告均缺少 by_question_type 数据）」；部分报告缺时对应单元格 `-` |
| 差值任一侧指标缺失 | 差值显示 `-`（不算 0） |
| 各报告 k 不一致 | 表头 k 取最后一份可解析报告的 k；不逐列适配 |
| 人工备注中含换行/格式变化 | 只有严格匹配 `- **stem**（...）：...` 的行能被保留；其他写法在下次生成时丢失 |
| trend.md 写入失败（权限等） | `OSError` 上抛，进程非 0 退出（未做兜底） |

## 5. 依赖

- **上游依赖**：`eval.run` 生成的报告 JSON（契约字段：`timestamp`、`retrieval_mode`、`overall{recall@k, mrr, ndcg@k, n_positive, n_negative}`、`by_question_type[{question_type, n, recall, mrr, ndcg}]`）。仅依赖 Python 标准库（argparse/json/re/sys/time/dataclasses/pathlib/typing）。
- **下游消费者**：人工查阅（控制台 + `eval/reports/trend.md`）；无代码调用方。

## 6. 验收标准（可测试）

- [ ] AC1：给定多份按时间命名的假报告，`load_summaries` 按文件名升序返回，字段（mode/recall/mrr/ndcg/k/n_total）提取正确。
- [ ] AC2：非 JSON 文件被忽略；损坏 JSON 被跳过且 stderr 出现「跳过无法解析」。
- [ ] AC3：`compute_deltas` 首份报告差值全 `None`，其余为相邻差值；任一侧缺失时差值 `None`。
- [ ] AC4：报告数 ≥ 2 时总体表含 `Δrecall@{k}` 等差值列且出现 `+0.100` / `-0.020` 格式；仅 1 份时无 `Δ` 列。
- [ ] AC5：分题型表按字母序行、按报告列展示 `0.600 (n=2)` 格式；某次报告无该题型时显示 `-`。
- [ ] AC6：目录不存在 / 无 JSON 报告时 `main` 返回 0 且不写 trend.md，stdout 有友好提示。
- [ ] AC7：`main` 正常路径生成 `trend.md`，含 `# 评测趋势报告`、「总体指标趋势」「分题型 recall@5 趋势」「各次报告备注」章节与每份报告的 `**stem**` 备注位。
- [ ] AC8：人工在备注位填写的文本在下次运行后仍保留。
- [ ] AC9：缺字段报告（无 `overall`、无 `by_question_type`）不导致崩溃，输出含 `-` 与「缺少 by_question_type」提示。

## 7. 现有测试覆盖与盲区

- **已覆盖**：`backend/tests/test_trend.py`（16 个用例，全部在 `tmp_path` 构造假报告，不触真实目录）——
  - `TestLoadSummaries`：排序、字段提取、非 JSON 忽略、损坏 JSON 跳过（AC1、AC2）；
  - `TestDeltas`：差值数值、表内 `+/-` 格式、字段缺失时差值 `None`（AC3、AC4）；
  - `TestConsoleTables`：总体表内容、`5 (正4/负1)` 格式、分题型表内容与缺失题型显示 `-`（AC4、AC5）；
  - `TestEdgeCases`：空目录、不存在目录、单报告无差值列、缺字段报告不崩溃（AC6、AC9）；
  - `TestTrendMarkdown`：trend.md 章节与关键数值、人工备注保留（AC7、AC8）。
- **盲区**：
  - 表头 k 的选取逻辑（`_metric_label`：多份报告 k 不一致时取最后一份；全部缺失默认 5）未测 —— 中。
  - `n_positive`/`n_negative` 单边缺失时 `QA总数` 显示 `正3/负-` 的分支未测 —— 低。
  - `_md_escape` 对 `retrieval_mode` / `question_type` 中 `|` 的转义未测 —— 低。
  - `timestamp` / `retrieval_mode` 类型不符（非 str）被置 `None` 的防御分支未测 —— 低。
  - JSON 顶层为合法非对象（如 `[...]`）触发 `ValueError` 跳过的分支未直接测（仅测了语法损坏）—— 低。
  - 分题型单元格 `n` 缺失时显示 `n=?` 未测 —— 低。
  - 备注解析对「不可读 trend.md（OSError）按无备注处理」与格式不符行的容忍未测 —— 低。
  - `trend.md` 写入失败（目录只读）上抛 `OSError` 的行为未定义也未测 —— 低。

## 8. 关键设计决策

- **纯标准库、零 app 依赖**：模块 docstring 明确「不导入 app 下任何模块」，使趋势查看永远离线可用、可秒级运行，也便于测试免 mock。
- **文件名即排序键与唯一标识**：报告文件名是时间戳（`eval.run` 约定），故按 `p.name` 字典序排序即时间序；备注保留也以 stem 为键。
- **缺数据显示 `-` 而非报错**：历史报告可能来自不同版本的 `eval.run`，字段缺失视为常态而非异常，保证趋势表永远能出。
- **无数据返回 0**：趋势查看是只读辅助操作，「没有报告」不是失败，不给 CI/脚本调用方制造假警报（与 `eval.run` 的阈值门禁退出码形成对比）。
- **trend.md 覆盖写 + 备注正则回读**：生成物本身是给人看的档案，人工备注通过固定行格式 `- **stem**（time）：note` 回读保留，避免引入额外的 sidecar 文件；代价是备注格式必须严格遵守该模式。
- **差值只与相邻报告比较**：不按基线/首次比较，保持语义简单；缺失侧差值置 `None` 而非 0，避免把「没数据」误读为「没变化」。
