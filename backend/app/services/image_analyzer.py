import base64
from typing import List, Dict, Any, Optional, AsyncIterator

from openai import AsyncOpenAI

from app.core.config import config
from app.core.logger import logger


class ImageAnalyzerService:
    """基于 Kimi 多模态能力的图片分析服务。"""

    def __init__(self):
        self.client = AsyncOpenAI(
            api_key=config.get("llm.api_key"),
            base_url=config.get("llm.base_url", "https://api.moonshot.cn/v1"),
            max_retries=1,
            timeout=120,
        )
        self.model = config.get("llm.model", "moonshot-v1-8k")

    def _get_temperature(self) -> float:
        # kimi-k2.6 系列模型目前只支持 temperature=1
        if "kimi-k2.6" in self.model or "kimi-k2" in self.model:
            return 1.0
        return 0.3

    def _encode_image(self, image_bytes: bytes) -> str:
        return base64.b64encode(image_bytes).decode("utf-8")

    def _guess_mime(self, filename: str) -> str:
        lower = filename.lower()
        if lower.endswith(".png"):
            return "image/png"
        if lower.endswith(".jpg") or lower.endswith(".jpeg"):
            return "image/jpeg"
        if lower.endswith(".gif"):
            return "image/gif"
        if lower.endswith(".webp"):
            return "image/webp"
        return "image/jpeg"

    async def analyze(
        self,
        image_bytes: bytes,
        filename: str,
        question: str,
    ) -> str:
        """分析单张图片并返回文本结果。"""
        b64 = self._encode_image(image_bytes)
        mime = self._guess_mime(filename)
        data_url = f"data:{mime};base64,{b64}"

        messages = [
            {
                "role": "system",
                "content": "你是一位学术研究助手，擅长分析论文截图、表格、公式和图表。请用中文简洁回答。",
            },
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": question or "请描述这张图片的内容，并解释其在学术论文中可能的含义。"},
                ],
            },
        ]

        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_tokens=2048,
                temperature=self._get_temperature(),
                timeout=120,
            )
            return response.choices[0].message.content or ""
        except Exception as e:
            logger.error(f"[image_analyzer] 图片分析失败: {e}", exc_info=True)
            return f"[图片分析失败: {e}]"

    async def analyze_stream(
        self,
        image_bytes: bytes,
        filename: str,
        question: str,
    ) -> AsyncIterator[str]:
        """流式分析图片。"""
        b64 = self._encode_image(image_bytes)
        mime = self._guess_mime(filename)
        data_url = f"data:{mime};base64,{b64}"

        messages = [
            {
                "role": "system",
                "content": "你是一位学术研究助手，擅长分析论文截图、表格、公式和图表。请用中文简洁回答。",
            },
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": question or "请描述这张图片的内容，并解释其在学术论文中可能的含义。"},
                ],
            },
        ]

        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_tokens=2048,
                temperature=self._get_temperature(),
                stream=True,
                timeout=120,
            )
            async for chunk in response:
                delta = chunk.choices[0].delta
                content = getattr(delta, "content", None) or ""
                if content:
                    yield content
        except Exception as e:
            logger.error(f"[image_analyzer_stream] 图片分析流式失败: {e}", exc_info=True)
            yield f"\n[图片分析失败: {e}]"


image_analyzer_service = ImageAnalyzerService()
