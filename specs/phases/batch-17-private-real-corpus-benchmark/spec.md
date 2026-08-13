# Batch 17 规格：私有真实语料 Benchmark v1

## 1. 背景与基线

用户已在 `papers/` 放入 36 个 PDF。只读盘点表明其中 17 组为逐字节重复副本，按 SHA-256 去重后是 19 篇唯一论文；SQLite 中恰有 19 篇 `processed=done` 文献、464 个 chunks，Chroma 与 SQLite chunk 数一致。除 demo 外有 18 篇真实导入论文。

现有公开 CC0 fixture 只验证评测链路正确性，不代表真实论文质量；`qa_candidates.jsonl` 只有 14 条未审候选、覆盖 4 篇，且短关键词 locator 会为单题解析出 2–9 个相关 chunk，不能作为正式 qrels。

## 2. 行为规格

### S1：私有数据边界

- PDF、数据库、向量库、真实 QA、参考答案和逐字证据均不得提交 Git。
- `backend/eval/private/` 与历史 `qa_candidates.jsonl` 必须显式 gitignore。
- 可提交报告只包含去标识化数量、哈希、聚合指标和问题 ID 哈希，不包含标题、路径、问题、答案或原文。
- 公开 CI 继续使用 CC0 fixture；私有真实库只作为本地质量 Gate，二者不得混算趋势。

### S2：真实语料去重 manifest

- 语料盘点按 PDF 内容 SHA-256 去重，不以物理文件数宣称论文数。
- manifest 记录内容 UID、入库/处理/chunk 状态、重复数量与评测 split；私有 manifest 可含本地路径，可提交摘要不得含路径和标题。
- 只报告重复文件，不自动删除、移动或覆盖用户文件。

### S3：稳定论文 UID 与唯一 evidence qrels

- 正式 qrels 使用规范化 `doi:<doi>`；无 DOI 时使用 `sha256:<pdf-content-sha256>`，禁止依赖数据库 `paper_id`。
- DOI 比较忽略 `https://doi.org/` 前缀、大小写、首尾空白和末尾句点。
- 每个 evidence quote 至少 20 字符，并必须在目标论文 chunks 中唯一命中；零命中或多命中立即阻断评测。
- 同一 PDF 的重复副本不得跨 train/dev/holdout split。

### S4：候选生成与人工审稿 Gate

- 候选生成覆盖至少 12–15 篇真实论文，目标 50–100 条；不能只截取论文开头，应按全文前/中/后分层取样。
- 候选默认 `reviewed=false`，不得直接进入正式 Benchmark。
- 正式集每条必须完成人工核对：问题可答、答案正确、UID 稳定、quote 唯一、题型正确；未审条目使 Gate 失败。
- split 以论文为单位：train 用于诊断、dev 用于单变量选择、holdout 冻结后只用于里程碑验收。

### S5：指标与改进纪律

- 首先冻结 count、BM25 与 production/hybrid 基线，报告 Recall@5、MRR、NDCG@5、分题型和 P95。
- 优化顺序为术语扩展→中英文 chunk BM25→RRF/候选池→语义池→reranker；每次只改变一个变量。
- dev Recall@5 不得回退，主判 NDCG@5，MRR 辅助；看过 holdout 后产生的新调参只能由下一版 holdout 验证。

## 3. 验收标准

1. Harness 能稳定报告当前 36 文件/19 唯一 PDF/19 入库/464 chunks，重复文件不被删除。
2. `sha256:` 和规范化 DOI evidence 均有 RED→GREEN 测试；零命中、多命中、重复 split 均 fail-close。
3. 私有目录和候选文件有 Git 泄漏测试；公开 fixture 回归保持 0.900 Recall@5。
4. 形成至少 50 条、覆盖至少 12 篇真实论文的候选集；正式基线只使用审稿通过的条目并披露覆盖率。
5. 生成测试报告，记录真实语料基线、薄弱题型、已知限制和每次实验痕迹。
