# Batch 13 规格：公开可复现 RAG Fixture 与稳定证据 Qrels

## 1. 背景

当前手动 Eval workflow 在干净 Runner 上没有 `data/papers.db`、私人论文与向量库，却直接执行真实库 hybrid 评测；运行结果依赖本机数据、导入顺序、模型网络和动态 chunk 定位，不能作为 CI 质量门禁。

Batch 13 建立一条完全独立于私人数据、模型下载和 LLM 的公开评测赛道。真实本地库仍作为私有扩展评测，但不得再与公开 CI 基线混算趋势。

## 2. 行为规格

### S1：公开语料 Fixture

- 仓库提交一套原创合成研究文献语料，不包含用户论文、笔记或真实数据库内容。
- 每篇论文有固定 DOI、标题和固定 chunk；seed 到临时 SQLite，不写入项目 `data/`。
- fixture schema 校验失败时应在评测前明确报错。

### S2：稳定证据 Qrels

- 新 QA 使用 `relevant_evidence`，每条证据由稳定 `paper_uid` 与逐字 `quote` 定位。
- `paper_uid` 首期支持 `doi:<doi>`；不能使用数据库自增 id 作为稳定身份。
- quote 至少 20 个字符，在目标论文中必须唯一命中；零命中或多命中均视为标注错误并中止。
- 检索指标仍针对运行时覆盖该证据的 chunk id 计算，因此合法的分块调整不会自动扩大 relevant 集。
- 旧 `relevant_chunks` locator 保持兼容，私人观察集不在本批迁移。

### S3：一键可复现评测

- `python -m eval.run --fixture ... --dataset ... --keyword-only` 可在无配置、无私人数据库、无模型网络时完成。
- 两次运行的 dataset/corpus/qrels/comparison key 和质量指标完全相同；延迟与时间戳不要求相同。
- 报告记录 `benchmark_id`、qrels SHA256 与 fixture 来源，不写绝对本机路径。

### S4：CI 门禁

- GitHub Eval workflow 使用公开 fixture、稳定 QA 和显式离线 profile。
- CI 运行 `count` 兼容基线与 `bm25` 候选 profile；BM25 Gate 使用冻结阈值。
- 任何 unresolved qrels、意外降级或指标低于阈值均应使 job 失败，同时上传报告。

## 3. 验收标准

1. fixture、evidence qrels、双次确定性与 clean-checkout 路径均有先 RED 后 GREEN 的测试。
2. 公开集覆盖全部正例 question type 与至少 2 条负例，涉及至少 3 篇合成论文。
3. 两次评测 comparison key 与 Recall/MRR/NDCG 一致，全部正例有且只有一个证据定位结果。
4. 后端全量 pytest、前端 lint/build、Python `pip check` 通过。
5. 生成 Batch 13 测试报告、更新开发计划，并分批提交推送。
