# Phase B：检索质量提升 TDD 任务分解

> 规格：同目录 spec.md。每项严格 RED → GREEN → REFACTOR；子代理不 commit，主代理统一提交。
> 测试命令：`cd backend && env -u PYTHONPATH venv/bin/python -m pytest tests/ -q`

## 任务清单

- [ ] T1（B1）：RerankerService + 检索重排集成——`backend/app/services/reranker.py`（新建）+ `retrieval.py` + `tests/test_retrieval_rerank.py`（新建）
- [ ] T2（B2）：摘要级 chunk——`backend/app/services/processor.py` + `tests/test_processor_abstract_chunk.py`（新建）
- [ ] T3（B4）：延迟指标——`backend/eval/metrics.py` + `backend/eval/run.py` + `tests/test_eval_latency.py`（新建）
- [ ] T4（B3）：QA 标注治理——**暂缓**（依赖 QA 候选集扩充完成，等 Moonshot 解冻）
- [ ] T5：定标与趋势——`python -m eval.run` rerank on/off 对比入趋势（T4 完成后做）

## T1：B1 Reranker 重排

### Step 1（RED）
`tests/test_retrieval_rerank.py`：

```python
# 契约（示意，实现以源码为准）：
# 1. retrieval.rerank=true 且模型可用：召回候选经 RerankerService._score 重排后截断 top_k
# 2. rerank=true 但模型不可用：回退原排序（不抛异常），记 warning
# 3. rerank=false（默认）：行为与现状完全一致（特征化，不调用 reranker）
```

要点：RerankerService 单例 + 懒加载 + 失败锁存（对齐 EmbeddingService 模式）；模型从 `retrieval.rerank_model` 配置读取（不硬编码）；重排候选数 20。

### Step 2（GREEN）
- 新建 `services/reranker.py`：CrossEncoder（sentence-transformers 已含，零新依赖，实现后跑 `pip check` 验证）
- `retrieval.py` 的 `VectorStore.search()`：RRF/语义结果取前 20 → `_score` → 重排 → 截断

### Step 3（REFACTOR）
- 确认降级路径日志文案统一 `[reranker]` 前缀

## T2：B2 摘要级 chunk

### Step 1（RED）
```python
# 契约：
# 1. 入库处理后存在 chunk_type="abstract" 的 chunk（abstract 字段为空时取首页前 1500 字符）
# 2. chunk id 形如 p{paper_id}_abstract；重复入库先删后写不重复
# 3. 首页文本也为空 → 跳过不阻塞入库
```

### Step 2（GREEN）
`processor.py` 分块完成后追加摘要 chunk 生成；ChromaDB 与 chunks 表同步写入。

## T3：B4 延迟指标

### Step 1（RED）
```python
# latency_stats([]) == {"p50": 0.0, "p95": 0.0, "mean": 0.0, "count": 0}
# latency_stats([100.0]) == p50=p95=mean=100.0
# 多条样本 P50/P95/mean 正确
# eval/run.py 报告 JSON 含 latency 字段；trend.py 读取缺字段报告不崩（兼容）
```

### Step 2（GREEN）
`metrics.py` 新增 `latency_stats()`；`run.py` 计时每次检索并写入报告。

## 验收门

- 全套件全绿；各子代理汇报 RED/GREEN 证据
- 主代理统一验证 + 提交；规格第 7 节盲区标记更新
