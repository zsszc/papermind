"""RAG 评测指标计算（纯标准库实现，不依赖 numpy / 第三方包）。

检索侧指标：
- recall_at_k：期望 chunk 出现在检索结果前 k 位的比例
- mrr：第一个命中位置的倒数（Mean Reciprocal Rank 的单条形式）
- ndcg_at_k：二值相关性下的归一化折损累计增益

生成侧指标（轻量、可复现）：
- citation_precision / citation_recall / citation_f1：引用正确性与覆盖程度
- citation_coverage：citation_recall 的向后兼容别名
- keyword_hit_rate：答案覆盖参考答案要点的比例

性能侧指标：
- latency_stats：检索延迟样本的 P50 / P95 / 均值 / 样本数

约定：
- chunk id 形如 "p{paper_id}_c{chunk_index}"（字符串）；
- 所有函数对空输入安全：空 relevant / 空关键词 / 空延迟样本等边界一律返回 0.0；
- 本模块不导入 app 下任何模块，保证加载本身不触发模型加载或 LLM 调用。
"""

from __future__ import annotations

import math
import re
from typing import Dict, Iterable, List, Sequence, Union

# ground_truth 要点切分符：顿号、分号、逗号（中英文）
_KEYWORD_SPLIT_RE = re.compile(r"[、，,；;]")

# 负例回答中常见的拒答表述（供评测脚本与测试共用）
REFUSAL_PHRASES = (
    "不知道",
    "无法回答",
    "无法确定",
    "没有相关信息",
    "未找到",
    "没有提到",
    "无法从",
    "资料中没有",
    "无法给出",
)


def _as_id_set(ids: Iterable) -> set:
    """将 id 列表归一化为集合（去重，保持原值类型）。"""
    return set(ids or [])


def recall_at_k(retrieved_ids: Sequence, relevant_ids: Sequence, k: int) -> float:
    """Recall@k：期望命中的 chunk 中，有多少比例出现在检索结果前 k 位。

    参数：
        retrieved_ids: 检索系统返回的 chunk id 列表（按相关度降序）。
        relevant_ids: 期望命中的 chunk id 列表（ground truth）。
        k: 截断位置，k <= 0 时返回 0.0。

    返回：
        |retrieved[:k] ∩ relevant| / |relevant|，范围 [0, 1]。
        relevant_ids 为空（如负例）时返回 0.0，调用方应自行决定是否纳入统计。
    """
    relevant = _as_id_set(relevant_ids)
    if not relevant or k <= 0:
        return 0.0
    top_k = _as_id_set(list(retrieved_ids or [])[:k])
    hits = len(top_k & relevant)
    return hits / len(relevant)


def mrr(retrieved_ids: Sequence, relevant_ids: Sequence) -> float:
    """MRR（单条）：第一个命中位置的倒数。

    参数：
        retrieved_ids: 检索系统返回的 chunk id 列表（按相关度降序）。
        relevant_ids: 期望命中的 chunk id 列表。

    返回：
        若第 r 位（1 起）首次命中期望 chunk，返回 1/r；未命中或任一空列表返回 0.0。
    """
    relevant = _as_id_set(relevant_ids)
    if not relevant:
        return 0.0
    for rank, rid in enumerate(retrieved_ids or [], start=1):
        if rid in relevant:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(retrieved_ids: Sequence, relevant_ids: Sequence, k: int) -> float:
    """NDCG@k：二值相关性（命中=1，未命中=0）下的 DCG / IDCG。

    DCG  = Σ_{i=1..k} rel_i / log2(i + 1)
    IDCG = Σ_{i=1..min(|relevant|, k)} 1 / log2(i + 1)

    参数：
        retrieved_ids: 检索系统返回的 chunk id 列表（按相关度降序）。
        relevant_ids: 期望命中的 chunk id 列表。
        k: 截断位置，k <= 0 时返回 0.0。

    返回：
        范围 [0, 1]；relevant_ids 为空时返回 0.0。
    """
    relevant = _as_id_set(relevant_ids)
    if not relevant or k <= 0:
        return 0.0

    # 同一个结果 id 只允许贡献一次相关性；否则 [a, a] 对 relevant=[a]
    # 会重复累计 DCG，甚至得到大于 1 的非法 NDCG。
    unique_retrieved = list(dict.fromkeys(retrieved_ids or []))[:k]
    dcg = 0.0
    for i, rid in enumerate(unique_retrieved):
        if rid in relevant:
            dcg += 1.0 / math.log2(i + 2)  # i 为 0 起，位置 = i + 1

    ideal_len = min(len(relevant), k)
    idcg = sum(1.0 / math.log2(i + 2) for i in range(ideal_len))
    if idcg == 0.0:
        return 0.0
    return dcg / idcg


def citation_precision(answer_citations: Sequence, relevant_ids: Sequence) -> float:
    """引用精确率：答案给出的引用中，有多少属于期望证据。"""
    cited = _as_id_set(answer_citations)
    if not cited:
        return 0.0
    relevant = _as_id_set(relevant_ids)
    return len(cited & relevant) / len(cited)


def citation_recall(answer_citations: Sequence, relevant_ids: Sequence) -> float:
    """引用召回率：期望证据中，有多少被答案引用。"""
    relevant = _as_id_set(relevant_ids)
    if not relevant:
        return 0.0
    cited = _as_id_set(answer_citations)
    return len(cited & relevant) / len(relevant)


def citation_f1(answer_citations: Sequence, relevant_ids: Sequence) -> float:
    """引用 F1：引用精确率与召回率的调和平均。"""
    precision = citation_precision(answer_citations, relevant_ids)
    recall = citation_recall(answer_citations, relevant_ids)
    if precision + recall == 0.0:
        return 0.0
    return 2.0 * precision * recall / (precision + recall)


def citation_coverage(answer_citations: Sequence, relevant_ids: Sequence) -> float:
    """向后兼容的引用覆盖率，定义等同于 ``citation_recall``。

    历史报告使用 ``citation_coverage`` 名称，但其分母一直是期望集合，实际
    表达引用召回而非引用精确率。新代码应优先使用 citation P/R/F1 三项。

    参数：
        answer_citations: 从答案中解析出的引用 chunk id 列表。
        relevant_ids: 期望命中的 chunk id 列表。

    返回：
        范围 [0, 1]；relevant_ids 为空时返回 0.0。
    """
    return citation_recall(answer_citations, relevant_ids)


def split_ground_truth_keywords(ground_truth: Union[str, Sequence[str]]) -> List[str]:
    """将参考答案按顿号/分号/逗号（中英文）切分为要点关键词列表。

    参数：
        ground_truth: 字符串（按标点切分）或已经是关键词列表（直接使用）。

    返回：
        去除首尾空白与空项后的关键词列表；空输入返回空列表。
    """
    if not ground_truth:
        return []
    if isinstance(ground_truth, str):
        parts = _KEYWORD_SPLIT_RE.split(ground_truth)
    else:
        parts = list(ground_truth)
    return [p.strip() for p in parts if isinstance(p, str) and p.strip()]


def keyword_hit_rate(answer: str, ground_truth_keywords: Union[str, Sequence[str]]) -> float:
    """要点命中率：答案覆盖参考答案要点的比例。

    参考答案按顿号/分号/逗号切分为要点（见 split_ground_truth_keywords），
    逐条判断其是否在答案中出现（ASCII 大小写不敏感）：
        命中要点数 / 要点总数

    参数：
        answer: 生成的答案文本。
        ground_truth_keywords: 参考答案原文（字符串）或预切分的要点列表。

    返回：
        范围 [0, 1]；要点列表为空或答案为空时返回 0.0。
    """
    keywords = split_ground_truth_keywords(ground_truth_keywords)
    if not keywords or not answer:
        return 0.0
    answer_lower = answer.lower()
    hits = sum(1 for kw in keywords if kw.lower() in answer_lower)
    return hits / len(keywords)


def contains_refusal(answer: str) -> bool:
    """判断答案是否包含「不知道 / 无法回答」类拒答表述（用于负例检查）。"""
    if not answer:
        return False
    return any(phrase in answer for phrase in REFUSAL_PHRASES)


def latency_stats(latencies: Sequence[float]) -> Dict[str, float]:
    """延迟统计：P50 / P95 / 均值 / 样本数。

    百分位采用线性插值法（与 numpy.percentile 默认 method="linear" 一致）：
    排序后 rank = p / 100 * (n - 1)，在相邻两个样本间按小数部分线性插值；
    单样本时 P50 == P95 == 该值。

    参数：
        latencies: 延迟样本列表（单位由调用方决定，eval.run 传入毫秒）。

    返回：
        {"p50": float, "p95": float, "mean": float, "count": int}；
        空列表或 None → {"p50": 0.0, "p95": 0.0, "mean": 0.0, "count": 0}。
    """
    samples = sorted(float(x) for x in (latencies or []))
    n = len(samples)
    if n == 0:
        return {"p50": 0.0, "p95": 0.0, "mean": 0.0, "count": 0}

    def _percentile(p: float) -> float:
        rank = p / 100.0 * (n - 1)
        lower = int(math.floor(rank))
        upper = min(lower + 1, n - 1)
        frac = rank - lower
        return samples[lower] + frac * (samples[upper] - samples[lower])

    return {
        "p50": _percentile(50.0),
        "p95": _percentile(95.0),
        "mean": sum(samples) / n,
        "count": n,
    }
