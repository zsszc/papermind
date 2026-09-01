# Batch 22L 测试报告：Benchmark v2 数据建设与盲测基线

## 1. 结论

真实语料 Benchmark v2 从 readiness 到盲测基线全程打通：33 篇新论文入库（52 篇全 done）、
readiness 34/12 达标、split 预冻结（12/11/11）、LLM 辅助 QA 生成 40 条（用户审稿全通过）、
证据独立复验 40/40、六重指纹冻结、**train/dev 盲测基线入档**（holdout 封存未消费）。

## 2. 盲测基线（生产 hybrid，确定性快照，只测不调）

| split | n | recall@5 | MRR | NDCG@5 | span_cov@5 | P95 | Gate(≥0.5) |
|---|---|---|---|---|---|---|---|
| train | 13 | 0.385 | 0.310 | 0.299 | 0.452 | 549ms | ❌ FAIL |
| dev | 12 | 0.708 | 0.322 | 0.417 | 0.750 | 869ms | ✅ PASS |

分型：train factoid 0.438 / method_detail 0.375 / summary 0.000（n=1）；
dev factoid 0.667 / method_detail 0.833。train 明显弱于 dev——新语料的真实难度面，
正是 v2 存在的意义；按纪律本批不因结果调任何检索算法。

## 3. 执行轨迹与实证要点

- **Kimi 空响应根因**：单次请求 3 条长 JSON → 整包空（115s）；n=1 → 正常（95.7s）。
  生成器改逐条调用（独立重试+类型轮换+start_seq 递增），4 测试同步，969→970。
- **余额墙两轮**：162 连撞 RateLimit（账户余额耗尽）→ v2 守望脚本+cron（30min 静默探活
  自动续跑）→ 用户充值后 4 轮续跑收敛（8→16→26→27 篇）。
- **证据长度鸿沟**：数据集 Gate 要求 quote ≥20 字符而生成器校验 10 起——2 条 dev 候选
  （16/18 字符）就地扩展为更长逐字子串（含换行跨表格单元，重新通过唯一解析），
  冻结前修正并重建冻结制品（旧冻结零消费，如实记录）。
- **HNSW 契约扩展**：Chroma 0.4.24 向量增长后持久化 `index_metadata.pickle`，
  原 4 文件集合契约 fail-closed——TDD 扩为「4 必需 + pickle 可选结构文件」（7/7 + 970 全绿）。
- **p1 坐标回填**：ReCo-MIL（用户论文）4 个粗 chunk 缺页内坐标致 train 解析 fail-close；
  锚点法原地回填 4/4（chunk id/向量零漂移，冻结指纹重算一致）。

## 4. 验收对照（spec 第 4 节）

| AC | 结果 |
|---|---|
| AC1 split 预冻结 12/11/11 零重叠 | ✅ |
| AC2 证据唯一解析双证 | ✅ 生成器自校验 + 独立复验 40/40 |
| AC3 用户审稿且每 split ≥12 | ✅ 全通过（13/12/15） |
| AC4 冻结指纹完整 + 基线入档 | ✅ freeze_sha256=3c2a6953…；上表 |
| AC5 holdout 零消费 + 全套件绿 | ✅ 无 claim 文件；970 passed |

## 5. 已知限制

- 7/34 篇论文 QA 生成失败（证据唯一性难过）——候选来自 27 篇
- 2 条证据 quote 含换行（表格跨行单元），逐字且唯一，属 PDF 文本流真实形态
- factoid 提问偏数值型（21/40）；dev 论文数仅 8 篇（12 条）

## 6. 制品（均 gitignore 私有）

`eval/private/`：`benchmark_v2_splits.json`、`qa_v2_candidates.jsonl`、
`qa_private_v2.jsonl`、`benchmark_v2_freeze.json`、`v2-vector-snapshot/`、
`reports-v2-train-hybrid/`、`reports-v2-dev-hybrid/`、`corpus_manifest_v2.json`

## 7. Batch 25 接班复核

本节只审查版本库中的生成器与已提交报告，未读取上述私有制品、论文或配置，也未重新调用
Kimi；因此第 1–5 节的私有数量与指标是 Batch 22L 报告记录，不是 Batch 25 独立复验。

接班审计补齐了以下 fail-closed 契约：

- `--resume` 必须严格解析 JSONL，只补齐缺失 question type，不再见到任一行就跳过整篇；
- 候选文件必须为普通文件、权限精确为 0600，损坏/空行/重复/未知 UID 或 split 一律拒绝追加；
- CLI 必须显式传 `--confirm-content-egress`，且 splits/output 限定在 `eval/private/`；
- 调用 LLM 前逐篇校验 split 中冻结的 `pdf_sha256` 与当前 DOI 映射 PDF，阻断源语料漂移。

## 8. 提交

`d25ad61`（三件套）→ `dda3e93`（生成器）→ `744938c`（逐题调用）→ `992a98e`（冻结与报告）；
Batch 25 修复见 `207b013`、`7207dce`、`7b3097f`、`1504c10`。
