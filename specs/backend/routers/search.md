# routers/search.py（混合检索端点 POST /api/search）规格说明书

> 本文件描述 `backend/app/routers/search.py` 的**行为契约**（做什么），不描述实现细节。
> 本模块是检索子系统的 HTTP 包装层：FTS5 清洗、关键词检索、RRF 融合、ChromaDB where 构建等服务侧契约统一归 `specs/backend/services/retrieval.md`（其 3.8–3.11 节已覆盖本文件中的管线函数），本规格聚焦端点签名、参数契约、开关组合行为、状态码与错误路径，不重复服务层细节。
> 依据源码实际内容反向工程整理（2026-08-04，search.py 全文 134 行）。

## 1. 背景与目标

`POST /api/search` 是前端检索页（`SearchPage`）与 MCP 工具之外唯一的检索 HTTP 入口，把「语义检索（ChromaDB + BGE-M3）」与「关键词检索（SQLite FTS5）」两路能力以**可独立开关**的方式暴露给客户端，双开时用 RRF 融合排序。设计目标：

- 调用方无需感知两路检索的差异与降级逻辑（Embedding 模型不可用时静默退化为关键词检索）；
- FTS 注入防护紧贴输入边界（宪法第 11 条，`_sanitize_fts_query` 位于本模块）；
- 结果为论文级（RRF 按 `paper_id` 去重），直接驱动前端文献卡片展示。

## 2. 范围

### 2.1 包含

- `POST /api/search` 端点（`search()`）的请求/响应契约、参数回落规则、四种开关组合行为、`source` 字段覆写语义
- 端点级错误路径：哪些异常有兜底、哪些冒泡为 500（含 `_build_where` 组合过滤缺陷在路由层**无兜底**的明确规约）
- 请求/响应 Pydantic 模型（`SearchRequest` / `SearchResponse` / `SearchResult`，定义于 `app/schemas.py`）的字段契约

### 2.2 非目标

- `_sanitize_fts_query()` 清洗规则、`_keyword_search()` FTS5 检索、`_reciprocal_rank_fusion()` RRF 融合算法、`VectorStore._build_where()` 映射与组合过滤缺陷的实证细节——**全部归 `specs/backend/services/retrieval.md`（3.8–3.11、3.5 节）**，本规格仅在端点行为中引用其结论
- `VectorStore` 本体（缓存、超量取回、score 换算）：归 retrieval.md
- Embedding 模型加载与 `available()` 语义：归 `specs/backend/services/embedding.md`
- `papers_fts` 建表与触发器：归 `specs/backend/models.md`
- 对话链路中的检索编排（`agent_graph` 的 retrieve 节点）：归 `specs/backend/services/agent_graph.md`
- MCP Server 的 `search_papers` 工具：归 `specs/backend/services/mcp_server.md`

## 3. 行为契约

### 3.1 路由挂载

- `main.py`：`app.include_router(search.router, prefix="/api/search", tags=["search"])`；端点装饰器为 `@router.post("")`，故完整路径为 **`POST /api/search`**（无尾斜杠；FastAPI 默认 `redirect_slashes`，`POST /api/search/` 会 307 重定向到 `/api/search`）。
- 路由层**不读 `config.yaml` 的 `retrieval.*` 配置**（`retrieval.rerank` 等为预留，当前无任何生效路径）。

### 3.2 `search(request: SearchRequest, db: Session = Depends(get_db))`（`POST /api/search`）

- **输入**（JSON 请求体，`SearchRequest`，照抄 `schemas.py`）：

| 字段 | 类型 | 缺省 | 约束 |
|------|------|------|------|
| `query` | `str` | **必填**（缺失 → 422） | 无长度/内容校验；空串合法 |
| `top_k` | `Optional[int]` | `10` | 无范围校验；`None` 或 `0` 经 `top_k or 10` 回落为 10；负数不拦截（行为见第 4 节） |
| `filters` | `Optional[Dict[str, Any]]` | `{}` | 仅 `year_gte` / `year_lte` / `paper_id` 三键被语义侧识别，其余静默忽略（retrieval.md 3.5） |
| `use_keyword` | `Optional[bool]` | `True` | 关键词检索开关 |
| `use_semantic` | `Optional[bool]` | `True` | 语义检索开关 |

- **输出**：`SearchResponse{query: str, results: List[SearchResult]}`；`SearchResult` 字段照抄 schemas：`paper_id: int`、`title/authors/year: Optional`、`content: str`、`page_number: Optional[int]`、`chunk_type: str`、`score: float`、`source: str`。`source` 取值语义：
  - `"semantic"`：仅语义路单开时的结果；
  - `"keyword"`：仅关键词路单开时的结果；
  - `"hybrid"`：**双开时全部结果无条件覆写**——即使语义侧因模型不可用实际为空、结果全部来自关键词路，也标 `hybrid`（已知语义噪声，见第 8 节）。
- **前置条件**：无（不要求文献库非空；FTS 表不存在时关键词路内部降级为空列表，接口仍 200）。
- **后置条件**：`len(results) <= top_k`（回落后）；论文级去重仅在双开融合路径保证（单开路径不去重，但单开时只有一路有结果，该路自身语义侧为 chunk 级、可能同论文多条——见第 4 节）。
- **行为规则**（按代码顺序）：
  1. `filters = request.filters or {}`；`top_k = request.top_k or 10`；
  2. `use_semantic` 为真**且** `store.available()` 为真 → `store.search(query, top_k=top_k*2, filters=filters)`（超量取回为融合预留候选），结果 `source` 覆写为 `"semantic"`；模型不可用则**静默跳过**（不报错、不标记）；
  3. `use_keyword` 为真 → `_keyword_search(db, query, limit=top_k*2)`；清洗后无有效 token 或 SQL 异常时返回 `[]`（retrieval.md 3.9，永不因关键词路 500）；
  4. **双开** → `_reciprocal_rank_fusion(semantic, keyword, top_k)`（RRF k=60，按 `paper_id` 去重、同论文多 chunk 分数累计、载体取先出现的语义项），全部结果 `source` 覆写 `"hybrid"`；
  5. **单开** → `(semantic_results + keyword_results)[:top_k]`（另一路为空列表，等价于取该路前 `top_k`；**此路径不做论文级去重**，语义单开时同一论文可出现多条 chunk）；
  6. **双关**（`use_semantic=False` 且 `use_keyword=False`）→ 两路皆空，返回 `{"query": ..., "results": []}`，200。
- **副作用**：ChromaDB 查询 + 写 60 秒语义缓存（仅语义路，retrieval.md 3.4）；FTS5 只读查询；`_keyword_search` 异常时写 `[search]` warning 日志；**无任何 DB 写入**。
- **异常与兜底（任务关切的明确规约）**：
  - 关键词路异常：**函数内全捕获**，warning 日志 + 返回 `[]`——路由层有兜底；
  - 语义路 `store.available()` 为假：跳过——有降级；
  - 语义路 `store.search()` 抛异常（**含 `_build_where` 组合过滤触发的 ChromaDB `ValueError`**：`year_gte+year_lte` 同给、或 `year_*` 与 `paper_id` 同给，0.4.24 实证必抛，见 retrieval.md 3.5）：**路由层（search.py 本文件）没有任何 try/except 兜底**，异常冒泡出端点，由 `main.py` 全局异常处理器统一捕获，返回 **500 + 通用脱敏 JSON**（`{"detail": "服务器内部错误，请稍后重试", "error_code": "internal_error", "path": "/api/search"}`），异常原文只写 `logs/app.log`（宪法第 13 条）。**即：组合过滤 = 500，不降级、不返回部分结果**；
  - Pydantic 校验失败（如缺 `query`、`top_k` 非整数）：FastAPI 自动 422。

## 4. 边界条件与错误处理

| 场景 | 期望行为 |
|------|----------|
| `use_semantic=true, use_keyword=false`，模型不可用 | 两路皆空 → `results=[]`，200（静默降级） |
| 双开关皆 false | `results=[]`，200 |
| 空查询串 / 纯特殊字符（`---`、`"*^:()`） | 关键词路清洗为空跳过；语义路若可用仍会以空串调 embed（服务层行为）；接口不报错 |
| FTS5 注入特征查询（`' OR "1"="1`、`NEAR(...)`、`cancer*` 等） | 特殊字符剥离/短语化，200（宪法第 11 条） |
| `filters` 组合条件（`year_gte`+`year_lte`，或 `year_*`+`paper_id`）且语义路实际执行 | ChromaDB `ValueError` → **500**（路由层无兜底，全局异常处理器脱敏） |
| `filters` 组合条件但模型不可用 / `use_semantic=false` | 语义路不执行，过滤缺陷不触发，200 |
| `filters` 含未识别键 | 静默忽略，不过滤 |
| `top_k=None` / `0` | 回落 10 |
| `top_k` 为负数 | 不拦截；语义/关键词 `LIMIT`/`n_results` 为负的行为由 SQLite/ChromaDB 决定，融合路径 `sorted_pids[:负数]` 在 Python 中返回**去掉尾部 |top_k| 条后的列表**（非空时结果异常多）——未规约的脏输入，前端不产生 |
| 语义单开且同论文多 chunk 命中 | 不去重，同一 `paper_id` 可出现多条（与双开路径行为不同） |
| 语义结果 `paper_id` 为 None（老旧脏数据） | 双开：RRF 跳过该条；单开：`SearchResult(**r)` 构造时 `paper_id: int` 校验失败 → 500（理论路径，正常数据不触发） |
| 语义缓存 60 秒窗口内库内容变更 | 结果不反映变更（最终一致性，retrieval.md 3.4） |
| 并发请求 | 无共享可变状态；`get_vector_store()` 单例双检锁 |

## 5. 依赖

- **上游依赖**：
  - `app.schemas.SearchRequest / SearchResponse / SearchResult`
  - `app.database.get_db`（请求级 SQLite 会话）
  - `app.services.retrieval.get_vector_store`（VectorStore 单例；测试经 monkeypatch `app.routers.search.get_vector_store` 桩化）
  - `app.models` 的 `papers_fts` 虚拟表（经 `_keyword_search` 的 SQL）
  - `app.core.logger.logger`
  - `main.py` 全局异常处理器（500 脱敏的唯一兜底）
- **下游消费者**：前端 `SearchPage`（经 `api.js`）；无其他后端消费者（chat/thesis/agent_graph 直接用 `VectorStore`，不经本端点）。

## 6. 验收标准（可测试）

- [ ] AC1：含 FTS5 语法符/注入特征的查询不返回 500，响应结构合法（`query` 回显、`results` 为列表）
- [ ] AC2：清洗后无有效 token 时返回空列表，200
- [ ] AC3：keyword-only 模式命中 FTS 文献，`source == "keyword"`
- [ ] AC4：多 token AND 语义——部分词不命中时返回空
- [ ] AC5：双开 + 语义模型不可用 → 优雅降级，仅靠关键词结果返回，200
- [ ] AC6：双关（两开关皆 false）→ `results == []`，200（**当前无测试**）
- [ ] AC7：`top_k=None` / `0` → 回落 10（**当前无测试**）
- [ ] AC8：语义单开时结果 `source == "semantic"` 且不做论文级去重（**当前无测试**，需可用桩 VectorStore）
- [ ] AC9：双开且语义有结果时全部结果 `source == "hybrid"`；语义降级时关键词结果也被误标 `hybrid`（**当前无测试**，固化现有语义）
- [ ] AC10：`filters` 组合过滤 + 语义路执行 → 500 且响应为全局脱敏格式（`error_code == "internal_error"`）（**当前无测试**；缺陷修复后本 AC 应改写为「组合过滤合法生效」）
- [ ] AC11：缺 `query` 字段 → 422（**当前无测试**）

## 7. 现有测试覆盖与盲区

- **已覆盖**（`backend/tests/test_search.py`，全部经 monkeypatch 桩化 `get_vector_store` → `available()=False`）：
  - `TestSanitizeFtsQuery`（8 用例，服务层契约，见 retrieval.md 第 7 节）
  - `TestSearchApi`：AC1（10 个注入特征参数化用例）、AC2、AC3、AC4、AC5
- **盲区**（端点层；服务层盲区见 retrieval.md 第 7 节，不重复）：
  - **高**：`_build_where` 组合过滤 → 500 的缺陷路径（AC10）无测试暴露——路由层无兜底这一行为无任何用例固化
  - **中**：双开关皆 false、`top_k` 回落（AC6/AC7）无测试
  - **中**：语义可用桩下的路径全无——`source="semantic"`、单开不去重、双开 RRF 融合后 `source="hybrid"`、语义降级误标 hybrid（AC8/AC9）均无测试（现有桩恒不可用）
  - **低**：缺 `query` 的 422、`filters` 未识别键忽略、尾斜杠 307 重定向，无测试
  - **低**：`top_k` 负数等脏输入行为未规约、无测试

## 8. 关键设计决策

- **管线函数放路由层而非服务层**：`_sanitize_fts_query` / `_keyword_search` / `_reciprocal_rank_fusion` 位于 `routers/search.py`（唯一天然消费点），宪法第 11 条安全闸紧贴输入边界；retrieval.md 将它们纳入服务侧规格只为构成完整检索契约，物理位置不变。
- **双开时 `source` 无条件覆写 `hybrid`**：实现简单（融合后统一打标），代价是语义降级时关键词结果被误标；前端仅用 `source` 做展示徽标，无逻辑分支，误标无害，故保留现状并在 AC9 固化。
- **各取 `top_k*2` 再融合截 `top_k`**：为 RRF 预留候选池，与 `VectorStore.search` 内部的 `max(top_k*2, 20)` 超量取回叠加（服务层决策，见 retrieval.md 第 8 节）。
- **关键词路吞异常、语义路不吞**：关键词路（FTS5）是兜底链路，必须永不 500；语义路异常（含组合过滤缺陷）选择冒泡 500 而非静默降级——**这并非有意设计而是缺陷未修**（retrieval.md 3.5 已实证），修复前以 AC10 固化现有行为，修复时同步修订本规格与 retrieval.md。
- **单开路径不做论文级去重**：单开语义时直接展示 chunk 级结果（含页码），服务「定位片段」场景；双开才折叠为论文级。两路径粒度不一致是刻意取舍。
