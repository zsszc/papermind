# Batch 28 测试报告：v2 train 证据 Route-Depth 归因

## 1. 结论

本批完成一个只读、离线、train-only 的证据深度归因 Harness，用于判断生产 hybrid 的失败
究竟来自候选池缺失，还是证据已经进入 semantic/BM25 深层结果但没有进入最终 top-5。

在 clean Git `23759fd`、冻结 SQLite/PDF/QA/Chroma 指纹下，13 个真实 v2 train 正例中：

- 5 题已由生产 top-5 完整覆盖；
- 4 题可由两路 top-20 的深层候选恢复；
- 4 题只找到正确论文，但两路 top-20 均未覆盖证据；
- 0 题完全缺失正确论文。

双路并集 `span_coverage` 从 @5 的 **0.5291** 提升到 @20 的 **0.7599**，说明下一步应优先
利用已存在的深层证据，而不是再次增加全局论文先验。按预注册映射，Batch 29 唯一候选冻结为
`paper-preserving-deep-route-v1`。本批没有实现候选、没有运行 dev/holdout、没有调用 Kimi，
生产检索默认未改变。

## 2. SDD / TDD 与失败痕迹

- `8d7698b`：先提交 spec/plan/tasks 与 RED；目标模块缺失导致 collection ImportError。
- `42cea5d`：实现纯聚合、严格私有 CLI、冻结指纹、临时 Chroma 副本和脱敏白名单输出。
- `577e3ff`、`30ad2a1`：真实运行前补齐固定阶段错误码，确保失败报告不泄漏路径、问题或异常原文。
- 第一次真实运行 fail closed：原契约要求 BM25 必须返回完整 20 项，但生产 BM25 会丢弃零分
  文档，合法结果天然可能少于 20。该失败没有被绕过；先补测试，再以 `5331865` 将契约改为
  semantic 必须 20、BM25 允许 0–20。
- `23759fd`：补齐 semantic/BM25 并集的 @5/@10/@20 曲线并再次在 clean Git 上运行。

专项测试最终为 **16 passed**；异常、重复身份、非完整 train、dirty Git、路径逃逸、symlink、
越界指标、基线不一致、私有字段泄漏与冻结源变异均 fail closed。

## 3. 真实 train 聚合结果

私有报告写入已忽略的 `backend/eval/private/batch28-route-depth-final.json`，权限为 `0600`。
报告不含 qa_id、chunk_id、问题、正文、标题、路径、DOI、paper UID 或逐题记录。

| 路由 | any-hit@5 | any-hit@10 | any-hit@20 | span@5 | span@10 | span@20 |
|---|---:|---:|---:|---:|---:|---:|
| semantic | 0.4615 | 0.4615 | 0.6923 | 0.4522 | 0.4522 | 0.6830 |
| BM25 bilingual | 0.3846 | 0.4615 | 0.6154 | 0.3753 | 0.4522 | 0.6061 |
| 双路并集 | 0.5385 | 0.5385 | 0.7692 | 0.5291 | 0.5291 | 0.7599 |

| 互斥类别 | 数量 | 比例 |
|---|---:|---:|
| `baseline_full` | 5 | 0.3846 |
| `deep_route_recoverable` | 4 | 0.3077 |
| `correct_paper_only` | 4 | 0.3077 |
| `paper_absent` | 0 | 0 |

双路并集 first-hit 深度为 1–5：7 题、6–10：0 题、11–20：3 题、未找到：3 题。
按问题类型，factoid/method_detail/summary 的 union span@20 分别为
`0.8598/0.5000/1.0000`。脱敏观察指纹为
`dcaaf8da457c9777c892610cd9d2b46210909ab2bc63843bd58ba2d3e0983ee9`。

## 4. 完整回归证据

| Gate | 结果 |
|---|---|
| Batch 28 专项 | **16 passed** |
| 后端全量 | **1028 passed**，1434 warnings，18.46s |
| Python 依赖 | `pip check`：No broken requirements found |
| 前端测试 | **15 files / 66 tests passed** |
| 前端 lint / build | **PASS / PASS**；保留既有大 chunk 警告 |
| Electron 默认测试 | **26 passed / 2 skipped / 0 failed** |
| 真实发布 E2E | **10/10 passed**，14.90s |
| 公开 count RAG | Recall@5 **0.900** / MRR **0.775** / NDCG@5 **0.806** |
| 公开 BM25 RAG | Recall@5 **0.900** / MRR **0.783** / NDCG@5 **0.813** |
| 公开生成 Guardrail | P/R/F1/拒答率均 **1.000**，PASS |
| 独立失败事务 | **11/11 scenarios**，PASS |

后端现有 warning 均为锁定依赖的弃用提示和既有测试环境提示；本批没有新增失败或依赖冲突。
前端构建仍报告 `ui` 与 `StatsPage` 大 chunk 警告，属于后续 UI/性能批次的已知优化项。

## 5. 安全边界与 Batch 29 入口

- 所有 embedding 查询均使用本地缓存模型并设置 HuggingFace/Transformers 离线变量；没有网络、
  LLM、Kimi 或子进程生成调用。
- Chroma 只从冻结源复制到临时目录后打开，运行前后源树指纹一致；SQLite、PDF、生产向量库
  和检索默认配置均未修改。
- dev 在候选完整 train Gate 通过前仍禁止，holdout 始终禁止。
- Batch 29 需先另建 SDD/TDD 契约：保持生产 top-5 的论文集合及每篇名额不变，只允许同论文
  深层候选替换，以直接利用 4 个可恢复样本并避免 Batch 27B 的 factoid 论文挤出。
