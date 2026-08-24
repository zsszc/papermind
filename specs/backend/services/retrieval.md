# 检索服务规格（Batch 22B 当前实现）

> 适用文件：`app/services/retrieval.py`、`app/services/retrieval_pipeline.py`、
> `app/routers/search.py`。最后核对：2026-08-24。

## 1. 分层边界

- `VectorStore` 是低层 BGE-M3/Chroma 适配器，只负责向量增删查、rerank 和 60 秒语义缓存。
- `RetrievalPipeline` 是聊天、重新生成、深度综述、论文引用推荐与 eval 共用的 chunk 级管线，负责 semantic、轻量 BM25、
  bilingual expansion、chunk-id RRF、filters 和降级诊断。
- `/api/search` 是论文级搜索适配器：语义 chunk + `papers_fts` title/authors/abstract，
  按 paper_id RRF。它与 RAG 的 chunk 证据检索不是同一返回粒度。
- graph expansion 是共享管线后的可选增强；显式 `paper_id` 范围下必须跳过，禁止扩展到其他论文。

## 2. VectorStore 契约

### 2.1 `add_chunks`

- chunk id 优先使用输入 `id`，否则为 `p{paper_id}_c{i}`；写入使用 Chroma `upsert`，
  崩溃重试与重复处理幂等。
- metadata 含 paper_id/chunk_index/chunk_type，按存在性附 title/authors/year/page_number。
- 成功写入后清除 `semantic_search:` 缓存。

### 2.2 `search`

- 参数：`query/top_k/filters/rerank/rerank_diagnostics`。
- 缓存键绑定 query、top_k、filters、vector_dir、rerank 开关和模型；TTL 60 秒。
- 缓存读写和返回值均深拷贝。调用方修改 `source/content` 不得污染后续请求。
- Chroma 候选数为 `max(top_k*2, 20)`；cosine score 为 `1-distance`。
- rerank 默认读取配置；失败返回原始顺序、写 diagnostics，失败结果不缓存。

### 2.3 filters 与 fail-closed

- 仅支持 `paper_id/year_gte/year_lte`；未知键抛 `ValueError`，不得静默忽略。
- 多条件按 Chroma `$and` 组合。
- Chroma 拒绝限制性 where 时返回同构空结果，不得重试 `where=None`。上层可用同一
  filters 的关键词路降级，但绝不能扩大用户指定范围。

## 3. RetrievalPipeline 契约

### 3.1 输入与 profile

```python
RetrievalPipeline(db, vector_store=store).search(
    query,
    top_k=5,
    filters={},
    profile="semantic" | "hybrid" | "keyword",
    lexical_profile="count" | "bm25" | "bm25-bilingual" |
                    "bm25-bilingual-neighbor",
    rerank=None,
    diagnostics={},
    rerank_diagnostics={},
)
```

- 正式生产默认：`profile=hybrid`、`lexical_profile=bm25-bilingual`、top_k=5。
- hybrid 的语义与词法候选池均为 `top_k*2`，按 chunk id 用 RRF(k=60) 去重排序。
- 兼容别名 `hybrid-bilingual` 等价于 hybrid + bm25-bilingual。
- RRF 保留首个命中分支的展示元数据/source；effective profile 只由 diagnostics 表达。

### 3.2 词法行为

- 技术 tokenizer 保留英文、数字、连字符、小数、科学计数法和百分号。
- bilingual 只使用代码中有限、可审计的中英领域词表，不做整句机器翻译。
- 词法结果必须含完整统一字段：chunk_id/paper_id/title/authors/year/content/
  page_number/chunk_type/score/source。
- DB 查询同时应用 paper_id 与年份范围，禁止跨论文/年份泄漏。
- `bm25-bilingual-neighbor` 是历史实验兼容 profile，生产默认不启用。

### 3.3 降级诊断

diagnostics 固定包含：

```json
{
  "requested_profile": "hybrid",
  "effective_profile": "hybrid|keyword-only|semantic-only|empty",
  "degraded": false,
  "reason": null
}
```

- semantic 不可用/运行异常：hybrid 返回同 filters 的关键词结果并标降级。
- 词法异常：返回语义结果并标降级；两路均失败返回空。
- 生产聊天以可用性优先；eval 读取 diagnostics，只要运行期降级就使质量 Gate 失败。
- 管线结果均为私有副本，不新增 hybrid 缓存。

## 4. `/api/search` 论文级契约

- FTS MATCH 查询必须先清洗并用绑定参数执行；特殊字符不能成为 FTS 语法。
- 关键词路应用与语义路相同的 paper_id/year filters。
- 路由不得原地修改 VectorStore 返回对象；语义/Hybrid source 用新字典生成。
- 语义与关键词双开时按 paper_id RRF；单开时返回对应分支；模型不可用、状态检查异常或
  查询异常时安全退为关键词，不能让仍可用的 FTS 路径返回 500。

## 5. 配置

```yaml
retrieval:
  chat_profile: hybrid
  lexical_profile: bm25-bilingual
  rerank: false
  rerank_model: BAAI/bge-reranker-v2-m3
```

本地 reranker 在当前 CPU 上不满足 P95<1s，必须保持关闭。`hybrid_weight` 是历史保留项，
当前 RRF 不读取它。

## 6. Harness

- `test_retrieval_pipeline.py`：融合、双路过滤、降级、复制隔离。
- `test_retrieval_pipeline_parity.py`：聊天与 eval chunk ID/顺序逐项一致。
- `test_cache_invalidation.py`：缓存失效与可变对象隔离。
- `test_search.py`：FTS 安全、filters、Chroma where fail-closed、语义异常关键词降级。
- `test_agent_graph.py` / `test_chat.py`：生产接线、重新生成与引用下游兼容。
- `test_deep_review.py` / `test_routes_sanitize.py`：旁路共享 profile、关键词降级与零证据拒绝生成。
