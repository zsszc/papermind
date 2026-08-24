# Batch 20 规格：共享检索管线与生产 Hybrid 晋级

## 1. 背景与问题

Batch 19 首次测得与聊天一致的纯语义 top-5：private dev Recall@5/MRR/NDCG@5 为
0.500/0.268/0.324，factoid Recall 为 0。与此同时，评测专用的 BGE-M3 +
`bm25-bilingual` + chunk RRF 达到 0.625/0.394/0.452，但这套实现位于
`eval/run.py`，聊天无法使用，指标不能代表生产效果。

Harness 还确认了三项正确性风险：

- `paper_id` 等限制性 Chroma 过滤被拒绝时会退成无过滤查询，可能混入范围外文献；
- 搜索页关键词路不应用 `paper_id/year` filters，hybrid 结果可能越过筛选范围；
- `VectorStore.search()` 缓存返回可变对象，调用方原地修改 `source` 会污染后续查询。

本批先原样下沉已验证的词法/RRF 算法并证明聊天与评测逐项一致，再只改变一个生产
策略变量：`semantic` 切换为既有 `hybrid + bm25-bilingual`。不在同批加入新的术语表、
邻块加权、Graph 或本地 reranker，避免无法归因。

## 2. 范围

### S1：共享 chunk RetrievalPipeline

- `VectorStore` 继续只负责 embedding/Chroma；新增服务层管线负责 semantic、chunk BM25、
  bilingual expansion、chunk-id RRF 和降级诊断。
- 管线显式接收 DB Session、VectorStore、query、top_k、filters、profile 和 lexical profile；
  返回统一字段的 chunk 列表与 requested/effective profile、降级原因、rerank 诊断。
- `hybrid` 两路候选池均为 `top_k * 2`，RRF 常数和稳定排序保持现有评测行为；不新增
  hybrid 缓存，只复用底层语义缓存。
- 词法结果必须补齐 `chunk_id/paper_id/title/authors/year/content/page_number/chunk_type/score/source`，
  并与语义路应用同一 `paper_id/year` filters。
- 返回对象必须与底层缓存隔离；任何调用方改写结果都不能污染后续命中。

### S2：限制性过滤 fail-closed

- 有限制性 `where` 时，Chroma 拒绝条件不得重试无过滤查询；应返回空语义结果或交由
  管线以相同 filters 的词法路降级。
- `/api/search` 的关键词路必须应用同一 `paper_id/year_gte/year_lte` 条件。
- 保持异常不导致 500，但不得以“可用性”为由放宽用户指定范围。

### S3：聊天与评测同源

- Agent 图聊天检索与消息重新生成都调用共享管线，top_k 固定为 5；指定 paper 时两路均
  只返回该 paper。
- eval 的生产 profile 只能调用共享管线；显式 VectorStore snapshot 约束继续保留，严禁
  隐式连接主向量库。
- 增加 parity Harness：同一 DB、同一假向量结果、同一 query/profile 下，聊天和 eval 的
  chunk ID 与顺序逐项一致。
- 论文级 `/api/search` 保留 FTS 适配层；`thesis` 与 `deep_review` 的迁移明确延期到后续批次，
  避免把不同检索契约强行混合。

### S4：可审计质量与延迟 Gate

- eval 报告同时记录 Recall@5、MRR、NDCG@5、factoid Recall、P95 和运行期降级状态；CLI
  支持为这些指标设置显式阈值，任一失败均退出 1。
- private 只运行 `dev`；本批不读取、不运行 holdout，不调用外部 LLM。
- 重构 parity Gate：共享 hybrid 必须与 Batch 18 有效 hybrid 的逐题结果一致。
- 生产晋级 Gate：Recall@5 >= 0.625、MRR >= 0.394、NDCG@5 >= 0.452、factoid Recall
  >= 0.333、P95 < 1000ms、runtime degradation=0。
- 公开冻结 BM25 继续保持 0.900/0.783/0.813，Recall@5 Gate >= 0.85。

## 3. 配置与兼容

- 新增 `retrieval.chat_profile`（`semantic`/`hybrid`）与
  `retrieval.lexical_profile`（本批生产仅允许 `bm25-bilingual`）。
- 先以 `semantic` 完成代码 parity；仅在全部晋级 Gate 通过后，单独提交把生产默认改为
  `hybrid`。已有私有配置缺少字段时使用代码默认值，默认值必须与公开模板一致。
- `retrieval.rerank` 保持 `false`；`graph_expand` 保持 `false`；历史
  `retrieval.hybrid_weight` 本批不改变含义，也不得偷偷参与 RRF。

## 4. 验收标准

1. 共享管线纯函数、过滤、降级、复制隔离和 parity 自动化测试先 RED 后 GREEN。
2. 限制性过滤绝不 fail-open；搜索关键词/hybrid filters 有接口级测试。
3. 聊天首次发送与重新生成均无私有 VectorStore 旁路。
4. private dev 指标和延迟通过 S4 Gate；公开冻结基准不回退。
5. 后端、前端、Electron 全量测试，前端 lint/build、依赖检查通过；报告只提交聚合指标，
   不提交真实 QA、证据或私有路径内容。
