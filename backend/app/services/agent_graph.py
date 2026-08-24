"""对话编排 Agent 图（LangGraph StateGraph）。

将 POST /api/chat 的前置编排链路建模为节点图：

    load_memory → retrieve → graph_expand → external_tools → build_messages

- load_memory：加载会话历史消息与用户背景记忆，生成基础 system prompt
- retrieve：向量库检索相关文献片段（Embedding 不可用时自动回退为空）
- graph_expand：引用图谱扩展检索（Phase G G2；开关 retrieval.graph_expand
  默认 false，开启时沿 paper_citations 1 跳扩展代表 chunk 并与向量召回
  RRF 融合，top_k 不变；无引用边/任何异常透传不回归）
- external_tools：外部 MCP 工具补充检索（Phase E E2；命中信号词且有可用
  arxiv.* 工具才触发，任何异常降级为纯本地路径，总耗时 10s 预算）
- build_messages：组装最终发给 LLM 的消息列表（system + history + RAG +
  外部检索补充 + 联网搜索提示 + Skill 角色注入），并判定是否启用联网搜索

流式生成（generate）刻意不放进图里：LangGraph 的流式语义与现有 SSE 契约
（delta / finished+citations / error 三种事件）差异较大，强行图内化会破坏契约，
因此生成仍由路由层驱动，本图只负责 LLM 调用前的上下文编排。

节点均为同步、近似纯函数（仅读取 db / 向量库 / 外部工具，不写库），外部依赖
（向量库、记忆管理、Skill、联网判断、MCP manager 单例）走模块级导入，
测试可直接 monkeypatch，无需真实 LLM / embedding / MCP server。
"""

import asyncio
import concurrent.futures
import re
import threading
from typing import Any, Dict, List, Optional, Tuple, TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy import bindparam, case, text
from sqlalchemy.orm import Session

from app.core.config import config
from app.core.logger import logger
from app.models import Chunk, Message, Paper
from app.services.memory_manager import MemoryManager
from app.services.retrieval import get_vector_store
from app.services.retrieval_pipeline import RetrievalPipeline
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

# Phase E E2：外部 MCP 工具触发信号词（小写匹配，见 spec 3.2）
EXTERNAL_TOOL_SIGNALS = ("arxiv", "论文检索", "最新研究", "未收录", "没有收录", "不在库中")
# 外部检索补充段头：注入 RAG 上下文；外部结果不进 citations（引用校验只覆盖本地 chunk）
EXTERNAL_CONTEXT_HEADER = "外部检索补充"
# 外部工具调用条数上限与节点总耗时预算（秒）
EXTERNAL_TOOL_LIMIT = 3
EXTERNAL_TOOL_BUDGET_SECONDS = 10

# 注入的历史消息条数上限与检索 top_k（与原 chat.py 保持一致）
HISTORY_LIMIT = 10
RETRIEVE_TOP_K = 5

# Phase G G2：图谱扩展每篇文献代表 chunk 上限（spec 3.2）与 chunk 级 RRF 常数 k
#（与 search.py _reciprocal_rank_fusion 的 k=60 对齐）；跳数走配置位
# retrieval.graph_expand_hops（默认 1 跳）
GRAPH_EXPAND_MAX_CHUNKS_PER_PAPER = 2
GRAPH_EXPAND_RRF_K = 60

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
    external_context: str            # 外部检索补充文本（空串 = 无补充，Phase E E2）
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
    """节点2：经共享管线检索相关文献片段；失败时回退为空列表。"""
    chunks: List[Dict[str, Any]] = []
    filters: Dict[str, Any] = {}
    if state.get("paper_id"):
        filters["paper_id"] = state["paper_id"]
    if state.get("user_message"):
        try:
            pipeline = RetrievalPipeline(
                state["db"], vector_store=get_vector_store()
            )
            chunks = pipeline.search(
                state["user_message"],
                top_k=RETRIEVE_TOP_K,
                filters=filters,
                profile=config.get("retrieval.chat_profile", "hybrid"),
                lexical_profile=config.get(
                    "retrieval.lexical_profile", "bm25-bilingual"
                ),
            )
        except Exception as e:
            logger.error(f"[agent_graph] 检索失败: {e}")
    return {"context_chunks": chunks}


# ---------- 引用图谱扩展（Phase G G2） ----------


def _expand_citation_neighbors(db: Session, hit_ids: set, hops: int) -> set:
    """沿 paper_citations 边做 hops 跳扩展（出边+入边双向），返回不含命中集本身的邻居 id。

    paper_citations 表结构归 G1（id / citing_id / cited_id / created_at），
    此处按契约用绑定参数原生 SQL 查询（宪法第 11 条）；表不存在等异常由调用方兜底。
    """
    stmt = text(
        "SELECT cited_id AS pid FROM paper_citations WHERE citing_id IN :ids "
        "UNION "
        "SELECT citing_id AS pid FROM paper_citations WHERE cited_id IN :ids"
    ).bindparams(bindparam("ids", expanding=True))
    seen = set(hit_ids)
    frontier = set(hit_ids)
    for _ in range(max(1, hops)):
        rows = db.execute(stmt, {"ids": list(frontier)}).fetchall()
        new_ids = {r.pid for r in rows} - seen
        if not new_ids:
            break
        seen |= new_ids
        frontier = new_ids
    return seen - set(hit_ids)


def _representative_chunks(db: Session, paper_ids: set) -> List[Dict[str, Any]]:
    """每篇扩展文献取至多 GRAPH_EXPAND_MAX_CHUNKS_PER_PAPER 个代表 chunk：
    abstract chunk 优先，其次按 chunk_index 升序补齐（spec 3.2）。

    返回字段与 retrieval.py 向量检索结果同构（source=graph 标记来源）；
    chunk_id 对齐向量库 p{pid}_c{chunk_index} 不变式（abstract 为 c-1），
    供 RRF 去重键使用。
    """
    abstract_first = case((Chunk.chunk_type == "abstract", 0), else_=1)
    rows = (
        db.query(Chunk, Paper.title, Paper.authors, Paper.year)
        .join(Paper, Paper.id == Chunk.paper_id)
        .filter(Chunk.paper_id.in_(paper_ids))
        .order_by(Chunk.paper_id, abstract_first, Chunk.chunk_index)
        .all()
    )
    chunks: List[Dict[str, Any]] = []
    per_paper: Dict[int, int] = {}
    for chunk_row, title, authors, year in rows:
        count = per_paper.get(chunk_row.paper_id, 0)
        if count >= GRAPH_EXPAND_MAX_CHUNKS_PER_PAPER:
            continue
        per_paper[chunk_row.paper_id] = count + 1
        chunks.append(
            {
                "chunk_id": f"p{chunk_row.paper_id}_c{chunk_row.chunk_index}",
                "paper_id": chunk_row.paper_id,
                "title": title,
                "authors": authors,
                "year": year,
                "content": chunk_row.content,
                "page_number": chunk_row.page_number,
                "chunk_type": chunk_row.chunk_type,
                "score": 0.0,
                "source": "graph",
            }
        )
    return chunks


def _rrf_chunk_key(item: Dict[str, Any]) -> Any:
    """chunk 级 RRF 去重键：优先 chunk_id，缺失时回退 (paper_id, page_number, content)。"""
    cid = item.get("chunk_id")
    if cid is not None:
        return ("chunk_id", cid)
    return ("meta", item.get("paper_id"), item.get("page_number"), item.get("content"))


def _rrf_fuse_chunks(
    vector_chunks: List[Dict[str, Any]],
    graph_chunks: List[Dict[str, Any]],
    top_k: int,
    k: int = GRAPH_EXPAND_RRF_K,
) -> List[Dict[str, Any]]:
    """chunk 级 RRF 融合（与 search.py _reciprocal_rank_fusion 同构，键由 paper_id 换为 chunk）。

    向量路先计入：同分时保留向量序（dict 插入序 + sorted 稳定性）；同一 chunk
    两路同现时分数叠加、保留首见元数据；合并后截断 top_k（spec：top_k 不变）。
    """
    scores: Dict[Any, float] = {}
    metas: Dict[Any, Dict[str, Any]] = {}

    def _add(results: List[Dict[str, Any]]) -> None:
        for rank, item in enumerate(results):
            key = _rrf_chunk_key(item)
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank + 1)
            if key not in metas:
                metas[key] = item

    _add(vector_chunks)
    _add(graph_chunks)
    sorted_keys = sorted(scores.keys(), key=lambda kk: scores[kk], reverse=True)
    return [metas[kk] for kk in sorted_keys[:top_k]]


def graph_expand(state: AgentState) -> Dict[str, Any]:
    """节点2.6（Phase G G2）：引用图谱扩展检索，位置在 retrieve 之后、external_tools 之前。

    开关 retrieval.graph_expand（config.yaml，默认 false，每次调用时读取）：
    关闭时返回空 dict，状态零变化（字节级不回归）。开启时：
    ① 取 retrieve 命中的去重 paper_id 集合；
    ② 沿 paper_citations 边扩展（retrieval.graph_expand_hops 配置位，默认 1 跳）；
    ③ 每篇扩展文献取至多 2 个代表 chunk（abstract 优先，否则首 chunk）；
    ④ 与向量召回做 chunk 级 RRF 融合，合并后 top_k 仍为 RETRIEVE_TOP_K。

    降级契约：无命中 / 无引用边 / 任何异常 → 透传 retrieve 结果不变，不阻断对话。
    """
    if not config.get("retrieval.graph_expand", False):
        return {}
    chunks = state.get("context_chunks") or []
    try:
        hit_ids = {c.get("paper_id") for c in chunks if c.get("paper_id") is not None}
        if not hit_ids:
            return {"context_chunks": chunks}
        hops = int(config.get("retrieval.graph_expand_hops", 1) or 1)
        expanded_ids = _expand_citation_neighbors(state["db"], hit_ids, hops)
        if not expanded_ids:
            return {"context_chunks": chunks}
        graph_chunks = _representative_chunks(state["db"], expanded_ids)
        if not graph_chunks:
            return {"context_chunks": chunks}
        return {"context_chunks": _rrf_fuse_chunks(chunks, graph_chunks, RETRIEVE_TOP_K)}
    except Exception as e:
        logger.warning(
            f"[graph_expand] 图谱扩展失败，透传向量召回结果: {type(e).__name__}: {e}"
        )
        return {"context_chunks": chunks}


# ---------- MCP client manager 单例（Phase E E2） ----------

_mcp_client_manager = None
_mcp_client_lock = threading.Lock()


def get_mcp_client_manager():
    """获取 MCPClientManager 单例（双检锁懒加载，模式同 get_vector_store）。

    配置来自 Config().get('mcp_servers', [])，缺省空列表 = 特性关闭。
    mcp_client 模块采用函数内延迟导入：未配置/模块不可用时不影响
    agent_graph 的导入与既有对话链路（spec 设计原则：默认关闭零行为变化）。
    """
    global _mcp_client_manager
    if _mcp_client_manager is None:
        with _mcp_client_lock:
            if _mcp_client_manager is None:
                from app.services.mcp_client import MCPClientManager

                servers = config.get("mcp_servers", []) or []
                _mcp_client_manager = MCPClientManager(servers)
    return _mcp_client_manager


def _has_external_signal(message: str) -> bool:
    """判断用户问题是否命中外部工具触发信号词（小写匹配）。"""
    lowered = (message or "").lower()
    return any(signal in lowered for signal in EXTERNAL_TOOL_SIGNALS)


async def _fetch_external_context(manager: Any, query: str) -> str:
    """异步段：discover → 选 arxiv.* 工具 → call_tool，整体 10s 预算。

    任何异常（含超时）都在此捕获并返回空串，不抛给上层（降级契约）。
    """
    try:
        async with asyncio.timeout(EXTERNAL_TOOL_BUDGET_SECONDS):
            tools = await manager.discover()
            arxiv_names = sorted(t.name for t in tools if t.name.startswith("arxiv."))
            if not arxiv_names:
                logger.info("[mcp-client] 命中信号但无 arxiv.* 可用工具，跳过外部补充")
                return ""
            # 初版：优先 arxiv.search，否则取排序后的第一个 arxiv.* 工具
            tool_name = "arxiv.search" if "arxiv.search" in arxiv_names else arxiv_names[0]
            result = await manager.call_tool(
                tool_name, {"query": query, "limit": EXTERNAL_TOOL_LIMIT}
            )
            return (result or "").strip()
    except Exception as e:
        logger.warning(
            f"[mcp-client] 外部工具 discover/call 失败，降级为纯本地路径: "
            f"{type(e).__name__}: {e}"
        )
        return ""


def external_tools(state: AgentState) -> Dict[str, Any]:
    """节点2.5（Phase E E2）：外部 MCP 工具补充检索，位置在 retrieve 之后、assemble 之前。

    触发条件（全部满足）：① 用户问题命中信号词；② manager.available()
    （有配置）；③ discover 发现 arxiv.* 工具。命中后以 query 原样 +
    limit=3 调用，结果封装为「外部检索补充」段写入 external_context。

    降级契约：任何一步异常/超时/空结果 → external_context=""，对话走纯
    本地路径，不阻断主链路；节点总耗时预算 10s（asyncio.timeout）。
    """
    user_message = state.get("user_message") or ""
    if not _has_external_signal(user_message):
        return {"external_context": ""}
    try:
        manager = get_mcp_client_manager()
        if not manager.available():
            return {"external_context": ""}
    except Exception as e:
        logger.warning(
            f"[mcp-client] 外部工具管理器不可用，降级为纯本地路径: "
            f"{type(e).__name__}: {e}"
        )
        return {"external_context": ""}

    # 同步节点内执行异步 manager 调用：路由层在 async 上下文中同步调图，
    # 本线程已有运行中的事件循环，不能直接 asyncio.run，
    # 故放独立线程跑新事件循环；future 超时作 asyncio.timeout 之外的护栏。
    executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="mcp-client"
    )
    try:
        future = executor.submit(
            asyncio.run, _fetch_external_context(manager, user_message)
        )
        text = future.result(timeout=EXTERNAL_TOOL_BUDGET_SECONDS + 2)
    except Exception as e:
        logger.warning(
            f"[mcp-client] 外部检索异常/超时，降级为纯本地路径: "
            f"{type(e).__name__}: {e}"
        )
        return {"external_context": ""}
    finally:
        executor.shutdown(wait=False)

    if not text:
        return {"external_context": ""}
    return {
        "external_context": (
            f"{EXTERNAL_CONTEXT_HEADER}（来自外部工具检索结果，仅供补充参考，"
            f"不属于本地文献库，不参与 [^i^] 引用编号）：\n\n{text}"
        )
    }


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

    # Phase E E2：外部检索补充（独立 system 消息，紧跟 RAG 上下文之后；
    # 外部结果不进 context_chunks，citations 结构与 SSE 帧不变）
    external_context = state.get("external_context") or ""
    if external_context:
        messages.append({"role": "system", "content": external_context})

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
    """构建并编译对话编排图：load_memory → retrieve → graph_expand → external_tools → build_messages。"""
    graph = StateGraph(AgentState)
    graph.add_node("load_memory", load_memory)
    graph.add_node("retrieve", retrieve)
    graph.add_node("graph_expand", graph_expand)
    graph.add_node("external_tools", external_tools)
    graph.add_node("build_messages", build_messages)
    graph.add_edge(START, "load_memory")
    graph.add_edge("load_memory", "retrieve")
    graph.add_edge("retrieve", "graph_expand")
    graph.add_edge("graph_expand", "external_tools")
    graph.add_edge("external_tools", "build_messages")
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
