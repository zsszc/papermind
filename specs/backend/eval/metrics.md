# eval/metrics.py（评测指标计算）规格说明书

> 本文件描述 `backend/eval/metrics.py` 的**行为契约**（做什么），不描述实现细节。
> 依据源码实际内容反向工程整理（2026-08-04）。

## 1. 背景与目标

本模块为 RAG 评测提供**纯标准库**的指标计算，分两侧：

- **检索侧**：`recall_at_k` / `mrr` / `ndcg_at_k`——衡量检索结果与期望 chunk 集合的重合质量；
- **生成侧**：`citation_coverage` / `keyword_hit_rate` / `contains_refusal`——轻量、可复现的答案质量启发式（不引入 LLM-as-judge）。

全模块只有 `math` / `re` / `typing` 三个标准库导入，**不导入 `app` 下任何模块**——加载本模块不触发模型加载、配置读取或网络调用（有专门的子进程纯净性测试守住这条契约，见第 7 节）。

## 2. 范围

### 2.1 包含

- 常量：`REFUSAL_PHRASES`（9 条拒答表述）、`_KEYWORD_SPLIT_RE`（要点切分正则）
- 检索侧指标：`recall_at_k()` / `mrr()` / `ndcg_at_k()`
- 生成侧指标：`citation_coverage()` / `keyword_hit_rate()` / `contains_refusal()`
- 辅助：`split_ground_truth_keywords()` / `_as_id_set()`

### 2.2 非目标

- 指标的逐条计算编排、分组聚合、均值与报告输出（归 `eval/run.py` 规格）
- 期望 chunk 的解析（归 `eval/dataset.py` 规格）
- 检索与 LLM 调用本身（归 `retrieval` / `llm` 服务规格）

## 3. 行为契约

通用约定（模块 docstring 明示）：

- chunk id 为字符串，形如 `p{paper_id}_c{chunk_index}`；
- **所有函数对空输入安全**：空 relevant / 空关键词 / 空答案等边界一律返回 `0.0`（`contains_refusal` 返回 `False`），不抛异常；
- `retrieved_ids` / `relevant_ids` 等 Sequence 参数传 `None` 时按空列表处理（`ids or []`）；元素**须可哈希**（内部走 `set`，传不可哈希元素抛 `TypeError`）。

### 3.1 `recall_at_k(retrieved_ids: Sequence, relevant_ids: Sequence, k: int) -> float`

- **公式**：`|set(retrieved[:k]) ∩ set(relevant)| / |set(relevant)|`
- **输入**：`retrieved_ids` 按相关度降序；`relevant_ids` 为期望集合；`k` 截断位置
- **输出**：`[0, 1]` 浮点
- **边界**：
  - `relevant` 为空（含 None）→ `0.0`——**负例语义由调用方决定是否纳入统计**（`eval/run.py` 选择不纳入）；
  - `k <= 0` → `0.0`；
  - `k` 超过 `retrieved_ids` 长度 → 按实际长度截断；
  - 截断后先转 set 再求交——`retrieved` 中的重复 id 只计一次命中；
  - 分母为**去重后**的期望数（`relevant` 里重复 id 会缩小分母、抬高指标）。

### 3.2 `mrr(retrieved_ids: Sequence, relevant_ids: Sequence) -> float`

- **公式**：首个命中位置（1 起）的倒数 `1/r`；未命中 → `0.0`
- **输出**：`[0, 1]` 浮点；`relevant` 为空或 `retrieved` 为空（含 None）→ `0.0`
- **注意**：这是单条查询的 Reciprocal Rank；跨样本的均值（Mean）由 `eval/run.py` 聚合，本模块不管。

### 3.3 `ndcg_at_k(retrieved_ids: Sequence, relevant_ids: Sequence, k: int) -> float`

- **公式**（二值相关性：命中 = 1，未命中 = 0）：
  - `DCG  = Σ_{i=0..k-1} rel_i / log2(i + 2)`（i 为 0 起下标，位置 = i + 1）
  - `IDCG = Σ_{i=0..min(|relevant|, k)-1} 1 / log2(i + 2)`（理想排序：全部期望 chunk 占据最前）
  - 返回 `DCG / IDCG`
- **输出**：`[0, 1]` 浮点；`relevant` 为空或 `k <= 0` → `0.0`；理想排序（期望全中且在最前）= `1.0`
- **边界**：
  - `idcg == 0.0` 时防御性返回 `0.0`——该分支实际不可达（`relevant` 非空且 `k > 0` 时 `idcg >= 1.0`），属纯防御代码；
  - **`retrieved` 前 k 位中同一期望 id 重复出现会重复累计 DCG**（分子可超过 IDCG，理论上返回值可 > 1）——契约假设 `retrieved_ids` 无重复（当前唯一调用方 `eval/run.py` 的检索结果按 chunk_id 去重，假设成立）；
  - `relevant` 经 set 去重后再算 `min(|relevant|, k)`。

### 3.4 `citation_coverage(answer_citations: Sequence, relevant_ids: Sequence) -> float`

- **公式**：`|set(citations) ∩ set(relevant)| / |set(relevant)|`
- **语义**：分母是**期望集合**而非引用数——关注的不是「引得多不多」，而是「该引的有没有引到」（docstring 原话）；引用去重后计交（重复引用同一 chunk 只算一次）
- **输出**：`[0, 1]`；`relevant` 为空 → `0.0`；`citations` 为空（含 None）→ `0.0`

### 3.5 `split_ground_truth_keywords(ground_truth: Union[str, Sequence[str]]) -> List[str]`

- **输入**：参考答案原文（字符串）或预切分的要点列表
- **行为**：
  - falsy 输入（`""` / `[]` / None）→ `[]`；
  - 字符串 → 按 `_KEYWORD_SPLIT_RE = re.compile(r"[、，,；;]")`（中英文顿号/逗号/分号）切分；
  - 非字符串 → 按列表直接使用（`list(ground_truth)`）；
  - 逐项 `strip()`，剔除空项与**非 str 元素**。
- **输出**：关键词列表（保持原顺序）

### 3.6 `keyword_hit_rate(answer: str, ground_truth_keywords: Union[str, Sequence[str]]) -> float`

- **公式**：`命中要点数 / 要点总数`
- **命中判定**：要点经 `str.lower()` 后作为**子串**在 `answer.lower()` 中出现——**仅 ASCII 大小写不敏感**（中文无大小写概念）；无词边界语义（要点 `"背景抑制"` 不会被 `"抑制背景"` 命中）
- **边界**：要点列表为空或 `answer` 为 falsy → `0.0`
- **输出**：`[0, 1]`

### 3.7 `contains_refusal(answer: str) -> bool`

- **行为**：`answer` 包含 `REFUSAL_PHRASES` 中任一表述即返回 `True`；空/None → `False`
- **`REFUSAL_PHRASES`**（9 条，照抄）：`不知道` / `无法回答` / `无法确定` / `没有相关信息` / `未找到` / `没有提到` / `无法从` / `资料中没有` / `无法给出`
- **用途**：负例（`has_answer=false`）评测——期望 LLM 拒答而非编造（幻觉检查）

### 3.8 `_as_id_set(ids: Iterable) -> set`

- **行为**：`set(ids or [])`——None/空列表归一为空集，去重，保持原值类型；模块内部辅助，不对外承诺稳定签名。

## 4. 边界条件与错误处理

| 场景 | 期望行为 |
|------|----------|
| `k = 0` 或负数（recall / ndcg） | 返回 `0.0`，不抛异常 |
| `relevant_ids` 为空 / None | 全部指标返回 `0.0`（`contains_refusal` 为 `False`） |
| `retrieved_ids` 为空 / None | recall / mrr / ndcg 返回 `0.0` |
| `k` 大于检索结果长度 | 按实际长度截断，正常计算 |
| `retrieved_ids` 含重复 id | recall：去重后计交；ndcg：**DCG 重复累计（可 > 1）**——调用方须保证 retrieved 无重复 |
| `relevant_ids` 含重复 id | 去重后参与分母/IDCG |
| 集合元素不可哈希（如 dict） | 抛 `TypeError`（无防御） |
| `answer` 为空 / 要点为空（keyword_hit_rate） | `0.0` |
| `ground_truth` 只含标点或空白项 | 切分后全被剔除 → `0.0` |
| 要点列表含非 str 元素 | 该元素被静默剔除 |
| 中文答案的大小写 | 不受影响（`.lower()` 对 CJK 为空操作） |
| `answer` 为 None（contains_refusal） | `False` |

## 5. 依赖

- **上游依赖**：仅标准库 `math` / `re` / `typing`——无第三方包、无 app 模块（设计红线，见第 8 节）
- **下游消费者**：
  - `eval/run.py`（导入全部 6 个公开函数：`citation_coverage` / `contains_refusal` / `keyword_hit_rate` / `mrr` / `ndcg_at_k` / `recall_at_k`）
  - `backend/tests/test_metrics.py`（唯一直接单测）

## 6. 验收标准（可测试）

- [ ] AC1：`recall_at_k` 手算用例（retrieved=[a,b,c,d,e]、relevant=[b,d,x]）：k=5 → 2/3；k=2 → 1/3；k=1 → 0；k>len 截断；空 relevant / 空 retrieved / k=0 / k<0 → 0.0
- [ ] AC2：`mrr` 手算用例：第 2 位命中 → 1/2；多位命中取首个；首位命中 → 1.0；未命中 / 任一空 → 0.0
- [ ] AC3：`ndcg_at_k` 手算已知值（k=4、k=2）、理想排序 = 1.0、全未命中 = 0.0、空输入与 k=0 → 0.0
- [ ] AC4：`citation_coverage` 手算用例（3 引用 1 命中 → 1/2）、全覆盖 = 1.0、空 relevant / 空引用 → 0.0、重复引用去重
- [ ] AC5：`keyword_hit_rate` 手算用例（`肿瘤、背景抑制；BiGRU` 切 3 要点）、ASCII 大小写不敏感、列表输入、空输入 → 0.0；`split_ground_truth_keywords` 中英文标点混合切分、空白项与非 str 剔除
- [ ] AC6：`contains_refusal` 命中拒答表述、正常答案不命中、空串 → False
- [ ] AC7：模块纯净性——干净子进程 `import eval.metrics` 后 `sys.modules` 不含 `chromadb` / `sentence_transformers` / `torch` / `openai` / `app`

## 7. 现有测试覆盖与盲区

- **已覆盖**（`backend/tests/test_metrics.py`，32 个用例，按类清点）：
  - `TestRecallAtK`（7 例）→ AC1；`TestMRR`（5 例）→ AC2；`TestNDCGAtK`（5 例）→ AC3
  - `TestCitationCoverage`（5 例）→ AC4；`TestKeywordHitRate`（6 例）→ AC5；`TestContainsRefusal`（3 例）→ AC6
  - `test_metrics_module_is_pure_stdlib`（1 例，子进程断言）→ AC7
- **盲区**（按严重程度标注）：
  - **中**：`ndcg_at_k` 对 retrieved 前 k 位含**重复期望 id** 时 DCG 重复累计、返回值可 > 1 的行为无测试（当前唯一调用方保证去重，属隐性契约）
  - **低**：`None` 入参（`retrieved_ids=None` / `relevant_ids=None` / `answer=None`）的 `or []` 归一，测试只覆盖了 `[]` 形态，未覆盖 None
  - **低**：`ndcg_at_k` 的 `k > len(retrieved)` 截断、IDCG 随 `min(|relevant|, k)` 变化（relevant 数多于 k 时）无显式用例
  - **低**：`REFUSAL_PHRASES` 9 条中仅 2 条（`不知道` / `没有相关信息`、`无法回答`）有用例，其余 7 条无逐条断言；新增/删除表述无测试守护
  - **低**：`keyword_hit_rate` 的子串匹配无词边界语义（要点顺序颠倒不命中）只有单条隐式用例（`抑制背景` vs `背景抑制`）
  - **低**：`idcg == 0.0` 防御分支不可达，无测试（也写不出通过正常输入触发的测试）

## 8. 关键设计决策

- **纯标准库、零 app 依赖**：评测 CI（`.github/workflows/eval.yml`）要在干净环境快速跑单测；指标模块若牵连 torch/ChromaDB，单测启动就要数分钟。纯净性用子进程测试固化（AC7），不是口头约定
- **空 relevant 一律返回 0.0 而非抛错**：负例（无期望 chunk）的指标语义由调用方决定——`eval/run.py` 选择把负例排除在检索指标均值之外单独计数；指标函数本身不替调用方做策略决定
- **citation_coverage 分母取期望集合**：评测目标是「该引的引没引到」（recall 语义），而非「引用里有多少是对的」（precision 语义）——多引不错引不加分
- **要点切分用中英文标点而非 NLP**：`keyword_hit_rate` 刻意保持轻量可复现（字符串包含判定），替代 LLM-as-judge；代价是语义近义不命中（`抑制背景` ≠ `背景抑制`），属可接受的保守低估
- **拒答检测用词表而非分类器**：负例评测只需判定「是否拒答」，9 条常见表述的子串匹配在当前数据规模下足够；词表与评测脚本、测试共用（`REFUSAL_PHRASES` 导出）
