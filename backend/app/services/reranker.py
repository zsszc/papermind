"""BGE-Reranker 重排服务（Phase B / B1）。

对检索召回的 (query, chunk) 候选对计算相关性分数，用于 VectorStore.search()
的精排。与 EmbeddingService 同模式：单例 + 懒加载 + 失败锁存（进程内不重试）。

- 模型名从 ``retrieval.rerank_model`` 配置读取（默认 ``BAAI/bge-reranker-v2-m3``）；
- 开关由调用方按 ``retrieval.rerank``（默认 false）判断，本服务自身不读开关；
- ``_score(pairs)`` 为可 mock 的测试钩子；模型不可用时抛 RuntimeError，
  由调用方（retrieval）负责降级为原始排序，本服务不兜底。
"""

import os
import threading
from typing import List, Tuple

from app.core.config import config
from app.core.logger import logger

# 国内 HuggingFace 镜像，加速模型下载（与 embedding.py 一致）
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

DEFAULT_RERANK_MODEL = "BAAI/bge-reranker-v2-m3"


class RerankerService:
    """本地 CrossEncoder 重排服务封装（单例 + 懒加载 + 失败锁存）。"""

    _instance = None
    _model = None
    _failed = False
    _error = None
    _load_lock = threading.Lock()
    _predict_lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance.model_name = config.get(
                "retrieval.rerank_model", DEFAULT_RERANK_MODEL
            )
        return cls._instance

    def _load_model(self):
        """懒加载 CrossEncoder；失败锁存（进程内不重试），返回 None 表示不可用。"""
        if self._model is None and not self._failed:
            with RerankerService._load_lock:
                if self._model is None and not self._failed:
                    try:
                        from sentence_transformers import CrossEncoder

                        device = config.get("embedding.device", "auto")
                        if device == "auto":
                            import torch

                            device = (
                                "mps" if torch.backends.mps.is_available() else "cpu"
                            )
                        self._model = CrossEncoder(self.model_name, device=device)
                        logger.info(f"[reranker] 模型已加载: {self.model_name}")
                    except Exception as e:
                        self._failed = True
                        self._error = str(e)
                        logger.error(f"[reranker] 模型加载失败: {e}")
        return self._model

    def available(self) -> bool:
        """模型是否可用；首次调用在调用方线程内同步触发懒加载（可能含模型下载）。"""
        return self._load_model() is not None

    def _score(self, pairs: List[Tuple[str, str]]) -> List[float]:
        """计算 (query, passage) 对的相关性分数列表（可 mock 的测试钩子）。

        模型不可用时抛 RuntimeError——降级决策交给调用方（对齐
        EmbeddingService._sync_embed 的语义：服务暴露失败，调用方兜底）。
        """
        if not pairs:
            return []
        model = self._load_model()
        if model is None:
            raise RuntimeError(f"Reranker 模型不可用: {self._error}")
        # CrossEncoder.predict 非线程安全保证，串行化以防并发检索竞争
        with RerankerService._predict_lock:
            scores = model.predict(list(pairs))
        return [float(s) for s in scores]
