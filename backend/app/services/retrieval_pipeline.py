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
from collections import Counter
from typing import Any, Dict, List, Optional

from sqlalchemy import tuple_
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

_CHUNK_ID_FULL_RE = re.compile(r"^p(-?\d+)_c(-?\d+)$")

_LOCAL_NEIGHBOR_SEED_POOL = 20
_LOCAL_NEIGHBOR_RADIUS = 2
_LOCAL_NEIGHBOR_DECAY = 0.5
_LOCAL_NEIGHBOR_EXPANDED_CAP = 20


def tokenize_technical_terms(text: str) -> List[str]:
    """提取 ASCII 技术锚点，保留连字符、小数、科学计数法和百分号。"""
    return [token.lower() for token in _TECHNICAL_TOKEN_RE.findall(text or "")]


def query_technical_terms(text: str, *, bilingual: bool = False) -> List[str]:
    """提取查询锚点，可选扩展显式中英领域术语。"""
    tokens = tokenize_technical_terms(text)
    if bilingual:
        for chinese, english_terms in _BILINGUAL_TERM_MAP:
            if chinese in (text or ""):
                tokens.extend(english_terms)
    return list(dict.fromkeys(tokens))


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
    filters: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """以技术锚点对 chunk 内容执行轻量 BM25。"""
    query_tokens = query_technical_terms(query, bilingual=bilingual)
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
            "source": "keyword-bm25-bilingual" if bilingual else "keyword-bm25",
        })
    scored.sort(key=lambda item: (-item["score"], item["chunk_id"]))
    return scored if limit is None else scored[:limit]


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
    if lexical_profile in {"bm25", "bm25-bilingual"}:
        return bm25_chunk_search(
            db,
            query,
            limit,
            bilingual=lexical_profile == "bm25-bilingual",
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


class RetrievalPipeline:
    """共享检索入口；生产降级、评测通过 diagnostics 决定是否接受。"""

    def __init__(self, db: Session, *, vector_store=None):
        self.db = db
        self.vector_store = vector_store

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
    ) -> List[Dict[str, Any]]:
        filters = dict(filters or {})
        requested_profile = profile
        if profile == "hybrid-bilingual":
            # RED 测试与早期实验名的兼容别名；正式配置使用 profile=hybrid。
            profile = "hybrid"
            lexical_profile = "bm25-bilingual"
        if profile not in {
            "semantic", "hybrid", "hybrid-local-neighbor", "keyword"
        }:
            raise ValueError(f"不支持的检索 profile: {requested_profile}")

        keyword_results: List[Dict[str, Any]] = []
        keyword_error = False
        if profile in {"hybrid", "hybrid-local-neighbor", "keyword"}:
            try:
                keyword_results = keyword_chunk_search(
                    self.db,
                    query,
                    top_k * 2,
                    lexical_profile=lexical_profile,
                    filters=filters,
                )
            except Exception as exc:
                keyword_error = True
                logger.warning(
                    f"[retrieval_pipeline] 词法检索失败: {type(exc).__name__}"
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
        try:
            store = self._store()
            if not store.available():
                semantic_reason = "semantic_unavailable"
            else:
                semantic_top_k = (
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
