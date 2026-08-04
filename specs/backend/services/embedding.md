# services/embedding.py（文本分块 TextChunker + 本地 Embedding 服务 EmbeddingService）规格说明书

> 本文件描述 `backend/app/services/embedding.py` 的**行为契约**（做什么），不描述实现细节。
> 依据源码实际内容反向工程整理（2026-08-04）。

## 1. 背景与目标

PaperMind 的语义检索依赖本地向量模型，本模块承担两件基础工作：

1. **文本分块**：`TextChunker` 把 PDF 解析出的按页文本切成适合向量化的段落块，并按章节关键词粗标内容类型（abstract/intro/method/…），供 SQLite `chunks` 表与 ChromaDB 共同消费。
2. **向量化**：`EmbeddingService` 以单例 + 单线程任务队列封装 sentence-transformers 的 BGE-M3 模型，向全项目提供「文档批量编码」与「查询编码」两个入口；模型加载失败时通过 `available()` 暴露降级信号，让检索层优雅退回纯关键词检索。

设计动机：BGE-M3 首次下载约 2GB 且模型推理是 CPU/MPS 密集型操作，放在单后台线程串行处理可避免并发请求争抢内存与 GIL；模型不可用时（无网络、依赖缺失）整站其余功能必须继续可用。

## 2. 范围

### 2.1 包含

- 模块导入期副作用：`HF_ENDPOINT` 环境变量默认指向 `hf-mirror.com` 镜像
- `TextChunker`：段落切分、长度贪心组块、块间重叠、章节关键词类型推断、chunk 字典结构
- `EmbeddingService`：单例创建、worker 线程生命周期、任务队列串行化、模型懒加载与失败锁存、`available()` 降级语义、长文本截断、encode 归一化契约、`embed_query` 查询前缀

### 2.2 非目标

- 向量的存储与检索（归 `services/retrieval.py` 的 `VectorStore` / ChromaDB）
- 分块结果落库与论文处理流水线（归 `services/processor.py`）
- 语义检索结果缓存（归 `services/cache.py`）
- PDF 文本提取（归 `services/pdf_parser.py`）
- BGE-Reranker 重排序（`config.yaml` 中 `retrieval.rerank` 默认 false，代码为预留，与本模块无关）

## 3. 行为契约

### 3.0 模块级副作用

导入本模块即执行 `os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")`：

- 若进程环境中**未设置** `HF_ENDPOINT`，则设为国内镜像 `https://hf-mirror.com`（加速 HuggingFace 模型下载）；
- 若已设置（如用户显式指定官方端点），**不覆盖**；
- 该副作用发生在任何类实例化之前，仅导入即生效。

### 3.1 `class TextChunker` / `__init__(self, chunk_size: int = 512, chunk_overlap: int = 50)`

- **输入**：`chunk_size` 单块字符数上限（组块阈值）；`chunk_overlap` 相邻块间保留的重叠字符数上限
- **输出**：`TextChunker` 实例；无全局状态
- **注意**：`config.yaml(.example)` 中虽有 `embedding.chunk_size` / `embedding.chunk_overlap` 配置项，但本类**不读取配置**，默认值硬编码为 512/50；当前唯一消费者 `processor.py` 以无参方式实例化，即配置项实际不生效。

### 3.2 `TextChunker.chunk_pages(self, pages: List[Dict[str, Any]]) -> List[Dict[str, Any]]`

- **输入**：按页文本列表，每项至少含 `text` 键，可选 `page_number` 键
- **输出**：chunk 字典列表（结构见 3.5），顺序为页序 × 页内块序；不跨页合并
- **前置条件**：`pages` 元素含 `"text"` 键（缺失会抛 `KeyError`）
- **副作用**：无

### 3.3 `TextChunker._chunk_text(self, text: str, page_number: Optional[int] = None) -> List[Dict[str, Any]]`

- **输入**：单页纯文本；`page_number` 透传进每个 chunk
- **输出**：该页的 chunk 列表；`text` 为空白（`strip()` 后为空）时返回 `[]`
- **行为规则**：
  1. 按正则 `\n\s*\n`（空行）切段，各段 `strip()`、丢弃空段；
  2. 以 `len()`（**字符数**，非 token 数）贪心累加段落；当 `current_len + para_len > chunk_size` 且当前缓冲非空时，先输出一块，再从刚输出块的**尾部**往回取若干完整段落作为下一块的开头，直到再取一段会使重叠总量超过 `chunk_overlap` 为止；
  3. 单个段落自身超过 `chunk_size` 时**不再二次切分**，整块保留（块长可超过 `chunk_size`）；
  4. 循环结束后缓冲非空则输出最后一块。
- **副作用**：无

### 3.4 `TextChunker._infer_chunk_type(self, paragraphs: List[str]) -> str`

- **输入**：组成某 chunk 的段落列表
- **输出**：内容类型字符串，取值 ∈ `{abstract, intro, method, result, discussion, conclusion, paragraph}`
- **规则**：将**前两段**拼接并转小写，按 `_SECTION_KEYWORDS` 字典定义顺序（abstract → intro → method → result → discussion → conclusion）做子串匹配，首个命中即返回；全部未命中返回 `"paragraph"`。关键词表为中英文混合（如 `"abstract"`/`"摘要"`、`"methods"`/`"材料与方法"`）。

### 3.5 `TextChunker._make_chunk(self, paragraphs: List[str], page_number: Optional[int]) -> Dict[str, Any]`

- **输出字典结构**（消费方 `processor.py`、`retrieval.py` 依赖此 schema）：

| 键 | 类型 | 语义 |
|----|------|------|
| `content` | `str` | 段落以 `"\n\n"` 拼接的块文本 |
| `page_number` | `Optional[int]` | 来源页码（透传，可为 None） |
| `chunk_type` | `str` | 3.4 的推断结果 |
| `token_count` | `int` | **字符数**（`len(content)`），非真实 token 数；命名与语义不一致是历史遗留，消费方仅作展示用途 |

### 3.6 `class EmbeddingService` / `__new__(cls)`

- **输出**：全进程唯一实例（类属性 `_instance` 锁存）
- **后置条件**：首次实例化时：
  1. 实例属性 `model_name` 取自 `config.get("embedding.local_model", "BAAI/bge-m3")`；
  2. 调用 `_start_worker()` 启动后台 worker 线程（守护线程，名称 `embedding-worker`）。
- **副作用**：启动线程、读配置、写日志 `[EmbeddingService] worker 线程已启动`
- **并发**：`_start_worker` 在 `_worker_lock` 保护下保证线程只启动一次；但 `__new__` 本身的「检查-创建」无锁，理论上多线程同时首次实例化可能重复进入（后进入者会在锁内直接返回，最终仍只有一个 worker 线程与一个实例）。

### 3.7 `EmbeddingService.available(self) -> bool`

- **输出**：模型可用返回 `True`；否则 `False`
- **副作用（重要）**：**首次调用即在调用方线程内同步触发模型加载**（含可能的 2GB 下载与数秒到数分钟的初始化），不是纯读操作；加载失败会把失败状态锁存（见 3.8）。
- **降级语义**：返回 `False` 时上层（`VectorStore.available()` → 检索路由）应将语义检索视为不可用并降级为纯关键词检索，不得因此崩溃。

### 3.8 `EmbeddingService._load_model(self)`

- **输出**：`SentenceTransformer` 实例，或失败时 `None`
- **行为规则**：
  1. 仅当 `_model is None and not _failed` 时尝试加载——加载**懒触发**、**至多尝试一次**；
  2. `device` 取自 `config.get("embedding.device", "auto")`；为 `"auto"` 时检测 `torch.backends.mps.is_available()`，可用则 `"mps"` 否则 `"cpu"`（**无 CUDA 分支**，Linux/Windows GPU 需显式配置）；
  3. 加载成功 → `_model` 赋值；任何异常 → `_failed = True`、`_error = str(e)`、写 error 日志，此后**永久不再重试**（进程内失败锁存，需重启进程恢复）。
- **异常**：内部全部捕获，不向外抛。
- **并发**：无锁；多线程并发首调理论上可能重复实例化模型，后完成者覆盖先完成者（worker 单线程消费模型，实际影响有限，属已知盲区）。

### 3.9 `EmbeddingService._sync_embed(self, texts: List[str], batch_size: int = 8) -> List[List[float]]`

- **输入**：待编码文本列表；`batch_size` 传给 `model.encode`
- **输出**：`List[List[float]]`；**经 L2 归一化**（`normalize_embeddings=True`），每条为 BGE-M3 dense 向量（维度由模型决定，BGE-M3 = 1024）；`texts` 为空时返回 `[]`（不触发模型加载）
- **前置处理**：逐条按**空白分词**（`str.split()`）计数，超过 512「词」则截断为前 512 词——注意对无空白的中文文本**不产生任何截断效果**
- **异常**：模型不可用（`_load_model()` 返回 `None`）→ 抛 `RuntimeError(f"Embedding 模型不可用: {self._error}")`；`model.encode` 自身异常向上抛
- **执行上下文**：正常路径下仅由 worker 线程调用（经任务队列），串行执行

### 3.10 `EmbeddingService.embed(self, texts: List[str], batch_size: int = 16) -> List[List[float]]`

- **输入**：待编码文本列表
- **输出**：与 3.9 相同的归一化向量列表；`texts` 为空时直接返回 `[]`（不进队列）
- **行为**：构造 `Future`，把 `(texts, future)` 投入类级任务队列，**无超时阻塞**等待 worker 结果；worker 抛出的异常经 `future.result()` 原样传播给调用方
- **已知契约缺陷**：形参 `batch_size` **被静默丢弃**——worker 调用 `_sync_embed(texts)` 时未透传，实际批次大小恒为 `_sync_embed` 默认值 8
- **串行保证**：单 worker 线程 ⇒ 所有 encode 任务严格按入队顺序逐个执行，无并发推理

### 3.11 `EmbeddingService.embed_query(self, query: str) -> List[float]`

- **输入**：检索查询串
- **输出**：单条归一化向量（`List[float]`）
- **行为**：为查询拼接固定英文指令前缀 `Represent this sentence for searching relevant passages: {query}` 后走 `embed()`；即**查询与文档的编码输入不对称**（文档不加前缀）

## 4. 边界条件与错误处理

| 场景 | 期望行为 |
|------|----------|
| `embed([])` / `_sync_embed([])` | 返回 `[]`，不触碰模型与队列 |
| 整页空白文本 | 该页产出 0 个 chunk |
| 单段长度超过 `chunk_size` | 不切分，整块保留（块长可超限） |
| 段落恰好使 `current_len + para_len == chunk_size` | 不触发切块，继续累加（严格大于才切） |
| 中文长文本 | 空白分词截断失效，整段进入 encode（依赖模型自身 8192 token 上限，存在内存峰值风险） |
| 模型下载/加载失败（无网络、依赖缺失、OOM） | `available()` 返回 `False`；`embed()` 抛 `RuntimeError`（含首次失败原因）；`_failed` 锁存，进程内不再重试 |
| 加载成功后某次 encode 运行时报错 | 异常经 `Future` 传播给该次调用方，worker 线程存活并继续处理后续任务（模型不标记失败） |
| 并发调用 `embed()` | 任务入 FIFO 队列由单 worker 串行消费；调用线程各自阻塞在自己的 `Future` 上 |
| 进程退出 | worker 为守护线程，随进程结束直接回收；队列中未消费的任务被静默丢弃 |
| 队列毒丸 `None` | `_worker_loop` 收到 `None` 即退出循环；**当前代码无任何投放点**，为预留的停止机制 |
| `HF_ENDPOINT` 已被用户显式设置 | 保留用户值，不覆盖 |

## 5. 依赖

- **上游依赖**：
  - `app.core.config.config`：读 `embedding.local_model`（默认 `BAAI/bge-m3`）、`embedding.device`（默认 `auto`）
  - `app.core.logger.logger`：日志
  - 第三方：`sentence-transformers==2.3.x`、`transformers==4.39.3`、`torch==2.2.2`（宪法第 16 条锁定：macOS x86_64 + Py3.12 上限，**禁止擅自升级**）
  - 环境变量 `HF_ENDPOINT`（默认镜像 `hf-mirror.com`，模型首次下载约 2GB 需联网）
  - 标准库：`queue`、`threading`、`concurrent.futures.Future`、`re`、`os`
- **下游消费者**：
  - `services/retrieval.py`（`VectorStore`）：`embed()` 编码文档块写入 ChromaDB、`embed_query()` 编码查询、`available()` 供检索路由降级判断
  - `services/processor.py`（`PaperProcessor`）：`TextChunker` 分块
  - 间接：`routers/search.py`（经 `VectorStore.available()`）、`services/agent_graph.py`（经向量库）

## 6. 验收标准（可测试）

- [ ] AC1：`TextChunker().chunk_pages([{"text": "  \n  ", "page_number": 1}])` 返回 `[]`
- [ ] AC2：分块结果每项含且仅含 `content` / `page_number` / `chunk_type` / `token_count` 四键，`token_count == len(content)`
- [ ] AC3：构造总长超过 `chunk_size` 的多段文本，切块点之后新块以旧块尾部若干完整段落开头，且重叠部分总长 ≤ `chunk_overlap`
- [ ] AC4：首两段含「摘要/abstract」等关键词的块 `chunk_type` 命中对应类型；无关键词时为 `"paragraph"`
- [ ] AC5：`EmbeddingService() is EmbeddingService()` 为真，且 worker 线程（名为 `embedding-worker`）全进程仅一个
- [ ] AC6：mock `SentenceTransformer` 抛异常后，`available()` 恒为 `False` 且重复调用不再触发加载；`embed(["x"])` 抛 `RuntimeError`
- [ ] AC7：`embed([])` 返回 `[]` 且任务队列无新增任务
- [ ] AC8：`model.encode` 被调用时固定带 `normalize_embeddings=True, convert_to_numpy=True, show_progress_bar=False`；返回值为 `List[List[float]]`
- [ ] AC9：`embed_query("q")` 实际编码的文本以 `Represent this sentence for searching relevant passages: ` 开头
- [ ] AC10：超过 512 个空白分词的英文文本被截断至 512 词后再编码
- [ ] AC11：进程未设 `HF_ENDPOINT` 时导入本模块后其值为 `https://hf-mirror.com`；已设时保持原值

## 7. 现有测试覆盖与盲区

- **已覆盖**：`backend/tests/` 中**没有任何直接针对本模块的测试**（TextChunker 与 EmbeddingService 均未被 import）。间接覆盖仅三处：
  - `tests/test_search.py`：用 `_StubVectorStore`（`available()` 恒 False）验证检索路由在语义不可用时的降级路径——验证的是路由层，不是本模块；
  - `tests/test_upload.py`：mock 掉后台处理入口，间接跳过 TextChunker；
  - `tests/test_agent_graph.py`：`_BoomStore` 抛出 `RuntimeError("embedding 挂了")`，验证 Agent 图在向量库异常时 `context_chunks == []`——同样是消费方行为。
- **盲区**：
  - **高**：`TextChunker` 全部行为无测试——切块阈值边界（恰好相等 vs 严格大于）、单段超长不切分、块间重叠计算、空白页、`chunk_type` 关键词优先级与「仅看前两段」规则、`token_count` 实为字符数（AC1–AC4 全部无落点）
  - **高**：`embed()` 的 `batch_size` 形参被静默丢弃（契约缺陷，见 3.10），无测试暴露；修复前任何依赖批次调优的调用都无效
  - **中**：`available()` 的失败锁存（`_failed` 置位后不再重试）、`_error` 进入 `RuntimeError` 消息、加载成功路径的 encode 固定参数（AC6/AC8）无测试
  - **中**：单例与 worker 生命周期无测试——毒丸 `None` 无投放点、守护线程随进程退出丢弃未消费任务、`embed()` 无超时永久阻塞的风险（worker 若意外死亡，调用方挂死）
  - **中**：长文本截断仅对空白分词语言（英文）生效、中文不截断的行为无测试（AC10 只覆盖英文）
  - **低**：`HF_ENDPOINT` 的 `setdefault` 幂等语义（AC11）、device `auto` 无 CUDA 分支、`embed_query` 前缀（AC9）无测试

## 8. 关键设计决策

- **单例 + 单线程 FIFO 队列**：BGE-M3 推理是内存/算力密集操作，单用户本地场景下串行化可避免并发请求造成的内存峰值与竞争；代价是 embedding 吞吐天然受限，且 `embed()` 无超时——worker 死亡则调用方永久阻塞（已知风险，未加看护）。
- **模型懒加载 + 失败锁存（`_failed`）**：首次 `available()`/`embed()` 才加载，避免启动期 2GB 下载阻塞服务；失败后锁存是为了不让每个请求都重试一次昂贵的加载，代价是进程内无法自愈（如下载中途断网，恢复网络后也必须重启进程）。
- **下载走 `hf-mirror.com`**：国内网络环境下 HuggingFace 官方端点不可达，模块导入期用 `setdefault` 设镜像，保留用户显式覆盖的自由。
- **`available()` 即触发加载**：降级探测与真实加载合为一体，语义上「问一下就加载」。首调会阻塞调用线程数秒到数分钟（取决于是否需下载），调用方需自行接受这一延迟；AGENTS.md 所述「后台线程加载」仅对 `embed()` 路径成立。
- **查询加 BGE 指令前缀、文档不加**：`embed_query` 沿用 BGE v1 风格的 instruction 前缀以拉近查询-文档分布；BGE-M3 官方已不再强制此前缀，但改动会破坏与存量 ChromaDB 向量的可比性，故保留。
- **字符数当 token 数**：分块阈值与 `token_count` 均按 `len()` 字符计，实现简单、跨语言一致，代价是与模型真实 token 上限（BGE-M3 8192）不严格对应——因此另有 512 空白词截断兜底英文，中文则依赖模型自身上限。
- **`embedding.chunk_size/chunk_overlap` 配置项存在但不读取**：TextChunker 默认值硬编码 512/50，`config.yaml` 中的同名键当前是死配置；规格以代码为准记录此现状，如需让配置生效应属行为变更，须按 TDD 先行补测。
