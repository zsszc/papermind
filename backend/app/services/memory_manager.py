"""记忆管理模块：统一四类记忆（short_term / long_term / preference / fact）的读写 API、
容量淘汰策略与 LLM 降级处理。

设计要点：
- get_memory / add_memory / clear_memory / delete_memory：四类记忆一致接口
- 写入后自动执行容量淘汰（short_term 删最旧；长期类删重要性最低、最旧）
- LLM 摘要失败时优雅降级（返回空串、跳过写入），不阻塞对话主流程
"""

from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.core.logger import logger
from app.models import MemorySummary, Message
from app.services.llm import llm_service

# 合法记忆类型
VALID_MEMORY_TYPES = ("short_term", "long_term", "preference", "fact")

# 长期类记忆（build_memory_context 聚合使用）
LONG_TERM_TYPES = ("long_term", "preference", "fact")

# 各类记忆的默认容量上限：short_term 只保留最近若干条，超出自动淘汰
DEFAULT_CAPACITY_LIMITS = {
    "short_term": 20,
    "long_term": 100,
    "preference": 50,
    "fact": 200,
}


class MemoryManager:
    """统一的记忆读写入口。"""

    def __init__(self, db: Session, capacity_limits: Optional[Dict[str, int]] = None):
        self.db = db
        # 容量上限允许调用方（如测试）覆盖
        self.capacity_limits = dict(DEFAULT_CAPACITY_LIMITS)
        if capacity_limits:
            self.capacity_limits.update(capacity_limits)

    # ---------- 统一读写 API ----------

    @staticmethod
    def _validate_type(memory_type: str) -> None:
        if memory_type not in VALID_MEMORY_TYPES:
            raise ValueError(
                f"非法 memory_type: {memory_type}，合法值为 {VALID_MEMORY_TYPES}"
            )

    def get_memory(
        self, memory_type: Optional[str] = None, limit: Optional[int] = None
    ) -> List[MemorySummary]:
        """读取记忆。memory_type 为 None 时返回全部类型（按时间倒序）。

        排序规则：short_term 按时间倒序；长期类按重要性降序、其次时间倒序。
        """
        query = self.db.query(MemorySummary)
        if memory_type is None:
            query = query.order_by(
                MemorySummary.created_at.desc(), MemorySummary.id.desc()
            )
        else:
            self._validate_type(memory_type)
            query = query.filter(MemorySummary.memory_type == memory_type)
            if memory_type == "short_term":
                query = query.order_by(
                    MemorySummary.created_at.desc(), MemorySummary.id.desc()
                )
            else:
                query = query.order_by(
                    MemorySummary.importance.desc(),
                    MemorySummary.created_at.desc(),
                    MemorySummary.id.desc(),
                )
        if limit:
            query = query.limit(limit)
        return query.all()

    def add_memory(
        self,
        memory_type: str,
        content: str,
        importance: int = 5,
        source_conversation_id: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[MemorySummary]:
        """写入一条记忆，并在该类型内执行容量淘汰。

        metadata 为接口预留参数：当前 MemorySummary 模型无对应列，暂不入库。
        数据库异常时回滚、记录日志并返回 None（不抛出），避免阻塞调用方主流程。
        """
        self._validate_type(memory_type)
        if not content or not content.strip():
            raise ValueError("记忆内容不能为空")
        try:
            mem = MemorySummary(
                memory_type=memory_type,
                content=content.strip(),
                importance=importance,
                source_conversation_id=source_conversation_id,
            )
            self.db.add(mem)
            self.db.commit()
            self._enforce_capacity(memory_type)
            return mem
        except Exception as e:
            self.db.rollback()
            logger.error(f"[memory] 写入记忆失败(type={memory_type}): {e}")
            return None

    def clear_memory(self, memory_type: str) -> int:
        """清空某类型的全部记忆，返回删除条数。"""
        self._validate_type(memory_type)
        deleted = (
            self.db.query(MemorySummary)
            .filter(MemorySummary.memory_type == memory_type)
            .delete(synchronize_session=False)
        )
        self.db.commit()
        return deleted

    def delete_memory(self, memory_id: int) -> bool:
        """按 id 删除一条记忆，不存在时返回 False。"""
        mem = (
            self.db.query(MemorySummary)
            .filter(MemorySummary.id == memory_id)
            .first()
        )
        if not mem:
            return False
        self.db.delete(mem)
        self.db.commit()
        return True

    # ---------- 容量淘汰 ----------

    def _enforce_capacity(self, memory_type: str) -> None:
        """超出容量上限时淘汰：short_term 删最旧；长期类删重要性最低、最旧。"""
        limit = self.capacity_limits.get(memory_type)
        if not limit or limit <= 0:
            return
        query = self.db.query(MemorySummary).filter(
            MemorySummary.memory_type == memory_type
        )
        total = query.count()
        if total <= limit:
            return
        if memory_type == "short_term":
            order = (MemorySummary.created_at.asc(), MemorySummary.id.asc())
        else:
            order = (
                MemorySummary.importance.asc(),
                MemorySummary.created_at.asc(),
                MemorySummary.id.asc(),
            )
        excess_ids = [m.id for m in query.order_by(*order).limit(total - limit).all()]
        self.db.query(MemorySummary).filter(MemorySummary.id.in_(excess_ids)).delete(
            synchronize_session=False
        )
        self.db.commit()
        logger.info(
            f"[memory] 容量淘汰: type={memory_type} 删除 {len(excess_ids)} 条，保留 {limit} 条"
        )

    # ---------- LLM 摘要（带降级） ----------

    async def summarize_conversation(self, conversation_id: int) -> str:
        """对会话做一句话摘要。LLM 调用失败时降级返回空串（不抛出）。"""
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

        try:
            result = await llm_service.chat_completion(
                [
                    {"role": "system", "content": "你是对话摘要助手。"},
                    {"role": "user", "content": prompt},
                ]
            )
            return (result or "").strip()
        except Exception as e:
            logger.error(f"[memory] 会话摘要失败(conversation={conversation_id}): {e}")
            return ""

    async def update_short_term_memory(self, conversation_id: int) -> None:
        """当对话消息数达到 5 的倍数时，更新短期记忆。

        任何失败（LLM 异常、数据库异常）均降级为跳过，不向调用方抛出。
        """
        try:
            count = (
                self.db.query(Message)
                .filter(Message.conversation_id == conversation_id)
                .count()
            )
            if count == 0 or count % 5 != 0:
                return
            summary = await self.summarize_conversation(conversation_id)
            if not summary:
                return
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
                self.db.commit()
            else:
                # 走统一写入入口，自动应用容量淘汰
                self.add_memory(
                    "short_term",
                    summary,
                    source_conversation_id=conversation_id,
                )
        except Exception as e:
            self.db.rollback()
            logger.error(
                f"[memory] 短期记忆更新失败(conversation={conversation_id}): {e}"
            )

    # ---------- Prompt 上下文 ----------

    def build_memory_context(self) -> str:
        """构建注入 Prompt 的记忆上下文。"""
        short_term = self.get_memory("short_term", limit=3)
        long_term = self.get_long_term_memories(limit=5)

        parts = []
        if long_term:
            parts.append(
                "用户背景与偏好：\n" + "\n".join([f"- {m.content}" for m in long_term])
            )
        if short_term:
            parts.append(
                "近期讨论主题：\n" + "\n".join([f"- {m.content}" for m in short_term])
            )

        return "\n\n".join(parts)

    # ---------- 兼容旧接口（内部走统一 API，签名保持不变） ----------

    def get_recent_short_term_memories(self, limit: int = 5) -> List[MemorySummary]:
        return self.get_memory("short_term", limit=limit)

    def get_long_term_memories(self, limit: int = 10) -> List[MemorySummary]:
        """聚合读取三类长期记忆（long_term / preference / fact），按重要性排序。"""
        return (
            self.db.query(MemorySummary)
            .filter(MemorySummary.memory_type.in_(LONG_TERM_TYPES))
            .order_by(MemorySummary.importance.desc(), MemorySummary.created_at.desc())
            .limit(limit)
            .all()
        )

    def add_long_term_memory(
        self, content: str, memory_type: str = "fact", importance: int = 5
    ) -> Optional[MemorySummary]:
        return self.add_memory(memory_type, content, importance=importance)
