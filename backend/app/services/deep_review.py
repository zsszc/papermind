"""深度综述服务（Phase F F1）：「规划 → 分派 → 汇总」三段链路。

依赖决策（spec §3.1 第一道工序，同 Phase D 模式）：实测 deepagents 0.7.4
会拖入 httpx 0.28.1 与 pydantic 2.13.4，与宪法 §16 锁定栈冲突（openai 1.12
+ httpx≥0.28 构造 client 即 TypeError: unexpected keyword argument 'proxies'），
故不引入；本模块基于现有栈手写三段契约——库选型是实现细节，行为契约不变。

三段行为契约（spec §3.1）：

    plan(topic, n_papers?) -> List[SubQuestion]   # LLM 拆 3-5 个子问题（硬上限 5）
    execute(sub_question) -> SubAnswer            # 现有 retrieve + llm 生成，带本地引用
    synthesize(topic, sub_answers) -> Review      # LLM 汇总结构化综述，保留 [^n^] 引用

- 中间产物全部内存态，不落库；
- 单个子问题失败不阻塞整体：execute 任何失败（检索异常 / LLM 出错 / 零检索）
  一律降级为「该子问题检索不足」标记返回，绝不抛出；plan 与 synthesize 失败
  抛 DeepReviewError，由路由层转为错误事件；
- 所有 LLM 调用经 llm_service（宪法 §8 唯一入口，Langfuse 自动观测）。
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app.core.logger import logger
from app.services.agent_graph import build_rag_prompt
from app.services.llm import llm_service
from app.services.retrieval import get_vector_store

# 子问题数硬上限（spec §7：5 子问题 × 生成 60-120s，防长任务失控）
MAX_SUB_QUESTIONS = 5
# 每子问题检索片段数（与主对话 agent_graph.RETRIEVE_TOP_K 对齐）
SUB_QUESTION_TOP_K = 5
# 单点失败 / 零检索的降级节标记（spec §3.1 原文用词）
INSUFFICIENT_NOTICE = "该子问题检索不足。"
# llm_service 带内错误串前缀（spec llm.md 3.7 判别约定）
_LLM_ERROR_PREFIX = "[调用 LLM 出错:"
# 引用标记正则：[^n^]，n 为正整数（局部/全局重编号用）
_CITATION_MARKER_PATTERN = re.compile(r"\[\^(\d+)\^\]")

# plan 系统提示：约束 LLM 只输出 questions JSON
PLAN_SYSTEM_PROMPT = """你是 PaperMind 的文献综述规划助手。用户要对本地文献库中的某个研究方向做综述。
请把综述主题拆解为 3-5 个可独立检索回答的子问题（覆盖该方向的不同侧面：方法、数据、指标、对比、局限等）。
只输出 JSON，格式：{"questions": ["子问题1", "子问题2", ...]}，不要输出任何其他内容。"""

# execute 系统提示：子问题作答规则（引用约定与 build_rag_prompt 一致）
SUB_ANSWER_SYSTEM_PROMPT = """你是 PaperMind 的文献综述子任务助手。请基于提供的本地文献片段回答给定子问题。
规则：1. 只依据片段内容作答，片段不足时明确说明；2. 引用片段时用 [^i^] 形式标注（i 为片段编号）；3. 回答专业、简洁，使用中文。"""

# synthesize 系统提示：结构化汇总规则（保留全局 [^n^] 标记、如实标注证据缺口）
SYNTHESIZE_SYSTEM_PROMPT = """你是 PaperMind 的文献综述汇总助手。给定综述主题与若干子问题答案（含全局统一的 [^n^] 引用标记与引用清单），
请汇总为一篇结构化综述：引言（主题背景与综述范围）、按子问题分节展开、结论（共识与研究空白）。
规则：1. 原样保留子答案中的 [^n^] 引用标记，编号不得改动或新增；
2. 标记「该子问题检索不足」的小节如实说明证据缺口，禁止编造；
3. 末尾按全局引用清单列出引用文献；4. 使用中文，学术文体。"""


class DeepReviewError(Exception):
    """深度综述链路不可恢复错误（plan / synthesize 失败），由路由层转为错误事件。"""


@dataclass
class SubQuestion:
    """plan 产出的子问题（index 为 1-based 序号）。"""

    index: int
    question: str


@dataclass
class SubAnswer:
    """execute 产出的子问题答案。

    ok=False 时 answer 为 INSUFFICIENT_NOTICE 降级标记，chunks 为空，
    error 记录失败原因（仅日志/排查用，不进用户文案）。
    """

    index: int
    question: str
    answer: str
    chunks: List[Dict[str, Any]] = field(default_factory=list)
    ok: bool = True
    error: Optional[str] = None


@dataclass
class Review:
    """synthesize 产出的结构化综述。

    content 为综述全文（引言/分节/结论，含全局 [^n^] 引用标记）；
    citations 为全局引用表（各子答案检索片段按序聚合，编号与 content 中
    [^n^] 一一对应）；sub_answers 保留中间产物（内存态，不落库）。
    """

    topic: str
    content: str
    citations: List[Dict[str, Any]] = field(default_factory=list)
    sub_answers: List[SubAnswer] = field(default_factory=list)


def _parse_questions(raw: str) -> List[str]:
    """解析 plan 的 LLM 输出为子问题字符串列表（硬上限 MAX_SUB_QUESTIONS）。

    LLM 带内错误串 / 非 JSON / 结构无法识别 / 空列表一律抛 DeepReviewError；
    兼容裸 JSON 数组与 {"questions": [...]} 两种形态；非字符串项与空白项剔除。
    """
    if not raw or raw.startswith(_LLM_ERROR_PREFIX):
        raise DeepReviewError(f"plan 失败：LLM 调用出错（{raw[:80]}）")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise DeepReviewError(f"plan 失败：LLM 输出不是合法 JSON（{e}）") from e
    if isinstance(data, dict):
        data = data.get("questions") or data.get("sub_questions") or []
    if not isinstance(data, list):
        raise DeepReviewError("plan 失败：LLM 输出 JSON 结构无法识别（期望 questions 数组）")
    questions = [q.strip() for q in data if isinstance(q, str) and q.strip()]
    if not questions:
        raise DeepReviewError("plan 失败：LLM 未给出任何有效子问题")
    return questions[:MAX_SUB_QUESTIONS]


async def plan(topic: str, n_papers: Optional[int] = None) -> List[SubQuestion]:
    """第一段：LLM 把综述主题拆为 3-5 个子问题（硬上限 5），失败抛 DeepReviewError。"""
    scope_hint = f"，综述范围约 {n_papers} 篇文献" if n_papers else ""
    messages = [
        {"role": "system", "content": PLAN_SYSTEM_PROMPT},
        {"role": "user", "content": f"综述主题：{topic}{scope_hint}"},
    ]
    raw = await llm_service.chat_completion(
        messages, json_mode=True, trace_metadata={"stage": "deep_review_plan"}
    )
    questions = _parse_questions(raw)
    logger.info(f"[deep_review] plan 完成：主题「{topic[:30]}」拆出 {len(questions)} 个子问题")
    return [SubQuestion(index=i, question=q) for i, q in enumerate(questions, start=1)]


async def execute(sub_question: SubQuestion) -> SubAnswer:
    """第二段：单个子问题走现有检索 + LLM 生成，带本地引用。

    降级契约（绝不抛出）：检索异常 / 向量库不可用 / 零检索片段 / LLM 出错
    一律返回 ok=False、answer=INSUFFICIENT_NOTICE 的 SubAnswer；
    零检索时跳过 LLM 调用，避免无依据编造。
    """
    base = {"index": sub_question.index, "question": sub_question.question}
    chunks: List[Dict[str, Any]] = []
    try:
        store = get_vector_store()
        if store.available():
            chunks = store.search(
                query=sub_question.question, top_k=SUB_QUESTION_TOP_K, filters={}
            )
    except Exception as e:
        logger.error(f"[deep_review] 子问题 {sub_question.index} 检索失败: {e}")
        return SubAnswer(
            **base, answer=INSUFFICIENT_NOTICE, chunks=[], ok=False, error=str(e)
        )
    if not chunks:
        logger.info(f"[deep_review] 子问题 {sub_question.index} 零检索片段，跳过 LLM 生成")
        return SubAnswer(**base, answer=INSUFFICIENT_NOTICE, chunks=[], ok=False)

    messages = [
        {"role": "system", "content": SUB_ANSWER_SYSTEM_PROMPT},
        # 引用格式沿用主对话 build_rag_prompt 的 [i] 编号 + [^i^] 标注约定
        {"role": "system", "content": build_rag_prompt(sub_question.question, chunks)},
    ]
    try:
        answer = await llm_service.chat_completion(
            messages,
            trace_metadata={
                "stage": "deep_review_execute",
                "sub_index": sub_question.index,
            },
        )
    except Exception as e:
        # chat_completion 契约上不抛异常（返回带内错误串），此处为防御性兜底
        logger.error(f"[deep_review] 子问题 {sub_question.index} LLM 调用异常: {e}")
        return SubAnswer(
            **base, answer=INSUFFICIENT_NOTICE, chunks=[], ok=False, error=str(e)
        )
    if answer.startswith(_LLM_ERROR_PREFIX):
        logger.warning(
            f"[deep_review] 子问题 {sub_question.index} LLM 生成失败: {answer[:80]}"
        )
        return SubAnswer(
            **base, answer=INSUFFICIENT_NOTICE, chunks=[], ok=False, error=answer
        )
    return SubAnswer(**base, answer=answer, chunks=chunks, ok=True)


def _build_synthesis_context(
    sub_answers: List[SubAnswer],
) -> Tuple[str, List[Dict[str, Any]]]:
    """聚合各子答案为汇总上下文：局部 [^n^] 重编号为全局编号，返回 (上下文, 全局引用表)。

    编号规则：按子答案顺序累计偏移，sa_k 的局部 [^i^] → 全局 [^偏移+i^]；
    越界局部标记（LLM 编造、超出该节片段数）直接剔除，避免污染全局编号空间。
    末尾附全局引用清单（标题 + 页码），供 LLM 写参考文献段。
    """
    citations: List[Dict[str, Any]] = []
    blocks: List[str] = []
    for sa in sub_answers:
        offset = len(citations)
        citations.extend(sa.chunks)
        local_count = len(sa.chunks)

        def _remap(match: "re.Match[str]", _offset=offset, _n=local_count) -> str:
            local = int(match.group(1))
            if 1 <= local <= _n:
                return f"[^{_offset + local}^]"
            return ""

        answer = _CITATION_MARKER_PATTERN.sub(_remap, sa.answer)
        blocks.append(f"### 子问题 {sa.index}：{sa.question}\n{answer}")

    if citations:
        ref_lines = []
        for i, c in enumerate(citations, start=1):
            line = f"[{i}] {c.get('title') or '未知文献'}"
            if c.get("page_number"):
                line += f" 第{c['page_number']}页"
            ref_lines.append(line)
        blocks.append("### 全局引用清单\n" + "\n".join(ref_lines))
    return "\n\n".join(blocks), citations


async def synthesize(topic: str, sub_answers: List[SubAnswer]) -> Review:
    """第三段：LLM 汇总结构化综述（引言/分节/结论），保留全局 [^n^] 引用。

    送入 LLM 前先把各子答案的局部引用编号重编号为全局编号（见
    _build_synthesis_context）；LLM 出错抛 DeepReviewError（与 plan 同一
    处理路径，由路由层转错误事件）。
    """
    context, citations = _build_synthesis_context(sub_answers)
    messages = [
        {"role": "system", "content": SYNTHESIZE_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"综述主题：{topic}\n\n以下是各子问题的答案（引用编号已全局统一）：\n\n{context}",
        },
    ]
    content = await llm_service.chat_completion(
        messages, trace_metadata={"stage": "deep_review_synthesize"}
    )
    if not content or content.startswith(_LLM_ERROR_PREFIX):
        raise DeepReviewError(f"synthesize 失败：LLM 调用出错（{content[:80]}）")
    logger.info(
        f"[deep_review] synthesize 完成：主题「{topic[:30]}」，全局引用 {len(citations)} 条"
    )
    return Review(
        topic=topic, content=content, citations=citations, sub_answers=list(sub_answers)
    )
