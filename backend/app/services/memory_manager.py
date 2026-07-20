from typing import List, Dict, Any

from sqlalchemy.orm import Session

from app.models import Message, MemorySummary
from app.services.llm import llm_service


class MemoryManager:
    def __init__(self, db: Session):
        self.db = db

    def get_recent_short_term_memories(self, limit: int = 5) -> List[MemorySummary]:
        return (
            self.db.query(MemorySummary)
            .filter(MemorySummary.memory_type == "short_term")
            .order_by(MemorySummary.created_at.desc())
            .limit(limit)
            .all()
        )

    def get_long_term_memories(self, limit: int = 10) -> List[MemorySummary]:
        return (
            self.db.query(MemorySummary)
            .filter(MemorySummary.memory_type.in_(["long_term", "preference", "fact"]))
            .order_by(MemorySummary.importance.desc(), MemorySummary.created_at.desc())
            .limit(limit)
            .all()
        )

    async def summarize_conversation(self, conversation_id: int) -> str:
        messages = (
            self.db.query(Message)
            .filter(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.asc())
            .all()
        )
        if len(messages) < 5:
            return ""

        text = "\n".join([f"{m.role}: {m.content[:500]}" for m in messages[-10:]])
        prompt = f"""请对以下对话进行一句话摘要，提取用户关注的研究主题、关键问题和偏好：

{text}

摘要（一句话）："""

        result = await llm_service.chat_completion([
            {"role": "system", "content": "你是对话摘要助手。"},
            {"role": "user", "content": prompt},
        ])
        return result.strip()

    async def update_short_term_memory(self, conversation_id: int):
        """当对话消息数达到 5 的倍数时，更新短期记忆。"""
        count = (
            self.db.query(Message)
            .filter(Message.conversation_id == conversation_id)
            .count()
        )
        if count > 0 and count % 5 == 0:
            summary = await self.summarize_conversation(conversation_id)
            if summary:
                existing = (
                    self.db.query(MemorySummary)
                    .filter(
                        MemorySummary.memory_type == "short_term",
                        MemorySummary.source_conversation_id == conversation_id,
                    )
                    .first()
                )
                if existing:
                    existing.content = summary
                else:
                    mem = MemorySummary(
                        memory_type="short_term",
                        content=summary,
                        source_conversation_id=conversation_id,
                    )
                    self.db.add(mem)
                self.db.commit()

    def build_memory_context(self) -> str:
        """构建注入 Prompt 的记忆上下文。"""
        short_term = self.get_recent_short_term_memories(limit=3)
        long_term = self.get_long_term_memories(limit=5)

        parts = []
        if long_term:
            parts.append("用户背景与偏好：\n" + "\n".join([f"- {m.content}" for m in long_term]))
        if short_term:
            parts.append("近期讨论主题：\n" + "\n".join([f"- {m.content}" for m in short_term]))

        return "\n\n".join(parts)

    def add_long_term_memory(self, content: str, memory_type: str = "fact", importance: int = 5):
        mem = MemorySummary(
            memory_type=memory_type,
            content=content,
            importance=importance,
        )
        self.db.add(mem)
        self.db.commit()
        return mem
