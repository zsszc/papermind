# RAG 评测入口规格（Batch 20 当前实现）

> 适用文件：`backend/eval/run.py`。最后核对：2026-08-24。

## 1. 目标

`python -m eval.run` 执行：加载数据集 → 解析 qrels → 共享 RetrievalPipeline →
检索/生成指标 → 多指标 Gate → JSON 报告。评测不走 HTTP，但生产 profile 与聊天调用
同一服务实现，禁止维护评测专用 Hybrid 排序。

## 2. 隔离与隐私

- 非 `--keyword-only` CLI 必须显式 `--vector-dir`，不得隐式打开主向量库。
- 公开 fixture 必须 `--keyword-only`，使用隔离内存 SQLite。
- private 选型只运行 `--split dev`；holdout 由单独验收授权控制。
- `--with-llm` 必须 private dev、显式 QA 白名单、显式调用预算、private report 目录；
  执行前健康预检，最多 512 tokens，不启用联网搜索。
- 未获真实论文内容出站授权时不得运行 private `--with-llm`。

## 3. 检索 profile

- `hybrid`：共享管线 semantic + 指定 lexical profile + chunk RRF。
- `semantic-production`：共享管线 semantic top-5；要求 top_k=5、显式 vector snapshot、
  显式 `--semantic-rerank off|on`。
- `--keyword-only`：共享管线 keyword，不初始化 VectorStore；公开稳定 Gate 使用此模式。
- 管线 diagnostics 出现运行期降级时，`runtime_degraded_count>0`，质量 Gate 必须 fail-close。

旧 `_bm25_chunk_search/_keyword_chunk_search/_rrf_fuse_chunks` 名称保留为兼容导出，实际绑定
到 `app.services.retrieval_pipeline` 实现。

## 4. 指标与 Gate

正例计算 Recall@k、MRR、NDCG@k，并按 question_type 分组；负例不进入检索均值。
每题记录 latency_ms，汇总 p50/p95/mean/count。

CLI Gate：

- `--threshold`：Recall@k 下限（始终启用）；
- `--min-mrr`：可选 MRR 下限；
- `--min-ndcg`：可选 NDCG@k 下限；
- `--min-factoid-recall`：可选 factoid Recall 下限，无该类型时 fail-close；
- `--max-p95-ms`：可选 P95 严格上限；
- runtime degradation：始终必须为 0。

所有启用的检查同时通过才退出 0；任一失败退出 1。阈值使用报告中的未舍入精确值，
不得把三位展示值直接当作非回退阈值。

## 5. 报告

报告 schema 版本保持 2.0，并保留旧字段兼容。关键增量结构：

```json
{
  "pipeline": {
    "profile": "hybrid",
    "effective_profile": "hybrid|runtime-degraded",
    "lexical_profile": "bm25-bilingual",
    "split": "dev",
    "top_k": 5
  },
  "diagnostics": {
    "runtime_degraded_count": 0,
    "rerank": {}
  },
  "gate": {
    "passed": true,
    "runtime_valid": true,
    "checks": {
      "recall@5": {"actual": 0.625, "threshold": 0.625, "operator": ">=", "passed": true},
      "p95_ms": {"actual": 275.7, "threshold": 1000, "operator": "<", "passed": true}
    }
  }
}
```

benchmark 必须记录 dataset/qrels/corpus 指纹与 comparison_key；只有指纹和 profile 参数一致
的报告可直接比较。private 报告被 gitignore，提交的测试报告只能写聚合值。

## 6. 生成侧

- 系统提示要求仅基于给定 chunk 回答、用 `[chunk_id]` 引用、资料不足时拒答。
- 生成错误串或异常计入 `generation.error_count`，使 `generation.valid=false` 且进程非零。
- 指标包括 citation precision/recall/F1、keyword hit rate 和负例拒答率。
- 检索 Gate 通过不得掩盖生成无效。

## 7. Harness

- `test_eval_quality_gate.py`：Recall/MRR/NDCG/factoid/P95/runtime 多门禁。
- `test_eval_controlled_generation.py`：白名单、预算、健康预检、semantic-production。
- `test_retrieval_pipeline_parity.py`：与聊天逐项同序。
- `test_eval_lexical.py`：token/BM25/bilingual/历史 neighbor profile。
- `test_eval_latency.py`：逐题延迟、报告与 runtime degradation。
- `test_eval_reproducible.py` / `test_eval_fixture.py`：指纹和公开 fixture。
