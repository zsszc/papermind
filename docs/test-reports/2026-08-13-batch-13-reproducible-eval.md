# Batch 13 测试与评测报告（2026-08-13）

## 1. 结论

Batch 13 已建立无需私人论文、运行时数据库、Embedding 模型、网络和 LLM 的公开离线 RAG 基准。公开语料为 PaperMind 原创 CC0 合成内容，共 3 篇论文、12 条 QA；正例使用稳定 DOI + 唯一 evidence quote 定位，CI 同时运行 count 与 BM25 profile，冻结 Recall@5 ≥ 0.85 Gate。

该公开集解决的是评测链路正确性、报告可比性与回归稳定性，不替代私人真实论文质量评测，也不据此把 BM25 切为生产默认策略。

## 2. 变更痕迹

| 提交 | 内容 |
|---|---|
| `f788390` | Batch 13 SDD spec/plan/tasks |
| `9175139` | 原创公开 fixture、12 条稳定 QA、内存 SQLite seed、evidence qrels |
| `3eeb9e9` | `--fixture` CLI、qrels/benchmark 指纹、双次复现测试和离线 GitHub Gate |

本批未读取或修改用户论文正文、真实数据库、向量库、`config.yaml` 与未审 `qa_candidates.jsonl`，未调用真实 LLM。

## 3. TDD 证据

### RED 1：fixture 与 evidence qrels

首次运行 `tests/test_eval_fixture.py` 在收集期失败：`ModuleNotFoundError: No module named 'eval.fixture'`。GREEN 后锁定：

- fixture 只 seed 到 `sqlite:///:memory:`；
- DOI/paper_uid 与 chunk_index 唯一；
- evidence quote 长度至少 20 字符；
- DOI 不存在、quote 零命中或多命中均明确失败；
- 旧 `relevant_chunks` 继续兼容。

### RED 2：CLI 与双次复现

5 个测试全部失败：CLI 不识别 `--fixture`、没有 qrels SHA、无法证明不连接真实 SessionLocal。GREEN 后连续两次运行的 comparison key、overall、by-type 与 relevant ids 完全一致，报告不包含 `/Users/` 或数据集绝对路径。

### RED 3：CI Gate

workflow 契约首次失败于仍使用真实库/模型评测。GREEN 后 GitHub Eval 在相关 PR/push 自动运行：

1. 评测 Harness 测试；
2. count 公开基线，Recall@5 ≥ 0.85；
3. BM25 候选基线，Recall@5 ≥ 0.85；
4. 无论成功失败都上传报告 artifact。

## 4. 公开稳定基线

环境：3 篇原创合成论文、12 chunks、12 QA（10 正例/2 负例）、top_k=5、内存 SQLite、keyword-only。

| Profile | Recall@5 | MRR | NDCG@5 | P95 |
|---|---:|---:|---:|---:|
| count | **0.900** | 0.775 | 0.806 | 0.3ms |
| BM25 | **0.900** | 0.783 | 0.813 | 0.4ms |

两者 Recall 相同；BM25 整体 MRR/NDCG 略高，但 method_detail 排序略低、summary 略高。该小型公开集不足以证明全面优越，因此 CI 将两者都作为 profile 回归，生产默认值保持不变。

## 5. Clean snapshot 证据

使用 `git archive HEAD` 解出不含工作区未跟踪文件、私人数据和配置的快照，再从快照执行：

```bash
cd backend
env -u PYTHONPATH <existing-python> -m eval.run \
  --fixture eval/fixtures/rag_public_v1.json \
  --dataset eval/dataset/qa_public_v1.jsonl \
  --keyword-only --lexical-profile bm25 --threshold 0.85
```

结果：Recall@5 0.900、MRR 0.783、NDCG@5 0.813，Gate PASS。证明公开评测不依赖项目 `data/`、论文目录、向量库或模型网络。

## 6. 工程回归

| 门禁 | 结果 |
|---|---|
| 后端全量 pytest | **505 passed**，927 warnings，17.76s |
| Python `pip check` | No broken requirements found |
| 前端 lint | 通过，零 warning |
| 前端 build | 通过；ui/StatsPage 大 chunk warning 仍在 |
| 前端官方 npm audit | **0 vulnerabilities** |
| Electron `node --check main.js` | 通过 |
| Electron 官方 npm audit | **13：1 critical / 12 high**，比 8 月 6 日新增 1 high |

927 条 warning 仍主要来自 SQLAlchemy `utcnow`、Pydantic class Config、httpx TestClient、PyPDF2 与底层依赖；新增 fixture 测试反复建表使 warning 计数上升，未发现本批新增运行时异常。

## 7. Gate 与下一步

- Batch 13 工程/评测 Gate：**PASS**。
- 公开基准稳定性：**PASS**。
- BM25 生产默认启用：**HOLD**，等待真实稳定 holdout 与统一 RetrievalPipeline。
- 发布 Gate：**BLOCKED**，Electron 仍有 13 项高危以上依赖问题。
- 下一批：Batch 14，先建立 Vitest/RTL/MSW 与 Electron `node:test`/启动退出 Harness，再进行 Electron 43 + builder 26 安全升级。
