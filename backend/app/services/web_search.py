import asyncio
from typing import AsyncIterator, List, Dict, Any, Optional

from openai import AsyncOpenAI

from app.core.config import config
from app.core.logger import logger


class WebSearchService:
    """基于 Kimi 内置 web_search tool 的联网搜索服务。"""

    def __init__(self):
        self.client = AsyncOpenAI(
            api_key=config.get("llm.api_key"),
            base_url=config.get("llm.base_url", "https://api.moonshot.cn/v1"),
            max_retries=1,
            timeout=120,
        )
        self.model = config.get("llm.model", "moonshot-v1-8k")

    async def search(
        self,
        query: str,
        top_n: int = 5,
    ) -> List[Dict[str, Any]]:
        """执行一次联网搜索，返回搜索结果列表。"""
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": query}],
                max_tokens=2048,
                temperature=0.3,
                tools=[
                    {
                        "type": "builtin_function",
                        "function": {
                            "name": "web_search",
                        },
                    }
                ],
                tool_choice="auto",
            )

            results = []
            for choice in response.choices:
                msg = choice.message
                # 如果模型调用了 web_search，tool_calls 中包含搜索参数
                if getattr(msg, "tool_calls", None):
                    for tc in msg.tool_calls:
                        if tc.function.name == "web_search":
                            try:
                                import json
                                args = json.loads(tc.function.arguments)
                                results.append({
                                    "type": "search_args",
                                    "query": args.get("query", query),
                                    "raw": tc.function.arguments,
                                })
                            except Exception:
                                pass

                # 收集搜索返回的引用/链接信息（部分模型会在 content 中返回）
                content = getattr(msg, "content", None) or ""
                if content:
                    results.append({
                        "type": "search_summary",
                        "content": content,
                    })

            return results
        except Exception as e:
            logger.warning(f"[web_search] 联网搜索失败: {e}", exc_info=True)
            return []

    async def search_stream(
        self,
        query: str,
    ) -> AsyncIterator[str]:
        """流式执行联网搜索，返回增量文本。"""
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": query}],
                max_tokens=2048,
                temperature=0.3,
                tools=[
                    {
                        "type": "builtin_function",
                        "function": {
                            "name": "web_search",
                        },
                    }
                ],
                tool_choice="auto",
                stream=True,
            )
            async for chunk in response:
                delta = chunk.choices[0].delta
                content = getattr(delta, "content", None) or ""
                if content:
                    yield content
        except Exception as e:
            logger.warning(f"[web_search_stream] 联网搜索流式失败: {e}", exc_info=True)
            yield f"\n[联网搜索调用失败: {e}]"

    def should_search_online(self, query: str) -> bool:
        """简单启发式判断是否需要联网搜索。"""
        query_lower = query.lower()
        online_keywords = [
            "最新", "最近", "news", "latest", "recent", "2024", "2025", "2026",
            "搜索", "查一下", "网上", "google", "百度", "arxiv",
        ]
        return any(kw in query_lower for kw in online_keywords) or query_lower.startswith("搜索")


web_search_service = WebSearchService()
