from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import MemorySummary
from app.schemas import ConversationResponse
from app.services.memory_manager import MemoryManager

router = APIRouter()


@router.get("/memories")
def list_memories(memory_type: str = None, db: Session = Depends(get_db)):
    query = db.query(MemorySummary)
    if memory_type:
        query = query.filter(MemorySummary.memory_type == memory_type)
    items = query.order_by(MemorySummary.created_at.desc()).all()
    return items


@router.post("/memories")
def add_memory(
    content: str,
    memory_type: str = "fact",
    importance: int = 5,
    db: Session = Depends(get_db),
):
    mgr = MemoryManager(db)
    mem = mgr.add_long_term_memory(content, memory_type, importance)
    return mem


@router.delete("/memories/{memory_id}")
def delete_memory(memory_id: int, db: Session = Depends(get_db)):
    mem = db.query(MemorySummary).filter(MemorySummary.id == memory_id).first()
    if not mem:
        raise HTTPException(status_code=404, detail="Memory not found")
    db.delete(mem)
    db.commit()
    return {"status": "ok"}
