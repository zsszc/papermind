# Batch 22B 测试报告：检索消费者收敛与 chunk 质量审计（2026-08-24）

## 1. 结论

本批完成了剩余两个 chunk RAG 旁路的收敛：深度综述和论文引用推荐现在与聊天、重新生成、
eval 共用生产 `RetrievalPipeline`。同时修复三类明确风险：显式单篇论文对话不再被 graph
扩展到其他论文；论文引用零证据时不再要求 Kimi 推荐文献；论文发现页的语义适配器异常不再
拖垮仍可用的 FTS 关键词结果。

生产检索 profile 与指标均未改变。公开 BM25 仍为 Recall@5/MRR/NDCG@5 =
0.900/0.783/0.813；本批没有运行 private dev/holdout，也没有向 Kimi 发送论文、QA 或证据。

## 2. SDD / TDD 留痕

规格、计划和任务位于 `specs/phases/batch-22b-retrieval-consumer-convergence/`。

| 微循环 | RED | GREEN |
|---|---:|---|
| 单篇 paper scope + graph | 1 fail | graph 定向 14 passed |
| Deep Review / Thesis 共享管线与零证据 | 4 fail | 消费者/路由定向 58 passed |
| 搜索语义异常关键词降级 | 2 fail / 30 pass | `test_search.py` 32 passed |
| 后端全量 | — | 643 passed，1193 warnings，12.92s |

关键提交：`c1b324a`（规格）、`6aebb9d`/`e20e2c9`（graph RED/GREEN）、
`c1c549e`/`359995f`（消费者 RED/GREEN）、`67bed5b`/`ba8a01a`（搜索 RED/GREEN）。

## 3. 行为变化与旁路边界

- `deep_review.execute(q, db=...)` 使用生产 `chat_profile/lexical_profile`、top_k=5、filters={}；
  语义不可用时可用同范围关键词证据继续回答，最终零 chunk 才返回不足提示并跳过 LLM。
- `/api/thesis/{id}/suggest-citations` 使用相同 shared hybrid；最终零证据返回本地提示与
  `citations=[]`，LLM 调用次数为 0。
- graph expansion 在 state 显式包含 `paper_id` 时直接跳过，不能扩大到其他论文。
- `/api/search` 保持论文级 title/authors/abstract 发现语义；`get_vector_store()`、
  `available()` 或 `search()` 异常时记录日志并保留关键词结果。
- MCP 论文元数据工具、processor/vector rebuild 写路径及论文级搜索不是 chunk RAG 旁路，
  未错误迁入共享管线。chat/regenerate 的可选 graph 后处理 parity 与 regenerate scope 持久化延期。

## 4. 真实 chunk 只读聚合审计

审计只读取当前 SQLite 和 private train 的匿名 qrel 聚合，不输出论文、问题或证据原文。

| 指标 | 结果 |
|---|---:|
| 已处理论文 / chunks | 19 / 464（正文 445，摘要哨兵 19） |
| 页面元数据 | 464/464 非空，覆盖 252 个论文-页坐标 |
| chunk 坐标 | 无重复、无正文索引缺口 |
| `section_title` | 0/464 |
| `token_count` | 445/464；已有值全部等于字符数，19 个摘要缺失 |
| 长度 >512 / >1024 / >2048 字符 | 437 / 415 / 211 |
| 正文长度中位数 / P95 / 最大 | 1872 / 约 5.75k / 9776 字符 |
| 空白分词估算超过 Embedding 512 词 | 约 87/445（19.6%） |
| private train qrel | 24/24 唯一解析；23/24 证据块 >512 字符 |

根因是 `TextChunker` 只在段落之间触发阈值，单个长段落原样保留；Embedding 对英文正文只编码
前 512 个空白词。证据位于长块后部时，其文本能被词法路看到，却可能没有进入对应语义向量，
这与 factoid 弱项一致，但因果关系仍需隔离候选验证。

## 5. 完整 Harness

| 门禁 | 结果 |
|---|---|
| 后端 pytest | 643 passed |
| 前端 Vitest | 39 passed / 12 files（ErrorBoundary 错误栈为预期） |
| 前端 lint / build | 通过；仅既有大 chunk warning |
| Electron node:test | 26 passed |
| 公开冻结 BM25 | 0.900/0.783/0.813，Recall Gate 0.85 PASS |
| Python / Node 依赖 | `pip check` 与两端 `npm ls --all` 退出 0 |
| 真实启动 | `/api/health` 200，`status=ok`、`llm_ready=true`，正常停止 |

项目 venv 未安装 Ruff，CI 也没有 Python Ruff 步骤；本批使用 `git diff --check`、pytest 与
现有 CI 等价门禁。在线 npm audit 未运行，本批未改依赖。

## 6. 下一步与剩余工作

下一批已冻结为 Batch 22C：只修改超长 chunk 粒度，在复制 SQLite 与 stage Chroma 上重建，
先跑 private train，只有通过冻结 Gate 才运行一次 dev；不看 holdout、不调用 Kimi、不自动换入
生产数据。`section_title` 识别暂不混入，确保变量可归因。

项目核心功能已完成，距离发布级收口约 5 个批次：隔离分块重建、检索候选晋级/回退、经授权的
真实生成烟测、经授权的数据修复换入，以及桌面制品与发布验收。主库 4 条历史 `paper_tags`
孤儿仍未修改；用户个人文件未触碰或暂存。

