import asyncio
import importlib
import json
import threading
from dataclasses import asdict, is_dataclass
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Conversation, MemorySummary, Message, Paper
from app.schemas import (
    ChatRequest,
    ConversationResponse,
    DeepReviewRequest,
    ImageAnalysisRequest,
    RegenerateRequest,
)
from app.core.config import config
from app.core.logger import logger
from app.services.llm import LLMGenerationError, is_llm_error_response, llm_service
from app.services.retrieval import get_vector_store
from app.services.retrieval_pipeline import RetrievalPipeline
from app.services.memory_manager import MemoryManager
from app.services.image_analyzer import image_analyzer_service
from app.services.skills import list_skills
from app.services.generation_guardrails import (
    select_cited_chunks,
    verify_citations_detailed,
)
from app.services.agent_graph import (
    NO_RETRIEVAL_GUARD,
    SYSTEM_PROMPT,
    build_rag_prompt as _build_rag_prompt,
    run_pre_orchestration,
    verify_citations,
)

router = APIRouter()
_GENERATION_ERROR_MESSAGE = "AI 服务暂时不可用，请稍后重试"
_ACTIVE_REGENERATIONS: set[tuple[int, int]] = set()
_ACTIVE_REGENERATIONS_LOCK = threading.Lock()


def _claim_regeneration(key: tuple[int, int]) -> bool:
    """进程内同目标只允许一个在途 regenerate，避免重复模型费用。"""
    with _ACTIVE_REGENERATIONS_LOCK:
        if key in _ACTIVE_REGENERATIONS:
            return False
        _ACTIVE_REGENERATIONS.add(key)
        return True


def _release_regeneration(key: tuple[int, int]) -> None:
    with _ACTIVE_REGENERATIONS_LOCK:
        _ACTIVE_REGENERATIONS.discard(key)


# ---------- Phase F F2：深度综述长任务（SSE 帧与 Guardrails 复用 /api/chat 模式） ----------
_REVIEW_DELTA_CHUNK_SIZE = 512  # 综述全文回放分块大小（字符）：保证长综述产生多个 delta 帧


def _sse_frame(payload: dict) -> str:
    """SSE 帧：data: <json>\\n\\n（与 /api/chat 相同帧格式，ensure_ascii=False）。"""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _generation_error_frame(conversation_id: int, error_code: str) -> str:
    """生成失败的唯一公开终态；禁止携带上游异常正文。"""
    return _sse_frame({
        "error": _GENERATION_ERROR_MESSAGE,
        "error_code": error_code,
        "conversation_id": conversation_id,
    })


def _field(obj, key, default=None):
    """兼容 dict 与对象属性两种取值形态（F1 服务产物的具体类型归其实现自定）。"""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _jsonable(obj):
    """把 F1 的 SubQuestion / chunk 产物转成 JSON 可序列化形态。

    容忍 str / dict / list / pydantic 模型（model_dump）/ dataclass；其余回退 str()。
    """
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(x) for x in obj]
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    return str(obj)


def _local_chunks(citations) -> list:
    """citations 仅本地 chunk（同时具备 source 与 paper_id），外部来源不进（spec 3.2）。"""
    return [
        c for c in (citations or [])
        if _field(c, "source") and _field(c, "paper_id") is not None
    ]


def _compat_verification(report: dict) -> dict:
    """SSE 继续暴露 Phase C 四字段契约，详细指标留给离线 Gate。"""
    return {key: report[key] for key in ("total", "valid", "removed", "verified")}


def _stored_citations(citations: list, verification: dict) -> list:
    """构建可持久化的实际引用快照。"""
    return [{
        "source": _field(item, "source") or _field(item, "chunk_id"),
        "paper_id": _field(item, "paper_id"),
        "title": _field(item, "title"),
        "authors": _field(item, "authors"),
        "year": _field(item, "year"),
        "page_number": _field(item, "page_number"),
        "content": _field(item, "content"),
        "verified": verification["verified"],
        "removed": verification["removed"],
    } for item in citations]


@router.get("/conversations", response_model=List[ConversationResponse])
def list_conversations(db: Session = Depends(get_db)):
    conversations = db.query(Conversation).order_by(Conversation.updated_at.desc()).all()
    return conversations


@router.post("/conversations", response_model=ConversationResponse)
def create_conversation(db: Session = Depends(get_db)):
    conv = Conversation(title="新对话", message_count=0)
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return conv


@router.get("/conversations/{conversation_id}/history")
def get_history(conversation_id: int, db: Session = Depends(get_db)):
    conv = db.query(Conversation).filter(Conversation.id == conversation_id).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    messages = (
        db.query(Message)
        .filter(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.asc())
        .all()
    )
    return {
        "conversation": conv,
        "messages": [
            {
                "id": m.id,
                "role": m.role,
                "content": m.content,
                "citations": m.citations,
                "revision": m.revision,
            }
            for m in messages
        ],
    }


@router.delete("/conversations/{conversation_id}", status_code=204)
def delete_conversation(conversation_id: int, db: Session = Depends(get_db)):
    conv = db.query(Conversation).filter(Conversation.id == conversation_id).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    db.query(MemorySummary).filter(
        MemorySummary.source_conversation_id == conversation_id
    ).update({MemorySummary.source_conversation_id: None}, synchronize_session=False)
    db.delete(conv)
    db.commit()
    return


@router.delete("/conversations/{conversation_id}/messages/{message_id}", status_code=204)
def delete_messages_from(
    conversation_id: int,
    message_id: int,
    db: Session = Depends(get_db),
):
    conv = db.query(Conversation).filter(Conversation.id == conversation_id).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    all_messages = (
        db.query(Message)
        .filter(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.asc())
        .all()
    )

    target_index = None
    for i, m in enumerate(all_messages):
        if m.id == message_id:
            target_index = i
            break
    if target_index is None:
        raise HTTPException(status_code=404, detail="Message not found")

    for m in all_messages[target_index:]:
        db.delete(m)
    # 回溯修正会话计数：message_count 须等于删除后实际剩余消息数（Batch7b-F11）
    conv.message_count = target_index
    db.commit()
    return


@router.post("/conversations/{conversation_id}/messages/{message_id}/regenerate")
async def regenerate_message(
    conversation_id: int,
    message_id: int,
    request: RegenerateRequest,
    db: Session = Depends(get_db),
):
    conv = db.query(Conversation).filter(Conversation.id == conversation_id).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    target_msg = (
        db.query(Message)
        .filter(Message.id == message_id, Message.conversation_id == conversation_id)
        .first()
    )
    if not target_msg or target_msg.role != "assistant":
        raise HTTPException(status_code=404, detail="Assistant message not found")
    if target_msg.revision != request.expected_revision:
        raise HTTPException(status_code=409, detail="Message revision conflict")

    regeneration_key = (conversation_id, message_id)
    if not _claim_regeneration(regeneration_key):
        raise HTTPException(status_code=409, detail="Message regeneration already active")

    try:
        all_messages = (
            db.query(Message)
            .filter(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.asc())
            .all()
        )
        target_index = next(
            (i for i, message in enumerate(all_messages) if message.id == message_id),
            None,
        )
        if target_index is None or target_index == 0:
            raise HTTPException(
                status_code=400,
                detail="No user message before this assistant message",
            )

        prev_user_msg = all_messages[target_index - 1]
        if prev_user_msg.role != "user":
            raise HTTPException(
                status_code=400,
                detail="Previous message is not a user message",
            )

        history_messages = all_messages[:target_index]
        query = prev_user_msg.content

        # 重新生成与主聊天共用同一检索策略，避免两条生产路径排序漂移。
        retrieved = []
        try:
            retrieved = RetrievalPipeline(
                db, vector_store=get_vector_store()
            ).search(
                query,
                top_k=5,
                filters={},
                profile=config.get("retrieval.chat_profile", "hybrid"),
                lexical_profile=config.get(
                    "retrieval.lexical_profile", "bm25-bilingual"
                ),
            )
        except Exception as e:
            logger.error("[regenerate] 检索失败: %s", type(e).__name__)

        memory_mgr = MemoryManager(db)
        memory_context = memory_mgr.build_memory_context()
        system_content = SYSTEM_PROMPT
        if memory_context:
            system_content += f"\n\n以下是关于用户的背景记忆，请在回答时参考：\n\n{memory_context}"

        messages = [{"role": "system", "content": system_content}]
        for history_message in history_messages:
            messages.append({
                "role": history_message.role,
                "content": history_message.content,
            })
        if retrieved:
            rag_context = _build_rag_prompt(query, retrieved)
            messages.append({"role": "system", "content": rag_context})
        else:
            messages[0]["content"] += f"\n\n{NO_RETRIEVAL_GUARD}"
    except Exception:
        _release_regeneration(regeneration_key)
        raise

    async def event_stream():
        full_content = ""
        try:
            # regenerate 无请求体、不含联网开关，固定关闭（确定性重生成，见 specs/backend/routers/chat.md 3.6）
            async for delta in llm_service.chat_stream(messages, enable_web_search=False):
                if is_llm_error_response(delta):
                    yield _generation_error_frame(
                        conversation_id, "llm_generation_failed"
                    )
                    return
                full_content += delta
                yield f"data: {json.dumps({'delta': delta, 'finished': False, 'conversation_id': conversation_id}, ensure_ascii=False)}\n\n"
                # 让出控制权，使客户端断开后能触发 CancelledError
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            logger.info(f"[regenerate] conversation {conversation_id} message {message_id} cancelled")
            return
        except Exception as e:
            logger.error(
                "[regenerate] 生成失败 conversation=%s message=%s type=%s",
                conversation_id,
                message_id,
                type(e).__name__,
            )
            yield _generation_error_frame(conversation_id, "llm_generation_failed")
            return

        try:
            if not full_content.strip():
                yield _generation_error_frame(conversation_id, "empty_generation")
                return
            cleaned_content, detailed_report, cited_ids = verify_citations_detailed(
                full_content, retrieved
            )
            if not cleaned_content.strip():
                yield _generation_error_frame(conversation_id, "empty_generation")
                return
            verification = _compat_verification(detailed_report)
            cited_chunks = select_cited_chunks(retrieved, cited_ids)
            finish_frame = _sse_frame({
                "delta": "",
                "finished": True,
                "conversation_id": conversation_id,
                "content": cleaned_content,
                "citations": cited_chunks,
                "verification": verification,
                "revision": request.expected_revision + 1,
            })

            # 正文、引用与 revision 条件更新；跨进程竞争或删除不得假成功。
            from app.database import SessionLocal
            with SessionLocal() as new_db:
                updated = (
                    new_db.query(Message)
                    .filter(
                        Message.id == message_id,
                        Message.conversation_id == conversation_id,
                        Message.role == "assistant",
                        Message.revision == request.expected_revision,
                    )
                    .update(
                        {
                            Message.content: cleaned_content,
                            Message.citations: _stored_citations(
                                cited_chunks, verification
                            ),
                            Message.revision: Message.revision + 1,
                        },
                        synchronize_session=False,
                    )
                )
                if updated != 1:
                    exists = new_db.query(Message.id).filter(
                        Message.id == message_id,
                        Message.conversation_id == conversation_id,
                        Message.role == "assistant",
                    ).first()
                    new_db.rollback()
                    error_code = (
                        "regenerate_conflict"
                        if exists
                        else "regenerate_target_missing"
                    )
                    yield _generation_error_frame(conversation_id, error_code)
                    return
                new_db.commit()
            yield finish_frame
        except Exception as e:
            logger.error(
                "[regenerate] 终态提交失败 conversation=%s message=%s type=%s",
                conversation_id,
                message_id,
                type(e).__name__,
            )
            yield _generation_error_frame(conversation_id, "finalization_failed")

    async def guarded_event_stream():
        try:
            async for frame in event_stream():
                yield frame
        finally:
            _release_regeneration(regeneration_key)

    return StreamingResponse(
        guarded_event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/analyze-image")
async def analyze_image(
    file: UploadFile = File(...),
    question: str = Form("请描述这张图片的内容，并解释其在学术论文中可能的含义。"),
):
    """上传图片进行分析（支持截图、表格、公式等）。"""
    image_bytes = await file.read()
    if not image_bytes:
        raise HTTPException(status_code=400, detail="图片内容为空")
    # 图片经 base64 内联进 prompt，必须限制体积防内存/费用放大（宪法第 13 条）
    if len(image_bytes) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="图片大小超过 10MB 上限")

    filename = file.filename or "image.jpg"

    async def event_stream():
        full_content = ""
        try:
            async for delta in image_analyzer_service.analyze_stream(
                image_bytes=image_bytes,
                filename=filename,
                question=question,
            ):
                full_content += delta
                yield _sse_frame({"delta": delta, "finished": False})
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            logger.info("[image-analyzer] streaming cancelled")
            return
        except Exception as e:
            logger.error("[image-analyzer] 流式失败: %s", type(e).__name__)
            yield _sse_frame({
                "error": "图片分析失败，请稍后重试",
                "error_code": "image_analysis_failed",
            })
            return
        if not full_content.strip():
            yield _sse_frame({
                "error": "图片分析失败，请稍后重试",
                "error_code": "empty_image_analysis",
            })
            return
        yield _sse_frame({
            "delta": "",
            "finished": True,
            "content": full_content,
        })

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("")
async def chat(request: ChatRequest, db: Session = Depends(get_db)):
    logger.info(
        "[chat] 收到请求: conversation_id=%s message_chars=%d",
        request.conversation_id,
        len(request.message),
    )
    # 处理会话
    if request.conversation_id:
        conv = db.query(Conversation).filter(Conversation.id == request.conversation_id).first()
        if not conv:
            raise HTTPException(status_code=404, detail="Conversation not found")
    else:
        conv = Conversation(title=request.message[:30] or "新对话", message_count=0)
        db.add(conv)
        db.flush()

    # 保存用户消息
    user_msg = Message(
        conversation_id=conv.id,
        role="user",
        content=request.message,
        citations=[],
    )
    db.add(user_msg)
    db.flush()

    # 更新记忆（异步后台触发，不阻塞回复）
    memory_mgr = MemoryManager(db)
    try:
        await memory_mgr.update_short_term_memory(conv.id)
    except Exception as e:
        logger.error("[chat] 记忆更新失败: %s", type(e).__name__)

    # LangGraph 前置编排：load_memory → retrieve → build_messages
    # 产出 messages / 检索片段（引用）/ 联网开关，流式生成与 SSE 发送逻辑维持不变
    state = run_pre_orchestration(
        db=db,
        conversation_id=conv.id,
        user_message=request.message,
        skill=request.skill,
        paper_id=request.paper_id,
        enable_web_search=bool(request.enable_web_search),
    )
    messages = state["messages"]
    retrieved = state["context_chunks"]
    enable_web_search = state["web_search_enabled"]

    # 这里只提交已经存在的 user；assistant 成功落库时再在同一事务更新计数。
    conv.message_count = state["history_total"]
    db.commit()

    if request.stream is False:
        try:
            raw_content = await llm_service.chat_completion(messages)
            if is_llm_error_response(raw_content) or not raw_content.strip():
                raise LLMGenerationError("empty_or_failed_completion")
            content, detailed_report, cited_ids = verify_citations_detailed(
                raw_content, retrieved
            )
            if not content.strip():
                raise LLMGenerationError("empty_final_content")
            verification = _compat_verification(detailed_report)
            cited_chunks = select_cited_chunks(retrieved, cited_ids)
            assistant_msg = Message(
                conversation_id=conv.id,
                role="assistant",
                content=content,
                citations=_stored_citations(cited_chunks, verification),
            )
            db.add(assistant_msg)
            db.flush()
            conv.message_count = db.query(Message).filter(
                Message.conversation_id == conv.id
            ).count()
            db.commit()
            return {
                "conversation_id": conv.id,
                "content": content,
                "citations": cited_chunks,
                "verification": verification,
            }
        except Exception as e:
            db.rollback()
            logger.error(
                "[chat] 非流式生成失败 conversation=%s type=%s",
                conv.id,
                type(e).__name__,
            )
            raise HTTPException(status_code=503, detail=_GENERATION_ERROR_MESSAGE)

    conv_id = conv.id
    async def event_stream():
        full_content = ""
        cleaned_content = ""
        verification = {"total": 0, "valid": 0, "removed": 0, "verified": True}
        cited_chunks = []
        try:
            async for delta in llm_service.chat_stream(messages, enable_web_search=enable_web_search):
                if is_llm_error_response(delta):
                    yield _generation_error_frame(conv_id, "llm_generation_failed")
                    return
                full_content += delta
                yield f"data: {json.dumps({'delta': delta, 'finished': False, 'conversation_id': conv_id}, ensure_ascii=False)}\n\n"
                # 让出控制权，使客户端断开后能触发 CancelledError
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            logger.info(f"[chat] conversation {conv_id} streaming cancelled")
            return
        except Exception as e:
            logger.error(
                "[chat] 流式生成失败 conversation=%s type=%s",
                conv_id,
                type(e).__name__,
            )
            yield _generation_error_frame(conv_id, "llm_generation_failed")
            return

        if not full_content.strip():
            yield _generation_error_frame(conv_id, "empty_generation")
            return

        # 保存助手回复（使用新的 Session，避免原 Session 已关闭）
        try:
            # Phase C C1：落库前校验引用忠实度——剔除越界 [^n^] 标记，
            # citations 附 verified / removed（不阻塞返回，先观测）
            cleaned_content, detailed_report, cited_ids = verify_citations_detailed(
                full_content, retrieved
            )
            if not cleaned_content.strip():
                yield _generation_error_frame(conv_id, "empty_generation")
                return
            verification = _compat_verification(detailed_report)
            cited_chunks = select_cited_chunks(retrieved, cited_ids)
            finish_frame = _sse_frame({
                "delta": "",
                "finished": True,
                "conversation_id": conv_id,
                "content": cleaned_content,
                "citations": cited_chunks,
                "verification": verification,
            })
            from app.database import SessionLocal
            with SessionLocal() as new_db:
                conv_row = new_db.query(Conversation).filter(
                    Conversation.id == conv_id
                ).first()
                if not conv_row:
                    raise RuntimeError("conversation_missing")
                assistant_msg = Message(
                    conversation_id=conv_id,
                    role="assistant",
                    content=cleaned_content,
                    citations=_stored_citations(cited_chunks, verification),
                )
                new_db.add(assistant_msg)
                new_db.flush()
                conv_row.message_count = new_db.query(Message).filter(
                    Message.conversation_id == conv_id
                ).count()
                new_db.commit()
            yield finish_frame
        except Exception as e:
            logger.error(
                "[chat] 终态提交失败 conversation=%s type=%s",
                conv_id,
                type(e).__name__,
            )
            yield _generation_error_frame(conv_id, "finalization_failed")

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/deep-review")
async def deep_review(request: DeepReviewRequest, db: Session = Depends(get_db)):
    """深度综述长任务（Phase F F2）：规划 → 分派 → 汇总，SSE 流式。

    事件序列：{type:"plan", questions:[...]} → 多个 {delta} → {finished, citations}；
    plan / synthesize 失败 → {error} 帧；单个子问题失败降级、不阻塞整体。
    服务层 services/deep_review.py（F1）按契约调用：plan(topic) /
    execute(sub_question, db=...) / synthesize(topic, sub_answers)。
    """
    logger.info(
        "[deep-review] 收到请求: conversation_id=%s topic_chars=%d",
        request.conversation_id,
        len(request.topic),
    )
    topic = request.topic.strip()
    if not topic:
        raise HTTPException(status_code=422, detail="topic 不能为空")

    existing_conv_id = request.conversation_id
    if existing_conv_id is not None:
        conv = db.query(Conversation).filter(Conversation.id == existing_conv_id).first()
        if not conv:
            raise HTTPException(status_code=404, detail="Conversation not found")

    async def event_stream():
        try:
            # 延迟导入：F1 服务模块并行开发中，顶层导入会让本路由强依赖其落地时序；
            # 接口兼容 deep_review_service 单例与模块级函数两种暴露方式
            _dr = importlib.import_module("app.services.deep_review")
            svc = getattr(_dr, "deep_review_service", _dr)

            # 1) 规划：LLM 拆子问题；plan 失败 → 错误事件（spec 3.1）
            try:
                questions = await svc.plan(topic)
            except Exception as e:
                logger.error("[deep-review] 规划失败: %s", type(e).__name__)
                yield _sse_frame({
                    "error": "深度综述规划失败，请稍后重试",
                    "error_code": "deep_review_plan_failed",
                    "conversation_id": existing_conv_id,
                })
                return
            yield _sse_frame({
                "type": "plan",
                "questions": [_jsonable(q) for q in questions],
                "conversation_id": existing_conv_id,
            })
            # 让出控制权，使客户端断开后能触发 CancelledError
            await asyncio.sleep(0)

            # 2) 分派：逐子问题执行；单个失败降级为占位子答案，不阻塞整体
            sub_answers = []
            for q in questions:
                try:
                    from app.database import SessionLocal
                    with SessionLocal() as work_db:
                        sub_answers.append(await svc.execute(q, db=work_db))
                except Exception as e:
                    logger.error(
                        "[deep-review] 子问题执行失败，降级处理: %s",
                        type(e).__name__,
                    )
                    sub_answers.append({
                        "question": _jsonable(q),
                        "answer": "该子问题检索不足",
                        "citations": [],
                    })

            # 3) 汇总：生成结构化综述；失败 → 错误事件
            try:
                review = await svc.synthesize(topic, sub_answers)
            except Exception as e:
                logger.error("[deep-review] 汇总失败: %s", type(e).__name__)
                yield _sse_frame({
                    "error": "深度综述汇总失败，请稍后重试",
                    "error_code": "deep_review_synthesis_failed",
                    "conversation_id": existing_conv_id,
                })
                return

            full_content = review if isinstance(review, str) else (
                _field(review, "content") or _field(review, "text") or ""
            )
            if not isinstance(full_content, str) or not full_content.strip():
                yield _generation_error_frame(existing_conv_id, "empty_generation")
                return
            # 引用优先取 Review 汇总产物；缺省回退为各子答案引用聚合（兼容 citations/chunks 两种命名）；
            # 仅保留本地 chunk
            citations = _local_chunks(
                _field(review, "citations")
                or [
                    c for sa in sub_answers
                    for c in (_field(sa, "citations") or _field(sa, "chunks") or [])
                ]
            )

            # 4) delta 帧：综述全文分块回放（拼接全部 delta == 落库全文，与 /api/chat 不变量一致）
            for i in range(0, len(full_content), _REVIEW_DELTA_CHUNK_SIZE):
                yield _sse_frame({
                    "delta": full_content[i:i + _REVIEW_DELTA_CHUNK_SIZE],
                    "finished": False,
                    "conversation_id": existing_conv_id,
                })
                await asyncio.sleep(0)

            # 5) Guardrails 后正文仍须非空；成功时才在单一事务内创建/更新会话。
            cleaned_content, verify_report = verify_citations(full_content, citations)
            if not cleaned_content.strip():
                yield _generation_error_frame(existing_conv_id, "empty_generation")
                return

            from app.database import SessionLocal
            with SessionLocal() as new_db:
                if existing_conv_id is None:
                    conv_row = Conversation(title=topic[:30] or "新对话", message_count=0)
                    new_db.add(conv_row)
                    new_db.flush()
                else:
                    conv_row = new_db.query(Conversation).filter(
                        Conversation.id == existing_conv_id
                    ).first()
                    if not conv_row:
                        raise RuntimeError("conversation_missing")

                committed_conv_id = conv_row.id
                new_db.add(Message(
                    conversation_id=committed_conv_id,
                    role="user",
                    content=topic,
                    citations=[],
                ))
                new_db.add(Message(
                    conversation_id=committed_conv_id,
                    role="assistant",
                    content=cleaned_content,
                    citations=[{
                        "source": _field(r, "source"),
                        "paper_id": _field(r, "paper_id"),
                        "title": _field(r, "title"),
                        "authors": _field(r, "authors"),
                        "year": _field(r, "year"),
                        "page_number": _field(r, "page_number"),
                        "content": _field(r, "content"),
                        "verified": verify_report["verified"],
                        "removed": verify_report["removed"],
                    } for r in citations],
                ))
                new_db.flush()
                conv_row.message_count = new_db.query(Message).filter(
                    Message.conversation_id == committed_conv_id
                ).count()
                finish_frame = _sse_frame({
                    "delta": "",
                    "finished": True,
                    "conversation_id": committed_conv_id,
                    "content": cleaned_content,
                    "citations": [_jsonable(c) for c in citations],
                    "verification": _compat_verification(verify_report),
                })
                new_db.commit()

            # 6) 只有提交完成后才发布成功终态。
            yield finish_frame
        except asyncio.CancelledError:
            logger.info(
                "[deep-review] conversation %s streaming cancelled",
                existing_conv_id,
            )
            return
        except Exception as e:
            # 兜底：任何未预期异常以带内错误帧收尾（脱敏，详情仅入日志，宪法第 13 条）
            logger.error("[deep-review] 未预期异常: %s", type(e).__name__)
            yield _generation_error_frame(
                existing_conv_id, "finalization_failed"
            )
            return

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/skills")
def get_skills():
    """返回可用 Skill 列表。"""
    return list_skills()
