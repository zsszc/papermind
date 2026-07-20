import asyncio
import json
from typing import AsyncIterator, List, Dict, Any, Optional

from openai import AsyncOpenAI, APIError, APITimeoutError

from app.core.config import config
from app.core.logger import logger


class LLMService:
    def __init__(self):
        self.client = AsyncOpenAI(
            api_key=config.get("llm.api_key"),
            base_url=config.get("llm.base_url", "https://api.moonshot.cn/v1"),
            max_retries=1,
            timeout=120,
        )
        self.model = config.get("llm.model", "moonshot-v1-8k")
        self.max_tokens = config.get("llm.max_tokens", 4096)
        self.temperature = config.get("llm.temperature", 0.3)
        self.max_total_chars = config.get("llm.max_total_chars", 200000)

    def _get_temperature(self) -> float:
        # kimi-k2.6 系列模型目前只支持 temperature=1
        if "kimi-k2.6" in self.model or "kimi-k2" in self.model:
            return 1.0
        return self.temperature

    def _truncate_messages(self, messages: List[Dict[str, str]]) -> List[Dict[str, str]]:
        """截断过长的消息内容，避免超出模型上下文。保留 system 消息，优先截断较早的非 system 消息。"""
        total = sum(len(m.get("content", "") or "") for m in messages)
        if total <= self.max_total_chars:
            return messages

        system_messages = [m for m in messages if m.get("role") == "system"]
        non_system = [m for m in messages if m.get("role") != "system"]

        # 截断策略：保留 system，从最早消息开始截断，每条消息至少保留 300 字符
        truncated_non_system = []
        for m in non_system:
            content = m.get("content", "") or ""
            if total > self.max_total_chars and len(content) > 300:
                keep = max(300, len(content) - (total - self.max_total_chars) // max(len(non_system), 1))
                content = content[-keep:]
                total -= (len(m.get("content", "")) - keep)
            truncated_non_system.append({**m, "content": content})

        # 如果仍然超长，只保留最近的 2 条非 system 消息
        if total > self.max_total_chars and len(truncated_non_system) > 2:
            truncated_non_system = truncated_non_system[-2:]

        return system_messages + truncated_non_system

    async def _async_retry(
        self,
        coro_factory,
        max_retries: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 10.0,
    ):
        """指数退避重试包装器，接收返回 coroutine 的工厂函数。"""
        last_exception = None
        for attempt in range(max_retries):
            try:
                return await coro_factory()
            except (APIError, APITimeoutError, TimeoutError, asyncio.TimeoutError) as e:
                last_exception = e
                if attempt < max_retries - 1:
                    delay = min(base_delay * (2 ** attempt), max_delay)
                    logger.warning(f"[LLM] 调用失败，第 {attempt + 1} 次重试，等待 {delay}s: {e}")
                    await asyncio.sleep(delay)
                else:
                    logger.error(f"[LLM] 调用失败，已达最大重试次数: {e}")
            except Exception as e:
                logger.error(f"[LLM] 非预期错误: {e}")
                raise
        raise last_exception

    async def chat_stream(
        self,
        messages: List[Dict[str, str]],
        enable_web_search: bool = False,
    ) -> AsyncIterator[str]:
        messages = self._truncate_messages(messages)
        last_exception = None

        for attempt in range(3):
            try:
                kwargs = {
                    "model": self.model,
                    "messages": messages,
                    "max_tokens": self.max_tokens,
                    "temperature": self._get_temperature(),
                    "stream": True,
                    "timeout": 180,
                }
                if enable_web_search:
                    kwargs["tools"] = [
                        {
                            "type": "builtin_function",
                            "function": {"name": "web_search"},
                        }
                    ]
                    kwargs["tool_choice"] = "auto"

                response = await self.client.chat.completions.create(**kwargs)
                async for chunk in response:
                    delta = chunk.choices[0].delta
                    content = getattr(delta, "content", None) or ""
                    if content:
                        yield content
                return
            except (APIError, APITimeoutError, TimeoutError, asyncio.TimeoutError) as e:
                last_exception = e
                if attempt < 2:
                    delay = min(1.0 * (2 ** attempt), 10.0)
                    logger.warning(f"[LLM] 流式调用失败，第 {attempt + 1} 次重试，等待 {delay}s: {e}")
                    await asyncio.sleep(delay)
                else:
                    logger.error(f"[LLM] 流式调用失败，已达最大重试次数: {e}")
            except Exception as e:
                logger.error(f"[LLM] 流式调用非预期错误: {e}")
                yield f"\n[调用 LLM 出错: {self._format_error(e)}]"
                return

        if last_exception:
            error_msg = self._format_error(last_exception)
            logger.error(f"[LLM] 流式调用最终失败: {error_msg}")
            yield f"\n[调用 LLM 出错: {error_msg}]"

    async def chat_completion(
        self,
        messages: List[Dict[str, str]],
        json_mode: bool = False,
        timeout: Optional[int] = None,
    ) -> str:
        messages = self._truncate_messages(messages)
        call_timeout = timeout or 120

        def _complete():
            kwargs = {
                "model": self.model,
                "messages": messages,
                "max_tokens": self.max_tokens,
                "temperature": self._get_temperature(),
                "timeout": call_timeout,
            }
            if json_mode:
                kwargs["response_format"] = {"type": "json_object"}
            return self.client.chat.completions.create(**kwargs)

        try:
            response = await self._async_retry(_complete, max_retries=3)
            return response.choices[0].message.content or ""
        except Exception as e:
            error_msg = self._format_error(e)
            logger.error(f"[LLM] 同步调用最终失败: {error_msg}")
            return f"[调用 LLM 出错: {error_msg}]"

    def _format_error(self, e: Exception) -> str:
        msg = str(e)
        if "429" in msg or "overloaded" in msg.lower():
            return "Kimi API 当前负载过高或请求频繁，请稍后再试。"
        if "401" in msg or "Authentication" in msg:
            return "API Key 无效或已过期，请检查 config.yaml 中的 llm.api_key。"
        if "timeout" in msg.lower() or "timed out" in msg.lower():
            return "Kimi API 响应超时，请稍后重试。"
        return msg

    def is_configured(self) -> bool:
        """检查 API Key 是否已配置且不像占位符。"""
        key = config.get("llm.api_key")
        if not key:
            return False
        key = str(key).strip()
        if key.startswith("sk-xxxx") or key.startswith("your-") or len(key) < 20:
            return False
        return True

    async def health_check(self, timeout: int = 8) -> Dict[str, Any]:
        """轻量健康检查：调用一次超短 completion。"""
        if not self.is_configured():
            return {"ok": False, "error": "llm.api_key 未配置或无效"}
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": "hi"}],
                max_tokens=1,
                temperature=self._get_temperature(),
                timeout=timeout,
            )
            return {"ok": True, "model": self.model}
        except Exception as e:
            error_msg = self._format_error(e)
            logger.warning(f"[LLM] 健康检查失败: {error_msg}")
            return {"ok": False, "error": error_msg}


llm_service = LLMService()
