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
from app.services.web_search import web_search_service
from app.services.image_analyzer import image_analyzer_service
from app.services.skills import build_skill_prompt, list_skills

router = APIRouter()


SYSTEM_PROMPT = """你是 PaperMind，一位专业的学术文献助手。你正在帮助用户管理结直肠癌 T 分期预测相关的文献、笔记与毕业论文写作。

请遵循以下规则：
1. 基于提供的参考文献片段回答，若片段不足以回答，请明确说明。
2. 回答需专业、简洁，优先使用中文。
3. 若引用文献片段，请在回答末尾以 [^1^] [^2^] 形式标注，并列出引用来源。
4. 若用户问题与当前文献无关，可作为一般学术讨论回答。
"""


def _build_rag_prompt(query: str, retrieved: List[dict]) -> str:
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
            async for delta in llm_service.chat_stream(messages, enable_web_search=enable_web_search):
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

    # 检索相关文献片段（Embedding 不可用时自动回退到纯 LLM 对话）
    retrieved = []
    filters = {}
    if request.paper_id:
        filters["paper_id"] = request.paper_id
    if request.message:
        try:
            store = get_vector_store()
            if store.available():
                retrieved = store.search(query=request.message, top_k=5, filters=filters)
        except Exception as e:
            logger.error(f"[chat] 检索失败: {e}")

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

    # 组装历史消息
    history = (
        db.query(Message)
        .filter(Message.conversation_id == conv.id)
        .order_by(Message.created_at.asc())
        .all()
    )
    # 构建 system prompt：基础设定 + 记忆 + RAG
    memory_context = memory_mgr.build_memory_context()
    system_content = SYSTEM_PROMPT
    if memory_context:
        system_content += f"\n\n以下是关于用户的背景记忆，请在回答时参考：\n\n{memory_context}"

    messages = [{"role": "system", "content": system_content}]
    for m in history[-10:]:
        messages.append({"role": m.role, "content": m.content})

    # 将检索结果作为 system 上下文追加到最后（history 中已包含当前 user 消息）
    if retrieved:
        rag_context = _build_rag_prompt(request.message, retrieved)
        messages.append({"role": "system", "content": rag_context})

    # 判断是否启用联网搜索
    enable_web_search = request.enable_web_search or web_search_service.should_search_online(request.message)
    if enable_web_search:
        messages.append({
            "role": "system",
            "content": "用户问题可能涉及最新信息。如果现有文献片段不足以回答，请调用联网搜索工具获取最新资料并标注来源。",
        })

    # 注入 Skill 角色设定
    skill_prompt = build_skill_prompt(request.skill, request.message)
    if skill_prompt:
        messages.append({"role": "system", "content": skill_prompt})

    conv.message_count = len(history) + 1
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
