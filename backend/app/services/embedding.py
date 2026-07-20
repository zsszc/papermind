import os
import queue
import re
import threading
from concurrent.futures import Future
from pathlib import Path
from typing import List, Dict, Any, Optional

from app.core.config import config
from app.core.logger import logger

# 国内 HuggingFace 镜像，加速模型下载
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")


class TextChunker:
    """基于内容类型的简单智能分块策略。"""

    _SECTION_KEYWORDS = {
        "abstract": ["abstract", "摘要"],
        "intro": ["introduction", "intro", "引言", "前言", "背景"],
        "method": ["methods", "methodology", "materials and methods", "方法", "材料与方法"],
        "result": ["results", "实验结果", "结果"],
        "discussion": ["discussion", "讨论"],
        "conclusion": ["conclusion", "conclusions", "结论"],
    }

    def __init__(self, chunk_size: int = 512, chunk_overlap: int = 50):
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap

    def chunk_pages(self, pages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """将按页文本列表分块。"""
        chunks = []
        for page in pages:
            page_chunks = self._chunk_text(
                page["text"],
                page_number=page.get("page_number"),
            )
            chunks.extend(page_chunks)
        return chunks

    def _chunk_text(self, text: str, page_number: Optional[int] = None) -> List[Dict[str, Any]]:
        if not text.strip():
            return []

        # 按段落拆分
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        chunks = []
        current = []
        current_len = 0

        for para in paragraphs:
            para_len = len(para)
            if current_len + para_len > self.chunk_size and current:
                chunks.append(self._make_chunk(current, page_number))
                # 保留重叠
                overlap = []
                overlap_len = 0
                for p in reversed(current):
                    if overlap_len + len(p) > self.chunk_overlap:
                        break
                    overlap.insert(0, p)
                    overlap_len += len(p)
                current = overlap
                current_len = overlap_len

            current.append(para)
            current_len += para_len

        if current:
            chunks.append(self._make_chunk(current, page_number))

        return chunks

    def _infer_chunk_type(self, paragraphs: List[str]) -> str:
        """根据段落开头关键词推断内容类型。"""
        first = " ".join(paragraphs[:2]).lower()
        for ctype, keywords in self._SECTION_KEYWORDS.items():
            for kw in keywords:
                if kw in first:
                    return ctype
        return "paragraph"

    def _make_chunk(self, paragraphs: List[str], page_number: Optional[int]) -> Dict[str, Any]:
        content = "\n\n".join(paragraphs)
        return {
            "content": content,
            "page_number": page_number,
            "chunk_type": self._infer_chunk_type(paragraphs),
            "token_count": len(content),
        }


class EmbeddingService:
    """本地 Embedding 服务封装（单例 + 单线程任务队列）。"""

    _instance = None
    _model = None
    _failed = False
    _error = None
    _task_queue = queue.Queue()
    _worker_thread = None
    _worker_lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance.model_name = config.get("embedding.local_model", "BAAI/bge-m3")
            cls._instance._start_worker()
        return cls._instance

    def _start_worker(self):
        """启动单线程 worker，串行处理 embedding 任务，避免并发竞争。"""
        with EmbeddingService._worker_lock:
            if EmbeddingService._worker_thread is not None:
                return
            t = threading.Thread(target=self._worker_loop, daemon=True, name="embedding-worker")
            t.start()
            EmbeddingService._worker_thread = t
            logger.info("[EmbeddingService] worker 线程已启动")

    def _worker_loop(self):
        while True:
            task = EmbeddingService._task_queue.get()
            if task is None:
                break
            texts, future = task
            try:
                result = self._sync_embed(texts)
                future.set_result(result)
            except Exception as e:
                logger.error(f"[EmbeddingService] encode 失败: {e}")
                future.set_exception(e)
            finally:
                EmbeddingService._task_queue.task_done()

    def _load_model(self):
        if self._model is None and not self._failed:
            try:
                from sentence_transformers import SentenceTransformer
                device = config.get("embedding.device", "auto")
                if device == "auto":
                    import torch
                    device = "mps" if torch.backends.mps.is_available() else "cpu"
                self._model = SentenceTransformer(self.model_name, device=device)
            except Exception as e:
                self._failed = True
                self._error = str(e)
                logger.error(f"[EmbeddingService] 模型加载失败: {e}")
        return self._model

    def available(self) -> bool:
        return self._load_model() is not None

    def _sync_embed(self, texts: List[str], batch_size: int = 8) -> List[List[float]]:
        if not texts:
            return []
        model = self._load_model()
        if model is None:
            raise RuntimeError(f"Embedding 模型不可用: {self._error}")

        # 长文本截断，避免 BGE-M3 输入过长导致内存峰值
        max_length = 512
        truncated = []
        for t in texts:
            tokens = t.split()
            if len(tokens) > max_length:
                t = " ".join(tokens[:max_length])
            truncated.append(t)

        embeddings = model.encode(
            truncated,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
            batch_size=batch_size,
        )
        return embeddings.tolist()

    def embed(self, texts: List[str], batch_size: int = 16) -> List[List[float]]:
        """将 embedding 任务提交到 worker 队列并等待结果。"""
        if not texts:
            return []
        future = Future()
        EmbeddingService._task_queue.put((texts, future))
        return future.result()

    def embed_query(self, query: str) -> List[float]:
        prefixed = f"Represent this sentence for searching relevant passages: {query}"
        return self.embed([prefixed])[0]
