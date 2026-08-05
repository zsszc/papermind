"""对话编排 Agent 图（LangGraph StateGraph）。

将 POST /api/chat 的前置编排链路建模为节点图：

    load_memory → retrieve → build_messages

- load_memory：加载会话历史消息与用户背景记忆，生成基础 system prompt
- retrieve：向量库检索相关文献片段（Embedding 不可用时自动回退为空）
- build_messages：组装最终发给 LLM 的消息列表（system + history + RAG +
  联网搜索提示 + Skill 角色注入），并判定是否启用联网搜索

流式生成（generate）刻意不放进图里：LangGraph 的流式语义与现有 SSE 契约
（delta / finished+citations / error 三种事件）差异较大，强行图内化会破坏契约，
因此生成仍由路由层驱动，本图只负责 LLM 调用前的上下文编排。

节点均为同步、近似纯函数（仅读取 db / 向量库，不写库），外部依赖
（向量库、记忆管理、Skill、联网判断）走模块级导入，测试可直接 monkeypatch，
无需真实 LLM / embedding。
"""

import re
import threading
from typing import Any, Dict, List, Optional, Tuple, TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy.orm import Session

from app.core.logger import logger
from app.models import Message
from app.services.memory_manager import MemoryManager
from app.services.retrieval import get_vector_store
from app.services.skills import build_skill_prompt
from app.services.web_search import web_search_service

# 基础 system prompt（与原 chat.py 内嵌版本逐字一致）
SYSTEM_PROMPT = """你是 PaperMind，一位专业的学术文献助手。你正在帮助用户管理结直肠癌 T 分期预测相关的文献、笔记与毕业论文写作。

请遵循以下规则：
1. 基于提供的参考文献片段回答，若片段不足以回答，请明确说明。
2. 回答需专业、简洁，优先使用中文。
3. 若引用文献片段，请在回答末尾以 [^1^] [^2^] 形式标注，并列出引用来源。
4. 若用户问题与当前文献无关，可作为一般学术讨论回答。
"""

# 联网搜索提示（与原 chat.py 内嵌版本逐字一致）
WEB_SEARCH_HINT = "用户问题可能涉及最新信息。如果现有文献片段不足以回答，请调用联网搜索工具获取最新资料并标注来源。"

# 零检索拒答硬约束（Phase C C2）：检索结果为空时追加到 system prompt 尾部
NO_RETRIEVAL_GUARD = "未检索到相关文献片段。必须明确回答「文献库中没有相关内容」，禁止编造任何引用标记。"

# 注入的历史消息条数上限与检索 top_k（与原 chat.py 保持一致）
HISTORY_LIMIT = 10
RETRIEVE_TOP_K = 5

# 引用标记正则：[^n^]，n 为整数（允许负数形式以便识别后剔除，编号为 1-based）
_CITATION_MARKER_PATTERN = re.compile(r"\[\^(-?\d+)\^\]")


def verify_citations(
    answer_text: str, retrieved_chunks: List[Dict[str, Any]]
) -> Tuple[str, Dict[str, Any]]:
    """校验答案中的 [^n^] 引用标记是否落在本次检索片段编号范围内（Phase C C1）。

    - 1 <= n <= len(retrieved_chunks) 的标记保留并计入有效引用；
    - 越界（含 0、负数）或零检索时的标记从文本剔除（保留语句本身）；
    - 返回 (清洗后文本, {"total", "valid", "removed", "verified"})，
      全部有效或无引用时 verified=True，有剔除时 verified=False；
    - 有剔除时记 [guardrails] warning（脱敏：只记编号列表，不记答案全文）。

    幂等纯函数：无 DB / 网络 / LLM 调用，可直接单测。
    """
    total = 0
    valid = 0
    removed_indices: List[int] = []

    def _replace(match: "re.Match[str]") -> str:
        nonlocal total, valid
        total += 1
        n = int(match.group(1))
        if 1 <= n <= len(retrieved_chunks):
            valid += 1
            return match.group(0)
        removed_indices.append(n)
        return ""

    cleaned = _CITATION_MARKER_PATTERN.sub(_replace, answer_text)
    removed = total - valid
    if removed:
        logger.warning(
            f"[guardrails] 剔除越界引用标记 {removed_indices}"
            f"（本次检索片段数={len(retrieved_chunks)}）"
        )
    return cleaned, {
        "total": total,
        "valid": valid,
        "removed": removed,
        "verified": removed == 0,
    }


def build_rag_prompt(query: str, retrieved: List[dict]) -> str:
    """把检索片段拼装为带引用编号的 RAG system prompt（与原 chat.py 实现一致）。"""
    context_parts = []
    for i, item in enumerate(retrieved, start=1):
        title = item.get("title") or "未知文献"
        authors = item.get("authors") or ""
        year = item.get("year") or ""
        page = item.get("page_number")
        content = item.get("content", "")
        header = f"[{i}] {title}"
        if authors:
            header += f" - {authors}"
        if year:
            header += f" ({year})"
        if page:
            header += f" 第{page}页"
        context_parts.append(f"{header}\n{content}\n")

    context = "\n---\n".join(context_parts)
    return f"""以下是可能相关的文献片段（每个片段开头 [i] 为引用编号，请在回答中需要引用时标注 [^i^]）：

{context}

---

用户问题：{query}

请基于以上片段回答，并在需要时标注引用来源 [^i^]。回答末尾请列出引用文献的标题与页码。"""


class AgentState(TypedDict, total=False):
    """Agent 编排图状态。

    输入（路由层填入）：db / conversation_id / user_message / skill / paper_id /
    enable_web_search。
    输出（图执行后读取）：system_prompt / context_chunks / history_messages /
    messages / web_search_enabled / history_total。
    """

    # ---- 输入 ----
    db: Session                      # SQLAlchemy 会话（只读使用）
    conversation_id: int             # 当前会话 ID
    user_message: str                # 用户本轮输入
    skill: Optional[str]             # Skill ID（可为空）
    paper_id: Optional[int]          # 限定检索的文献 ID（可为空）
    enable_web_search: bool          # 前端显式开启联网搜索
    # ---- 中间/输出 ----
    memory_context: str              # 用户背景记忆文本
    system_prompt: str               # 基础 system prompt（含记忆）
    history_messages: List[Dict[str, str]]  # 最近若干条历史消息（含当前 user 消息）
    history_total: int               # 会话消息总数（用于 message_count）
    context_chunks: List[Dict[str, Any]]    # 检索片段（含引用信息）
    web_search_enabled: bool         # 最终判定是否启用联网搜索
    skill_prompt: Optional[str]      # 注入的 Skill 角色 prompt
    messages: List[Dict[str, str]]   # 最终发给 LLM 的消息列表


# ---------- 节点函数 ----------


def load_memory(state: AgentState) -> Dict[str, Any]:
    """节点1：加载会话历史消息与用户背景记忆，生成基础 system prompt。"""
    db = state["db"]
    history = (
        db.query(Message)
        .filter(Message.conversation_id == state["conversation_id"])
        .order_by(Message.created_at.asc())
        .all()
    )
    history_messages = [
        {"role": m.role, "content": m.content} for m in history[-HISTORY_LIMIT:]
    ]

    memory_context = MemoryManager(db).build_memory_context()
    system_prompt = SYSTEM_PROMPT
    if memory_context:
        system_prompt += f"\n\n以下是关于用户的背景记忆，请在回答时参考：\n\n{memory_context}"

    return {
        "history_messages": history_messages,
        "history_total": len(history),
        "memory_context": memory_context,
        "system_prompt": system_prompt,
    }


def retrieve(state: AgentState) -> Dict[str, Any]:
    """节点2：向量库检索相关文献片段；失败或不可用时回退为空列表。"""
    chunks: List[Dict[str, Any]] = []
    filters: Dict[str, Any] = {}
    if state.get("paper_id"):
        filters["paper_id"] = state["paper_id"]
    if state.get("user_message"):
        try:
            store = get_vector_store()
            if store.available():
                chunks = store.search(
                    query=state["user_message"],
                    top_k=RETRIEVE_TOP_K,
                    filters=filters,
                )
        except Exception as e:
            logger.error(f"[agent_graph] 检索失败: {e}")
    return {"context_chunks": chunks}


def build_messages(state: AgentState) -> Dict[str, Any]:
    """节点3：组装最终消息列表，并判定联网搜索开关（顺序与原 chat.py 一致）。"""
    chunks = state.get("context_chunks") or []

    # Phase C C2：零检索时在 system prompt 尾部追加拒答硬约束段；
    # 有检索结果时 prompt 与现状一致（不回归）
    system_content = state["system_prompt"]
    if not chunks:
        system_content += f"\n\n{NO_RETRIEVAL_GUARD}"

    messages: List[Dict[str, str]] = [
        {"role": "system", "content": system_content}
    ]
    # 历史消息（history 中已包含当前 user 消息）
    messages.extend(state.get("history_messages") or [])

    # 检索结果作为 system 上下文追加到最后
    if chunks:
        messages.append(
            {"role": "system", "content": build_rag_prompt(state["user_message"], chunks)}
        )

    # 联网搜索：显式开启或启发式命中
    web_search_enabled = bool(state.get("enable_web_search")) or (
        web_search_service.should_search_online(state["user_message"])
    )
    if web_search_enabled:
        messages.append({"role": "system", "content": WEB_SEARCH_HINT})

    # Skill 角色注入
    skill_prompt = build_skill_prompt(state.get("skill"), state["user_message"])
    if skill_prompt:
        messages.append({"role": "system", "content": skill_prompt})

    return {
        "messages": messages,
        "web_search_enabled": web_search_enabled,
        "skill_prompt": skill_prompt,
    }


# ---------- 图构建与单例 ----------


def build_agent_graph():
    """构建并编译对话编排图：load_memory → retrieve → build_messages。"""
    graph = StateGraph(AgentState)
    graph.add_node("load_memory", load_memory)
    graph.add_node("retrieve", retrieve)
    graph.add_node("build_messages", build_messages)
    graph.add_edge(START, "load_memory")
    graph.add_edge("load_memory", "retrieve")
    graph.add_edge("retrieve", "build_messages")
    graph.add_edge("build_messages", END)
    return graph.compile()


_compiled_graph = None
_graph_lock = threading.Lock()


def get_agent_graph():
    """获取编译后的 Agent 图单例（双检锁懒加载，线程安全）。"""
    global _compiled_graph
    if _compiled_graph is None:
        with _graph_lock:
            if _compiled_graph is None:
                _compiled_graph = build_agent_graph()
    return _compiled_graph


def run_pre_orchestration(
    db: Session,
    conversation_id: int,
    user_message: str,
    skill: Optional[str] = None,
    paper_id: Optional[int] = None,
    enable_web_search: bool = False,
) -> AgentState:
    """执行 LLM 调用前的编排链路，返回图的最终状态。

    路由层从返回状态中读取 messages / context_chunks / web_search_enabled /
    history_total，随后维持既有流式生成与 SSE 发送逻辑不变。
    """
    return get_agent_graph().invoke(
        {
            "db": db,
            "conversation_id": conversation_id,
            "user_message": user_message,
            "skill": skill,
            "paper_id": paper_id,
            "enable_web_search": enable_web_search,
        }
    )
