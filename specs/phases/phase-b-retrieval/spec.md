# Phase B：检索质量提升 规格说明书

> 来源：`.hermes/plans/2026-07-28_..._Phase2...md` Phase B 章节 + specs 反推实证。
> 目标：针对 factoid / summary 类检索短板，recall@5 较 Phase A 基线提升 ≥0.05（20 篇规模重新定标）。

## 1. 背景与目标

Phase A 基线（1 篇示例论文）：keyword-only recall@5=0.447，hybrid recall@5=0.477/MRR=0.557。规格反推另发现两个直接相关事实：(a) `config.yaml` 的 `retrieval.rerank` 是预留开关、BGE-Reranker 代码未实现；(b) 19/19 篇论文 `abstract` 全空（提取逻辑从未实现 + LLM 增强静默失败），摘要级 chunk 不能依赖 abstract 字段。

## 2. 范围

### 2.1 包含

- B1：**BGE-Reranker 重排**。`bge-reranker-v2-m3` 本地加载（HF 镜像，懒加载同 embedding 模式）；向量+FTS 召回 top-20 → Reranker 重排 → 取 top-k；`retrieval.rerank: false` 默认关闭，开启后生效；模型不可用时降级为原排序
- B2：**摘要级 chunk**。每篇论文入库时额外生成 1 个摘要级 chunk（优先 abstract 字段；为空时取首页前 ~1500 字符启发式），metadata 标 `chunk_type: "abstract"`，写入 ChromaDB 与 chunks 表
- B3：**factoid 短板治理**。`eval/dataset.py` 的 `relevant_chunks` 支持一对多标注（若已支持仅改种子数据）；放宽过窄标注
- B4：**检索延迟指标**。`eval/metrics.py` 新增 `latency_stats()`（P50/P95）；`eval/run.py` 报告输出延迟统计

### 2.2 非目标

- 不修复 LLM 元数据增强链路（abstract 提取逻辑）——属独立数据质量议题，需单独 spec
- 不改 RRF 融合算法本身
- 不引入 rerank 到 MCP 工具路径以外的变更（search_papers 走同一 retrieval 层，自然受益）

## 3. 行为契约

### 3.1 B1：Reranker 服务与重排

- **加载**：`RerankerService` 单例 + 后台线程懒加载（与 EmbeddingService 同模式）；`available()` 失败锁存、进程内不重试
- **配置**：`retrieval.rerank`（默认 false）、`retrieval.rerank_model`（默认 `BAAI/bge-reranker-v2-m3`）
- **重排时机**：hybrid 检索 RRF 融合后（或仅语义检索结果上），对前 20 个候选计算 (query, chunk) 相关性分数，按分数重排取 top-k
- **降级**：rerank 开启但模型不可用 → 记 warning，返回原排序（不抛异常、不 500）
- **测试钩子**：模型调用走可 mock 的方法（`_score(pairs) -> List[float]`）

### 3.2 B2：摘要级 chunk

- **生成时机**：论文入库处理流水线（processor）分块完成后
- **内容来源**：`paper.abstract` 非空 → 用之；否则取第一页文本前 1500 字符（学术 PDF 首页通常含摘要段）
- **标记**：`chunk_type: "abstract"`；chunk id 形如 `p{paper_id}_abstract`；**`chunk_index: -1`**（2026-08-05 实证补充：TextChunker 的段落分类器也会把以 Abstract 开头的普通段落标为 `chunk_type=abstract`，区分两者的唯一可靠依据是 `chunk_index=-1` 或 id 模式；消费方查询摘要级 chunk 必须带此过滤）
- **幂等**：重复处理同篇论文时先删旧摘要 chunk 再写（不产生重复；19 篇重处理实证每篇恰 1 个）
- **降级**：首页文本也为空 → 跳过，不阻塞入库

### 3.3 B3：标注治理

- 若 `resolve_relevant_chunks` 已支持多 locator：仅治理种子/候选数据（人工审稿环节）
- 若不支持：扩展为一对多解析（先 RED 测试）

### 3.4 B4：延迟统计

- `latency_stats(latencies: List[float]) -> Dict[str, float]`：返回 `{p50, p95, mean, count}`
- 空列表 → `{p50: 0.0, p95: 0.0, mean: 0.0, count: 0}`
- `eval/run.py` 报告 JSON 增加 `latency` 字段（trend.py 读取兼容：缺字段不崩）

## 4. 边界条件与错误处理

| 场景 | 期望行为 |
|------|----------|
| reranker 模型下载失败 | 锁存失败、降级原排序、warning 日志 |
| 候选不足 20 个 | 对现有全部候选重排 |
| 摘要 chunk 重复入库 | 先删后写，无重复 |
| 延迟样本为 1 条 | p50=p95=该值 |

## 5. 依赖

- retrieval.md（RRF/search 契约）、embedding.md（懒加载模式）、processor.md（流水线）、eval/{dataset,metrics,run}.md
- 宪法第 16 条：新增模型下载不改锁定依赖版本；sentence-transformers 已含 CrossEncoder，**预计零新依赖**（实现时验证 pip check）

## 6. 验收标准（可测试）

- [ ] AC1：mock reranker 下，重排调用顺序正确（召回→RRF→rerank→截断）；reranker 不可用时回退原顺序
- [ ] AC2：`retrieval.rerank: false` 时行为与现状完全一致（特征化）
- [ ] AC3：新论文入库后存在 `chunk_type=abstract` 的 chunk 且可被检索命中；重复入库无重复
- [ ] AC4：`latency_stats` 边界（空/单条/多条）正确；报告含 latency 字段且 trend.py 兼容
- [ ] AC5：全套件全绿；`python -m eval.run` 可跑通（定标待 QA 数据集齐备后门控）

## 7. 现有测试覆盖与盲区

- ~~reranker 全功能为零~~ **B1 已实现（3c4bc7b）**：16 契约用例（服务级 7 + 集成 9），降级三路径锁定
- ~~摘要 chunk 为零~~ **B2 已实现（bfda87f）**：5 用例（abstract 优先/首页回退/幂等/跳过）
- ~~延迟统计~~ **B4 已实现（e5a3d61）**：latency_stats 边界 + 报告集成 + trend 兼容
- dataset 多 locator 解析：现有 16 用例部分覆盖（specs/backend/eval/dataset.md 第 7 节）
- **定标测量（2026-08-05 实测）**：19 篇规模 hybrid recall@5=0.295/MRR=0.350（阈值 0.5 FAIL）——B1/B2 尚未对存量论文激活（rerank 默认关、旧论文无摘要 chunk），待重处理后重新定标；factoid/summary 仍为 0.000 弱项
- 门控测量依赖 QA 数据集（当前 14 条候选，Moonshot 解冻守望中）

## 8. 关键设计决策

- rerank 默认关闭：渐进增强，先证明提升再默认开启；评测对比 on/off 由 eval.run 配置切换完成
- 摘要 chunk 不依赖 abstract 字段：该字段全空是已证实状（pdf_parser.md 第 8 节），启发式首页截取是当前唯一可靠来源
- Reranker 独立服务而非嵌入 retrieval.py：与 EmbeddingService 模式对齐，可独立 mock/降级
