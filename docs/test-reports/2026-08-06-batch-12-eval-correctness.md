# Batch 12 测试与评测报告（2026-08-06）

## 1. 结论

Batch 12 完成 RAG 评测数学修复、报告 v2 可追溯信息与一个默认关闭的 BM25 词法观察 profile。工程回归通过；BM25 在当前动态 qrels 上显著提升词法检索指标，但 factoid/summary 仍为 0，且稳定 benchmark 尚未建立，因此本结果只作为候选方案证据，不修改生产默认检索策略。

## 2. 变更痕迹

| 提交 | 内容 |
|---|---|
| `8fe4786` | 新开发计划表与 Batch 12 SDD spec/plan/tasks |
| `441cc1d` | 摘要引用解析、NDCG 去重、citation P/R/F1、qrels preflight、报告 v2 指纹与降级诊断 |
| `3ca8504` | 技术锚点 tokenizer 与 BM25 观察 profile；默认 count 不变 |

未修改真实数据库、论文、向量库、`config.yaml` 和未审 `qa_candidates.jsonl`；未调用真实 LLM。

## 3. TDD 证据

### RED 1：评测数学

```bash
cd backend
env -u PYTHONPATH venv/bin/python -m pytest tests/test_eval_correctness.py -q
```

首次失败：`ImportError: cannot import name 'citation_f1'`。随后生成侧报告测试按预期失败于 `KeyError: 'citation_precision'`。这证明旧实现缺少 citation P/R/F1，且单一 coverage 不能识别错误引用。

### RED 2：报告诊断

新增 unresolved qrels 与 benchmark fingerprint 测试后，首次失败于缺少 `_build_benchmark_metadata` / `_resolve_qrels_or_raise`。

### RED 3：词法实验

```bash
cd backend
env -u PYTHONPATH venv/bin/python -m pytest tests/test_eval_lexical.py -q
```

首次失败：缺少 `_bm25_chunk_search` 与 `_tokenize_technical_terms`。GREEN 后 4 条契约覆盖技术词、稀有词 IDF、长度归一与纯中文安全降级。

## 4. RAG 消融结果

相同条件：2026-08-06 的 19 篇/464 chunks 私有本地库、`qa_seed.jsonl` 25 条（22 正例、3 负例）、top_k=5、keyword-only、不调用模型/LLM。

| Profile | Recall@5 | MRR | NDCG@5 | P50 | P95 |
|---|---:|---:|---:|---:|---:|
| count（改动前/兼容 profile） | 0.198 | 0.239 | 0.208 | 8.0ms | 11.6ms |
| BM25 技术锚点（观察） | **0.439** | **0.500** | **0.454** | 53.1ms | 66.0ms |
| 差值 | **+0.241** | **+0.261** | **+0.246** | +45.1ms | +54.4ms |

| 类型 | count Recall@5 | BM25 Recall@5 | 差值 |
|---|---:|---:|---:|
| comparison | 0.250 | 0.500 | +0.250 |
| experiment_data | 0.200 | 0.750 | +0.550 |
| method_detail | 0.371 | 0.629 | +0.258 |
| factoid | 0.000 | 0.000 | 0.000 |
| summary | 0.000 | 0.000 | 0.000 |

复现命令：

```bash
cd backend
env -u PYTHONPATH venv/bin/python -m eval.run --keyword-only --lexical-profile count --threshold 0
env -u PYTHONPATH venv/bin/python -m eval.run --keyword-only --lexical-profile bm25 --threshold 0
```

历史 hybrid 观察基线仍为 Recall@5 0.193、MRR 0.307、NDCG@5 0.195、P95 149ms。本次 hybrid 复跑因 BGE-M3 加载器访问 `hf-mirror.com` 时 DNS 不可用而中止，没有把基础设施失败写成新的 hybrid 质量结果。

### 指标限制

- 当前 QA 几乎都围绕 paper 1，很多事实/总结问题没有显式论文作用域。
- 动态 qrels 的平均 relevant 数曾从 3.045 增到 3.864（约 +26.9%），历史趋势不完全可比。
- BM25 只提取答案无关的 ASCII 技术锚点，没有使用 ground truth 或为单题写规则；纯中文问题安全返回空词法结果。
- Batch 13 冻结公开 fixture、证据跨度 qrels 与 holdout 后，才允许判断该 profile 是否进入统一生产 RetrievalPipeline。

## 5. 工程测试报告

| 门禁 | 命令 | 结果 |
|---|---|---|
| 评测定向回归 | `pytest` metrics/dataset/eval 相关文件 | 81 passed |
| 后端全量 | `env -u PYTHONPATH venv/bin/python -m pytest tests/ -q` | **491 passed**，781 warnings，12.22s |
| Python 依赖 | `venv/bin/python -m pip check` | No broken requirements found |
| Python Ruff | `venv/bin/python -m ruff ...` | **未执行：当前 venv 未安装 ruff** |
| 前端 lint | `npm run lint` | 通过，零 warning |
| 前端 build | `npm run build` | 通过；仍有 ui/StatsPage > 1.1MB 的性能 warning |
| 前端安全审计 | `npm audit --registry=https://registry.npmjs.org --json` | **0 vulnerabilities** |
| Electron 语法 | `node --check main.js` | 通过 |
| Electron 安全审计 | 同上（electron 目录） | **12：1 critical / 11 high**，发布阻断保持 |

弃用 warning 主要来自 SQLAlchemy `utcnow`、Pydantic class Config、httpx TestClient、PyPDF2 与底层科学计算依赖；本批没有新增运行时弃用 API，但新增建表测试使 warning 计数从 762 增至 781。

## 6. Gate 判定与下一步

- Batch 12 行为与工程 Gate：**PASS**。
- BM25 进入生产默认策略：**HOLD**，等待 Batch 13 稳定 benchmark 与共享检索链路。
- 发布 Gate：**FAIL/BLOCKED**，Electron 12 项漏洞仍需在 Harness 建立后升级修复。
- 下一批：Batch 13，建立公开可复现 fixture、证据 qrels、clean-checkout seed→eval 和可比趋势门禁。
