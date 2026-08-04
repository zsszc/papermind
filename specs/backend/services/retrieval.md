# services/retrieval.py（向量库 VectorStore）+ 检索管线规格说明书

> 本文件描述 `backend/app/services/retrieval.py` 的**行为契约**（做什么），不描述实现细节。
> 由于「混合检索」的完整行为由 `retrieval.py`（语义侧）与 `routers/search.py`（FTS5 清洗 / 关键词检索 / RRF 融合 / 检索端点）共同实现，本规格将路由层的检索管线函数一并纳入（第 3.8–3.11 节），以构成完整的行为契约。
> 依据源码实际内容反向工程整理（2026-08-04）。

## 1. 背景与目标

PaperMind 的检索子系统解决「在个人文献库中快速定位相关内容」的问题，采用**语义检索与关键词检索双路可独立开关、同时开启时 RRF 融合**的架构：

- **语义检索**：`VectorStore` 封装本地 ChromaDB（持久化于 `vector_db/`，cosine 距离），向量由 `EmbeddingService`（BGE-M3）计算；结果带 60 秒内存缓存以降低重复查询的 embedding 与查询开销。
- **关键词检索**：`routers/search.py` 中的 `_keyword_search` 走 SQLite FTS5 虚拟表 `papers_fts`（title/authors/abstract 三列，由触发器与 `papers` 表同步），返回论文级结果。
- **降级设计**：Embedding 模型不可用（未下载/加载失败）时，语义侧通过 `available()` 暴露不可用信号，调用方静默跳过语义检索，系统退化为纯关键词检索，整站不崩溃。
- **安全约束**：FTS 查询串必须先经 `_sanitize_fts_query()` 清洗（宪法第 11 条），杜绝 FTS5 语法错误与 MATCH 注入。

## 2. 范围

### 2.1 包含

- `VectorStore`：ChromaDB 集合初始化、`available()`、`add_chunks()`、`search()`（含 60 秒缓存、超量取回再截断、score 换算、where 过滤构建）、`delete_by_paper_id()`
- `get_vector_store()`：双重检查锁单例
- 路由层检索管线（`routers/search.py`，本规格的另一半）：`_sanitize_fts_query()` 清洗规则、`_keyword_search()` FTS5 检索、`_reciprocal_rank_fusion()` RRF 融合、`POST /api/search` 端点的开关与降级行为

### 2.2 非目标

- Embedding 模型加载、worker 队列、查询前缀（归 `services/embedding.py` 规格）
- 分块策略（归 `TextChunker` / embedding 规格）
- 缓存实现本身（归 `services/cache.py`；本规格只描述 retrieval 对它的使用契约）
- `papers_fts` 建表与触发器（归 `models.py` / `database.py` 规格）
- BGE-Reranker 精排（`config.yaml` 中 `retrieval.rerank` 默认 `false`，代码为预留，当前无任何生效路径）
- 对话中的检索编排（`services/agent_graph.py` 的 `retrieve` 节点仅是本模块的消费者）

## 3. 行为契约

### 3.1 `class VectorStore` / `__init__(self)`

- **输出**：`VectorStore` 实例
- **后置条件**：
  1. `vector_dir` 定位为项目根下 `vector_db/`（`Path(__file__).resolve().parents[3] / "vector_db"`），不存在则创建（含父目录）；
  2. 创建 `chromadb.PersistentClient`（关闭匿名遥测），并 `get_or_create_collection(name="papers", metadata={"hnsw:space": "cosine"})`——集合不存在则新建（cosine 空间），已存在则直接复用（**注意：复用时不会校验既有集合的距离空间**）；
  3. 构造 `EmbeddingService()`（单例，触发其 worker 线程启动，但**不加载模型**）。
- **副作用**：文件系统建目录；ChromaDB 在 `vector_db/` 下初始化/打开持久化文件；实例化 EmbeddingService 单例。
- **异常**：目录不可写、ChromaDB 数据损坏等会向外抛（构造方 `get_vector_store()` 不兜底）。

### 3.2 `VectorStore.available(self) -> bool`

- **输出**：透传 `EmbeddingService.available()`；`True` 表示语义检索可用
- **副作用（重要）**：**首次调用会在调用方线程内同步触发模型加载**（可能含约 2GB 模型下载与数秒至数分钟初始化），不是纯读操作；加载失败后进程内永久锁存为不可用（见 embedding 规格）
- **降级语义**：返回 `False` 时调用方必须跳过 `search()`，退化为纯关键词检索或空结果；本项目所有消费点（search 路由、chat 路由、agent_graph、thesis 路由、eval）均遵守此约定

### 3.3 `VectorStore.add_chunks(self, paper_id: int, chunks: List[Dict[str, Any]], paper_metadata: Optional[Dict[str, Any]] = None)`

- **输入**：
  - `paper_id`：文献主键；chunk id 生成为 `p{paper_id}_c{i}`（`i` 为 chunks 内序号）
  - `chunks`：每个元素必须含 `content` 键（缺失抛 `KeyError`）；可选 `chunk_type`（默认 `"paragraph"`）、`page_number`（非 None 才写入 metadata）
  - `paper_metadata`：可选；取 `title`/`authors`/`year` 三键。`title`/`authors` 为 None 时不写入对应 metadata 键；`year` 为 None 时不写入
- **输出**：无返回值
- **前置条件**：Embedding 模型可用——本方法**内部不做 `available()` 检查**，模型不可用时 `embed()` 会抛 `RuntimeError("Embedding 模型不可用: ...")` 并传播给调用方（当前唯一调用方 `processor.py` 也未检查，PDF 处理任务因此整体失败）
- **后置条件**：全部 chunk 的向量、原文、metadata 写入 `papers` 集合；同一 chunk id 重复 add 会抛 ChromaDB 重复 id 错误（**本方法不幂等**，去重依赖调用方先 `delete_by_paper_id`，`processor.py` 正是这样做的）
- **副作用**：网络无关；调用 embedding worker 队列（阻塞等待）；ChromaDB 持久化写入

### 3.4 `VectorStore.search(self, query: str, top_k: int = 10, filters: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]`

- **输入**：`query` 查询串；`top_k` 返回条数上限；`filters` 可选过滤（见 3.5）
- **输出**：chunk 级结果列表，每项字典：

| 键 | 类型 | 语义 |
|----|------|------|
| `chunk_id` | `str` | `p{paper_id}_c{i}` |
| `paper_id` | `Optional[int]` | 来自 chunk metadata |
| `title` / `authors` / `year` | 可选 | 来自 chunk metadata（写入时缺失则为 None） |
| `content` | `str` | chunk 原文 |
| `page_number` / `chunk_type` | 可选 | 来自 chunk metadata |
| `score` | `float` | `1.0 - cosine_distance`，越大越相似；无距离值时为 0.0 |
| `source` | `str` | 固定 `"semantic"` |

- **行为规则**：
  1. **缓存先行**：缓存键 `f"semantic_search:{hash(query)}:{top_k}:{hash(str(sorted((filters or {}).items())))}"`，命中（`cache.get` 非 None）直接返回缓存列表，不触碰 embedding 与 ChromaDB；
  2. 未命中：`embed_query(query)` 计算查询向量（带 BGE 查询指令前缀），`n_results = max(top_k * 2, 20)` **超量取回**，再截断为 `top_k` 条；
  3. 写入缓存，**TTL = 60 秒**；
  4. ChromaDB 无结果时各字段取 `[[]]` 兜底，返回空列表（空列表也会被缓存 60 秒）。
- **前置条件**：调用方须先确认 `available()` 为 `True`；否则 `embed_query` 抛 `RuntimeError`（本方法自身不检查、不兜底，异常直接传播）。
- **副作用**：embedding 计算；ChromaDB 查询；写全局内存缓存（`services/cache.py` 的 `cache` 实例）。
- **已知契约缺陷**：
  - 缓存键使用 Python 内置 `hash()`：字符串哈希**按进程随机加盐**（PYTHONHASHSEED），键仅在当前进程内稳定（对纯内存缓存无害），且理论上存在哈希碰撞导致串结果的可能；
  - 缓存命中返回的是**同一列表对象的引用**，调用方对元素字典的修改（如 search 路由覆写 `source`）会作用于缓存对象——当前覆写值与原值相同（`"semantic"`），故无实际危害，但属隐患；
  - 60 秒窗口内新增/删除的 chunk 不会反映在缓存结果中（最终一致性）。

### 3.5 `VectorStore._build_where(self, filters: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]`

- **输入**：过滤字典，识别三个键：`year_gte`、`year_lte`、`paper_id`；其余键**静默忽略**
- **输出**：ChromaDB where 字典；`filters` 为空或无可识别键时返回 `None`（不过滤）
- **映射规则**：`year_gte` → `{"year": {"$gte": v}}`；`year_lte` → `{"year": {"$lte": v}}`；`paper_id` → `{"paper_id": v}`
- **组合限制（已对项目锁定的 ChromaDB 0.4.24 实证验证）**：
  - 仅当**恰好一个**条件生效时，产出的 where 合法；
  - `year_gte` 与 `year_lte` 同时给出 → `{"year": {"$gte":…, "$lte":…}}`，ChromaDB 抛 `ValueError: Expected operator expression to have exactly one operator`；
  - `year_*` 与 `paper_id` 同时给出 → 多顶层键字典，ChromaDB 抛 `ValueError: Expected where to have exactly one operator`；
  - 组合过滤的合法写法应为 `$and`，当前实现未做。该异常在 `search()` 内无兜底：`/api/search` 路径会冒泡为 500；chat / agent_graph 路径有 try/except，降级为空结果。

### 3.6 `VectorStore.delete_by_paper_id(self, paper_id: int)`

- **输入**：文献主键
- **输出**：无返回值
- **后置条件**：`papers` 集合中 `metadata.paper_id == paper_id` 的条目全部删除
- **异常**：**内部全捕获**——任何异常仅写 warning 日志（`[VectorStore] 删除 paper {paper_id} 向量失败`，含堆栈），绝不向外抛；调用方（papers 删除路由、processor）无法感知失败
- **注意**：只清 ChromaDB，不清 SQLite `chunks` 表（各自由调用方负责）；不清检索缓存（60 秒内旧结果仍可命中）

### 3.7 `get_vector_store() -> VectorStore`

- **输出**：全进程唯一 `VectorStore` 实例
- **行为**：经典双重检查锁（模块级 `_vector_store_instance` + `threading.Lock`），首次调用时构造，此后直接返回；线程安全
- **副作用**：首次调用触发 3.1 的全部副作用
- **注意**：无重置/关闭接口；测试中通过 monkeypatch 替换本函数（`app.routers.search.get_vector_store`）实现桩化

### 3.8 `routers/search.py :: _sanitize_fts_query(query: str) -> str`（宪法第 11 条安全闸）

- **输入**：用户原始查询串
- **输出**：FTS5 安全的 MATCH 串；空输入或清洗后无有效 token 时返回 `""`（调用方据此跳过关键词检索）
- **清洗规则**（顺序固定）：
  1. 用正则 `[\"*^:()@~<>$\\|+=\[\]{}!?,.;#%&/\-]` 把 FTS5 特殊字符（含连字符、引号、星号、冒号、括号等）**统一替换为空格**——既剥离语法符又充当分词边界（如 `"bge-m3"` → 两个词）；
  2. 按空白分词；无 token 返回 `""`；
  3. 每个 token 包装为双引号短语（literal 匹配，`NEAR`/`AND`/`OR` 等关键字被短语化后失去语法含义）；token 内残留双引号按 FTS5 规则转义为两个双引号（防御性，经第 1 步后实际不会残留）；
  4. token 间以空格连接 = **AND 语义**（全部命中才返回）。
- **示例**：`title:"cancer"*` → `"title" "cancer"`；`"*^:()` / `---` / 空白 → `""`；`结直肠癌 分期` → `"结直肠癌" "分期"`。

### 3.9 `routers/search.py :: _keyword_search(db: Session, query: str, limit: int = 20) -> List[Dict[str, Any]]`

- **输入**：SQLAlchemy 会话、原始查询串、返回上限
- **输出**：论文级结果列表，字段同 3.4 的输出结构，但：`content` = 摘要（无摘要回退标题，再无则 `""`）、`page_number = None`、`chunk_type = "abstract"`、`score = 0.0`（**不提供真实 BM25 分数**）、`source = "keyword"`
- **行为**：先经 `_sanitize_fts_query` 清洗；清洗结果为空串 → 直接返回 `[]`（不碰数据库）；否则以绑定参数执行 `papers_fts MATCH :query` + `JOIN papers`，`ORDER BY rank`（FTS5 内建相关性），`LIMIT :limit`
- **异常**：**全捕获**——任何异常（FTS 表不存在、SQL 错误等）写 warning 日志并返回 `[]`，接口永不因关键词检索失败而 500

### 3.10 `routers/search.py :: _reciprocal_rank_fusion(semantic_results, keyword_results, top_k, k: int = 60) -> List[Dict[str, Any]]`

- **输入**：两路结果列表（各自已按相关性排序）、返回上限、RRF 常数 `k=60`
- **输出**：按 RRF 得分降序的**论文级**结果，至多 `top_k` 条
- **规则**：
  1. 对每路结果按名次累加 `1.0 / (k + rank + 1)`（rank 从 0 起）；两路得分相加；
  2. 以 `paper_id` 为去重键——**chunk 级的语义结果被折叠为论文级**，同一论文多篇 chunk 的 RRF 分会**累计**（变相实现「命中 chunk 多的论文排前」）；
  3. `paper_id` 为 None 的条目跳过；
  4. 每篇论文保留**首次出现**的结果字典作为展示载体（先加语义路，故语义结果优先成为载体；关键词独有的论文则以关键词结果为载体——此时 `score` 恒为 0.0）。
- **注意**：融合不改变结果字典内的 `score` 字段（RRF 分只用于排序，不回写）；`source` 由端点在融合后统一覆写为 `"hybrid"`。

### 3.11 `routers/search.py :: search(request: SearchRequest, db: Session = Depends(get_db))`（`POST /api/search`）

- **输入**：`SearchRequest{query: str, top_k: Optional[int] = 10, filters: Optional[Dict] = {}, use_keyword: Optional[bool] = True, use_semantic: Optional[bool] = True}`
- **输出**：`SearchResponse{query, results: List[SearchResult]}`；`SearchResult.source` ∈ `semantic / keyword / hybrid`
- **行为规则**：
  1. `top_k` 为 None/缺省按 10；`filters` 为 None 按 `{}`；
  2. `use_semantic` 且 `store.available()` → 语义检索，`top_k` 以 `top_k * 2` 传入（为融合预留候选）；模型不可用则**静默跳过**（降级）；
  3. `use_keyword` → 关键词检索，`limit = top_k * 2`；
  4. **双开** → RRF 融合取 `top_k`，所有结果 `source` 覆写为 `"hybrid"`（**即使语义侧因降级实际为空，关键词结果也会被标为 hybrid**）；
  5. **单开** → 两路拼接后截断 `top_k`（单开时另一路为空列表，等价于直接取该路前 `top_k`）；
  6. **双关** → 返回空结果列表，不报错。
- **异常**：语义检索内部异常（含 3.5 的组合 filters 触发的 ChromaDB `ValueError`）**无兜底**，冒泡为 500；关键词检索异常被 3.9 吞掉。

## 4. 边界条件与错误处理

| 场景 | 期望行为 |
|------|----------|
| Embedding 模型不可用（未下载/加载失败） | `available()` 返回 False；所有消费点跳过语义检索；双开时退化为纯关键词检索（结果标 hybrid），单开语义时返回空列表；不崩溃 |
| 空查询串 / 纯特殊字符查询（如 `---`、`"*^:()`） | 清洗后为 `""`，关键词检索跳过返回 `[]`；接口 200 |
| FTS5 注入特征查询（`' OR "1"="1'`、`NEAR(...)`、`cancer*` 等） | 特殊字符被剥离/短语化，接口 200 且不抛异常（宪法第 11 条） |
| 关键词检索 SQL 异常（如 FTS 表缺失） | warning 日志 + 返回 `[]`，接口不 500 |
| `use_semantic` = true 但 `use_keyword` = false，且模型不可用 | 两路皆空，返回空结果 |
| 双开关皆 false | 返回空结果列表，200 |
| `filters` 同时含 `year_gte` + `year_lte`，或 `year_*` + `paper_id` | ChromaDB 抛 `ValueError`（0.4.24 实证）；`/api/search` 无兜底 → 500；chat/agent_graph 有 try/except → 降级为空结果 |
| 语义缓存 60 秒窗口内新增/删除文献 | 缓存结果不反映变更（最终一致性，可接受） |
| `add_chunks` 传空 chunks | 直接返回，无任何写入 |
| `add_chunks` 时模型不可用 | 抛 `RuntimeError`，PDF 处理任务整体失败（processor 未兜底） |
| `delete_by_paper_id` 删除失败 | 仅 warning 日志，不抛异常，调用方无感知 |
| 多线程并发首次调用 `get_vector_store()` | 双重检查锁保证只构造一次 |
| `top_k = 0` 或 None | None → 10；`top_k=0` 时路由层 `top_k or 10` 同样回落为 10 |

## 5. 依赖

- **上游依赖**：
  - `app.core.config.config`（当前 retrieval.py 导入但实际未使用——历史遗留导入）
  - `app.core.logger.logger`
  - `app.services.embedding.EmbeddingService`（向量化与可用性信号）
  - `app.services.cache.cache`（全局内存缓存实例，默认 TTL 300s/容量 1000，retrieval 显式以 ttl=60 写入）
  - `chromadb` 0.4.24（PersistentClient，cosine HNSW）
  - 路由层另依赖：`app.models` 的 `papers_fts` 虚拟表与同步触发器、`app.schemas.SearchRequest/SearchResponse/SearchResult`
- **下游消费者**：
  - `routers/search.py`（混合检索端点）
  - `routers/chat.py`（对话前检索相关片段，top_k=5，try/except 兜底）
  - `services/agent_graph.py`（`retrieve` 节点，支持 `paper_id` 过滤，try/except 兜底）
  - `routers/thesis.py`（段落相关文献推荐，top_k=5，先查 `available()`）
  - `routers/papers.py`（删除文献时 `delete_by_paper_id`）
  - `services/processor.py`（PDF 处理流水线 `add_chunks`，先删旧后写新）
  - `eval/run.py`（RAG 评测，支持 `--keyword-only` 降级）

## 6. 验收标准（可测试）

- [ ] AC1：`_sanitize_fts_query` 对普通词、多词、特殊字符、FTS 关键字、纯特殊字符、空/空白、连字符、中文 token 的输出符合 3.8 规则
- [ ] AC2：含 FTS5 语法符/注入特征的查询走 `POST /api/search` 不返回 500，且响应结构合法
- [ ] AC3：清洗后无有效 token 时关键词检索返回空列表（不报错）
- [ ] AC4：keyword-only 模式能命中 FTS 表中文献，`source == "keyword"`
- [ ] AC5：多 token AND 语义——部分词不命中时返回空
- [ ] AC6：混合模式 + 语义模型不可用 → 优雅降级，仅靠关键词结果返回且接口 200
- [ ] AC7：语义检索结果写入 60 秒 TTL 缓存，缓存命中时不重复调用 embedding 与 ChromaDB（**当前无测试**）
- [ ] AC8：`_build_where` 单条件（year_gte / year_lte / paper_id 任一）产出合法 where；组合条件的行为被明确规约（**当前无测试，且实现存在 3.5 所述缺陷**）
- [ ] AC9：RRF 融合按 `1/(k+rank+1)` 计分、按 paper_id 去重、同论文多 chunk 分数累计（**当前无测试**）
- [ ] AC10：`get_vector_store()` 单例：多次调用返回同一对象（**当前无测试**）

## 7. 现有测试覆盖与盲区

- **已覆盖**（`backend/tests/test_search.py`，全部经路由层桩化 `get_vector_store` → `_StubVectorStore.available() = False`）：
  - `TestSanitizeFtsQuery`：AC1 的 8 个用例（普通词/多词 AND/特殊字符剥离/NEAR·OR 短语化/纯特殊字符返空/空与空白/连字符与标点分词/中文 token）
  - `TestSearchApi`：AC2（10 个注入特征参数化查询不 500）、AC3、AC4、AC5、AC6（混合模式语义降级）
  - 测试环境不加载真实 BGE-M3、不访问真实 `vector_db/`（monkeypatch 桩 + 内存 SQLite + 手动 `ensure_papers_fts`）
- **盲区**（按严重程度标注）：
  - **高**：`VectorStore` 本体完全无测试——`search()` 的超量取回与截断、`score = 1 - distance` 换算、60 秒缓存命中/过期/键构成（含 filters 参与键）、空结果也缓存，均无覆盖
  - **高**：`_reciprocal_rank_fusion` 无直接单测——RRF 计分公式、paper_id 去重、同论文多 chunk 分数累计、载体选择优先级（语义优先）均未验证；现有「混合降级」用例只走了单路非空的融合路径
  - ~~**高**：`_build_where` 组合过滤缺陷~~ **已修复（Batch7-F1, 6cec40c）**：多条件包装 `$and` + `_query_with_fallback` 降级兜底，`tests/test_search.py::TestBuildWhere` 4 用例固化
  - **中**：`add_chunks` 的 id 生成、metadata 条件写入（title/authors/year/page_number 为 None 时不写）、空 chunks 短路、重复 id 不幂等，无测试
  - **中**：`delete_by_paper_id` 的按 metadata 过滤删除与吞异常行为，无测试
  - **中**：`get_vector_store()` 单例与并发双重检查锁，无测试
  - **中**：`_keyword_search` 内部异常降级（FTS 表缺失 → 返回 `[]`）无测试
  - **低**：双开关皆 false → 空结果；`top_k=None/0` → 回落 10；融合结果 `source` 恒被覆写为 `hybrid`（语义降级时关键词结果被误标 hybrid），无测试
  - **低**：缓存键使用进程加盐 `hash()` 的碰撞/跨进程不稳定特性，无测试（实际影响小）

## 8. 关键设计决策

- **ChromaDB 本地持久化 + cosine**：本地优先原则（宪法第 1 条），向量库与 SQLite 并列存于项目根 `vector_db/`；cosine 距离配合 BGE-M3 的 L2 归一化向量，`score = 1 - distance` 即余弦相似度
- **语义/关键词独立开关 + RRF 融合**：语义擅长意译匹配、关键词擅长精确术语；RRF（k=60）不需要两路分数同分布，只依赖名次，天然适合「语义有 cosine 分、关键词 BM25 分不外露（score=0.0）」的场景
- **超量取回再截断（`n_results = max(top_k*2, 20)`）**：为路由层融合预留候选池，同时保证小 top_k 时有足够候选；路由层再各取 `top_k*2` 融合、最终截 `top_k`
- **60 秒缓存语义结果**：embedding 计算（CPU/MPS）与 ChromaDB 查询是检索链路最重环节；短 TTL 在「重复翻页/相似查询提速」与「库内容变更可见性」之间取折中
- **单例 + 双重检查锁**：ChromaDB PersistentClient 初始化昂贵且多实例指向同一目录有风险；锁保证并发首调只构造一次
- **`available()` 显式降级信号而非内部兜底**：`search()`/`add_chunks()` 内部不检查模型可用性，把降级决策交给调用方——检索类调用方（search/chat/thesis/agent_graph/eval）检查并跳过；处理类调用方（processor）不检查、让任务失败并暴露错误。两套语义由调用场景决定，是有意为之
- **FTS 清洗放路由层而非服务层**：`_sanitize_fts_query` 与 `_keyword_search` 位于 `routers/search.py`（唯一天然消费点），宪法第 11 条的安全闸紧贴输入边界；`retrieval.py` 只负责向量侧
- **RRF 按 paper_id 去重、同论文分数累计**：检索目标是「找文献」而非「找片段」，多篇 chunk 命中同一论文应提升该论文排名；展示载体取首次出现项（语义优先），牺牲「展示最相关片段」换取实现简单
- **`delete_by_paper_id` 吞异常**：文献删除是级联清理的最后一环，向量清理失败不应阻断主流程（SQLite 已删），残留向量至多成为无属主的孤儿数据
- **`retrieval.rerank` 配置为预留**：`config.yaml` 默认 `false`，BGE-Reranker 精排代码当前不存在生效路径；融合完全由 RRF 承担
