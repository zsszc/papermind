# eval/run.py（RAG 一键评测脚本）规格说明书

> 本文件描述 `backend/eval/run.py` 的**行为契约**（做什么），不描述实现细节。
> 依据源码实际内容反向工程整理（2026-08-04）。

## 1. 背景与目标

`eval/run.py` 是 PaperMind RAG 评测的执行入口（`python -m eval.run`），把「加载数据集 → 解析期望 chunk → 逐条检索 → 算指标 → （可选）生成答案算生成侧指标 → 汇总 → 写报告 → 阈值门禁」串成一条命令。它同时是 **CI 质量门禁**：`.github/workflows/eval.yml`（手动触发）在干净环境跑本脚本，`recall@k` 均值低于阈值时退出码非 0 使流水线失败；产出的 JSON 报告被 `eval/trend.py` 消费做趋势追踪。

设计要点：**不走 HTTP**，直接调用 app 内部函数（`VectorStore.search` + 自建 chunk 级关键词检索 + RRF 融合）；语义模型不可用时优雅降级为纯关键词检索，评测不中断。

## 2. 范围

### 2.1 包含

- CLI 参数定义与入口（`build_parser()` / `main()`）
- `Retriever`：评测用检索器（hybrid / 降级 keyword-only）
- `_keyword_chunk_search()` / `_rrf_fuse_chunks()`：chunk 级关键词检索与 RRF 融合
- `--with-llm` 生成侧：`_generate_answer()` / `_extract_citations()`
- `run_eval()`：评测主流程、汇总统计、控制台输出、JSON 报告写入、退出码判定
- 报告 JSON 的 schema（`eval/trend.py` 的下游数据契约）

### 2.2 非目标

- 数据集 schema 校验与期望 chunk 解析公式（归 `eval/dataset.py` 规格，本规格只描述调用关系）
- 指标公式本身（归 `eval/metrics.py` 规格）
- 趋势追踪（归 `eval/trend.py`）、候选 QA 生成（归 `eval/generate_qa.py`）
- 路由层检索管线（FTS 清洗 / 论文级 RRF / `/api/search` 端点，归 `specs/backend/services/retrieval.md` 第 3.8–3.11 节）——本模块的关键词检索与 RRF 是**chunk 级的自建平行实现**，原因见第 8 节

## 3. 行为契约

### 3.1 CLI 接口（`build_parser()` / `main(argv: Optional[List[str]] = None) -> int`）

`python -m eval.run`（须在 `backend/` 目录下、以 `env -u PYTHONPATH venv/bin/python` 运行）：

| 参数 | 类型 | 默认 | 语义 |
|------|------|------|------|
| `--dataset` | str | None → `eval.dataset.DEFAULT_SEED_PATH` | 评测数据集 JSONL 路径 |
| `--top-k` | int | 5 | 检索截断位置 k（报告指标键为 `recall@{k}` / `ndcg@{k}`） |
| `--threshold` | float | 0.5 | 正例 `recall@k` 均值达标阈值，低于则退出码 1 |
| `--keyword-only` | flag | False | 强制仅关键词检索（不加载语义模型，快） |
| `--with-llm` | flag | False | 加跑生成侧指标（**真实调用 LLM API**） |
| `--report-dir` | str | `backend/eval/reports/`（`DEFAULT_REPORT_DIR`） | JSON 报告输出目录 |

`main()` 仅做参数解析与 `--dataset` 缺省回填，随即调用 `run_eval(args)` 并返回其退出码；`__main__` 下 `sys.exit(main())`。

### 3.2 `_keyword_chunk_search(db, query: str, limit: int = 20) -> List[Dict[str, Any]]`

- **存在理由**：路由层 `_keyword_search` 基于 `papers_fts` 只返回**论文级**结果（无 chunk id），无法满足 chunk 级评测，故本函数直接对 `chunks` 表做轻量打分
- **行为**：
  1. 查询按**纯空白**分词（`re.split(r"\s+", query.strip())`），无 token → 返回 `[]`；**不做 FTS 清洗、不做中文分词**——无空格的中文问题整体作为一个 token 做子串匹配；
  2. 全表扫描 `chunks`，每个 token 在 `content.lower()` 中每出现一次 +1 分，累加为该 chunk 得分；
  3. 得分 > 0 者保留，按 `(-score, chunk_id)` 排序，截断 `limit` 条。
- **输出**：元素字典 `{chunk_id: f"p{paper_id}_c{chunk_index}", paper_id, content, score: float, source: "keyword"}`
- **副作用**：只读查询；`from app.models import Chunk` 为函数内延迟导入

### 3.3 `_rrf_fuse_chunks(semantic_results, keyword_results, top_k, k: int = 60) -> List[Dict[str, Any]]`

- **公式**：与路由层论文级 RRF 同式——对每路结果按名次（0 起）累加 `1.0 / (k + rank + 1)`，两路得分相加；**去重键换成 `chunk_id`**
- **行为**：`chunk_id` 为 None 的条目跳过；每个 chunk 保留**首次出现**的结果字典为载体（先加语义路，故语义结果优先成为载体）；按 RRF 分降序截断 `top_k`；同分时排序稳定（语义路条目在前）
- **注意**：RRF 分只用于排序，**不回写**结果字典的 `score` 字段

### 3.4 `class Retriever`（`__init__(self, db, top_k: int, keyword_only: bool = False)` / `mode` / `search(self, query: str)`）

- **`__init__` 降级三分支**：
  1. `keyword_only=True` → `degraded=True`，reason = `--keyword-only 指定，跳过语义检索`（**不导入 retrieval、不触碰 ChromaDB**）；
  2. 否则 `get_vector_store()` 后 `store.available()` 为假 → `degraded=True`，reason = `Embedding 模型加载失败（详见日志）`；
  3. 初始化/可用性检查**任何异常被全捕获** → `degraded=True`，reason = `语义检索初始化异常: {e}`——模型/向量库故障不阻断评测。
- **`mode` property**：`degraded` → `"keyword-only(degraded)"`，否则 `"hybrid"`（写入报告的 `retrieval_mode` 字段）
- **`search(query)`**：
  1. 关键词路总是先跑，`limit = top_k * 2`；
  2. `degraded` → 直接返回关键词结果前 `top_k` 条；
  3. 否则 `store.search(query=query, top_k=top_k * 2)` 取语义候选，经 `_rrf_fuse_chunks(..., top_k)` 融合返回；
  4. **运行期**语义检索抛异常 → 打印 `[warn]` 到 stderr，**该条查询降级**为关键词结果前 `top_k`（`degraded` 标志不翻转，下一条仍尝试语义）。
- **注意**：`__init__` 非降级路径调用 `get_vector_store()` 会初始化 ChromaDB（触碰 `vector_db/`），`available()` 首次调用会在本线程**同步触发 Embedding 模型加载**（行为归 `retrieval.md` 第 3.1/3.2 节）

### 3.5 `_generate_answer(question: str, contexts: List[Dict[str, Any]]) -> str`

- **行为**：把检索结果拼成 `[{chunk_id}] {content[:800]}` 多段文本（每段 content 截 800 字符），无结果时用占位 `（未检索到任何资料）`；以系统提示 + 用户消息调 `llm_service.chat_completion_sync(messages)`
- **系统提示**（`_GEN_SYSTEM_PROMPT`，照抄）：`你是文献问答助手。请仅根据给定资料回答问题，回答末尾用 [chunk_id] 形式标注引用的资料块（例如 [p1_c2]）。若资料中没有相关信息，请明确回答“不知道”，不要编造。`
- **异常/错误语义**：`chat_completion_sync` 对 API/超时错误重试 3 次后**返回带内错误串** `[调用 LLM 出错: ...]`（不抛，归 llm 服务规格）；非预期异常则抛出——`run_eval` **未兜底**，会中断整个评测（`finally` 仅保证 `db.close()`，报告不写入）。错误串被当正常 answer 记录时：引用提取为空、`keyword_hit_rate` 趋 0、`contains_refusal` 一般为 False（错误串不含拒答表述）

### 3.6 `_extract_citations(answer: str) -> List[str]`

- **行为**：以 `_CHUNK_ID_RE = re.compile(r"p\d+_c\d+")` 全文提取 chunk 引用，**去重且保持首次出现顺序**；`answer` 为 None/空 → `[]`
- **注意**：正则会命中答案正文里任何形如 `p<数字>_c<数字>` 的串，不区分是否处于 `[...]` 引用括号内

### 3.7 `run_eval(args: argparse.Namespace) -> int`

- **流程**（顺序固定）：
  1. `load_dataset(args.dataset)` → `validate_dataset(items)`（校验失败 `ValueError` 直接上抛，进程非零退出）；打印数据集条数；
  2. `db = SessionLocal()`（`app.database`，**连接真实 SQLite**，只读使用；`finally db.close()`）；
  3. 构造 `Retriever(db, top_k, keyword_only)` 并打印检索模式（降级时附原因）；
  4. 逐条样本：`resolve_relevant_chunks(db, entry)` 解析期望 id → `retriever.search(question)` → 记录 `{qa_id, question_type, has_answer, relevant_ids, retrieved_ids}`；
  5. **正例**（`has_answer` 为真）：算 `recall_at_k` / `mrr` / `ndcg_at_k`（k = `args.top_k`），计入按 `question_type` 分组的桶；**负例只计数 `negative_total`，不进任何检索指标**；
  6. `--with-llm`：每条调 `_generate_answer`；正例另算 `citation_coverage`（对 `_extract_citations` 结果）与 `keyword_hit_rate`（对 `ground_truth`）；负例用 `contains_refusal` 判定拒答并计数 `negative_refused`；
  7. 汇总：overall = 各分组指标扁平化后的均值 + `n_positive` / `n_negative`；按类型生成均值表行（类型名排序）；
  8. 控制台打印汇总表（`_print_table`，含一行 `ALL`）与负例计数；`--with-llm` 时附生成侧均值与负例拒答率；
  9. 写 JSON 报告（见 3.8）；
  10. 退出码判定（见 3.9）。
- **副作用**：真实 SQLite 只读连接；非 `--keyword-only` 时初始化 ChromaDB 并加载 Embedding 模型；`--with-llm` 时真实 LLM API 调用；写报告文件；控制台/stderr 输出
- **进度输出**：每条完成打印 `(idx/总数) qa_id 完成` 并以 `\r` 回车覆盖

### 3.8 JSON 报告 schema（`eval/reports/<%Y%m%d_%H%M%S>.json`）

- **写入**：`Path(args.report_dir).mkdir(parents=True, exist_ok=True)` 自动建目录；文件名秒级时间戳；`json.dumps(..., ensure_ascii=False, indent=2)`，UTF-8
- **顶层键**（已对真实报告实证核对）：

| 键 | 类型 | 语义 |
|----|------|------|
| `timestamp` | str | `%Y-%m-%dT%H:%M:%S` |
| `dataset` | str | `str(args.dataset)` |
| `top_k` / `threshold` | int / float | 运行参数回显 |
| `retrieval_mode` | str | `"hybrid"` 或 `"keyword-only(degraded)"` |
| `degraded` / `degrade_reason` | bool / str | 降级标志与原因（未降级原因为 `""`） |
| `with_llm` | bool | 是否含生成侧 |
| `elapsed_seconds` | float | 保留两位小数 |
| `overall` | dict | `{f"recall@{k}", "mrr", f"ndcg@{k}", "n_positive", "n_negative"}`——**指标键名嵌 k 值** |
| `by_question_type` | list[dict] | `{question_type, n, recall, mrr, ndcg}`，按类型名排序 |
| `items` | list[dict] | 逐条记录：必有 `{qa_id, question_type, has_answer, relevant_ids, retrieved_ids}`；正例另有 `{recall, mrr, ndcg}`；`--with-llm` 正例另有 `{answer, citations, citation_coverage, keyword_hit_rate}`、负例另有 `{answer, refused}` |
| `generation` | dict（**仅 `--with-llm`**） | `{citation_coverage, keyword_hit_rate, negative_refusal_rate, negative_refused, negative_total}`；**无负例时 `negative_refusal_rate` 为 `None`（JSON null）** |

- **下游耦合（隐性契约）**：`eval/trend.py` 按文件名时间戳排序报告，用正则 `^recall@(\d+)$` / `^ndcg@(\d+)$` 从 `overall` 定位指标键并解析 k，另消费 `timestamp` / `retrieval_mode` / `overall.mrr` / `n_positive` / `n_negative` / `by_question_type[].question_type,n,recall`——**改这些键名/结构会破坏 trend**；`tests/test_trend.py` 的假报告亦按此 schema 构造

### 3.9 退出码（CI 门禁）

- `overall[f"recall@{args.top_k}"] >= args.threshold` → **0**（PASS），否则 **1**（FAIL）；控制台打印 `recall@{k}=... 阈值=... -> PASS/FAIL`
- 只有 recall 参与门禁，MRR/NDCG/生成侧指标不影响退出码
- **空正例集**（数据集无正例）：`_mean([]) = 0.0`，阈值 > 0 时 FAIL
- 数据集加载/校验失败（`FileNotFoundError` / `ValueError`）、LLM 非预期异常等未捕获路径：Python 异常退出（退出码非 0 但非 1 的语义化值），**报告不写入**

## 4. 边界条件与错误处理

| 场景 | 期望行为 |
|------|----------|
| 数据集文件不存在 / 非法 JSON / 校验失败 | 异常上抛，进程非零退出，无报告 |
| 数据集为空或全负例 | 正常跑完，`n_positive=0`，overall 各均值为 0.0，一般 FAIL（阈值 > 0） |
| `--keyword-only` | 不加载语义模型/ChromaDB，纯关键词评测，报告标 `degraded` |
| Embedding 模型不可用 / 语义初始化抛异常 | 降级为纯关键词，控制台与报告标注原因，评测继续 |
| 运行期单条语义检索失败 | stderr 打 `[warn]`，该条降级用关键词结果；后续条目仍尝试语义 |
| 查询无空白可分的 token（空白查询） | 关键词路返回 `[]`；hybrid 模式仅靠语义路 |
| 无空格中文查询走关键词路 | 整句作单 token 子串匹配（无分词），命中偏保守 |
| 检索结果不足 `top_k` 条 | 指标按实际返回计算（recall 分母仍是期望数） |
| `--with-llm` 且 LLM API/超时错误 | 重试 3 次后错误串 `[调用 LLM 出错: ...]` 被当 answer 记录（带内错误），该条生成侧指标趋 0，评测不中断 |
| `--with-llm` 且 LLM 非预期异常 | **上抛中断整个评测**，无报告（仅 `db.close()` 有保证） |
| 负例数为 0 的 `--with-llm` 运行 | `negative_refusal_rate = None`（JSON null） |
| 同一秒启动两次评测 | 报告文件名相同，**后跑覆盖先跑**（秒级时间戳粒度） |
| `--report-dir` 不存在 | 自动 `mkdir(parents=True)` |
| 数据集含多余字段（如 `reviewed`） | `validate_dataset` 容忍（见 dataset 规格 3.3），不影响评测 |
| 评测运行期间数据库被并发写入 | 无事务快照保证；`resolve_relevant_chunks` 与关键词检索逐条查库，结果反映查询时点状态 |

## 5. 依赖

- **上游依赖**：
  - `eval.dataset`（`load_dataset` / `validate_dataset` / `resolve_relevant_chunks` / `DEFAULT_SEED_PATH`）
  - `eval.metrics`（`recall_at_k` / `mrr` / `ndcg_at_k` / `citation_coverage` / `keyword_hit_rate` / `contains_refusal`）
  - `app.database.SessionLocal`（真实 SQLite，延迟导入）
  - `app.models.Chunk`（经 `_keyword_chunk_search` / `resolve_relevant_chunks` 延迟导入）
  - `app.services.retrieval.get_vector_store`（语义检索，延迟导入；行为归 `specs/backend/services/retrieval.md`）
  - `app.services.llm.llm_service.chat_completion_sync`（`--with-llm` 时，延迟导入；符合宪法第 8 条 LLM 唯一入口）
- **下游消费者**：
  - `.github/workflows/eval.yml`（workflow_dispatch 手动触发：`python -m eval.run`，报告目录作为 artifact 上传）
  - `eval/trend.py`（读取本脚本产出的报告 JSON，schema 耦合见 3.8）
  - `tests/test_trend.py`（构造对齐本 schema 的假报告，间接耦合）
  - 无 `tests/test_run.py`——**无任何直接自动化测试**

## 6. 验收标准（可测试）

- [ ] AC1：默认运行（模型不可用环境）自动降级为 keyword-only，评测跑完、报告写入、退出码按 recall@5 ≥ 0.5 判定
- [ ] AC2：`--keyword-only` 运行不触发语义模型加载，报告 `retrieval_mode == "keyword-only(degraded)"`
- [ ] AC3：正例计入并按 question_type 分组聚合；负例不进检索指标均值，`n_positive` / `n_negative` 计数正确
- [ ] AC4：报告 JSON 顶层键与 `overall` / `by_question_type` / `items` 结构符合 3.8 表（trend.py 可直接消费）
- [ ] AC5：`--with-llm`（mock LLM）时正例含 `citation_coverage` / `keyword_hit_rate`，负例含 `refused` 判定，报告含 `generation` 块
- [ ] AC6：recall@k 均值 ≥ 阈值退出码 0，低于则 1（**当前无测试**）
- [ ] AC7：运行期语义检索异常时该条降级为关键词结果且评测继续（**当前无测试**）

## 7. 现有测试覆盖与盲区

- **已覆盖**：无直接测试文件（`backend/tests/` 下 13 个测试文件逐一核对，无 `test_run.py`）；间接覆盖仅有两处——
  - `tests/test_trend.py` 按 3.8 schema 构造假报告（验证 trend 对报告的消费，不验证本模块产出）；
  - CI `eval.yml` 手动触发跑真实评测（非自动化断言，仅门禁性质）。
- **盲区**（按严重程度标注）：
  - **高**：`run_eval` 主流程全链路无测试——逐条编排、正/负例分流、分组均值、报告写入与 schema、退出码判定（AC6）均无覆盖；CI 门禁（退出码）本身无回归保护
  - **高**：`Retriever` 降级三分支（keyword-only / available() 为假 / 初始化异常）与 `search` 运行期单条降级（AC7）无测试
  - **高**：`--with-llm` 生成侧流程（答案记录、引用提取、citation_coverage/keyword_hit_rate 聚合、负例拒答率、LLM 错误串被当 answer 记录的带内错误路径）无测试
  - **中**：`_keyword_chunk_search` 的打分（token 出现次数累加）、排序（`-score, chunk_id`）、`limit` 截断、空白查询返回 `[]`、无空格中文整句作单 token 的行为无测试
  - **中**：`_rrf_fuse_chunks` 的 RRF 计分、chunk_id 去重、载体首选语义路、top_k 截断无测试（路由层论文级 RRF 同样无直接单测，见 retrieval.md 第 7 节）
  - **中**：报告 schema 与 `eval/trend.py` 的键名耦合（`recall@{k}` 嵌 k 键名、`by_question_type` 结构）无契约测试——改键名两处同时漂移的风险存在
  - **低**：`_extract_citations` 的去重保序、误命中非引用语境的 `p\d+_c\d+` 串无测试；报告文件名秒级碰撞覆盖无测试
  - **低**：`_print_table` 控制台输出格式、进度行 `\r` 覆盖行为无测试

## 8. 关键设计决策

- **不走 HTTP、直调内部函数**：评测对象是检索/生成质量而非接口契约；直调省去起服务与鉴权噪音，也便于在 CI 干净环境跑。代价是评测链路（chunk 级关键词检索 + RRF）与线上路由层（论文级 FTS + RRF）是**两套平行实现**——路由层 `_keyword_search` 走 FTS5 且只给论文级结果，无法满足 chunk 级指标，故自建；两处 RRF 公式保持一致（k=60）是有意对齐
- **降级优先于失败**：语义模型不可用时降级为纯关键词并显式标注 `degraded`，而非让评测崩溃——保证 CI 在任何环境（含无 GPU、模型未下载）都能产出可比的 keyword 基线；运行期单条失败也只降级该条，不把 25 条评测葬送于一次抖动
- **负例隔离统计**：负例没有期望 chunk，`recall@k` 对它们无定义（metrics 层返回 0.0）；若计入均值会把 recall 拉低且语义错误，故只单独计数，幻觉检查交由 `--with-llm` 的拒答率承担
- **阈值门禁只看 recall@k**：recall 是「该找回来的找回来了没有」的最直接指标；MRR/NDCG/生成侧作为观察指标不进门禁，避免多指标门禁的维护成本
- **报告落盘 JSON + 秒级时间戳**：趋势分析（trend.py）与人工查阅共用同一数据源；`ensure_ascii=False` 保留中文可读性；秒级文件名在「一天手动跑几次」的使用强度下足够，未防同秒碰撞
- **指标键名嵌 k（`recall@5`）**：允许不同 k 的报告共存于 trend 视图，k 由键名自描述（trend.py 用正则解析），无需额外元数据字段
- **全程延迟导入 app 模块**：与 dataset/metrics 的纯净性约定同向——`python -m eval.run --help` 等轻操作不应触发配置读取、ChromaDB 初始化或模型加载
