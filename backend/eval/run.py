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

from eval.dataset import load_dataset, resolve_relevant_chunks, validate_dataset
from eval.metrics import (
    citation_f1,
    citation_precision,
    citation_recall,
    contains_refusal,
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


def _build_benchmark_metadata(db, dataset_path: Path) -> Dict[str, Any]:
    """构建不含正文的 benchmark 指纹与规模元数据。

    corpus manifest 以 chunk 的稳定定位字段与正文 SHA 组成；报告只保存最终
    manifest SHA，不保存私有论文正文或逐条正文摘要。
    """
    from app.models import Chunk, Paper

    dataset_path = Path(dataset_path)
    chunks = db.query(Chunk).order_by(Chunk.paper_id, Chunk.chunk_index).all()
    manifest = [
        {
            "paper_id": row.paper_id,
            "chunk_index": row.chunk_index,
            "content_sha256": _sha256_bytes((row.content or "").encode("utf-8")),
        }
        for row in chunks
    ]
    manifest_bytes = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return {
        "dataset_sha256": _sha256_bytes(dataset_path.read_bytes()),
        "corpus_manifest_sha256": _sha256_bytes(manifest_bytes),
        "n_papers": db.query(Paper).count(),
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


def _resolve_qrels_or_raise(db, items: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    """预解析 qrels；正例无法解析时立即失败，避免混入模型质量分数。"""
    resolved: Dict[str, List[str]] = {}
    unresolved: List[str] = []
    for entry in items:
        ids = resolve_relevant_chunks(db, entry)
        resolved[entry["qa_id"]] = ids
        if entry["has_answer"] and not ids:
            unresolved.append(entry["qa_id"])
    if unresolved:
        raise ValueError(
            "正例 qrels 无法解析，评测已中止: " + ", ".join(unresolved)
        )
    return resolved


# ---------------------------------------------------------------------------
# 检索
# ---------------------------------------------------------------------------

_TECHNICAL_TOKEN_RE = re.compile(
    r"[A-Za-z0-9]+(?:[.\-][A-Za-z0-9]+)*%?"
)


def _tokenize_technical_terms(text: str) -> List[str]:
    """提取中英混合问题中的 ASCII 技术锚点。

    保留连字符、小数、科学计数法和百分号，例如 ReCo-MIL、F1-score、
    1e-4、87.3%。纯中文不做猜测式翻译，返回空列表。
    """
    return [token.lower() for token in _TECHNICAL_TOKEN_RE.findall(text or "")]


def _bm25_chunk_search(db, query: str, limit: int = 20) -> List[Dict[str, Any]]:
    """基于技术锚点的轻量 BM25 chunk 检索（Batch 12 观察 profile）。"""
    from app.models import Chunk

    query_tokens = list(dict.fromkeys(_tokenize_technical_terms(query)))
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
    return scored[:limit]

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
    if lexical_profile == "bm25":
        return _bm25_chunk_search(db, query, limit)

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


class Retriever:
    """评测用检索器：优先语义+关键词混合，模型不可用时降级为仅关键词。"""

    def __init__(
        self,
        db,
        top_k: int,
        keyword_only: bool = False,
        lexical_profile: str = "count",
    ):
        self.db = db
        self.top_k = top_k
        self.degraded = False
        self.degrade_reason = ""
        self._store = None
        self.lexical_profile = lexical_profile
        self.last_query_mode = "keyword-only" if keyword_only else "hybrid"
        self.last_query_degraded = bool(keyword_only)
        self.last_query_error: Optional[str] = None
        self.runtime_degraded_count = 0

        if keyword_only:
            self.degraded = True
            self.degrade_reason = "--keyword-only 指定，跳过语义检索"
        else:
            try:
                from app.services.retrieval import get_vector_store

                store = get_vector_store()
                if store.available():
                    self._store = store
                else:
                    self.degraded = True
                    self.degrade_reason = "Embedding 模型加载失败（详见日志）"
            except Exception as e:  # 模型/向量库任何异常都降级，不中断评测
                self.degraded = True
                self.degrade_reason = f"语义检索初始化异常: {e}"

    @property
    def mode(self) -> str:
        return "keyword-only(degraded)" if self.degraded else "hybrid"

    def search(self, query: str) -> List[Dict[str, Any]]:
        """对单条查询返回 top_k 的 chunk 级结果（含 chunk_id 与 content）。"""
        self.last_query_mode = "keyword-only" if self.degraded else "hybrid"
        self.last_query_degraded = self.degraded
        self.last_query_error = None
        keyword_results = _keyword_chunk_search(
            self.db,
            query,
            limit=self.top_k * 2,
            lexical_profile=self.lexical_profile,
        )
        if self.degraded:
            return keyword_results[: self.top_k]
        try:
            assert self._store is not None  # degraded 时已在上方提前返回
            semantic_results = self._store.search(query=query, top_k=self.top_k * 2)
        except Exception as e:
            # 运行期语义检索失败：本次查询降级为关键词结果
            print(f"  [warn] 语义检索失败，本条降级为关键词检索: {e}", file=sys.stderr)
            self.last_query_mode = "keyword-only(runtime-degraded)"
            self.last_query_degraded = True
            self.last_query_error = type(e).__name__
            self.runtime_degraded_count += 1
            return keyword_results[: self.top_k]
        return _rrf_fuse_chunks(semantic_results, keyword_results, self.top_k)


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
    return llm_service.chat_completion_sync(messages)


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


def run_eval(args: argparse.Namespace) -> int:
    """执行评测，返回进程退出码。"""
    items = load_dataset(args.dataset)
    validate_dataset(items)
    print(f"[eval] 数据集 {args.dataset} 共 {len(items)} 条")

    fixture_database = None
    fixture_metadata: Dict[str, Any] = {}
    if args.fixture:
        from eval.fixture import open_fixture_database

        fixture_database = open_fixture_database(args.fixture)
        fixture_metadata = fixture_database.metadata
        db = fixture_database.session_factory()
    else:
        from app.database import SessionLocal  # 延迟导入，连接真实 SQLite（只读）

        db = SessionLocal()
    try:
        t0 = time.time()
        benchmark = _build_benchmark_metadata(db, Path(args.dataset))
        benchmark.update({
            "qrels_sha256": _qrels_sha256(items),
            "benchmark_id": fixture_metadata.get("benchmark_id", "private-local-observation"),
        })
        if fixture_metadata:
            benchmark["fixture_license"] = fixture_metadata["license"]
        resolved_qrels = _resolve_qrels_or_raise(db, items)
        retriever = Retriever(
            db,
            top_k=args.top_k,
            keyword_only=args.keyword_only,
            lexical_profile=args.lexical_profile,
        )
        print(f"[eval] 检索模式: {retriever.mode}"
              + (f"（{retriever.degrade_reason}）" if retriever.degraded else ""))

        per_item: List[Dict[str, Any]] = []
        by_type: Dict[str, Dict[str, List[float]]] = defaultdict(
            lambda: {"recall": [], "mrr": [], "ndcg": []})
        gen_metrics = {
            "citation_precision": [],
            "citation_recall": [],
            "citation_f1": [],
            "keyword_hit_rate": [],
        }
        retrieval_latencies: List[float] = []  # 每次检索的延迟（毫秒）
        negative_total = 0
        negative_refused = 0

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
            else:
                negative_total += 1

            if args.with_llm:
                answer = _generate_answer(entry["question"], results)
                record["answer"] = answer
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
        type_rows = [
            {"question_type": qtype, "n": len(g["recall"]),
             "recall": _mean(g["recall"]), "mrr": _mean(g["mrr"]),
             "ndcg": _mean(g["ndcg"])}
            for qtype, g in sorted(by_type.items())
        ]

        comparison_key = ":".join((
            benchmark["dataset_sha256"],
            benchmark["qrels_sha256"],
            benchmark["corpus_manifest_sha256"],
            retriever.mode,
            args.lexical_profile,
            str(args.top_k),
        ))
        passed = overall[f"recall@{args.top_k}"] >= args.threshold
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
                "profile": retriever.mode,
                "lexical_profile": args.lexical_profile,
                "top_k": args.top_k,
            },
            "diagnostics": {
                "unresolved_qrels": [],
                "runtime_degraded_count": retriever.runtime_degraded_count,
            },
            "gate": {
                "passed": passed,
                "metric": f"recall@{args.top_k}",
                "threshold": args.threshold,
                "actual": overall[f"recall@{args.top_k}"],
            },
            "dataset": Path(args.dataset).name if args.fixture else str(args.dataset),
            "top_k": args.top_k,
            "threshold": args.threshold,
            "retrieval_mode": retriever.mode,
            "degraded": retriever.degraded,
            "degrade_reason": retriever.degrade_reason,
            "with_llm": args.with_llm,
            "elapsed_seconds": round(elapsed, 2),
            "latency": latency_stats(retrieval_latencies),
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
        print(f"[eval] recall@{args.top_k}={overall[f'recall@{args.top_k}']:.3f} "
              f"阈值={args.threshold} -> {'PASS' if passed else 'FAIL'}")
        return 0 if passed else 1
    finally:
        db.close()
        if fixture_database is not None:
            fixture_database.close()


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
    parser.add_argument("--top-k", type=int, default=5,
                        help="检索截断位置 k（默认 5，即 recall@5 / NDCG@5）")
    parser.add_argument("--threshold", type=float, default=0.5,
                        help="recall@k 达标阈值，低于则退出码为 1（默认 0.5）")
    parser.add_argument("--keyword-only", action="store_true",
                        help="强制仅关键词检索（不加载语义模型，速度快）")
    parser.add_argument(
        "--lexical-profile",
        choices=("count", "bm25"),
        default="count",
        help="chunk 词法检索策略；bm25 为观察实验，默认 count 保持历史行为",
    )
    parser.add_argument("--with-llm", action="store_true",
                        help="加跑生成侧指标（会真实调用 LLM API，默认关闭）")
    parser.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR),
                        help="JSON 报告输出目录（默认 eval/reports/）")
    return parser


def _validate_fixture_args(args: argparse.Namespace) -> Optional[str]:
    """返回 fixture CLI 参数错误；合法时返回 None。"""
    if args.fixture and not args.keyword_only:
        return "fixture 评测必须显式使用 --keyword-only"
    if args.fixture and args.with_llm:
        return "fixture 评测不得使用 --with-llm"
    return None


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    # load_dataset 接受 None 表示默认种子集
    if args.dataset is None:
        from eval.dataset import DEFAULT_SEED_PATH

        args.dataset = DEFAULT_SEED_PATH
    fixture_error = _validate_fixture_args(args)
    if fixture_error:
        print(f"[eval] 参数错误: {fixture_error}", file=sys.stderr)
        return 2
    return run_eval(args)


if __name__ == "__main__":
    sys.exit(main())
