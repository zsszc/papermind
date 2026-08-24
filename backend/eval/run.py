"""RAG 一键评测脚本入口。

用法（在 backend/ 目录下）：
    env -u PYTHONPATH venv/bin/python -m eval.run                 # 仅检索指标（默认）
    env -u PYTHONPATH venv/bin/python -m eval.run --keyword-only  # 强制只用关键词检索（不加载模型，快）
    env -u PYTHONPATH venv/bin/python -m eval.run --keyword-only --lexical-profile bm25
                                                               # BM25 技术锚点观察实验
    env -u PYTHONPATH venv/bin/python -m eval.run --fixture eval/fixtures/rag_public_v1.json \
        --dataset eval/dataset/qa_public_v1.jsonl --keyword-only --lexical-profile bm25
                                                               # 公开可复现基准
    env -u PYTHONPATH venv/bin/python -m eval.run --with-llm      # 加跑生成侧指标（会真实调用 LLM）
    env -u PYTHONPATH venv/bin/python -m eval.run --threshold 0.6 # 自定义 recall@5 达标阈值

行为说明：
- 加载种子集（--dataset 可换），逐条 resolve_relevant_chunks 解析期望 chunk；
- 检索直接调用 app 内部函数（VectorStore.search / 自建 chunk 级关键词检索 + RRF 融合），
  不走 HTTP；语义检索模型未就绪（加载失败或 --keyword-only）时优雅降级为
  仅关键词检索，并在报告与控制台中标注 degraded；
- 统计 recall@k / MRR / NDCG@k 的均值并按 question_type 分组；
  负例（has_answer=false，无期望 chunk）不计入检索指标均值，单独计数；
- 每次检索用 time.perf_counter() 计时（毫秒），逐条记入 item 的 latency_ms，
  汇总经 metrics.latency_stats 得到 {p50, p95, mean, count} 写入报告顶层 latency 字段；
- --with-llm 时走 llm_service 生成答案，计算 citation_coverage / keyword_hit_rate，
  其中 citation_coverage 均值同时写入报告 overall（Phase C / C3 增量字段），
  并对负例检查是否出现「不知道/无法回答」类拒答表述；
- 控制台打印汇总表格，JSON 报告写入 eval/reports/<timestamp>.json（目录自动创建）；
- 报告 v2 记录 dataset/corpus 指纹、Git/Python、逐题降级与 gate，只有 comparison_key
  一致的报告才可直接比较；
- 退出码：正例 recall@k 均值低于 --threshold（默认 0.5）时返回 1，否则 0（供 CI 使用）。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import platform
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from eval.dataset import (
    _default_page_loader,
    load_dataset,
    resolve_relevant_chunks,
    resolve_relevant_spans_v2,
    validate_dataset,
)
from eval.metrics import (
    citation_f1,
    citation_precision,
    citation_recall,
    contains_refusal,
    evidence_any_hit_at_k,
    evidence_span_coverage_at_k,
    keyword_hit_rate,
    latency_stats,
    mrr,
    ndcg_at_k,
    recall_at_k,
)

# 只解析显式方括号引用；chunk_index=-1 代表摘要 chunk。
_CHUNK_ID_RE = re.compile(r"\[(p\d+_c-?\d+)\]")

# 评测报告默认输出目录（backend/eval/reports/）
DEFAULT_REPORT_DIR = Path(__file__).resolve().parent / "reports"
PRIVATE_EVAL_ROOT = Path(__file__).resolve().parent / "private"

REPORT_SCHEMA_VERSION = "2.0"


def _sha256_bytes(data: bytes) -> str:
    """返回 bytes 的 SHA256 十六进制摘要。"""
    return hashlib.sha256(data).hexdigest()


def _git_sha() -> Optional[str]:
    """读取当前 Git 提交；不可用时返回 None，不中断离线评测。"""
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[2],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout.strip() or None


def _manifest_chunk_paper_uid(chunk: Any, uid_by_id: Dict[int, str]) -> str:
    """返回 manifest 中 chunk 的稳定论文身份。

    正常数据库必须通过外键找到 paper UID；部分隔离评测测试使用只有
    chunk 的最小夹具，此时以正文内容哈希建立稳定虚拟身份，不泄漏动态 ID。
    """
    uid = uid_by_id.get(chunk.paper_id)
    if uid is not None:
        return uid
    content_hash = _sha256_bytes((chunk.content or "").encode("utf-8"))
    return f"fixture-orphan:{content_hash}"


def _build_benchmark_metadata(
    db,
    dataset_path: Path,
    runtime_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """构建不含正文的 benchmark 指纹与规模元数据。

    corpus manifest 以 chunk 的稳定定位字段与正文 SHA 组成；报告只保存最终
    manifest SHA，不保存私有论文正文或逐条正文摘要。
    """
    from app.models import Chunk, Paper
    from eval.private_benchmark import paper_uid

    if runtime_root is None:
        from app.core.config import config

        runtime_root = config.runtime_root
    runtime_root = Path(runtime_root)

    dataset_path = Path(dataset_path)
    chunks = db.query(Chunk).order_by(Chunk.paper_id, Chunk.chunk_index).all()
    papers = {paper.id: paper for paper in db.query(Paper).all()}
    uid_by_id: Dict[int, str] = {}
    for paper_id, paper in papers.items():
        try:
            uid_by_id[paper_id] = paper_uid(paper, runtime_root)
        except ValueError:
            # 公开内存 fixture 不携带真实 PDF；其 DOI 仍提供稳定身份。
            uid_by_id[paper_id] = f"fixture-paper:{paper_id}"
    manifest = sorted([
        {
            "paper_uid": _manifest_chunk_paper_uid(row, uid_by_id),
            "chunk_index": row.chunk_index,
            "page_number": row.page_number,
            "page_start": row.page_start,
            "page_end": row.page_end,
            "content_sha256": _sha256_bytes((row.content or "").encode("utf-8")),
        }
        for row in chunks
    ], key=lambda item: (item["paper_uid"], item["chunk_index"]))
    manifest_bytes = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return {
        "dataset_sha256": _sha256_bytes(dataset_path.read_bytes()),
        "corpus_manifest_sha256": _sha256_bytes(manifest_bytes),
        "n_papers": len(papers),
        "n_chunks": len(chunks),
    }


def _qrels_sha256(items: List[Dict[str, Any]]) -> str:
    """计算相关性标注的稳定指纹，排除问题文本和参考答案。"""
    qrels = [
        {
            "qa_id": item["qa_id"],
            "has_answer": item["has_answer"],
            "relevant_evidence": item.get("relevant_evidence"),
            "relevant_chunks": item.get("relevant_chunks"),
        }
        for item in items
    ]
    payload = json.dumps(
        qrels, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return _sha256_bytes(payload)


def _select_split(
    items: List[Dict[str, Any]], split: str
) -> List[Dict[str, Any]]:
    """按私有基准分区筛选条目。

    公开旧数据集没有 split 字段，因此只有显式指定分区时才过滤；
    若该分区为空则立即失败，避免生成看似有效的空报告。
    """
    if split == "all":
        return items
    selected = [item for item in items if item.get("split") == split]
    if not selected:
        raise ValueError(f"split={split} 没有可评测条目")
    return selected


def _resolve_qrels_or_raise(
    db,
    items: List[Dict[str, Any]],
    runtime_root: Optional[Path] = None,
) -> Dict[str, List[str]]:
    """预解析 qrels；正例无法解析时立即失败，避免混入模型质量分数。"""
    resolved: Dict[str, List[str]] = {}
    unresolved: List[str] = []
    for entry in items:
        ids = resolve_relevant_chunks(db, entry, runtime_root=runtime_root)
        resolved[entry["qa_id"]] = ids
        if entry["has_answer"] and not ids:
            unresolved.append(entry["qa_id"])
    if unresolved:
        raise ValueError(
            "正例 qrels 无法解析，评测已中止: " + ", ".join(unresolved)
        )
    return resolved


class _CachedPageLoader:
    """一次评测内缓存 PDF 页解析，并生成不含原文的稳定页文本指纹。"""

    def __init__(self, runtime_root: Path):
        self.runtime_root = Path(runtime_root)
        self._load = _default_page_loader(self.runtime_root)
        self._cache: Dict[int, list[dict]] = {}
        self._paper_uids: Dict[int, str] = {}

    def __call__(self, paper) -> list[dict]:
        from eval.private_benchmark import paper_uid

        if paper.id not in self._cache:
            self._cache[paper.id] = self._load(paper)
            self._paper_uids[paper.id] = paper_uid(paper, self.runtime_root)
        return self._cache[paper.id]

    def manifest_sha256(self) -> str:
        manifest = [
            {
                "paper_uid": self._paper_uids[paper_id],
                "pages": [
                    {
                        "page_number": page.get("page_number"),
                        "text_sha256": _sha256_bytes(
                            (page.get("text") or "").encode("utf-8")
                        ),
                    }
                    for page in pages
                ],
            }
            for paper_id, pages in self._cache.items()
        ]
        manifest.sort(key=lambda item: item["paper_uid"])
        payload = json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return _sha256_bytes(payload)


def _resolve_span_qrels_or_raise(
    db,
    items: List[Dict[str, Any]],
    runtime_root: Path,
    page_loader=None,
) -> tuple[Dict[str, List[str]], Dict[str, list[dict]], str]:
    """预解析 page-span-v2 qrels，并返回页文本指纹。"""
    loader = page_loader or _CachedPageLoader(runtime_root)
    resolved: Dict[str, List[str]] = {}
    spans: Dict[str, list[dict]] = {}
    unresolved: List[str] = []
    for entry in items:
        groups = resolve_relevant_spans_v2(
            db,
            entry,
            runtime_root=runtime_root,
            page_loader=loader,
        )
        ids = list(dict.fromkeys(
            chunk["chunk_id"]
            for group in groups
            for chunk in group["chunks"]
        ))
        resolved[entry["qa_id"]] = ids
        spans[entry["qa_id"]] = groups
        if entry["has_answer"] and not ids:
            unresolved.append(entry["qa_id"])
    if unresolved:
        raise ValueError(
            "正例 span qrels 无法解析，评测已中止: " + ", ".join(unresolved)
        )
    manifest = (
        loader.manifest_sha256()
        if hasattr(loader, "manifest_sha256")
        else "injected-page-loader"
    )
    return resolved, spans, manifest


# ---------------------------------------------------------------------------
# 检索
# ---------------------------------------------------------------------------

_TECHNICAL_TOKEN_RE = re.compile(
    r"[A-Za-z0-9]+(?:[.\-][A-Za-z0-9]+)*%?"
)

# 真实集的问题为中文、论文正文主要为英文。该表只做可审计的
# 领域术语扩展，不对整句做猜测式机器翻译。
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


def _tokenize_technical_terms(text: str) -> List[str]:
    """提取中英混合问题中的 ASCII 技术锚点。

    保留连字符、小数、科学计数法和百分号，例如 ReCo-MIL、F1-score、
    1e-4、87.3%。纯中文不做猜测式翻译，返回空列表。
    """
    return [token.lower() for token in _TECHNICAL_TOKEN_RE.findall(text or "")]


def _query_technical_terms(text: str, *, bilingual: bool = False) -> List[str]:
    """提取查询锨点，可选扩展显式中英领域术语。"""
    tokens = _tokenize_technical_terms(text)
    if bilingual:
        for chinese, english_terms in _BILINGUAL_TERM_MAP:
            if chinese in (text or ""):
                tokens.extend(english_terms)
    return list(dict.fromkeys(tokens))


def _bm25_chunk_search(
    db, query: str, limit: Optional[int] = 20, *, bilingual: bool = False
) -> List[Dict[str, Any]]:
    """基于技术锚点的轻量 BM25 chunk 检索（Batch 12 观察 profile）。"""
    from app.models import Chunk

    query_tokens = _query_technical_terms(query, bilingual=bilingual)
    if not query_tokens:
        return []

    rows = db.query(Chunk).all()
    if not rows:
        return []
    tokenized = [_tokenize_technical_terms(row.content or "") for row in rows]
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
    for row, tokens, doc_length in zip(rows, tokenized, lengths):
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
        if score > 0.0:
            scored.append({
                "chunk_id": f"p{row.paper_id}_c{row.chunk_index}",
                "paper_id": row.paper_id,
                "content": row.content or "",
                "score": score,
                "source": "keyword-bm25",
            })
    scored.sort(key=lambda item: (-item["score"], item["chunk_id"]))
    return scored if limit is None else scored[:limit]


_CHUNK_ID_FULL_RE = re.compile(r"^p(-?\d+)_c(-?\d+)$")


def _rerank_bm25_neighbors(
    scored: List[Dict[str, Any]], *, neighbor_weight: float = 0.5
) -> List[Dict[str, Any]]:
    """用同论文相邻块的 BM25 分数增强连续证据。

    分数严格为 ``S + weight * (S(prev) + S(next))``，相邻块由
    同一论文的 ``chunk_index ± 1`` 确定。返回新字典，不改写基线
    profile 的原始结果。
    """
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
        updated = dict(item)
        key = chunk_keys.get(str(item.get("chunk_id", "")))
        score = float(item.get("score", 0.0))
        if key is not None:
            paper_id, chunk_index = key
            previous = base_scores.get((paper_id, chunk_index - 1), 0.0)
            following = base_scores.get((paper_id, chunk_index + 1), 0.0)
            score += neighbor_weight * (previous + following)
        updated["score"] = score
        updated["source"] = "keyword-bm25-bilingual-neighbor"
        reranked.append(updated)

    reranked.sort(key=lambda item: (-item["score"], item["chunk_id"]))
    return reranked

def _keyword_chunk_search(
    db,
    query: str,
    limit: int = 20,
    lexical_profile: str = "count",
) -> List[Dict[str, Any]]:
    """chunk 级关键词检索：按查询词在 chunk content 中的出现次数打分。

    路由层 _keyword_search 基于 papers_fts 只返回论文级结果（无 chunk id），
    无法满足 chunk 级评测需求，因此这里直接对 chunks 表做轻量打分：
    每个查询 token 在 content 中每出现一次 +1 分（大小写不敏感）。

    返回按得分降序的 chunk 列表，元素含 chunk_id / paper_id / content / score。
    """
    if lexical_profile == "bm25-bilingual-neighbor":
        scored = _bm25_chunk_search(db, query, None, bilingual=True)
        return _rerank_bm25_neighbors(scored)[:limit]
    if lexical_profile in {"bm25", "bm25-bilingual"}:
        return _bm25_chunk_search(
            db, query, limit, bilingual=lexical_profile == "bm25-bilingual"
        )

    from app.models import Chunk  # 延迟导入，避免加载本模块即连库

    tokens = [t for t in re.split(r"\s+", query.strip()) if t]
    if not tokens:
        return []

    scored: List[Dict[str, Any]] = []
    for row in db.query(Chunk).all():
        content = row.content or ""
        lowered = content.lower()
        score = sum(lowered.count(t.lower()) for t in tokens)
        if score > 0:
            scored.append({
                "chunk_id": f"p{row.paper_id}_c{row.chunk_index}",
                "paper_id": row.paper_id,
                "content": content,
                "score": float(score),
                "source": "keyword",
            })
    scored.sort(key=lambda r: (-r["score"], r["chunk_id"]))
    return scored[:limit]


def _rrf_fuse_chunks(
    semantic_results: List[Dict[str, Any]],
    keyword_results: List[Dict[str, Any]],
    top_k: int,
    k: int = 60,
) -> List[Dict[str, Any]]:
    """chunk 级 RRF 融合（与路由层论文级 RRF 同公式，键换成 chunk_id）。"""
    scores: Dict[str, float] = {}
    metas: Dict[str, Dict[str, Any]] = {}

    def _add(results: List[Dict[str, Any]]) -> None:
        for rank, item in enumerate(results):
            cid = item.get("chunk_id")
            if cid is None:
                continue
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank + 1)
            metas.setdefault(cid, item)

    _add(semantic_results)
    _add(keyword_results)
    ordered = sorted(scores, key=lambda cid: scores[cid], reverse=True)
    return [metas[cid] for cid in ordered[:top_k]]


# Batch 20：保留上述函数名作为下游兼容 API，但实际实现统一来自生产服务。
# 这样旧测试/脚本无需改 import，聊天与 eval 又不会继续维护两套算法。
from app.services.retrieval_pipeline import (  # noqa: E402
    bm25_chunk_search as _bm25_chunk_search,
    keyword_chunk_search as _keyword_chunk_search,
    query_technical_terms as _query_technical_terms,
    rerank_bm25_neighbors as _rerank_bm25_neighbors,
    rrf_fuse_chunks as _rrf_fuse_chunks,
    tokenize_technical_terms as _tokenize_technical_terms,
)


def _open_eval_vector_store(vector_dir: Path):
    """只打开显式评测快照中已存在的 papers collection。"""
    import chromadb
    from chromadb.config import Settings

    from app.services.embedding import EmbeddingService
    from app.services.retrieval import VectorStore

    vector_dir = Path(vector_dir)
    if not vector_dir.is_dir():
        raise FileNotFoundError(f"评测向量快照不存在: {vector_dir}")
    client = chromadb.PersistentClient(
        path=str(vector_dir), settings=Settings(anonymized_telemetry=False)
    )
    collection = client.get_collection(name="papers")
    store = VectorStore.__new__(VectorStore)
    store.vector_dir = vector_dir
    store.client = client
    store.collection = collection
    store.embedding_service = EmbeddingService()
    return store


class Retriever:
    """评测用检索器：优先语义+关键词混合，模型不可用时降级为仅关键词。"""

    def __init__(
        self,
        db,
        top_k: int,
        keyword_only: bool = False,
        lexical_profile: str = "count",
        vector_dir: Optional[Path] = None,
        retrieval_profile: str = "hybrid",
        semantic_rerank: Optional[bool] = None,
    ):
        self.db = db
        self.top_k = top_k
        self.degraded = False
        self.degrade_reason = ""
        self._store = None
        self.keyword_only = keyword_only
        self.lexical_profile = lexical_profile
        self.retrieval_profile = retrieval_profile
        self.semantic_rerank = semantic_rerank
        self.rerank_diagnostics: Dict[str, Any] = {
            "requested": semantic_rerank,
            "effective": False,
            "error": None,
        }
        self.last_query_mode = "keyword-only" if keyword_only else "hybrid"
        self.last_query_degraded = bool(keyword_only)
        self.last_query_error: Optional[str] = None
        self.runtime_degraded_count = 0

        if keyword_only:
            self.degraded = True
            self.degrade_reason = "--keyword-only 指定，跳过语义检索"
        else:
            try:
                if vector_dir is not None:
                    store = _open_eval_vector_store(vector_dir)
                else:
                    # 保留类的直接调用兼容；CLI 已强制要求隔离快照。
                    from app.services.retrieval import get_vector_store
                    store = get_vector_store()
                self._store = store
                if store.available():
                    pass
                else:
                    self.degraded = True
                    self.degrade_reason = "Embedding 模型加载失败（详见日志）"
            except Exception as e:  # 模型/向量库任何异常都降级，不中断评测
                self.degraded = True
                self.degrade_reason = f"语义检索初始化异常: {e}"

    @property
    def mode(self) -> str:
        if self.retrieval_profile == "semantic-production" and not self.degraded:
            return "semantic-production"
        if self.retrieval_profile == "semantic-production":
            return "semantic-production(degraded)"
        if (
            self.retrieval_profile == "hybrid-local-neighbor"
            and not self.degraded
        ):
            return "hybrid-local-neighbor"
        return "keyword-only(degraded)" if self.degraded else "hybrid"

    def search(self, query: str) -> List[Dict[str, Any]]:
        """对单条查询返回 top_k 的 chunk 级结果（含 chunk_id 与 content）。"""
        from app.services.retrieval_pipeline import RetrievalPipeline

        self.last_query_mode = "keyword-only" if self.keyword_only else self.mode
        self.last_query_degraded = self.degraded and not self.keyword_only
        self.last_query_error = None
        pipeline_diagnostics: Dict[str, Any] = {}
        if self.keyword_only:
            profile = "keyword"
        elif self.retrieval_profile == "semantic-production":
            profile = "semantic"
        elif self.retrieval_profile == "hybrid-local-neighbor":
            profile = "hybrid-local-neighbor"
        else:
            profile = "hybrid"

        # 初始化异常时绝不让评测管线隐式回连主 VectorStore。
        if profile != "keyword" and self._store is None:
            self.last_query_mode = f"{self.mode}(runtime-degraded)"
            self.last_query_degraded = True
            self.last_query_error = "semantic_initialization_failed"
            self.runtime_degraded_count += 1
            return []

        results = RetrievalPipeline(
            self.db, vector_store=self._store
        ).search(
            query,
            top_k=5 if profile == "semantic" else self.top_k,
            filters={},
            profile=profile,
            lexical_profile=self.lexical_profile,
            rerank=self.semantic_rerank,
            diagnostics=pipeline_diagnostics,
            rerank_diagnostics=self.rerank_diagnostics,
        )

        if self.keyword_only:
            return results

        degraded = bool(pipeline_diagnostics.get("degraded"))
        reason = pipeline_diagnostics.get("reason")
        if self.semantic_rerank is True and not self.rerank_diagnostics["effective"]:
            degraded = True
            reason = self.rerank_diagnostics.get("error") or "rerank_not_effective"
        if degraded:
            self.last_query_degraded = True
            self.last_query_error = reason
            self.runtime_degraded_count += 1
            self.last_query_mode = (
                f"{self.retrieval_profile}(runtime-degraded)"
            )
        else:
            self.last_query_mode = self.mode
        return results


# ---------------------------------------------------------------------------
# 生成（--with-llm）
# ---------------------------------------------------------------------------

_GEN_SYSTEM_PROMPT = (
    "你是文献问答助手。请仅根据给定资料回答问题，回答末尾用 [chunk_id] 形式标注引用的资料块"
    "（例如 [p1_c2]）。若资料中没有相关信息，请明确回答“不知道”，不要编造。"
)


def _generate_answer(question: str, contexts: List[Dict[str, Any]]) -> str:
    """调用 llm_service 基于检索到的 chunk 生成答案（会真实调用 LLM API）。"""
    from app.services.llm import llm_service  # 延迟导入

    context_text = "\n\n".join(
        f"[{c['chunk_id']}] {c.get('content', '')[:800]}" for c in contexts
    ) or "（未检索到任何资料）"
    messages = [
        {"role": "system", "content": _GEN_SYSTEM_PROMPT},
        {"role": "user", "content": f"资料：\n{context_text}\n\n问题：{question}"},
    ]
    return llm_service.chat_completion_sync(messages, max_tokens=512)


def _is_llm_error_answer(answer: str) -> bool:
    """识别 LLMService 的带内错误契约，防止错误被计为有效答案。"""
    return (answer or "").lstrip().startswith("[调用 LLM 出错:")


def _generation_error_kind(answer: str) -> Optional[str]:
    """返回可审计的生成错误类型；有效答案返回 None。"""
    if not (answer or "").strip():
        return "empty_response"
    if _is_llm_error_answer(answer):
        return "llm_error_response"
    return None


def _extract_citations(answer: str) -> List[str]:
    """从答案文本中提取去重后的 chunk 引用 id（保持出现顺序）。"""
    seen: Dict[str, None] = {}
    for cid in _CHUNK_ID_RE.findall(answer or ""):
        seen.setdefault(cid)
    return list(seen)


# ---------------------------------------------------------------------------
# 汇总与输出
# ---------------------------------------------------------------------------

def _mean(values: List[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _build_quality_gate(
    *,
    overall: Dict[str, Any],
    type_rows: List[Dict[str, Any]],
    latency: Dict[str, Any],
    top_k: int,
    recall_threshold: float,
    min_mrr: Optional[float] = None,
    min_ndcg: Optional[float] = None,
    min_factoid_recall: Optional[float] = None,
    max_p95_ms: Optional[float] = None,
    runtime_valid: bool,
    primary_metric: Optional[str] = None,
) -> Dict[str, Any]:
    """构造可审计多指标 Gate；未显式配置的可选指标不进入 checks。"""
    recall_key = primary_metric or f"recall@{top_k}"
    ndcg_key = f"ndcg@{top_k}"
    checks: Dict[str, Dict[str, Any]] = {
        recall_key: {
            "actual": overall[recall_key],
            "threshold": recall_threshold,
            "operator": ">=",
            "passed": overall[recall_key] >= recall_threshold,
        }
    }

    def _minimum(name: str, actual: Optional[float], threshold: Optional[float]):
        if threshold is None:
            return
        checks[name] = {
            "actual": actual,
            "threshold": threshold,
            "operator": ">=",
            "passed": actual is not None and actual >= threshold,
        }

    _minimum("mrr", overall.get("mrr"), min_mrr)
    _minimum(ndcg_key, overall.get(ndcg_key), min_ndcg)
    factoid_row = next(
        (row for row in type_rows if row.get("question_type") == "factoid"),
        None,
    )
    factoid_field = (
        "span_coverage" if primary_metric and primary_metric.startswith(
            "span_coverage@"
        ) else "recall"
    )
    _minimum(
        "factoid_recall",
        factoid_row.get(factoid_field) if factoid_row else None,
        min_factoid_recall,
    )
    if max_p95_ms is not None:
        actual_p95 = latency.get("p95")
        checks["p95_ms"] = {
            "actual": actual_p95,
            "threshold": max_p95_ms,
            "operator": "<",
            "passed": actual_p95 is not None and actual_p95 < max_p95_ms,
        }

    passed = runtime_valid and all(item["passed"] for item in checks.values())
    # metric/threshold/actual 保留 v2 旧消费者兼容；checks 是 Batch 20 增量。
    return {
        "passed": passed,
        "metric": recall_key,
        "threshold": recall_threshold,
        "actual": overall[recall_key],
        "runtime_valid": runtime_valid,
        "checks": checks,
    }


def _print_table(rows: List[Dict[str, Any]], k: int) -> None:
    """打印控制台汇总表格（纯文本，不依赖第三方库）。"""
    header = ("question_type", "n", f"recall@{k}", "MRR", f"NDCG@{k}")
    widths = [max(len(h), *(len(f"{r[c]}") for r in rows)) for c, h in zip(
        ("question_type", "n", "recall", "mrr", "ndcg"), header)]
    line = "  ".join(h.ljust(w) for h, w in zip(header, widths))
    print(line)
    print("-" * len(line))
    for r in rows:
        cells = (r["question_type"], str(r["n"]), f"{r['recall']:.3f}",
                 f"{r['mrr']:.3f}", f"{r['ndcg']:.3f}")
        print("  ".join(c.ljust(w) for c, w in zip(cells, widths)))


def _prepare_eval_items(
    args: argparse.Namespace,
) -> tuple[List[Dict[str, Any]], Optional[str]]:
    """加载、校验并按 split/QA 白名单筛选，不触发任何外部调用。"""
    items = load_dataset(args.dataset)
    validate_dataset(items)
    items = _select_split(items, args.split)
    if args.qa_id:
        requested = list(dict.fromkeys(args.qa_id))
        by_id = {item["qa_id"]: item for item in items}
        missing = [qa_id for qa_id in requested if qa_id not in by_id]
        if missing:
            return [], "--qa-id 不属于指定 split: " + ", ".join(missing)
        items = [by_id[qa_id] for qa_id in requested]
    if args.with_llm and len(items) > args.max_llm_calls:
        return [], (
            f"选中 {len(items)} 条 QA，超过 --max-llm-calls="
            f"{args.max_llm_calls} 硬预算"
        )
    return items, None


def _llm_health_preflight() -> Dict[str, Any]:
    """在任何生成调用前做一次同步 CLI 探活。"""
    from app.services.llm import llm_service

    return asyncio.run(llm_service.health_check())


def run_eval(args: argparse.Namespace) -> int:
    """执行评测，返回进程退出码。"""
    items, item_error = _prepare_eval_items(args)
    if item_error:
        print(f"[eval] 参数错误: {item_error}", file=sys.stderr)
        return 2
    llm_status: Optional[Dict[str, Any]] = None
    if args.with_llm:
        try:
            llm_status = _llm_health_preflight()
        except Exception:
            print("[eval] LLM 健康预检异常，未发起生成调用", file=sys.stderr)
            return 2
        if not llm_status.get("ok"):
            print("[eval] LLM 健康预检失败，未发起生成调用", file=sys.stderr)
            return 2
    print(
        f"[eval] 数据集 {args.dataset} 共 {len(items)} 条"
        + (f"（split={args.split}）" if args.split != "all" else "")
    )

    fixture_database = None
    explicit_database_engine = None
    fixture_metadata: Dict[str, Any] = {}
    if args.fixture:
        from eval.fixture import open_fixture_database

        fixture_database = open_fixture_database(args.fixture)
        fixture_metadata = fixture_database.metadata
        db = fixture_database.session_factory()
    elif args.database:
        from app.services.data_integrity import (
            open_readonly_sqlalchemy_database,
        )

        explicit_database_engine, session_factory = (
            open_readonly_sqlalchemy_database(Path(args.database))
        )
        db = session_factory()
    else:
        from app.database import SessionLocal  # 延迟导入，连接真实 SQLite（只读）

        db = SessionLocal()
    try:
        t0 = time.time()
        if args.corpus_root:
            runtime_root = Path(args.corpus_root).resolve()
        else:
            from app.core.config import config

            runtime_root = config.runtime_root
        benchmark = _build_benchmark_metadata(
            db, Path(args.dataset), runtime_root=runtime_root
        )
        benchmark.update({
            "qrels_sha256": _qrels_sha256(items),
            "benchmark_id": fixture_metadata.get("benchmark_id", "private-local-observation"),
            "resolver_version": args.evidence_resolver,
        })
        if fixture_metadata:
            benchmark["fixture_license"] = fixture_metadata["license"]
        span_qrels: Dict[str, list[dict]] = {}
        if args.evidence_resolver == "page-span-v2":
            resolved_qrels, span_qrels, page_manifest = (
                _resolve_span_qrels_or_raise(
                    db, items, runtime_root=runtime_root
                )
            )
            benchmark["page_text_manifest_sha256"] = page_manifest
        else:
            resolved_qrels = _resolve_qrels_or_raise(
                db, items, runtime_root=runtime_root
            )
        retriever = Retriever(
            db,
            top_k=args.top_k,
            keyword_only=args.keyword_only,
            lexical_profile=args.lexical_profile,
            vector_dir=Path(args.vector_dir) if args.vector_dir else None,
            retrieval_profile=args.retrieval_profile,
            semantic_rerank=(
                args.semantic_rerank == "on"
                if args.semantic_rerank is not None else None
            ),
        )
        print(f"[eval] 检索模式: {retriever.mode}"
              + (f"（{retriever.degrade_reason}）" if retriever.degraded else ""))

        per_item: List[Dict[str, Any]] = []
        by_type: Dict[str, Dict[str, List[float]]] = defaultdict(
            lambda: {
                "recall": [], "mrr": [], "ndcg": [],
                "any_hit": [], "span_coverage": [],
            })
        gen_metrics = {
            "citation_precision": [],
            "citation_recall": [],
            "citation_f1": [],
            "keyword_hit_rate": [],
        }
        retrieval_latencies: List[float] = []  # 每次检索的延迟（毫秒）
        negative_total = 0
        negative_refused = 0
        generation_attempted = 0
        generation_succeeded = 0
        generation_errors: List[Dict[str, str]] = []

        for idx, entry in enumerate(items, start=1):
            relevant_ids = resolved_qrels[entry["qa_id"]]
            search_t0 = time.perf_counter()
            results = retriever.search(entry["question"])
            latency_ms = round((time.perf_counter() - search_t0) * 1000.0, 3)
            retrieval_latencies.append(latency_ms)
            retrieved_ids = [r["chunk_id"] for r in results]

            record: Dict[str, Any] = {
                "qa_id": entry["qa_id"],
                "question_type": entry["question_type"],
                "has_answer": entry["has_answer"],
                "relevant_ids": relevant_ids,
                "retrieved_ids": retrieved_ids,
                "latency_ms": latency_ms,
                "mode_used": retriever.last_query_mode,
                "degraded": retriever.last_query_degraded,
            }
            if retriever.last_query_error:
                record["retrieval_error"] = retriever.last_query_error

            if entry["has_answer"]:
                # 正例：计入检索指标
                rec = recall_at_k(retrieved_ids, relevant_ids, args.top_k)
                rr = mrr(retrieved_ids, relevant_ids)
                nd = ndcg_at_k(retrieved_ids, relevant_ids, args.top_k)
                record.update({"recall": rec, "mrr": rr, "ndcg": nd})
                grp = by_type[entry["question_type"]]
                grp["recall"].append(rec)
                grp["mrr"].append(rr)
                grp["ndcg"].append(nd)
                if args.evidence_resolver == "page-span-v2":
                    groups = span_qrels[entry["qa_id"]]
                    any_hit = evidence_any_hit_at_k(
                        retrieved_ids, groups, args.top_k
                    )
                    span_coverage = evidence_span_coverage_at_k(
                        retrieved_ids, groups, args.top_k
                    )
                    record.update({
                        "relevant_span_count": len(groups),
                        "relevant_chunk_count": len(relevant_ids),
                        "any_hit": any_hit,
                        "span_coverage": span_coverage,
                    })
                    grp["any_hit"].append(any_hit)
                    grp["span_coverage"].append(span_coverage)
            else:
                negative_total += 1

            if args.with_llm:
                generation_attempted += 1
                try:
                    answer = _generate_answer(entry["question"], results)
                    error_kind = _generation_error_kind(answer)
                    if error_kind:
                        generation_errors.append({
                            "qa_id": entry["qa_id"], "error": error_kind
                        })
                    else:
                        generation_succeeded += 1
                except Exception as e:
                    answer = ""
                    generation_errors.append({
                        "qa_id": entry["qa_id"], "error": type(e).__name__
                    })
                record["answer"] = answer
                if _generation_error_kind(answer):
                    record["generation_error"] = generation_errors[-1]["error"]
                if entry["has_answer"]:
                    cited = _extract_citations(answer)
                    precision = citation_precision(cited, relevant_ids)
                    recall = citation_recall(cited, relevant_ids)
                    f1 = citation_f1(cited, relevant_ids)
                    hit = keyword_hit_rate(answer, entry["ground_truth"])
                    record.update({
                        "citations": cited,
                        "citation_precision": precision,
                        "citation_recall": recall,
                        "citation_f1": f1,
                        # 旧字段保持兼容，定义与 citation_recall 相同。
                        "citation_coverage": recall,
                        "keyword_hit_rate": hit,
                    })
                    gen_metrics["citation_precision"].append(precision)
                    gen_metrics["citation_recall"].append(recall)
                    gen_metrics["citation_f1"].append(f1)
                    gen_metrics["keyword_hit_rate"].append(hit)
                else:
                    refused = contains_refusal(answer)
                    record["refused"] = refused
                    negative_refused += int(refused)

            per_item.append(record)
            print(f"[eval] ({idx}/{len(items)}) {entry['qa_id']} 完成", end="\r")

        print()  # 换掉进度行
        elapsed = time.time() - t0

        # 汇总
        all_recall = [v for g in by_type.values() for v in g["recall"]]
        all_mrr = [v for g in by_type.values() for v in g["mrr"]]
        all_ndcg = [v for g in by_type.values() for v in g["ndcg"]]
        overall = {
            f"recall@{args.top_k}": _mean(all_recall),
            "mrr": _mean(all_mrr),
            f"ndcg@{args.top_k}": _mean(all_ndcg),
            "n_positive": len(all_recall),
            "n_negative": negative_total,
        }
        if args.evidence_resolver == "page-span-v2":
            all_any_hit = [
                value for group in by_type.values()
                for value in group["any_hit"]
            ]
            all_span_coverage = [
                value for group in by_type.values()
                for value in group["span_coverage"]
            ]
            overall[f"any_hit@{args.top_k}"] = _mean(all_any_hit)
            overall[f"span_coverage@{args.top_k}"] = _mean(all_span_coverage)
        type_rows = [
            {"question_type": qtype, "n": len(g["recall"]),
             "recall": _mean(g["recall"]), "mrr": _mean(g["mrr"]),
             "ndcg": _mean(g["ndcg"]),
             **({
                 "any_hit": _mean(g["any_hit"]),
                 "span_coverage": _mean(g["span_coverage"]),
             } if args.evidence_resolver == "page-span-v2" else {})}
            for qtype, g in sorted(by_type.items())
        ]

        comparison_key = ":".join((
            benchmark["dataset_sha256"],
            benchmark["qrels_sha256"],
            benchmark["corpus_manifest_sha256"],
            retriever.mode,
            args.lexical_profile,
            args.evidence_resolver,
            benchmark.get("page_text_manifest_sha256", "none"),
            str(args.top_k),
        ))
        runtime_valid = retriever.runtime_degraded_count == 0
        latency_summary = latency_stats(retrieval_latencies)
        gate = _build_quality_gate(
            overall=overall,
            type_rows=type_rows,
            latency=latency_summary,
            top_k=args.top_k,
            recall_threshold=args.threshold,
            min_mrr=args.min_mrr,
            min_ndcg=args.min_ndcg,
            min_factoid_recall=args.min_factoid_recall,
            max_p95_ms=args.max_p95_ms,
            runtime_valid=runtime_valid,
            primary_metric=(
                f"span_coverage@{args.top_k}"
                if args.evidence_resolver == "page-span-v2" else None
            ),
        )
        passed = gate["passed"]
        effective_profile = (
            "runtime-degraded" if not runtime_valid else retriever.mode
        )
        report: Dict[str, Any] = {
            "report_schema": REPORT_SCHEMA_VERSION,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "run": {
                "git_sha": _git_sha(),
                "python": platform.python_version(),
            },
            "benchmark": {
                **benchmark,
                "comparison_key": comparison_key,
            },
            "pipeline": {
                "profile": args.retrieval_profile,
                "effective_profile": effective_profile,
                "lexical_profile": args.lexical_profile,
                "semantic_rerank": args.semantic_rerank,
                "split": args.split,
                "evidence_resolver": args.evidence_resolver,
                "top_k": args.top_k,
            },
            "diagnostics": {
                "unresolved_qrels": [],
                "runtime_degraded_count": retriever.runtime_degraded_count,
                "rerank": retriever.rerank_diagnostics,
            },
            "gate": gate,
            "dataset": Path(args.dataset).name if args.fixture else str(args.dataset),
            "top_k": args.top_k,
            "threshold": args.threshold,
            "retrieval_mode": retriever.mode,
            "degraded": retriever.degraded,
            "degrade_reason": retriever.degrade_reason,
            "with_llm": args.with_llm,
            "elapsed_seconds": round(elapsed, 2),
            "latency": latency_summary,
            "overall": overall,
            "by_question_type": type_rows,
            "items": per_item,
        }
        if args.with_llm:
            precision_mean = _mean(gen_metrics["citation_precision"])
            recall_mean = _mean(gen_metrics["citation_recall"])
            f1_mean = _mean(gen_metrics["citation_f1"])
            # C3：citation_coverage 均值同时写入 overall（报告增量字段；
            # trend.py 对缺该字段的旧报告按未知字段忽略，天然兼容）
            overall.update({
                "citation_precision": precision_mean,
                "citation_recall": recall_mean,
                "citation_f1": f1_mean,
                "citation_coverage": recall_mean,
            })
            report["generation"] = {
                "valid": not generation_errors,
                "model": (llm_status or {}).get("model"),
                "selected_qa_ids": [item["qa_id"] for item in items],
                "max_llm_calls": args.max_llm_calls,
                "attempted": generation_attempted,
                "succeeded": generation_succeeded,
                "error_count": len(generation_errors),
                "errors": generation_errors,
                "citation_precision": precision_mean,
                "citation_recall": recall_mean,
                "citation_f1": f1_mean,
                "citation_coverage": recall_mean,
                "keyword_hit_rate": _mean(gen_metrics["keyword_hit_rate"]),
                "negative_refusal_rate": (
                    negative_refused / negative_total if negative_total else None),
                "negative_refused": negative_refused,
                "negative_total": negative_total,
            }

        # 控制台输出
        print(f"\n[eval] 检索模式: {report['retrieval_mode']}，耗时 {elapsed:.1f}s")
        lat = report["latency"]
        print(f"[eval] 检索延迟(ms): p50={lat['p50']:.1f} "
              f"p95={lat['p95']:.1f} mean={lat['mean']:.1f} (n={lat['count']})")
        _print_table(type_rows + [{"question_type": "ALL", "n": overall["n_positive"],
                                   "recall": overall[f"recall@{args.top_k}"],
                                   "mrr": overall["mrr"],
                                   "ndcg": overall[f"ndcg@{args.top_k}"]}], args.top_k)
        print(f"[eval] 负例 {negative_total} 条（不计入检索指标）")
        if args.with_llm:
            g = report["generation"]
            print(f"[eval] 生成侧: citation_P/R/F1="
                  f"{g['citation_precision']:.3f}/{g['citation_recall']:.3f}/"
                  f"{g['citation_f1']:.3f} "
                  f"keyword_hit_rate={g['keyword_hit_rate']:.3f} "
                  f"负例拒答率={g['negative_refusal_rate']}")

        # 写 JSON 报告
        report_dir = Path(args.report_dir)
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / f"{time.strftime('%Y%m%d_%H%M%S')}.json"
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[eval] 报告已写入 {report_path}")

        # 退出码判定
        gate_metric = gate["metric"]
        print(f"[eval] {gate_metric}={gate['actual']:.3f} "
              f"阈值={args.threshold} -> {'PASS' if passed else 'FAIL'}")
        if not runtime_valid:
            print("[eval] hybrid 运行期发生语义检索降级，本次质量门禁无效")
        generation_valid = (
            not args.with_llm or report["generation"]["valid"]
        )
        return 0 if passed and generation_valid else 1
    finally:
        db.close()
        if fixture_database is not None:
            fixture_database.close()
        if explicit_database_engine is not None:
            explicit_database_engine.dispose()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m eval.run", description="PaperMind RAG 一键评测")
    parser.add_argument("--dataset", default=None,
                        help="评测数据集 JSONL 路径，缺省为内置种子集")
    parser.add_argument(
        "--fixture",
        default=None,
        help="公开 fixture JSON；启用后使用隔离内存 SQLite，不连接真实数据库",
    )
    parser.add_argument(
        "--database",
        default=None,
        help="显式只读候选 SQLite；用于隔离评测，不连接生产 SessionLocal",
    )
    parser.add_argument(
        "--corpus-root",
        default=None,
        help="解析稳定 PDF 身份的只读语料根目录",
    )
    parser.add_argument(
        "--evidence-resolver",
        choices=("chunk-v1", "page-span-v2"),
        default="chunk-v1",
        help="证据解析契约；历史默认 chunk-v1，跨块评测须显式选 page-span-v2",
    )
    parser.add_argument("--top-k", type=int, default=5,
                        help="检索截断位置 k（默认 5，即 recall@5 / NDCG@5）")
    parser.add_argument(
        "--split",
        choices=("all", "train", "dev", "holdout"),
        default="all",
        help="只评测指定私有基准分区（默认 all）",
    )
    parser.add_argument("--threshold", type=float, default=0.5,
                        help="recall@k 达标阈值，低于则退出码为 1（默认 0.5）")
    parser.add_argument(
        "--min-mrr", type=float, default=None,
        help="可选 MRR 下限；与 recall Gate 同时满足才通过",
    )
    parser.add_argument(
        "--min-ndcg", type=float, default=None,
        help="可选 NDCG@k 下限；与 recall Gate 同时满足才通过",
    )
    parser.add_argument(
        "--min-factoid-recall", type=float, default=None,
        help="可选 factoid Recall 下限；数据集无该类型时 fail-close",
    )
    parser.add_argument(
        "--max-p95-ms", type=float, default=None,
        help="可选检索 P95 严格上限（毫秒）",
    )
    parser.add_argument("--keyword-only", action="store_true",
                        help="强制仅关键词检索（不加载语义模型，速度快）")
    parser.add_argument(
        "--retrieval-profile",
        choices=(
            "hybrid", "hybrid-local-neighbor", "semantic-production"
        ),
        default="hybrid",
        help=("评测检索管线；hybrid-local-neighbor 为 Batch21 候选；"
              "semantic-production 严格仅跑生产语义 top5"),
    )
    parser.add_argument(
        "--vector-dir",
        default=None,
        help="hybrid 评测必须显式指定的隔离 Chroma 快照目录",
    )
    parser.add_argument(
        "--semantic-rerank",
        choices=("off", "on"),
        default=None,
        help="semantic-production 必须显式选择的生产语义重排开关",
    )
    parser.add_argument(
        "--lexical-profile",
        choices=(
            "count", "bm25", "bm25-bilingual", "bm25-bilingual-v2",
            "bm25-bilingual-neighbor"
        ),
        default="count",
        help=("chunk 词法检索策略；bm25-bilingual-v2 为 Batch22 病理术语"
              "候选；bm25-bilingual-neighbor 额外增加同论文相邻块分数；"
              "默认 count 保持历史行为"),
    )
    parser.add_argument("--with-llm", action="store_true",
                        help="加跑生成侧指标（会真实调用 LLM API，默认关闭）")
    parser.add_argument(
        "--qa-id", action="append", default=[],
        help="受控生成评测的 QA 白名单（可重复指定）",
    )
    parser.add_argument(
        "--max-llm-calls", type=int, default=0,
        help="真实生成调用硬上限；--with-llm 时必须大于等于白名单数",
    )
    parser.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR),
                        help="JSON 报告输出目录（默认 eval/reports/）")
    return parser


def _validate_cli_args(args: argparse.Namespace) -> Optional[str]:
    """返回 CLI 安全契约错误；合法时返回 None。"""
    for name in ("threshold", "min_mrr", "min_ndcg", "min_factoid_recall"):
        value = getattr(args, name, None)
        if value is not None and not 0.0 <= value <= 1.0:
            return f"--{name.replace('_', '-')} 必须位于 0 到 1"
    if args.max_p95_ms is not None and args.max_p95_ms <= 0:
        return "--max-p95-ms 必须为正数"
    if args.fixture and not args.keyword_only:
        return "fixture 评测必须显式使用 --keyword-only"
    if args.fixture and (args.database or args.corpus_root):
        return "fixture 评测不得指定 --database/--corpus-root"
    if args.fixture and args.evidence_resolver != "chunk-v1":
        return "fixture 评测只支持 chunk-v1"
    if args.database and not args.corpus_root:
        return "显式 --database 必须同时指定 --corpus-root"
    if args.evidence_resolver == "page-span-v2":
        if not args.database or not args.corpus_root:
            return "page-span-v2 必须显式指定 --database/--corpus-root"
        if args.split == "all":
            return "page-span-v2 必须显式指定 train/dev/holdout 分区"
    if args.fixture and args.with_llm:
        return "fixture 评测不得使用 --with-llm"
    if args.with_llm and args.split != "dev":
        return "--with-llm 必须显式使用 --split dev"
    if args.with_llm and not args.qa_id:
        return "--with-llm 必须至少指定一个 --qa-id"
    if args.with_llm and args.max_llm_calls <= 0:
        return "--with-llm 必须指定正数 --max-llm-calls"
    if args.with_llm:
        report_dir = Path(args.report_dir).resolve()
        private_root = PRIVATE_EVAL_ROOT.resolve()
        dataset_path = Path(args.dataset).resolve() if args.dataset else None
        if dataset_path is None or not dataset_path.is_relative_to(private_root):
            return "--with-llm 的 dataset 必须位于 eval/private 内"
        if not report_dir.is_relative_to(private_root):
            return "--with-llm 的 --report-dir 必须位于 eval/private 内"
    if args.retrieval_profile == "semantic-production":
        if args.keyword_only:
            return "semantic-production 不得使用 --keyword-only"
        if args.top_k != 5:
            return "semantic-production 必须使用 top-k=5"
        if not args.vector_dir:
            return "semantic-production 必须显式指定 --vector-dir"
        if args.semantic_rerank is None:
            return "semantic-production 必须显式指定 --semantic-rerank off/on"
    elif args.semantic_rerank is not None:
        return "--semantic-rerank 仅适用于 semantic-production"
    if not args.keyword_only and not args.vector_dir:
        return "hybrid 评测必须显式指定 --vector-dir 隔离向量快照"
    return None


def _validate_fixture_args(args: argparse.Namespace) -> Optional[str]:
    """保留旧名供下游测试与调用方兼容。"""
    return _validate_cli_args(args)


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    # load_dataset 接受 None 表示默认种子集
    if args.dataset is None:
        from eval.dataset import DEFAULT_SEED_PATH

        args.dataset = DEFAULT_SEED_PATH
    cli_error = _validate_cli_args(args)
    if cli_error:
        print(f"[eval] 参数错误: {cli_error}", file=sys.stderr)
        return 2
    return run_eval(args)


if __name__ == "__main__":
    sys.exit(main())
