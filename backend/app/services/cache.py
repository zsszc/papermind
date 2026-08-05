"""轻量级内存缓存，用于缓存检索结果、LLM 常见回答等高频数据。"""

import hashlib
import time
from functools import wraps
from typing import Any, Callable, Optional


class SimpleCache:
    def __init__(self, default_ttl: int = 300, max_size: int = 1000):
        self._store: dict = {}
        self.default_ttl = default_ttl
        self.max_size = max_size

    def _make_key(self, *args, **kwargs) -> str:
        key_data = str(args) + str(sorted(kwargs.items()))
        return hashlib.md5(key_data.encode("utf-8")).hexdigest()

    def get(self, key: str) -> Any:
        item = self._store.get(key)
        if not item:
            return None
        if item["expires"] < time.time():
            self._store.pop(key, None)
            return None
        return item["value"]

    def set(self, key: str, value: Any, ttl: Optional[int] = None):
        if len(self._store) >= self.max_size:
            # 容量驱逐：优先清已过期项；无过期项则驱逐「最早过期」的项（非 LRU）
            now = time.time()
            expired_keys = [k for k, v in self._store.items() if v["expires"] < now]
            if expired_keys:
                for k in expired_keys[:100]:
                    self._store.pop(k, None)
            else:
                oldest = min(self._store.items(), key=lambda x: x[1]["expires"])[0]
                self._store.pop(oldest, None)

        self._store[key] = {
            "value": value,
            "expires": time.time() + (ttl or self.default_ttl),
        }

    def delete_prefix(self, prefix: str) -> int:
        """删除指定前缀的缓存项，返回删除数量。"""
        keys = [key for key in self._store if key.startswith(prefix)]
        for key in keys:
            self._store.pop(key, None)
        return len(keys)

    def cached(
        self,
        ttl: Optional[int] = None,
        key_func: Optional[Callable[..., str]] = None,
    ):
        def decorator(func: Callable) -> Callable:
            @wraps(func)
            def wrapper(*args, **kwargs):
                cache_key = key_func(*args, **kwargs) if key_func else self._make_key(*args, **kwargs)
                cached_value = self.get(cache_key)
                if cached_value is not None:
                    return cached_value
                result = func(*args, **kwargs)
                self.set(cache_key, result, ttl)
                return result

            return wrapper

        return decorator


# 全局缓存实例
cache = SimpleCache(default_ttl=300, max_size=1000)
