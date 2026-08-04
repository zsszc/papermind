import asyncio
import json
from typing import AsyncIterator, List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Conversation, Message, Paper
from app.schemas import ChatRequest, ConversationResponse, ImageAnalysisRequest
from app.core.logger import logger
from app.services.llm import llm_service
from app.services.retrieval import get_vector_store
from app.services.memory_manager import MemoryManager
from app.services.image_analyzer import image_analyzer_service
from app.services.skills import list_skills
from app.services.agent_graph import (
    SYSTEM_PROMPT,
    build_rag_prompt as _build_rag_prompt,
    run_pre_orchestration,
)

router = APIRouter()


async def _stream_response(messages: List[dict]) -> AsyncIterator[str]:
    async for delta in llm_service.chat_stream(messages):
        yield f"data: {json.dumps({'delta': delta, 'finished': False}, ensure_ascii=False)}\n\n"
    yield f"data: {json.dumps({'delta': '', 'finished': True}, ensure_ascii=False)}\n\n"


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
            {"id": m.id, "role": m.role, "content": m.content, "citations": m.citations}
            for m in messages
        ],
    }


@router.delete("/conversations/{conversation_id}", status_code=204)
def delete_conversation(conversation_id: int, db: Session = Depends(get_db)):
    conv = db.query(Conversation).filter(Conversation.id == conversation_id).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
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
    db.commit()
    return


@router.post("/conversations/{conversation_id}/messages/{message_id}/regenerate")
async def regenerate_message(
    conversation_id: int,
    message_id: int,
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
    if target_index is None or target_index == 0:
        raise HTTPException(status_code=400, detail="No user message before this assistant message")

    prev_user_msg = all_messages[target_index - 1]
    if prev_user_msg.role != "user":
        raise HTTPException(status_code=400, detail="Previous message is not a user message")

    history_messages = all_messages[:target_index]
    query = prev_user_msg.content

    # 检索相关片段
    retrieved = []
    try:
        store = get_vector_store()
        if store.available():
            retrieved = store.search(query=query, top_k=5)
    except Exception as e:
        logger.error(f"[regenerate] 检索失败: {e}")

    memory_mgr = MemoryManager(db)
    memory_context = memory_mgr.build_memory_context()
    system_content = SYSTEM_PROMPT
    if memory_context:
        system_content += f"\n\n以下是关于用户的背景记忆，请在回答时参考：\n\n{memory_context}"

    messages = [{"role": "system", "content": system_content}]
    for m in history_messages:
        messages.append({"role": m.role, "content": m.content})
    if retrieved:
        rag_context = _build_rag_prompt(query, retrieved)
        messages.append({"role": "system", "content": rag_context})

    async def event_stream():
        full_content = ""
        try:
            # regenerate 无请求体、不含联网开关，固定关闭（确定性重生成，见 specs/backend/routers/chat.md 3.6）
            async for delta in llm_service.chat_stream(messages, enable_web_search=False):
                full_content += delta
                yield f"data: {json.dumps({'delta': delta, 'finished': False, 'conversation_id': conversation_id}, ensure_ascii=False)}\n\n"
                # 让出控制权，使客户端断开后能触发 CancelledError
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            logger.info(f"[regenerate] conversation {conversation_id} message {message_id} cancelled")
            return

        # 保存替换后的内容
        from app.database import SessionLocal
        with SessionLocal() as new_db:
            msg = (
                new_db.query(Message)
                .filter(Message.id == message_id, Message.conversation_id == conversation_id)
                .first()
            )
            if msg:
                msg.content = full_content
                msg.citations = [{"source": r["source"], "paper_id": r["paper_id"]} for r in retrieved]
                new_db.commit()

        yield f"data: {json.dumps({'delta': '', 'finished': True, 'conversation_id': conversation_id, 'citations': retrieved}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(),
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
        async for delta in image_analyzer_service.analyze_stream(
            image_bytes=image_bytes,
            filename=filename,
            question=question,
        ):
            yield f"data: {json.dumps({'delta': delta, 'finished': False}, ensure_ascii=False)}\n\n"
        yield f"data: {json.dumps({'delta': '', 'finished': True}, ensure_ascii=False)}\n\n"

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
    logger.info(f"[chat] 收到请求: conversation_id={request.conversation_id}, message={request.message[:50]}")
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
        logger.error(f"[chat] 记忆更新失败: {e}")

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

    conv.message_count = state["history_total"] + 1
    db.commit()

    if request.stream is False:
        content = await llm_service.chat_completion(messages)
        assistant_msg = Message(
            conversation_id=conv.id,
            role="assistant",
            content=content,
            citations=[{
        "source": r["source"],
        "paper_id": r["paper_id"],
        "title": r.get("title"),
        "authors": r.get("authors"),
        "year": r.get("year"),
        "page_number": r.get("page_number"),
        "content": r.get("content"),
    } for r in retrieved],
        )
        db.add(assistant_msg)
        db.commit()
        return {
            "conversation_id": conv.id,
            "content": content,
            "citations": retrieved,
        }

    conv_id = conv.id
    async def event_stream():
        full_content = ""
        try:
            async for delta in llm_service.chat_stream(messages, enable_web_search=enable_web_search):
                full_content += delta
                yield f"data: {json.dumps({'delta': delta, 'finished': False, 'conversation_id': conv_id}, ensure_ascii=False)}\n\n"
                # 让出控制权，使客户端断开后能触发 CancelledError
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            logger.info(f"[chat] conversation {conv_id} streaming cancelled")
            return

        # 保存助手回复（使用新的 Session，避免原 Session 已关闭）
        if full_content.strip():
            from app.database import SessionLocal
            with SessionLocal() as new_db:
                assistant_msg = Message(
                    conversation_id=conv_id,
                    role="assistant",
                    content=full_content,
                    citations=[{
        "source": r["source"],
        "paper_id": r["paper_id"],
        "title": r.get("title"),
        "authors": r.get("authors"),
        "year": r.get("year"),
        "page_number": r.get("page_number"),
        "content": r.get("content"),
    } for r in retrieved],
                )
                new_db.add(assistant_msg)
                new_db.commit()

        yield f"data: {json.dumps({'delta': '', 'finished': True, 'conversation_id': conv_id, 'citations': retrieved}, ensure_ascii=False)}\n\n"

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
