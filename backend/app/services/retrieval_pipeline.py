"""聊天与评测共享的 chunk 级检索管线。

``VectorStore`` 保持为向量基础设施适配器；本模块统一负责轻量 BM25、
可审计中英术语扩展、chunk-id RRF、过滤和运行期降级诊断。生产聊天允许
在语义不可用时以相同范围的关键词结果继续工作；评测读取 diagnostics 后
fail-close，不能把降级结果记成有效 Hybrid 指标。
"""

from __future__ import annotations

import copy
import math
import re
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional

from sqlalchemy import func, tuple_
from sqlalchemy.orm import Session

from app.core.logger import logger
from app.models import Chunk, Paper


_TECHNICAL_TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:[.\-][A-Za-z0-9]+)*%?")

# 真实问题以中文为主、论文正文以英文为主。这里只扩展有限且可审计的领域术语，
# 不进行猜测式整句翻译；本表原样迁移自 Batch 17 已验证 profile。
_BILINGUAL_TERM_MAP = (
    ("多实例学习", ("multiple", "instance", "learning", "mil")),
    ("全切片", ("whole", "slide", "image", "wsi")),
    ("生存预测", ("survival", "prediction")),
    ("交叉验证", ("cross-validation",)),
    ("外部测试集", ("external", "test", "set")),
    ("可解释性", ("interpretability", "interpretable")),
    ("消融实验", ("ablation",)),
    ("原型", ("prototype",)),
    ("分类", ("classification",)),
    ("推理", ("inference",)),
    ("数据集", ("dataset",)),
    ("准确率", ("accuracy",)),
    ("阈值", ("threshold",)),
    ("队列", ("cohort",)),
    ("病例", ("cases", "patients")),
    ("筛选", ("filter", "filtering")),
    ("临床", ("clinical",)),
    ("聚类", ("cluster", "clustering")),
    ("跨区域", ("cross-region", "inter-region")),
    ("组织", ("tissue",)),
    ("语义", ("semantic",)),
    ("模块", ("module",)),
    ("专家", ("expert",)),
    ("样本", ("sample",)),
    ("两阶段", ("two-stage",)),
    ("训练", ("training",)),
    ("验证", ("validation",)),
    ("实验", ("experiment",)),
    ("表现", ("performance",)),
    ("指标", ("metric",)),
)

# Batch 22 train-only 匿名审计后冻结的病理术语增量。保持独立 profile，
# 不直接修改已晋级生产的 v1 映射，失败实验不会改变旧查询排序。
_BILINGUAL_TERM_MAP_V2_ADDITIONS = (
    ("切片", ("slide",)),
    ("肿瘤", ("tumor",)),
    ("特征提取", ("feature", "extraction")),
    ("特征", ("feature",)),
)

_CHUNK_ID_FULL_RE = re.compile(r"^p(-?\d+)_c(-?\d+)$")

_LOCAL_NEIGHBOR_SEED_POOL = 20
_LOCAL_NEIGHBOR_RADIUS = 2
_LOCAL_NEIGHBOR_DECAY = 0.5
_LOCAL_NEIGHBOR_EXPANDED_CAP = 20
_PARENT_CHILD_POOL = 40
_PARENT_CHILD_DISCOUNTS = (1.0, 0.5, 0.25)
_PAPER_FIRST_POOL = 20
_PAPER_FIRST_RRF_K = 60
_PAPER_FIRST_BONUS = 0.25
_PAPER_FIRST_MAX_PER_PAPER = 2
_DEEP_ROUTE_PRODUCTION_POOL = 10
_DEEP_ROUTE_CANDIDATE_POOL = 20
_DEEP_ROUTE_RRF_K = 60
_ANCHOR_UNIT_TOKENS = frozenset({
    "mm", "cm", "m", "km", "ms", "s", "min", "h", "hz", "khz", "mhz",
    "kb", "mb", "gb", "tb", "mg", "g", "kg", "ml", "l",
})


class ParentMappingError(ValueError):
    """初召回 child 无法安全映射到冻结 parent 时抛出。"""

    def __init__(self, message: str, *, reason: str):
        super().__init__(message)
        self.reason = reason


class WeightedRRFError(ValueError):
    """Weighted-RRF 输入或冻结参数不满足候选契约。"""


class PaperFirstRerankError(ValueError):
    """论文优先证据重排的输入或冻结参数不满足候选契约。"""


class DeepRouteContractError(ValueError):
    """保论文集合深层证据候选的输入不满足冻结契约。"""


class WithinPaperContractError(ValueError):
    """论文内全块查询候选的输入不满足冻结契约。"""


class WithinPaperSemanticContractError(ValueError):
    """论文内语义选块候选的输入不满足冻结契约。"""


def tokenize_technical_terms(text: str) -> List[str]:
    """提取 ASCII 技术锚点，保留连字符、小数、科学计数法和百分号。"""
    return [token.lower() for token in _TECHNICAL_TOKEN_RE.findall(text or "")]


def query_technical_terms(
    text: str,
    *,
    bilingual: bool = False,
    bilingual_profile: str = "v1",
) -> List[str]:
    """提取查询锚点，可选扩展显式中英领域术语。"""
    if bilingual_profile not in {"v1", "v2"}:
        raise ValueError(f"不支持的双语术语 profile: {bilingual_profile}")
    tokens = tokenize_technical_terms(text)
    if bilingual:
        term_map = _BILINGUAL_TERM_MAP
        if bilingual_profile == "v2":
            term_map += _BILINGUAL_TERM_MAP_V2_ADDITIONS
        for chinese, english_terms in term_map:
            if chinese in (text or ""):
                tokens.extend(english_terms)
    return list(dict.fromkeys(tokens))


def extract_factoid_anchors(text: str) -> List[str]:
    """提取查询中的数值、单位和方法/数据集缩写，保持稳定顺序。"""
    raw_tokens = _TECHNICAL_TOKEN_RE.findall(text or "")
    anchors: List[str] = []
    previous_has_digit = False
    for raw_token in raw_tokens:
        normalized = raw_token.lower()
        has_digit = any(char.isdigit() for char in raw_token)
        letters = [char for char in raw_token if char.isalpha()]
        uppercase_count = sum(char.isupper() for char in letters)
        compact_length = len("".join(letters))
        is_acronym = (
            2 <= compact_length <= 12
            and uppercase_count >= 2
        )
        is_adjacent_unit = previous_has_digit and normalized in _ANCHOR_UNIT_TOKENS
        if has_digit or is_acronym or is_adjacent_unit:
            if normalized not in anchors:
                anchors.append(normalized)
        previous_has_digit = has_digit
    return anchors


def _filtered_chunk_query(db: Session, filters: Optional[Dict[str, Any]]):
    """构造应用统一范围约束的 chunk 查询，不在调用方重复过滤逻辑。"""
    filters = filters or {}
    supported = {"paper_id", "year_gte", "year_lte"}
    unknown = sorted(set(filters) - supported)
    if unknown:
        raise ValueError(f"不支持的检索过滤条件: {', '.join(unknown)}")

    query = db.query(Chunk, Paper).join(Paper, Paper.id == Chunk.paper_id)
    if "paper_id" in filters:
        query = query.filter(Chunk.paper_id == filters["paper_id"])
    if "year_gte" in filters:
        query = query.filter(Paper.year >= filters["year_gte"])
    if "year_lte" in filters:
        query = query.filter(Paper.year <= filters["year_lte"])
    return query


def _filtered_chunk_rows(db: Session, filters: Optional[Dict[str, Any]]):
    return _filtered_chunk_query(db, filters).order_by(
        Chunk.paper_id, Chunk.chunk_index
    ).all()


def expand_semantic_chunk_neighbors(
    db: Session,
    semantic_results: List[Dict[str, Any]],
    *,
    filters: Optional[Dict[str, Any]] = None,
    radius: int = _LOCAL_NEIGHBOR_RADIUS,
    decay: float = _LOCAL_NEIGHBOR_DECAY,
    limit: int = _LOCAL_NEIGHBOR_EXPANDED_CAP,
) -> List[Dict[str, Any]]:
    """按语义 rank prior 在同论文内传播分数，并以一次 SQL 读取真实邻块。

    摘要使用 ``chunk_index=-1`` 哨兵，不能因为数字相邻而传播到正文。无效
    seed 不参与传播；多个 seed 覆盖同一候选时只保留最大传播分。
    """
    if radius < 0:
        raise ValueError("语义邻域半径不能为负")
    if not 0.0 <= decay <= 1.0:
        raise ValueError("语义邻域衰减必须位于 0 到 1")
    if limit <= 0:
        return []

    anchors: List[tuple[int, int, int]] = []
    requested_keys: set[tuple[int, int]] = set()
    for rank, item in enumerate(semantic_results, start=1):
        match = _CHUNK_ID_FULL_RE.fullmatch(str(item.get("chunk_id", "")))
        if match is None:
            continue
        paper_id, chunk_index = (int(value) for value in match.groups())
        if item.get("paper_id") != paper_id:
            continue
        anchors.append((paper_id, chunk_index, rank))
        if chunk_index == -1:
            requested_keys.add((paper_id, -1))
            continue
        for offset in range(-radius, radius + 1):
            candidate_index = chunk_index + offset
            if candidate_index >= 0:
                requested_keys.add((paper_id, candidate_index))

    if not anchors or not requested_keys:
        return []

    rows = (
        _filtered_chunk_query(db, filters)
        .filter(tuple_(Chunk.paper_id, Chunk.chunk_index).in_(requested_keys))
        .order_by(Chunk.paper_id, Chunk.chunk_index, Chunk.id)
        .all()
    )
    row_by_key: Dict[tuple[int, int], tuple[Chunk, Paper]] = {}
    duplicate_keys: set[tuple[int, int]] = set()
    for chunk, paper in rows:
        key = (chunk.paper_id, chunk.chunk_index)
        if key in row_by_key:
            duplicate_keys.add(key)
            continue
        row_by_key[key] = (chunk, paper)
    if duplicate_keys:
        logger.warning(
            f"[retrieval_pipeline] 检测到重复 chunk 坐标: {len(duplicate_keys)}"
        )

    # 候选值为 (传播分, 距离, seed rank)，先确认 seed 自身真实存在。
    propagated: Dict[tuple[int, int], tuple[float, int, int]] = {}
    for paper_id, chunk_index, rank in anchors:
        if (paper_id, chunk_index) not in row_by_key:
            continue
        candidate_indexes = (
            (chunk_index,)
            if chunk_index == -1
            else range(max(0, chunk_index - radius), chunk_index + radius + 1)
        )
        for candidate_index in candidate_indexes:
            key = (paper_id, candidate_index)
            if key not in row_by_key:
                continue
            distance = abs(candidate_index - chunk_index)
            score = (decay ** distance) / rank
            previous = propagated.get(key)
            candidate_order = (-score, distance, rank)
            if previous is None or candidate_order < (
                -previous[0], previous[1], previous[2]
            ):
                propagated[key] = (score, distance, rank)

    expanded: List[Dict[str, Any]] = []
    for key, (score, distance, rank) in propagated.items():
        chunk, paper = row_by_key[key]
        expanded.append({
            "chunk_id": f"p{chunk.paper_id}_c{chunk.chunk_index}",
            "paper_id": chunk.paper_id,
            "title": paper.title,
            "authors": paper.authors,
            "year": paper.year,
            "content": chunk.content or "",
            "page_number": chunk.page_number,
            "chunk_type": chunk.chunk_type,
            "score": score,
            "source": "semantic-neighbor",
            "neighbor_score": score,
            "neighbor_distance": distance,
            "best_seed_rank": rank,
        })
    expanded.sort(key=lambda item: (
        -item["neighbor_score"],
        item["neighbor_distance"],
        item["best_seed_rank"],
        item["chunk_id"],
    ))
    return expanded[:limit]


def bm25_chunk_search(
    db: Session,
    query: str,
    limit: Optional[int] = 20,
    *,
    bilingual: bool = False,
    bilingual_profile: str = "v1",
    filters: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """以技术锚点对 chunk 内容执行轻量 BM25。"""
    query_tokens = query_technical_terms(
        query,
        bilingual=bilingual,
        bilingual_profile=bilingual_profile,
    )
    if not query_tokens:
        return []

    rows = _filtered_chunk_rows(db, filters)
    if not rows:
        return []
    tokenized = [tokenize_technical_terms(chunk.content or "") for chunk, _ in rows]
    lengths = [len(tokens) for tokens in tokenized]
    avg_length = sum(lengths) / len(lengths) if lengths else 0.0
    if avg_length <= 0.0:
        return []

    doc_freq = {
        token: sum(1 for tokens in tokenized if token in set(tokens))
        for token in query_tokens
    }
    n_docs = len(rows)
    k1 = 1.2
    b = 0.9
    scored: List[Dict[str, Any]] = []
    for (chunk, paper), tokens, doc_length in zip(rows, tokenized, lengths):
        counts = Counter(tokens)
        score = 0.0
        for token in query_tokens:
            tf = counts.get(token, 0)
            if tf == 0:
                continue
            df = doc_freq[token]
            idf = math.log(1.0 + (n_docs - df + 0.5) / (df + 0.5))
            length_norm = 1.0 - b + b * doc_length / avg_length
            score += idf * (tf * (k1 + 1.0)) / (tf + k1 * length_norm)
        if score <= 0.0:
            continue
        scored.append({
            "chunk_id": f"p{chunk.paper_id}_c{chunk.chunk_index}",
            "paper_id": chunk.paper_id,
            "title": paper.title,
            "authors": paper.authors,
            "year": paper.year,
            "content": chunk.content or "",
            "page_number": chunk.page_number,
            "chunk_type": chunk.chunk_type,
            "score": score,
            "source": (
                "keyword-bm25-bilingual-v2"
                if bilingual and bilingual_profile == "v2"
                else "keyword-bm25-bilingual" if bilingual else "keyword-bm25"
            ),
        })
    scored.sort(key=lambda item: (-item["score"], item["chunk_id"]))
    return scored if limit is None else scored[:limit]


def within_paper_bm25_search(
    db: Session,
    query: str,
    *,
    paper_ids: List[int],
    filters: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """在生产已选论文的全部 chunk 上批量执行冻结双语 BM25。"""
    if (
        not isinstance(paper_ids, list)
        or not paper_ids
        or any(
            not isinstance(value, int) or isinstance(value, bool) or value <= 0
            for value in paper_ids
        )
        or len(paper_ids) != len(set(paper_ids))
    ):
        raise WithinPaperContractError("paper_ids 必须是非空唯一正整数列表")
    query_tokens = query_technical_terms(query, bilingual=True)
    if not query_tokens:
        return []

    rows = (
        _filtered_chunk_query(db, filters)
        .filter(Chunk.paper_id.in_(paper_ids))
        .order_by(Chunk.paper_id, Chunk.chunk_index, Chunk.id)
        .all()
    )
    if not rows:
        return []
    allowed_papers = set(paper_ids)
    if any(chunk.paper_id not in allowed_papers for chunk, _ in rows):
        raise WithinPaperContractError("局部查询返回未选论文")

    tokenized = [
        tokenize_technical_terms(chunk.content or "") for chunk, _ in rows
    ]
    lengths = [len(tokens) for tokens in tokenized]
    avg_length = sum(lengths) / len(lengths) if lengths else 0.0
    if avg_length <= 0.0:
        return []
    doc_freq = {
        token: sum(1 for tokens in tokenized if token in set(tokens))
        for token in query_tokens
    }
    n_docs = len(rows)
    k1 = 1.2
    b = 0.9
    scored: List[Dict[str, Any]] = []
    for (chunk, paper), tokens, doc_length in zip(rows, tokenized, lengths):
        counts = Counter(tokens)
        score = 0.0
        for token in query_tokens:
            tf = counts.get(token, 0)
            if tf == 0:
                continue
            df = doc_freq[token]
            idf = math.log(1.0 + (n_docs - df + 0.5) / (df + 0.5))
            length_norm = 1.0 - b + b * doc_length / avg_length
            score += idf * (tf * (k1 + 1.0)) / (tf + k1 * length_norm)
        if score <= 0.0:
            continue
        scored.append({
            "chunk_id": f"p{chunk.paper_id}_c{chunk.chunk_index}",
            "paper_id": chunk.paper_id,
            "title": paper.title,
            "authors": paper.authors,
            "year": paper.year,
            "content": chunk.content or "",
            "page_number": chunk.page_number,
            "chunk_type": chunk.chunk_type,
            "score": score,
            "source": "within-paper-bm25-bilingual",
        })
    scored.sort(key=lambda item: (-item["score"], item["chunk_id"]))
    return scored


def within_paper_semantic_search(
    db: Session,
    store: Any,
    query_embedding: List[float],
    *,
    paper_ids: List[int],
    filters: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """复用查询向量，仅获取生产已选论文各自的 semantic top-5。"""
    if (
        not isinstance(paper_ids, list)
        or not paper_ids
        or len(paper_ids) != len(set(paper_ids))
        or any(
            not isinstance(value, int) or isinstance(value, bool) or value <= 0
            for value in paper_ids
        )
    ):
        raise WithinPaperSemanticContractError(
            "paper_ids 必须是非空唯一正整数列表"
        )
    counts = dict(
        db.query(Chunk.paper_id, func.count(Chunk.id))
        .filter(Chunk.paper_id.in_(paper_ids))
        .group_by(Chunk.paper_id)
        .all()
    )
    if set(counts) != set(paper_ids):
        raise WithinPaperSemanticContractError("已选论文缺少 chunk")
    output: List[Dict[str, Any]] = []
    for paper_id in paper_ids:
        scoped_filters = dict(filters or {})
        existing = scoped_filters.get("paper_id")
        if existing is not None and existing != paper_id:
            raise WithinPaperSemanticContractError("论文过滤范围冲突")
        scoped_filters["paper_id"] = paper_id
        limit = min(5, int(counts[paper_id]))
        rows = store.search_by_embedding(
            query_embedding, top_k=limit, filters=scoped_filters
        )
        if len(rows) != limit:
            raise WithinPaperSemanticContractError("论文内向量结果数量不完整")
        for rank, item in enumerate(rows, start=1):
            chunk_id = str(item.get("chunk_id", ""))
            match = _CHUNK_ID_FULL_RE.fullmatch(chunk_id)
            if (
                match is None
                or int(match.group(1)) != paper_id
                or item.get("paper_id") != paper_id
            ):
                raise WithinPaperSemanticContractError("论文内向量结果越界")
            row = copy.deepcopy(item)
            row.update({
                "within_paper_rank": rank,
                "source": "within-paper-semantic",
            })
            output.append(row)
    return output


def anchor_chunk_search(
    db: Session,
    query: str,
    *,
    limit: int = 10,
    filters: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """只用冻结 factoid 锚点执行第三路 BM25，不做双语扩展。"""
    anchors = extract_factoid_anchors(query)
    if not anchors:
        return []
    results = bm25_chunk_search(
        db,
        " ".join(anchors),
        limit,
        bilingual=False,
        filters=filters,
    )
    for item in results:
        item["source"] = "keyword-anchor"
    return results


def keyword_chunk_search(
    db: Session,
    query: str,
    limit: int = 20,
    *,
    lexical_profile: str = "count",
    filters: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """按显式 profile 执行 chunk 级词法检索。"""
    if lexical_profile == "bm25-bilingual-neighbor":
        scored = bm25_chunk_search(
            db, query, None, bilingual=True, filters=filters
        )
        return rerank_bm25_neighbors(scored)[:limit]
    if lexical_profile in {"bm25", "bm25-bilingual", "bm25-bilingual-v2"}:
        return bm25_chunk_search(
            db,
            query,
            limit,
            bilingual=lexical_profile in {
                "bm25-bilingual", "bm25-bilingual-v2"
            },
            bilingual_profile=(
                "v2" if lexical_profile == "bm25-bilingual-v2" else "v1"
            ),
            filters=filters,
        )
    if lexical_profile != "count":
        raise ValueError(f"不支持的词法检索 profile: {lexical_profile}")

    tokens = [token for token in re.split(r"\s+", query.strip()) if token]
    if not tokens:
        return []
    scored: List[Dict[str, Any]] = []
    for chunk, paper in _filtered_chunk_rows(db, filters):
        content = chunk.content or ""
        lowered = content.lower()
        score = sum(lowered.count(token.lower()) for token in tokens)
        if score <= 0:
            continue
        scored.append({
            "chunk_id": f"p{chunk.paper_id}_c{chunk.chunk_index}",
            "paper_id": chunk.paper_id,
            "title": paper.title,
            "authors": paper.authors,
            "year": paper.year,
            "content": content,
            "page_number": chunk.page_number,
            "chunk_type": chunk.chunk_type,
            "score": float(score),
            "source": "keyword",
        })
    scored.sort(key=lambda item: (-item["score"], item["chunk_id"]))
    return scored[:limit]


def rerank_bm25_neighbors(
    scored: List[Dict[str, Any]], *, neighbor_weight: float = 0.5
) -> List[Dict[str, Any]]:
    """兼容历史实验：按同论文 chunk_index±1 的 BM25 分数做邻块增强。"""
    base_scores: Dict[tuple[int, int], float] = {}
    chunk_keys: Dict[str, tuple[int, int]] = {}
    for item in scored:
        chunk_id = str(item.get("chunk_id", ""))
        match = _CHUNK_ID_FULL_RE.fullmatch(chunk_id)
        if match is None:
            continue
        paper_id, chunk_index = (int(value) for value in match.groups())
        chunk_keys[chunk_id] = (paper_id, chunk_index)
        base_scores[(paper_id, chunk_index)] = float(item.get("score", 0.0))

    reranked: List[Dict[str, Any]] = []
    for item in scored:
        updated = copy.deepcopy(item)
        key = chunk_keys.get(str(item.get("chunk_id", "")))
        score = float(item.get("score", 0.0))
        if key is not None:
            paper_id, chunk_index = key
            score += neighbor_weight * (
                base_scores.get((paper_id, chunk_index - 1), 0.0)
                + base_scores.get((paper_id, chunk_index + 1), 0.0)
            )
        updated["score"] = score
        updated["source"] = "keyword-bm25-bilingual-neighbor"
        reranked.append(updated)
    reranked.sort(key=lambda item: (-item["score"], item["chunk_id"]))
    return reranked


def rrf_fuse_chunks(
    semantic_results: List[Dict[str, Any]],
    keyword_results: List[Dict[str, Any]],
    top_k: int,
    *,
    k: int = 60,
) -> List[Dict[str, Any]]:
    """按 chunk_id 去重的稳定 RRF；不修改任一路输入。"""
    scores: Dict[str, float] = {}
    metas: Dict[str, Dict[str, Any]] = {}
    order: Dict[str, int] = {}

    def _add(results: List[Dict[str, Any]]) -> None:
        for rank, item in enumerate(results):
            chunk_id = item.get("chunk_id")
            if chunk_id is None and _CHUNK_ID_FULL_RE.fullmatch(
                str(item.get("source", ""))
            ):
                # 兼容 Phase 1–3 的旧检索桩/插件结果：当 source 本身就是
                # p{paper}_c{index} 时可作为 chunk id；普通 "semantic" 等
                # 来源标签绝不能参与去重。
                chunk_id = str(item["source"])
            if chunk_id is None:
                continue
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank + 1)
            if chunk_id not in metas:
                order[chunk_id] = len(order)
                metas[chunk_id] = copy.deepcopy(item)

    _add(semantic_results)
    _add(keyword_results)
    ordered = sorted(scores, key=lambda cid: (-scores[cid], order[cid], cid))
    # 保留首个命中分支的 source 与元数据，兼容既有聊天引用契约；
    # 管线级 effective_profile 由 diagnostics 单独表达。
    return [copy.deepcopy(metas[cid]) for cid in ordered[:top_k]]


def paper_first_evidence_fuse_chunks(
    semantic_results: List[Dict[str, Any]],
    keyword_results: List[Dict[str, Any]],
    top_k: int,
    *,
    k: int = _PAPER_FIRST_RRF_K,
    paper_bonus: float = _PAPER_FIRST_BONUS,
    max_per_paper: int = _PAPER_FIRST_MAX_PER_PAPER,
) -> List[Dict[str, Any]]:
    """按论文先验重排两路 RRF，并限制单篇论文占用的最终名额。

    候选只读取每路前 20 项。与生产旧 RRF 不同，此实验 profile 对输入
    采取严格契约：每路不得重复，chunk ID 必须规范且与 paper_id 一致。
    """
    if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k < 0:
        raise PaperFirstRerankError("top_k 必须是非负整数")
    if top_k == 0:
        return []
    if not isinstance(k, int) or isinstance(k, bool) or k <= 0:
        raise PaperFirstRerankError("k 必须是正整数")
    if (
        not isinstance(paper_bonus, (int, float))
        or isinstance(paper_bonus, bool)
        or not math.isfinite(float(paper_bonus))
        or float(paper_bonus) < 0.0
    ):
        raise PaperFirstRerankError("paper_bonus 必须是有限非负数")
    if (
        not isinstance(max_per_paper, int)
        or isinstance(max_per_paper, bool)
        or max_per_paper <= 0
    ):
        raise PaperFirstRerankError("max_per_paper 必须是正整数")

    scores: Dict[str, float] = {}
    metas: Dict[str, Dict[str, Any]] = {}
    order: Dict[str, int] = {}
    papers: Dict[str, int] = {}

    def _canonical(item: Dict[str, Any]) -> tuple[str, int]:
        chunk_id = item.get("chunk_id")
        match = _CHUNK_ID_FULL_RE.fullmatch(str(chunk_id or ""))
        if match is None:
            raise PaperFirstRerankError("chunk_id 必须是 canonical pN_cN")
        paper_id, chunk_index = (int(value) for value in match.groups())
        canonical = f"p{paper_id}_c{chunk_index}"
        if (
            paper_id <= 0
            or chunk_index < -1
            or canonical != chunk_id
            or item.get("paper_id") != paper_id
        ):
            raise PaperFirstRerankError("chunk_id 与 paper_id 不符合契约")
        return canonical, paper_id

    def _add_route(results: List[Dict[str, Any]]) -> None:
        seen: set[str] = set()
        for rank, item in enumerate(results[:_PAPER_FIRST_POOL], start=1):
            chunk_id, paper_id = _canonical(item)
            if chunk_id in seen:
                raise PaperFirstRerankError("单路结果含重复 chunk_id")
            seen.add(chunk_id)
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank)
            if chunk_id not in metas:
                order[chunk_id] = len(order)
                metas[chunk_id] = copy.deepcopy(item)
                papers[chunk_id] = paper_id

    _add_route(semantic_results)
    _add_route(keyword_results)
    paper_priors: Dict[int, float] = {}
    for chunk_id, score in scores.items():
        paper_id = papers[chunk_id]
        paper_priors[paper_id] = max(paper_priors.get(paper_id, 0.0), score)
    candidate_scores = {
        chunk_id: score + float(paper_bonus) * paper_priors[papers[chunk_id]]
        for chunk_id, score in scores.items()
    }
    ordered = sorted(
        scores,
        key=lambda chunk_id: (
            -candidate_scores[chunk_id], order[chunk_id], chunk_id
        ),
    )
    selected: List[Dict[str, Any]] = []
    paper_counts: Counter[int] = Counter()
    for chunk_id in ordered:
        paper_id = papers[chunk_id]
        if paper_counts[paper_id] >= max_per_paper:
            continue
        item = copy.deepcopy(metas[chunk_id])
        item.update({
            "score": candidate_scores[chunk_id],
            "chunk_rrf_score": scores[chunk_id],
            "paper_prior_score": paper_priors[paper_id],
            "paper_first_score": candidate_scores[chunk_id],
        })
        selected.append(item)
        paper_counts[paper_id] += 1
        if len(selected) >= top_k:
            break
    return selected


def paper_preserving_deep_route_fuse_chunks(
    semantic_results: List[Dict[str, Any]],
    keyword_results: List[Dict[str, Any]],
    top_k: int,
) -> List[Dict[str, Any]]:
    """保持生产论文 slot，仅在同论文内以 top-20 RRF 选择证据块。

    生产控制始终由两路前 10 项的 legacy RRF 决定；完整 top-20 只参与
    已入选论文内部的 chunk 选择，不能引入、删除或移动论文 slot。
    """
    if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k < 0:
        raise DeepRouteContractError("top_k 必须是非负整数")
    if top_k == 0:
        return []

    route_rows: list[list[tuple[str, int, Dict[str, Any]]]] = []
    for results in (semantic_results, keyword_results):
        seen: set[str] = set()
        rows: list[tuple[str, int, Dict[str, Any]]] = []
        for item in results[:_DEEP_ROUTE_CANDIDATE_POOL]:
            chunk_id = item.get("chunk_id")
            match = _CHUNK_ID_FULL_RE.fullmatch(str(chunk_id or ""))
            if match is None:
                raise DeepRouteContractError("chunk_id 必须是 canonical pN_cN")
            paper_id, chunk_index = (int(value) for value in match.groups())
            canonical = f"p{paper_id}_c{chunk_index}"
            if (
                paper_id <= 0
                or chunk_index < -1
                or canonical != chunk_id
                or item.get("paper_id") != paper_id
            ):
                raise DeepRouteContractError("chunk_id 与 paper_id 不符合契约")
            if chunk_id in seen:
                raise DeepRouteContractError("单路结果含重复 chunk_id")
            seen.add(chunk_id)
            rows.append((chunk_id, paper_id, item))
        route_rows.append(rows)

    semantic_rows, keyword_rows = route_rows
    baseline = rrf_fuse_chunks(
        [row[2] for row in semantic_rows[:_DEEP_ROUTE_PRODUCTION_POOL]],
        [row[2] for row in keyword_rows[:_DEEP_ROUTE_PRODUCTION_POOL]],
        top_k,
        k=_DEEP_ROUTE_RRF_K,
    )
    if not baseline:
        return []

    baseline_ids = [str(item["chunk_id"]) for item in baseline]
    baseline_rank = {chunk_id: rank for rank, chunk_id in enumerate(baseline_ids)}
    slot_papers = [int(item["paper_id"]) for item in baseline]
    paper_quota = Counter(slot_papers)

    scores: Dict[str, float] = {}
    papers: Dict[str, int] = {}
    metas: Dict[str, Dict[str, Any]] = {}
    first_seen: Dict[str, int] = {}
    for rows in route_rows:
        for rank, (chunk_id, paper_id, item) in enumerate(rows, start=1):
            scores[chunk_id] = (
                scores.get(chunk_id, 0.0)
                + 1.0 / (_DEEP_ROUTE_RRF_K + rank)
            )
            papers.setdefault(chunk_id, paper_id)
            if chunk_id not in metas:
                first_seen[chunk_id] = len(first_seen)
                metas[chunk_id] = copy.deepcopy(item)

    selected_by_paper: Dict[int, List[str]] = {}
    for paper_id, quota in paper_quota.items():
        candidates = [
            chunk_id for chunk_id, candidate_paper in papers.items()
            if candidate_paper == paper_id
        ]
        candidates.sort(key=lambda chunk_id: (
            -scores[chunk_id],
            0 if chunk_id in baseline_rank else 1,
            baseline_rank.get(chunk_id, len(baseline_rank)),
            first_seen[chunk_id],
            chunk_id,
        ))
        if len(candidates) < quota:
            raise DeepRouteContractError("深层候选无法满足冻结论文 slot")
        selected_by_paper[paper_id] = candidates[:quota]

    paper_offsets: Counter[int] = Counter()
    selected: List[Dict[str, Any]] = []
    for slot, paper_id in enumerate(slot_papers):
        offset = paper_offsets[paper_id]
        chunk_id = selected_by_paper[paper_id][offset]
        paper_offsets[paper_id] += 1
        if chunk_id == baseline_ids[slot]:
            selected.append(copy.deepcopy(baseline[slot]))
            continue
        item = copy.deepcopy(metas[chunk_id])
        item.update({
            "score": scores[chunk_id],
            "deep_route_score": scores[chunk_id],
            "replaced_production_chunk": baseline_ids[slot],
        })
        selected.append(item)

    if [int(item["paper_id"]) for item in selected] != slot_papers:
        raise DeepRouteContractError("候选改变了冻结论文 slot")
    if len({item["chunk_id"] for item in selected}) != len(selected):
        raise DeepRouteContractError("候选产生重复 chunk")
    return selected


def within_paper_query_fuse_chunks(
    semantic_results: List[Dict[str, Any]],
    keyword_results: List[Dict[str, Any]],
    local_results: List[Dict[str, Any]],
    top_k: int,
) -> List[Dict[str, Any]]:
    """锁定生产论文 slot，只替换局部 BM25 为零的 incumbent。"""
    if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k < 0:
        raise WithinPaperContractError("top_k 必须是非负整数")
    if top_k == 0:
        return []

    def canonical(
        item: Dict[str, Any], *, require_positive_score: bool
    ) -> tuple[str, int]:
        chunk_id = item.get("chunk_id")
        match = _CHUNK_ID_FULL_RE.fullmatch(str(chunk_id or ""))
        if match is None:
            raise WithinPaperContractError("chunk_id 必须是 canonical pN_cN")
        paper_id, chunk_index = (int(value) for value in match.groups())
        canonical_id = f"p{paper_id}_c{chunk_index}"
        if (
            paper_id <= 0
            or chunk_index < -1
            or canonical_id != chunk_id
            or item.get("paper_id") != paper_id
        ):
            raise WithinPaperContractError("chunk_id 与 paper_id 不符合契约")
        if require_positive_score:
            score = item.get("score")
            if (
                not isinstance(score, (int, float))
                or isinstance(score, bool)
                or not math.isfinite(float(score))
                or float(score) <= 0.0
            ):
                raise WithinPaperContractError("局部 BM25 分数必须为有限正数")
        return canonical_id, paper_id

    for route in (semantic_results, keyword_results):
        seen: set[str] = set()
        for item in route[:10]:
            chunk_id, _ = canonical(item, require_positive_score=False)
            if chunk_id in seen:
                raise WithinPaperContractError("生产单路含重复 chunk_id")
            seen.add(chunk_id)

    baseline = rrf_fuse_chunks(
        semantic_results[:10], keyword_results[:10], top_k, k=60
    )
    if not baseline:
        if local_results:
            raise WithinPaperContractError("无生产 slot 时不得存在局部候选")
        return []
    baseline_ids = [str(item["chunk_id"]) for item in baseline]
    slot_papers = [int(item["paper_id"]) for item in baseline]
    selected_papers = set(slot_papers)

    local_by_id: Dict[str, Dict[str, Any]] = {}
    for item in local_results:
        chunk_id, paper_id = canonical(item, require_positive_score=True)
        if chunk_id in local_by_id:
            raise WithinPaperContractError("局部结果含重复 chunk_id")
        if paper_id not in selected_papers:
            raise WithinPaperContractError("局部结果含未选论文")
        local_by_id[chunk_id] = copy.deepcopy(item)

    incumbents = set(baseline_ids)
    replacement_by_paper: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for chunk_id, item in local_by_id.items():
        if chunk_id not in incumbents:
            replacement_by_paper[int(item["paper_id"])].append(item)
    for rows in replacement_by_paper.values():
        rows.sort(key=lambda item: (-float(item["score"]), item["chunk_id"]))

    offsets: Counter[int] = Counter()
    used = set(baseline_ids)
    selected: List[Dict[str, Any]] = []
    for slot, incumbent in enumerate(baseline):
        incumbent_id = baseline_ids[slot]
        paper_id = slot_papers[slot]
        if incumbent_id in local_by_id:
            selected.append(copy.deepcopy(incumbent))
            continue
        candidates = replacement_by_paper.get(paper_id, [])
        offset = offsets[paper_id]
        while offset < len(candidates) and candidates[offset]["chunk_id"] in used:
            offset += 1
        offsets[paper_id] = offset
        if offset >= len(candidates):
            selected.append(copy.deepcopy(incumbent))
            continue
        replacement = copy.deepcopy(candidates[offset])
        offsets[paper_id] += 1
        used.add(replacement["chunk_id"])
        replacement["replaced_production_chunk"] = incumbent_id
        selected.append(replacement)

    if [int(item["paper_id"]) for item in selected] != slot_papers:
        raise WithinPaperContractError("候选改变了冻结论文 slot")
    if len({item["chunk_id"] for item in selected}) != len(selected):
        raise WithinPaperContractError("候选产生重复 chunk")
    return selected


def within_paper_semantic_fuse_chunks(
    semantic_results: List[Dict[str, Any]],
    keyword_results: List[Dict[str, Any]],
    local_results: List[Dict[str, Any]],
    top_k: int,
) -> List[Dict[str, Any]]:
    """冻结生产论文 slot，锁定局部 top-5 incumbent 并同论文替换。"""
    if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k < 0:
        raise WithinPaperSemanticContractError("top_k 必须是非负整数")
    if top_k == 0:
        return []

    def canonical(item: Dict[str, Any]) -> tuple[str, int]:
        chunk_id = item.get("chunk_id")
        match = _CHUNK_ID_FULL_RE.fullmatch(str(chunk_id or ""))
        if match is None:
            raise WithinPaperSemanticContractError("chunk_id 非法")
        paper_id, chunk_index = (int(value) for value in match.groups())
        if (
            paper_id <= 0
            or chunk_index < -1
            or chunk_id != f"p{paper_id}_c{chunk_index}"
            or item.get("paper_id") != paper_id
        ):
            raise WithinPaperSemanticContractError("chunk 与论文 metadata 不一致")
        return str(chunk_id), paper_id

    baseline = rrf_fuse_chunks(
        semantic_results[:10], keyword_results[:10], top_k, k=60
    )
    if not baseline:
        if local_results:
            raise WithinPaperSemanticContractError("无生产 slot 时存在局部结果")
        return []
    baseline_ids = [canonical(item)[0] for item in baseline]
    slot_papers = [canonical(item)[1] for item in baseline]
    selected_papers = set(slot_papers)

    by_paper: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    seen_ids: set[str] = set()
    seen_ranks: Dict[int, set[int]] = defaultdict(set)
    for item in local_results:
        chunk_id, paper_id = canonical(item)
        rank = item.get("within_paper_rank")
        if (
            not isinstance(rank, int)
            or isinstance(rank, bool)
            or not 1 <= rank <= 5
        ):
            raise WithinPaperSemanticContractError("论文内语义 rank 非法")
        if paper_id not in selected_papers:
            raise WithinPaperSemanticContractError("局部结果含未选论文")
        if chunk_id in seen_ids or rank in seen_ranks[paper_id]:
            raise WithinPaperSemanticContractError("局部结果含重复 ID 或 rank")
        seen_ids.add(chunk_id)
        seen_ranks[paper_id].add(rank)
        by_paper[paper_id].append(copy.deepcopy(item))
    for rows in by_paper.values():
        rows.sort(key=lambda item: (item["within_paper_rank"], item["chunk_id"]))

    used = set(baseline_ids)
    selected: List[Dict[str, Any]] = []
    for incumbent, incumbent_id, paper_id in zip(
        baseline, baseline_ids, slot_papers
    ):
        local_ids = {item["chunk_id"] for item in by_paper.get(paper_id, [])}
        if incumbent_id in local_ids:
            selected.append(copy.deepcopy(incumbent))
            continue
        replacement = next(
            (
                item for item in by_paper.get(paper_id, [])
                if item["chunk_id"] not in used
            ),
            None,
        )
        if replacement is None:
            selected.append(copy.deepcopy(incumbent))
            continue
        row = copy.deepcopy(replacement)
        row["replaced_production_chunk"] = incumbent_id
        used.add(row["chunk_id"])
        selected.append(row)
    if [int(item["paper_id"]) for item in selected] != slot_papers:
        raise WithinPaperSemanticContractError("候选改变论文 slot")
    if len({item["chunk_id"] for item in selected}) != len(selected):
        raise WithinPaperSemanticContractError("候选产生重复 chunk")
    return selected


def anchor_rrf_fuse_chunks(
    semantic_results: List[Dict[str, Any]],
    keyword_results: List[Dict[str, Any]],
    anchor_results: List[Dict[str, Any]],
    top_k: int,
    *,
    k: int = 60,
) -> List[Dict[str, Any]]:
    """保留 production legacy RRF 语义并增加等权第三路。"""
    if not anchor_results:
        return rrf_fuse_chunks(
            semantic_results, keyword_results, top_k, k=k
        )
    scores: Dict[Any, float] = {}
    metas: Dict[Any, Dict[str, Any]] = {}
    order: Dict[Any, int] = {}
    for route in (semantic_results, keyword_results, anchor_results):
        for rank, item in enumerate(route):
            chunk_id = item.get("chunk_id")
            if chunk_id is None and _CHUNK_ID_FULL_RE.fullmatch(
                str(item.get("source", ""))
            ):
                chunk_id = str(item["source"])
            if chunk_id is None:
                continue
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank + 1)
            if chunk_id not in metas:
                order[chunk_id] = len(order)
                metas[chunk_id] = copy.deepcopy(item)
    ordered = sorted(scores, key=lambda cid: (-scores[cid], order[cid], cid))
    return [copy.deepcopy(metas[cid]) for cid in ordered[:top_k]]


def weighted_rrf_fuse_chunks(
    semantic_results: List[Dict[str, Any]],
    keyword_results: List[Dict[str, Any]],
    top_k: int,
    *,
    semantic_weight: float = 1.0,
    keyword_weight: float = 1.0,
    k: int = 60,
) -> List[Dict[str, Any]]:
    """严格去重且稳定排序的 Weighted-RRF；不改变历史等权函数。"""
    if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k < 0:
        raise WeightedRRFError("top_k 必须是非负整数")
    if top_k == 0:
        return []
    if not isinstance(k, int) or isinstance(k, bool) or k <= 0:
        raise WeightedRRFError("k 必须是正整数")
    for name, value in (
        ("semantic_weight", semantic_weight),
        ("keyword_weight", keyword_weight),
    ):
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(float(value))
            or float(value) <= 0
        ):
            raise WeightedRRFError(f"{name} 必须是有限正数")

    scores: Dict[str, float] = {}
    metas: Dict[str, Dict[str, Any]] = {}

    def canonical_id(item: Dict[str, Any]) -> str:
        chunk_id = item.get("chunk_id")
        match = _CHUNK_ID_FULL_RE.fullmatch(str(chunk_id or ""))
        if match is None:
            raise WeightedRRFError("chunk_id 必须是 canonical pN_cN")
        paper_id, chunk_index = (int(value) for value in match.groups())
        canonical = f"p{paper_id}_c{chunk_index}"
        if paper_id <= 0 or chunk_index < -1 or canonical != chunk_id:
            raise WeightedRRFError("chunk_id 必须是 canonical pN_cN")
        return canonical

    def add_route(results: List[Dict[str, Any]], weight: float) -> None:
        unique: list[tuple[str, Dict[str, Any]]] = []
        seen: set[str] = set()
        for item in results:
            chunk_id = canonical_id(item)
            if chunk_id in seen:
                continue
            seen.add(chunk_id)
            unique.append((chunk_id, item))
        for rank, (chunk_id, item) in enumerate(unique, start=1):
            scores[chunk_id] = (
                scores.get(chunk_id, 0.0) + float(weight) / (k + rank)
            )
            # semantic 路先执行，固定为跨路 metadata 优先来源。
            metas.setdefault(chunk_id, copy.deepcopy(item))

    add_route(semantic_results, semantic_weight)
    add_route(keyword_results, keyword_weight)
    ordered = sorted(scores, key=lambda chunk_id: (-scores[chunk_id], chunk_id))
    return [copy.deepcopy(metas[chunk_id]) for chunk_id in ordered[:top_k]]


def weighted_rrf_compat_fuse_chunks(
    semantic_results: List[Dict[str, Any]],
    keyword_results: List[Dict[str, Any]],
    top_k: int,
    *,
    semantic_weight: float = 1.0,
    keyword_weight: float = 1.0,
    k: int = 60,
) -> List[Dict[str, Any]]:
    """只改变分支分子的旧版兼容 Weighted-RRF。

    该函数有意保留 ``rrf_fuse_chunks`` 的原始 rank、重复贡献、ID
    fallback、首次 metadata、tie 与切片/异常语义；新增校验只约束新引入的
    两个权重。等权时应与旧函数逐值相等。
    """
    for name, value in (
        ("semantic_weight", semantic_weight),
        ("keyword_weight", keyword_weight),
    ):
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(float(value))
            or float(value) <= 0
        ):
            raise WeightedRRFError(f"{name} 必须是有限正数")

    scores: Dict[Any, float] = {}
    metas: Dict[Any, Dict[str, Any]] = {}
    order: Dict[Any, int] = {}

    def _add(results: List[Dict[str, Any]], weight: float) -> None:
        for rank, item in enumerate(results):
            chunk_id = item.get("chunk_id")
            if chunk_id is None and _CHUNK_ID_FULL_RE.fullmatch(
                str(item.get("source", ""))
            ):
                chunk_id = str(item["source"])
            if chunk_id is None:
                continue
            scores[chunk_id] = (
                scores.get(chunk_id, 0.0) + float(weight) / (k + rank + 1)
            )
            if chunk_id not in metas:
                order[chunk_id] = len(order)
                metas[chunk_id] = copy.deepcopy(item)

    _add(semantic_results, semantic_weight)
    _add(keyword_results, keyword_weight)
    ordered = sorted(scores, key=lambda cid: (-scores[cid], order[cid], cid))
    return [copy.deepcopy(metas[cid]) for cid in ordered[:top_k]]


def parent_child_fuse_chunks(
    semantic_results: List[Dict[str, Any]],
    keyword_results: List[Dict[str, Any]],
    parent_map: Dict[str, str],
    top_k: int,
    *,
    k: int = 60,
) -> List[Dict[str, Any]]:
    """按冻结 RRF/parent 折扣/round-robin 契约融合 child 结果。"""
    if top_k <= 0:
        return []
    child_scores: Dict[str, float] = {}
    metas: Dict[str, Dict[str, Any]] = {}
    for route in (semantic_results, keyword_results):
        seen_route: set[str] = set()
        unique_route: list[Dict[str, Any]] = []
        for item in route[:_PARENT_CHILD_POOL]:
            chunk_id = str(item.get("chunk_id", ""))
            if not _CHUNK_ID_FULL_RE.fullmatch(chunk_id):
                raise ParentMappingError(
                    f"非法 child id: {chunk_id!r}",
                    reason="parent_mapping_invalid",
                )
            if chunk_id in seen_route:
                continue
            seen_route.add(chunk_id)
            unique_route.append(item)
        for rank, item in enumerate(unique_route):
            chunk_id = str(item["chunk_id"])
            if chunk_id not in parent_map:
                raise ParentMappingError(
                    f"child {chunk_id} 缺少 parent 映射",
                    reason="parent_mapping_missing",
                )
            parent_id = str(parent_map[chunk_id])
            child_match = _CHUNK_ID_FULL_RE.fullmatch(chunk_id)
            parent_match = _CHUNK_ID_FULL_RE.fullmatch(parent_id)
            if (
                parent_match is None
                or child_match is None
                or parent_match.group(1) != child_match.group(1)
            ):
                raise ParentMappingError(
                    f"child {chunk_id} 的 parent id 非法: {parent_id!r}",
                    reason="parent_mapping_invalid",
                )
            child_scores[chunk_id] = (
                child_scores.get(chunk_id, 0.0) + 1.0 / (k + rank + 1)
            )
            metas.setdefault(chunk_id, copy.deepcopy(item))

    children_by_parent: Dict[str, List[str]] = defaultdict(list)
    for chunk_id in child_scores:
        children_by_parent[parent_map[chunk_id]].append(chunk_id)
    parent_scores: Dict[str, float] = {}
    for parent_id, child_ids in children_by_parent.items():
        child_ids.sort(key=lambda cid: (-child_scores[cid], cid))
        parent_scores[parent_id] = sum(
            discount * child_scores[chunk_id]
            for discount, chunk_id in zip(
                _PARENT_CHILD_DISCOUNTS, child_ids
            )
        )
        # 冻结契约只允许前三个 child 贡献且进入 round-robin。
        del child_ids[len(_PARENT_CHILD_DISCOUNTS):]

    ordered_parents = sorted(
        children_by_parent,
        key=lambda parent_id: (-parent_scores[parent_id], parent_id),
    )
    results: List[Dict[str, Any]] = []
    for round_index in range(len(_PARENT_CHILD_DISCOUNTS)):
        for parent_id in ordered_parents:
            child_ids = children_by_parent[parent_id]
            if round_index >= len(child_ids):
                continue
            chunk_id = child_ids[round_index]
            item = copy.deepcopy(metas[chunk_id])
            item.update({
                "score": child_scores[chunk_id],
                "child_rrf_score": child_scores[chunk_id],
                "parent_chunk_id": parent_id,
                "parent_score": parent_scores[parent_id],
            })
            results.append(item)
            if len(results) >= top_k:
                return results
    return results


class RetrievalPipeline:
    """共享检索入口；生产降级、评测通过 diagnostics 决定是否接受。"""

    def __init__(
        self,
        db: Session,
        *,
        vector_store=None,
        parent_map: Optional[Dict[str, str]] = None,
    ):
        self.db = db
        self.vector_store = vector_store
        self.parent_map = dict(parent_map or {})

    def _store(self):
        if self.vector_store is None:
            from app.services.retrieval import get_vector_store

            self.vector_store = get_vector_store()
        return self.vector_store

    @staticmethod
    def _write_diagnostics(
        target: Optional[Dict[str, Any]],
        requested: str,
        effective: str,
        *,
        degraded: bool,
        reason: Optional[str],
    ) -> None:
        if target is not None:
            target.clear()
            target.update({
                "requested_profile": requested,
                "effective_profile": effective,
                "degraded": degraded,
                "reason": reason,
            })

    def search(
        self,
        query: str,
        *,
        top_k: int = 5,
        filters: Optional[Dict[str, Any]] = None,
        profile: str = "semantic",
        lexical_profile: str = "bm25-bilingual",
        rerank: Optional[bool] = None,
        diagnostics: Optional[Dict[str, Any]] = None,
        rerank_diagnostics: Optional[Dict[str, Any]] = None,
        rrf_lexical_weight: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        filters = dict(filters or {})
        requested_profile = profile
        if profile == "hybrid-bilingual":
            # RED 测试与早期实验名的兼容别名；正式配置使用 profile=hybrid。
            profile = "hybrid"
            lexical_profile = "bm25-bilingual"
        if profile not in {
            "semantic", "hybrid", "hybrid-local-neighbor",
            "parent-child-v1", "weighted-rrf-v1",
            "weighted-rrf-compat-v1", "hybrid-anchor-v1",
            "paper-first-evidence-rerank-v1",
            "paper-preserving-deep-route-v1",
            "within-paper-query-rerank-v1",
            "within-paper-semantic-rerank-v1", "keyword"
        }:
            raise ValueError(f"不支持的检索 profile: {requested_profile}")
        weighted_profiles = {"weighted-rrf-v1", "weighted-rrf-compat-v1"}
        if profile not in weighted_profiles and rrf_lexical_weight is not None:
            raise ValueError("rrf_lexical_weight 仅适用于 Weighted-RRF profile")

        keyword_results: List[Dict[str, Any]] = []
        keyword_error = False
        if profile in {
            "hybrid", "hybrid-local-neighbor", "parent-child-v1",
            "weighted-rrf-v1", "weighted-rrf-compat-v1",
            "hybrid-anchor-v1", "paper-first-evidence-rerank-v1",
            "paper-preserving-deep-route-v1",
            "within-paper-query-rerank-v1",
            "within-paper-semantic-rerank-v1", "keyword"
        }:
            try:
                keyword_results = keyword_chunk_search(
                    self.db,
                    query,
                    _PARENT_CHILD_POOL
                    if profile == "parent-child-v1"
                    else _PAPER_FIRST_POOL
                    if profile == "paper-first-evidence-rerank-v1"
                    else _DEEP_ROUTE_CANDIDATE_POOL
                    if profile == "paper-preserving-deep-route-v1"
                    else top_k * 2,
                    lexical_profile=lexical_profile,
                    filters=filters,
                )
            except Exception as exc:
                keyword_error = True
                logger.warning(
                    f"[retrieval_pipeline] 词法检索失败: {type(exc).__name__}"
                )

        anchor_results: List[Dict[str, Any]] = []
        anchor_error = False
        anchors = (
            extract_factoid_anchors(query)
            if profile == "hybrid-anchor-v1" else []
        )
        if anchors:
            try:
                anchor_results = anchor_chunk_search(
                    self.db,
                    query,
                    limit=top_k * 2,
                    filters=filters,
                )
            except Exception as exc:
                anchor_error = True
                logger.warning(
                    "[retrieval_pipeline] 锚点检索失败: "
                    f"{type(exc).__name__}"
                )

        if profile == "keyword":
            effective = "empty" if keyword_error else "keyword-only"
            self._write_diagnostics(
                diagnostics,
                requested_profile,
                effective,
                degraded=keyword_error,
                reason="keyword_search_failed" if keyword_error else None,
            )
            return copy.deepcopy(keyword_results[:top_k])

        semantic_results: List[Dict[str, Any]] = []
        semantic_reason: Optional[str] = None
        shared_query_embedding: Optional[List[float]] = None
        try:
            store = self._store()
            if not store.available():
                semantic_reason = "semantic_unavailable"
            elif profile == "within-paper-semantic-rerank-v1":
                shared_query_embedding = store.embedding_service.embed_query(query)
                semantic_results = store.search_by_embedding(
                    shared_query_embedding,
                    top_k=20,
                    filters=filters,
                )[:10]
            else:
                semantic_top_k = (
                    _PARENT_CHILD_POOL
                    if profile == "parent-child-v1"
                    else
                    _PAPER_FIRST_POOL
                    if profile == "paper-first-evidence-rerank-v1"
                    else
                    _DEEP_ROUTE_CANDIDATE_POOL
                    if profile == "paper-preserving-deep-route-v1"
                    else
                    _LOCAL_NEIGHBOR_SEED_POOL
                    if profile == "hybrid-local-neighbor"
                    else top_k if profile == "semantic" else top_k * 2
                )
                search_kwargs = {
                    "query": query,
                    "top_k": semantic_top_k,
                    "filters": filters,
                }
                # 生产旧调用方不显式控制 rerank 时保持三参数兼容；评测或
                # 实验显式传值时才附加诊断参数。
                if rerank is not None or rerank_diagnostics is not None:
                    search_kwargs.update({
                        "rerank": rerank,
                        "rerank_diagnostics": (
                            rerank_diagnostics
                            if rerank_diagnostics is not None
                            else {
                                "requested": bool(rerank),
                                "effective": False,
                                "error": None,
                            }
                        ),
                    })
                semantic_results = store.search(**search_kwargs)
        except Exception as exc:
            semantic_reason = "semantic_search_failed"
            logger.warning(
                f"[retrieval_pipeline] 语义检索失败: {type(exc).__name__}"
            )

        if profile == "semantic":
            self._write_diagnostics(
                diagnostics,
                requested_profile,
                "semantic" if semantic_reason is None else "empty",
                degraded=semantic_reason is not None,
                reason=semantic_reason,
            )
            return copy.deepcopy(semantic_results[:top_k])

        if profile == "within-paper-query-rerank-v1":
            if semantic_reason is not None:
                self._write_diagnostics(
                    diagnostics, requested_profile, "empty",
                    degraded=True, reason=semantic_reason,
                )
                return []
            if keyword_error:
                self._write_diagnostics(
                    diagnostics, requested_profile, "empty",
                    degraded=True, reason="keyword_search_failed",
                )
                return []
            try:
                baseline = rrf_fuse_chunks(
                    semantic_results, keyword_results, top_k
                )
                paper_ids = list(dict.fromkeys(
                    int(item["paper_id"]) for item in baseline
                ))
                local_results = (
                    within_paper_bm25_search(
                        self.db,
                        query,
                        paper_ids=paper_ids,
                        filters=filters,
                    )
                    if paper_ids else []
                )
                results = within_paper_query_fuse_chunks(
                    semantic_results, keyword_results, local_results, top_k
                )
            except Exception as exc:
                logger.warning(
                    "[retrieval_pipeline] 论文内全块查询候选失败: "
                    f"{type(exc).__name__}"
                )
                self._write_diagnostics(
                    diagnostics, requested_profile, "empty",
                    degraded=True, reason="within_paper_query_failed",
                )
                return []
            self._write_diagnostics(
                diagnostics, requested_profile, profile,
                degraded=False, reason=None,
            )
            return results

        if profile == "within-paper-semantic-rerank-v1":
            if semantic_reason is not None or shared_query_embedding is None:
                self._write_diagnostics(
                    diagnostics, requested_profile, "empty",
                    degraded=True, reason=semantic_reason or "embedding_missing",
                )
                return []
            if keyword_error:
                self._write_diagnostics(
                    diagnostics, requested_profile, "empty",
                    degraded=True, reason="keyword_search_failed",
                )
                return []
            try:
                baseline = rrf_fuse_chunks(
                    semantic_results, keyword_results, top_k
                )
                paper_ids = list(dict.fromkeys(
                    int(item["paper_id"]) for item in baseline
                ))
                local_results = (
                    within_paper_semantic_search(
                        self.db,
                        store,
                        shared_query_embedding,
                        paper_ids=paper_ids,
                        filters=filters,
                    )
                    if paper_ids else []
                )
                results = within_paper_semantic_fuse_chunks(
                    semantic_results, keyword_results, local_results, top_k
                )
            except Exception as exc:
                logger.warning(
                    "[retrieval_pipeline] 论文内语义选块候选失败: "
                    f"{type(exc).__name__}"
                )
                self._write_diagnostics(
                    diagnostics, requested_profile, "empty",
                    degraded=True, reason="within_paper_semantic_failed",
                )
                return []
            self._write_diagnostics(
                diagnostics, requested_profile, profile,
                degraded=False, reason=None,
            )
            return results

        if profile == "paper-first-evidence-rerank-v1":
            if semantic_reason is not None:
                self._write_diagnostics(
                    diagnostics, requested_profile, "empty",
                    degraded=True, reason=semantic_reason,
                )
                return []
            if keyword_error:
                self._write_diagnostics(
                    diagnostics, requested_profile, "empty",
                    degraded=True, reason="keyword_search_failed",
                )
                return []
            try:
                results = paper_first_evidence_fuse_chunks(
                    semantic_results, keyword_results, top_k
                )
            except PaperFirstRerankError as exc:
                logger.warning(
                    "[retrieval_pipeline] 论文优先证据重排失败: "
                    f"{type(exc).__name__}"
                )
                self._write_diagnostics(
                    diagnostics, requested_profile, "empty",
                    degraded=True, reason="paper_first_contract_invalid",
                )
                return []
            self._write_diagnostics(
                diagnostics, requested_profile, profile,
                degraded=False, reason=None,
            )
            return results

        if profile == "paper-preserving-deep-route-v1":
            if semantic_reason is not None:
                self._write_diagnostics(
                    diagnostics, requested_profile, "empty",
                    degraded=True, reason=semantic_reason,
                )
                return []
            if keyword_error:
                self._write_diagnostics(
                    diagnostics, requested_profile, "empty",
                    degraded=True, reason="keyword_search_failed",
                )
                return []
            try:
                results = paper_preserving_deep_route_fuse_chunks(
                    semantic_results, keyword_results, top_k
                )
            except DeepRouteContractError as exc:
                logger.warning(
                    "[retrieval_pipeline] 保论文深层证据候选失败: "
                    f"{type(exc).__name__}"
                )
                self._write_diagnostics(
                    diagnostics, requested_profile, "empty",
                    degraded=True, reason="deep_route_contract_invalid",
                )
                return []
            self._write_diagnostics(
                diagnostics, requested_profile, profile,
                degraded=False, reason=None,
            )
            return results

        if profile in weighted_profiles:
            if semantic_reason is not None:
                self._write_diagnostics(
                    diagnostics, requested_profile, "empty",
                    degraded=True, reason=semantic_reason,
                )
                return []
            if keyword_error:
                self._write_diagnostics(
                    diagnostics, requested_profile, "empty",
                    degraded=True, reason="keyword_search_failed",
                )
                return []
            try:
                fuse = (
                    weighted_rrf_compat_fuse_chunks
                    if profile == "weighted-rrf-compat-v1"
                    else weighted_rrf_fuse_chunks
                )
                results = fuse(
                    semantic_results,
                    keyword_results,
                    top_k,
                    semantic_weight=1.0,
                    keyword_weight=rrf_lexical_weight,
                )
            except WeightedRRFError as exc:
                logger.warning(
                    "[retrieval_pipeline] Weighted-RRF 融合失败: "
                    f"{type(exc).__name__}"
                )
                self._write_diagnostics(
                    diagnostics, requested_profile, "empty",
                    degraded=True, reason="weighted_rrf_contract_invalid",
                )
                return []
            self._write_diagnostics(
                diagnostics, requested_profile, profile,
                degraded=False, reason=None,
            )
            return results

        if profile == "parent-child-v1":
            if semantic_reason is not None:
                self._write_diagnostics(
                    diagnostics,
                    requested_profile,
                    "empty",
                    degraded=True,
                    reason=semantic_reason,
                )
                return []
            if keyword_error:
                self._write_diagnostics(
                    diagnostics,
                    requested_profile,
                    "empty",
                    degraded=True,
                    reason="keyword_search_failed",
                )
                return []
            try:
                results = parent_child_fuse_chunks(
                    semantic_results,
                    keyword_results,
                    self.parent_map,
                    top_k,
                )
            except ParentMappingError as exc:
                logger.warning(
                    "[retrieval_pipeline] Parent-Child 映射失败: "
                    f"{type(exc).__name__}"
                )
                self._write_diagnostics(
                    diagnostics,
                    requested_profile,
                    "empty",
                    degraded=True,
                    reason=exc.reason,
                )
                return []
            except Exception as exc:
                logger.warning(
                    "[retrieval_pipeline] Parent-Child 聚合失败: "
                    f"{type(exc).__name__}"
                )
                self._write_diagnostics(
                    diagnostics,
                    requested_profile,
                    "empty",
                    degraded=True,
                    reason="parent_aggregation_failed",
                )
                return []
            self._write_diagnostics(
                diagnostics,
                requested_profile,
                "parent-child-v1",
                degraded=False,
                reason=None,
            )
            return results

        if profile == "hybrid-anchor-v1":
            if semantic_reason is not None:
                effective = "keyword-only" if not keyword_error else "empty"
                self._write_diagnostics(
                    diagnostics,
                    requested_profile,
                    effective,
                    degraded=True,
                    reason=(
                        semantic_reason
                        if not keyword_error else "both_routes_failed"
                    ),
                )
                return copy.deepcopy(keyword_results[:top_k])
            if keyword_error:
                self._write_diagnostics(
                    diagnostics,
                    requested_profile,
                    "semantic-only",
                    degraded=True,
                    reason="keyword_search_failed",
                )
                return copy.deepcopy(semantic_results[:top_k])
            baseline = rrf_fuse_chunks(
                semantic_results, keyword_results, top_k
            )
            if anchor_error:
                self._write_diagnostics(
                    diagnostics,
                    requested_profile,
                    "hybrid",
                    degraded=True,
                    reason="anchor_search_failed",
                )
                return baseline
            self._write_diagnostics(
                diagnostics,
                requested_profile,
                "hybrid-anchor-v1",
                degraded=False,
                reason=None,
            )
            return anchor_rrf_fuse_chunks(
                semantic_results, keyword_results, anchor_results, top_k
            )

        baseline_semantic_results = semantic_results[:top_k * 2]
        neighbor_error = False
        if profile == "hybrid-local-neighbor" and semantic_reason is None:
            try:
                semantic_results = expand_semantic_chunk_neighbors(
                    self.db,
                    semantic_results,
                    filters=filters,
                    radius=_LOCAL_NEIGHBOR_RADIUS,
                    decay=_LOCAL_NEIGHBOR_DECAY,
                    limit=_LOCAL_NEIGHBOR_EXPANDED_CAP,
                )
            except Exception as exc:
                neighbor_error = True
                semantic_results = baseline_semantic_results
                logger.warning(
                    "[retrieval_pipeline] 语义邻域扩展失败: "
                    f"{type(exc).__name__}"
                )

        if semantic_reason is not None:
            effective = "keyword-only" if not keyword_error else "empty"
            self._write_diagnostics(
                diagnostics,
                requested_profile,
                effective,
                degraded=True,
                reason=semantic_reason if not keyword_error else "both_routes_failed",
            )
            return copy.deepcopy(keyword_results[:top_k])
        if neighbor_error:
            effective = "hybrid" if not keyword_error else "semantic-only"
            self._write_diagnostics(
                diagnostics,
                requested_profile,
                effective,
                degraded=True,
                reason="semantic_neighbor_expansion_failed",
            )
            if keyword_error:
                return copy.deepcopy(semantic_results[:top_k])
            return rrf_fuse_chunks(semantic_results, keyword_results, top_k)
        if keyword_error:
            self._write_diagnostics(
                diagnostics,
                requested_profile,
                "semantic-only",
                degraded=True,
                reason="keyword_search_failed",
            )
            return copy.deepcopy(semantic_results[:top_k])

        effective = (
            requested_profile
            if requested_profile in {"hybrid-bilingual", "hybrid-local-neighbor"}
            else "hybrid"
        )
        self._write_diagnostics(
            diagnostics,
            requested_profile,
            effective,
            degraded=False,
            reason=None,
        )
        return rrf_fuse_chunks(semantic_results, keyword_results, top_k)
