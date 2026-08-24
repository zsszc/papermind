# Batch 22 规格：病理术语查询扩展 v2

## 1. 背景与选择依据

Batch 21 证明无条件传播相邻 chunk 会扰动 21/24 个 dev top-5，无法提升 factoid。Batch 22
改为 train-first：只在 private train 做匿名失败审计，冻结规则后才允许对 dev 评测一次。

生产 shared hybrid 的 train 基线为 Recall@5/MRR/NDCG@5 =
0.6666666667/0.4236111111/0.4852888182，factoid Recall=0.50，P95=326.18ms。
8 个 miss 的最终 top-5 均已命中正确论文，说明主要问题是同论文内证据块排序，而非论文召回。
现有 bilingual token 在 6/8 miss 的证据中已有交集，因此不引入大词表、LLM 翻译或伪相关反馈。

## 2. 唯一变量

新增显式词法候选 profile `bm25-bilingual-v2`，只在现有 v1 映射后增加四条通用病理术语：

- `切片` → `slide`
- `肿瘤` → `tumor`
- `特征提取` → `feature`, `extraction`
- `特征` → `feature`

稳定去重后，新规则最多自然增加 `slide/tumor/feature/extraction` 四个 token。不直接修改
`bm25-bilingual`，避免失败实验改变生产默认。semantic top-10、BM25 `k1=1.2/b=0.9`、
RRF `k=60`、最终 top-5、filters 与 rerank 全部保持不变。

## 3. 正确性与兼容契约

- v2 继续使用 exact substring 与声明顺序；不得做猜测翻译、分词模型、拼音或 question type 分支。
- 技术缩写、数字、百分比、连字符、小数与科学计数法保持现有 tokenizer 行为。
- `特征提取` 与 `特征` 重叠时 `feature` 只出现一次，结果必须幂等、稳定。
- 旧 `query_technical_terms(..., bilingual=True)`、`bm25-bilingual` 结果和 diagnostics 字节级不变。
- 候选只改变 lexical query tokens；semantic 调用 query/top_k/filters 与 Batch 20 shared hybrid 一致。
- 聊天、重新生成与 eval 继续只经 `RetrievalPipeline`，不得在 eval 新增算法副本。
- 未命中新词是正常 no-op；未知 profile 显式拒绝。词法失败继续按既有 diagnostics 降级为
  semantic-only，eval fail-close，绝不无过滤重试。

## 4. 评测与晋级 Gate

### Train 冻结 Gate

- Recall@5 >= 17/24（0.7083333333，至少新增 1 题）；
- MRR >= 0.4236111111111111；
- NDCG@5 >= 0.4852888182323138；
- factoid Recall >= 0.50；
- experiment_data/method_detail Recall 均 >= 5/6，summary Recall >= 3/6；
- P95 < 500ms、运行期降级数为 0。

Train 任一门禁失败时，候选不进入 dev，不修改生产默认。Train 全部通过后冻结代码，再对 dev
只运行一次：Recall/MRR/NDCG 不低于 0.625/0.39375/0.4517186824830735，factoid 不低于
1/3，P95 < 500ms、零降级，并要求至少一项质量指标严格提升才有晋级价值。公开 BM25 三项
保持 0.900/0.783/0.813。

本批不读取或运行 holdout，不调用 Kimi，不把 private QA/证据写入 Git 报告。

## 5. 验收标准

1. 合成 TDD 覆盖 v2 映射、重叠去重、稳定顺序、token 保真、旧 v1 零变化和候选 parser。
2. 同一假 VectorStore 下，v1/v2 的 semantic 调用完全相同，只有 lexical 排序允许变化。
3. 先通过 train Gate 才允许一次 dev Gate；失败候选保留显式复现但不晋级。
4. 后端、前端、Electron 全量 Harness、公开基准、健康检查与依赖检查通过。
