# Phase G：GraphRAG 引用图谱 规格说明书

> 来源：Phase 2 计划 Phase G 章节 + models/database/agent_graph/papers/search 现状契约。
> 目标：文献间引用关系成图，支持「这个方法的后续工作有哪些」多跳问答。

## 1. 背景与目标

现状检索只有向量/关键词两路（search.py，RRF 融合）。文献之间的引用关系是未利用的强信号：命中一篇关键文献后，其参考文献与被引文献大概率相关。本阶段：解析 PDF 参考文献 → 建库内引用边 → 图谱扩展检索。

**范围纪律**：不引 GROBID 等重依赖；正则+启发式解析，接受不完美召回（先建图、后提精度）。

## 2. 现状（代码实证）

- `models.py`：11 表 + FTS 虚拟表；**无 Alembic**，新表走 `database.py ensure_schema()` 轻量迁移分支（宪法）
- `services/processor.py`：论文入库流水线（PDF 解析→分块→向量化），B2 摘要 chunk 已接入
- `services/agent_graph.py`：5 节点（load_memory → retrieve → external_tools → build_messages）——graph_expand 插 retrieve 之后
- `routers/search.py`：RRF 融合函数可复用
- `routers/papers.py`：文献 CRUD 路由，citation-graph 端点加这里
- 依赖：pdfplumber 已在栈；标题模糊匹配用 **stdlib difflib**（零新增依赖）

## 3. 设计

### 3.1 G1：参考文献解析与引用边（`services/reference_parser.py` 新建）

- **新表** `paper_citations`：`id, citing_id, cited_id, created_at`（citing_id → cited_id，均 FK papers.id）；唯一约束 (citing_id, cited_id)；`ensure_schema()` 加迁移分支（`CREATE TABLE IF NOT EXISTS` 风格，与现有分支同构）
- **解析**：取 PDF 全文末尾「References/参考文献」段（启发式定位最后一个独立标题行），按编号条目切分（`[1]` / `1.` 两种主流格式），每条提取标题候选（引号内或年份前的最长段）
- **匹配**：标题候选 vs 库内 papers.title，difflib.SequenceMatcher 相似度 ≥ 0.85 记一条边；自引跳过；重复边去重
- **接入点**：processor 流水线尾部追加 reference 解析步骤（**失败隔离**：解析异常仅记 `[references]` warning，不影响入库主流程）；另提供 `scripts/backfill_citations.py` 对存量论文回填（幂等：先清该 paper 的出边再重建）
- **日志前缀** `[references]`

### 3.2 G2：图谱扩展检索节点 + 图谱 API

- `agent_graph.py` 新增 `graph_expand` 节点（retrieve 之后、external_tools 之前）：
  - 取 retrieve 命中的去重 paper_id 集合，沿 `paper_citations` 边扩展 1 跳（初版 1 跳，2 跳留配置位）
  - 对扩展到的 paper 取其代表性 chunk（每篇至多 2 个：摘要 chunk 优先，否则首 chunk）
  - 与向量召回结果 RRF 融合（复用 search.py 的融合函数或同构实现），合并后 top_k 不变
  - **降级契约**：无引用边/任何异常 → 透传 retrieve 结果不变；开关 `retrieval.graph_expand`（config.yaml，默认 false）
- `GET /api/papers/{id}/citation-graph`：返回 `{nodes: [{id,title,year}], edges: [{citing,cited}]}`——以该文献为中心的 1 跳子图（出边+入边）；papers 路由新增；schemas.py 加响应模型
- **eval 门控**：config 开关 on/off 各跑一次 eval，结果入 trend（graph 扩展不改变 relevant 集解析逻辑，分母稳定可比）

### 3.3 测试计划

- G1：构造含 References 段的文本断言条目解析数；构造库内标题断言匹配率与阈值边界；ensure_schema 新分支幂等（二次启动不报错）；processor 失败隔离用例
- G2：内存库造边 → 断言扩展 chunk 注入与 RRF 融合序；开关关闭时字节级不回归；API 端点 200/404
- eval A/B 对比为人工门禁（主代理执行）

## 4. 接口与数据

- 新表 `paper_citations`（ensure_schema 迁移分支）
- 新增内部服务 `reference_parser`；agent_graph 拓扑变 5 节点
- 新增 `GET /api/papers/{id}/citation-graph`
- config.yaml 增 `retrieval.graph_expand: false`（默认关）

## 5. 验收标准（可测试）

- [ ] AC1：解析/匹配/建边单测全绿；ensure_schema 迁移分支幂等
- [ ] AC2：processor 接入后入库流程既有测试不回归；解析失败不影响入库
- [ ] AC3：graph_expand 开/关两条路径用例；异常降级透传
- [ ] AC4：citation-graph 端点 200 结构断言 + 404
- [ ] AC5：eval graph on/off 对比入 trend（主代理门禁执行）
- [ ] AC6：全套件全绿；零新增依赖

## 6. 现有测试覆盖与盲区

- 新增 `tests/test_reference_parser.py`、`tests/test_graph_expand.py`、papers 路由图谱用例
- 盲区/遗留：解析精度量化（无标注集，先记解析/匹配计数日志）；2 跳扩展；前端图谱可视化（Phase H）

## 7. 风险与回退

- **解析召回低**：References 格式千差万别——接受，计数日志先观测，精度优化后续迭代
- **扩展噪声**：1 跳+每篇 2 chunk 上限控制；默认关闭，eval 数据决定是否开
- 回退：`retrieval.graph_expand: false` + 表保留不碍事
