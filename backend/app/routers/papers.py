import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import aiofiles
from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.database import get_db
from app.models import Paper, Tag, Chunk, PaperAnnotation, ThesisCitation, ThesisFile
from app.schemas import (
    CitationGraphResponse,
    PaperCreate,
    PaperDetail,
    PaperListResponse,
    PaperListItem,
    PaperUpdate,
    TagCreate,
    TagResponse,
    PaperBatchDelete,
    PaperBatchStatus,
    PaperBatchTags,
    PaperAnnotationCreate,
    PaperAnnotationResponse,
    PaperStatsResponse,
)
from app.core.logger import logger
from app.services.pdf_parser import PDFParser
from app.services.processor import PaperProcessor
from app.services.llm import llm_service
from app.services.auto_tag import auto_tag_service
from app.services.retrieval import get_vector_store

router = APIRouter()

# 上传限制：单文件 50MB，分块读取大小 1MB
MAX_UPLOAD_SIZE = 50 * 1024 * 1024
_UPLOAD_CHUNK_SIZE = 1024 * 1024


async def _save_upload_file(file: UploadFile, target_path: Path, max_size: Optional[int] = None) -> int:
    """分块异步写入上传文件，避免在事件循环中同步写盘阻塞。

    按实际读取字节数做大小校验，超限（或写入失败）时清理残留文件并抛错。
    """
    limit = max_size if max_size is not None else MAX_UPLOAD_SIZE
    total = 0
    try:
        async with aiofiles.open(target_path, "wb") as f:
            while True:
                chunk = await file.read(_UPLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=f"文件超过大小限制（最大 {limit // (1024 * 1024)}MB）",
                    )
                await f.write(chunk)
    except Exception:
        # 清理不完整文件，避免留下损坏的 PDF
        try:
            target_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return total


def _enhance_metadata_with_llm_sync(pdf_path: str) -> Dict[str, Any]:
    """PDFParser.enhance_metadata_with_llm 的同步镜像版。

    后台线程没有事件循环，不能复用 async LLM client，这里改用同步入口
    chat_completion_sync。prompt 与结果解析逻辑需与异步版保持一致。
    """
    path = Path(pdf_path)
    front_text = PDFParser()._extract_front_text(path, max_pages=3)
    if not front_text.strip():
        return {}

    prompt = f"""请从以下学术论文的前几页文本中提取元数据，并以 JSON 格式返回。

请提取以下字段：
- title: 论文标题（字符串，完整标题，去除页眉页脚和 arXiv 水印）
- authors: 作者列表（字符串，用逗号分隔）
- year: 发表年份（整数，如 2024）
- journal: 期刊或会议名称（字符串）
- abstract: 摘要（字符串，尽量完整）
- doi: DOI（字符串，没有则留空）
- authors_list: 作者列表（数组，每个元素一个作者姓名）
- confidence: 对象，包含 title/authors/year/journal/abstract/doi 的置信度，1-5 分
- source_lines: 对象，包含 title/authors/year/journal/abstract/doi 的原始来源文本片段

注意：
1. 只返回 JSON，不要有任何其他解释文字。
2. 如果某个字段无法确定，使用空字符串或 null。
3. 标题和作者必须准确，不要包含页眉页脚或噪声。
4. 作者名不要包含邮箱地址、机构名或 "and" 等连接词。

文本内容：
{front_text[:4000]}

JSON 输出："""

    messages = [
        {"role": "system", "content": "你是专业的学术论文元数据提取助手，只输出 JSON。"},
        {"role": "user", "content": prompt},
    ]
    result = llm_service.chat_completion_sync(messages, json_mode=True)
    try:
        data = json.loads(result)
        return {
            "title": data.get("title") or None,
            "authors": data.get("authors") or None,
            "year": int(data["year"]) if data.get("year") else None,
            "journal": data.get("journal") or None,
            "abstract": data.get("abstract") or None,
            "doi": data.get("doi") or None,
            "authors_list": data.get("authors_list") or None,
            "confidence": data.get("confidence") or {},
            "source_lines": data.get("source_lines") or {},
        }
    except Exception as e:
        logger.warning(f"[PDFParser] LLM 增强元数据解析失败（同步）: {e}", exc_info=True)
        return {}

# 按 paper_id 的细粒度锁，避免不同 PDF 互相阻塞
_paper_locks: Dict[int, threading.Lock] = {}
_paper_locks_lock = threading.Lock()


def _get_paper_lock(paper_id: int) -> threading.Lock:
    with _paper_locks_lock:
        if paper_id not in _paper_locks:
            _paper_locks[paper_id] = threading.Lock()
        return _paper_locks[paper_id]


def get_papers_dir() -> Path:
    project_root = Path(__file__).resolve().parents[3]
    papers_dir = project_root / "papers"
    papers_dir.mkdir(parents=True, exist_ok=True)
    return papers_dir


def get_notes_dir() -> Path:
    project_root = Path(__file__).resolve().parents[3]
    notes_dir = project_root / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    return notes_dir


def get_summaries_dir() -> Path:
    project_root = Path(__file__).resolve().parents[3]
    summaries_dir = project_root / "summaries"
    summaries_dir.mkdir(parents=True, exist_ok=True)
    return summaries_dir


def _enhance_paper_metadata(paper_id: int):
    """独立后台任务：使用 LLM 补全元数据并自动生成标签。

    该任务与核心向量化流程解耦，失败不会影响 paper.processed 状态。
    在独立线程中运行，全程使用同步 LLM 入口，避免线程内 asyncio.run()
    复用 async client 造成跨事件循环问题。
    """
    from app.database import SessionLocal

    with SessionLocal() as db:
        paper = db.query(Paper).filter(Paper.id == paper_id).first()
        if not paper:
            logger.warning(f"[background] enhance metadata paper {paper_id} 不存在，跳过")
            return

        try:
            pdf_path = str(Path(__file__).resolve().parents[3] / paper.file_path)
            enhanced = _enhance_metadata_with_llm_sync(pdf_path)
            if enhanced.get("title"):
                paper.title = enhanced["title"]
            if enhanced.get("authors"):
                paper.authors = enhanced["authors"]
            if enhanced.get("year"):
                paper.year = enhanced["year"]
            if enhanced.get("journal"):
                paper.journal = enhanced["journal"]
            if enhanced.get("abstract"):
                paper.abstract = enhanced["abstract"]
            if enhanced.get("doi"):
                paper.doi = enhanced["doi"]
            paper.metadata_json = {**(paper.metadata_json or {}), **enhanced}
            db.commit()
            logger.info(f"[background] paper {paper_id} 元数据增强完成")
        except Exception as e:
            logger.error(f"[background] enhance metadata paper {paper_id} failed: {e}")
            return

        # 自动生成标签（LLM 调用限时 60 秒）
        try:
            generated_tags = auto_tag_service.generate_tags_sync(paper, db, timeout=60)
            for tag in generated_tags:
                if tag not in paper.tags:
                    paper.tags.append(tag)
            db.commit()
            logger.info(f"[background] paper {paper_id} 自动标签完成: {[t.name for t in generated_tags]}")
        except Exception as e:
            logger.error(f"[background] auto tag paper {paper_id} failed: {e}")


def _process_paper_background(paper_id: int):
    """后台异步处理：提取文本、分块、向量化。

    使用按 paper_id 的细粒度锁避免同一篇论文被重复处理，不同 PDF 之间可并发。
    """
    from app.database import SessionLocal

    lock = _get_paper_lock(paper_id)
    acquired = lock.acquire(blocking=False)
    if not acquired:
        logger.info(f"[background] paper {paper_id} 正在处理中，跳过重复任务")
        return

    try:
        with SessionLocal() as db:
            paper = db.query(Paper).filter(Paper.id == paper_id).first()
            if not paper:
                logger.warning(f"[background] paper {paper_id} 不存在，跳过")
                return

            paper.processed = "processing"
            db.commit()

            # 文本提取与向量化（失败可重试 1 次）
            process_ok = False
            for attempt in range(2):
                try:
                    processor = PaperProcessor()
                    processor.process(paper, db)
                    process_ok = True
                    break
                except Exception as e:
                    logger.error(f"[background] process paper {paper_id} attempt {attempt + 1} failed: {e}")
                    if attempt == 1:
                        paper.processed = "error"
                        db.commit()
                        return

            if not process_ok:
                return

            # 核心处理完成，立即标记为 done，不等待 LLM 元数据增强
            paper.processed = "done"
            db.commit()
            logger.info(f"[background] paper {paper_id} 核心处理完成")
    finally:
        lock.release()
        with _paper_locks_lock:
            _paper_locks.pop(paper_id, None)

    # LLM 元数据增强与自动标签作为独立后台任务执行，不阻塞核心流程
    threading.Thread(target=_enhance_paper_metadata, args=(paper_id,), daemon=True).start()


@router.post("/import")
async def import_papers(
    files: List[UploadFile] = File(...),
    background_tasks: BackgroundTasks = None,
    db: Session = Depends(get_db),
):
    papers_dir = get_papers_dir()
    parser = PDFParser()
    imported = []
    errors = []

    for file in files:
        # 扩展名白名单：仅允许 PDF（校验失败仍整体中止，保持 400/413 契约不变）
        if not file.filename or not file.filename.lower().endswith(".pdf"):
            raise HTTPException(status_code=400, detail=f"只支持 .pdf 文件: {file.filename or '未命名文件'}")

        # 单文件大小上限：先按声明大小快速拦截，写盘时再按实际字节数兜底
        if file.size is not None and file.size > MAX_UPLOAD_SIZE:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"文件超过大小限制（最大 {MAX_UPLOAD_SIZE // (1024 * 1024)}MB）: {file.filename}",
            )

        safe_name = Path(file.filename).name
        target_path = papers_dir / safe_name

        # 处理重名
        counter = 1
        while target_path.exists():
            stem = Path(safe_name).stem
            suffix = Path(safe_name).suffix
            target_path = papers_dir / f"{stem}_{counter}{suffix}"
            counter += 1

        # 逐篇容错（Batch7b-F9）：写盘/建档/笔记任一环节失败，清理该篇已落盘的
        # PDF 与笔记文件并回滚其 DB 记录，记入错误标记后继续后续篇目，不留孤儿文件
        note_path = None
        try:
            # 分块异步写盘，避免同步写阻塞事件循环
            await _save_upload_file(file, target_path)

            try:
                metadata = await run_in_threadpool(parser.parse_metadata, str(target_path))
            except Exception as e:
                logger.warning(f"[import] 解析元数据失败 {safe_name}: {e}")
                metadata = {"parse_error": str(e)}

            paper = Paper(
                title=metadata.get("title") or target_path.stem,
                authors=metadata.get("authors"),
                year=metadata.get("year"),
                journal=metadata.get("journal"),
                abstract=metadata.get("abstract"),
                doi=metadata.get("doi"),
                pages=metadata.get("pages"),
                file_path=str(target_path.relative_to(Path(__file__).resolve().parents[3])),
                filename=target_path.name,
                status="unread",
                source="local",
                metadata_json=metadata,
            )
            db.add(paper)
            db.flush()

            # 创建对应的空笔记文件（放线程池，避免同步写盘阻塞事件循环）
            notes_dir = get_notes_dir()
            note_path = notes_dir / f"{paper.id}.md"
            note_content = f"# {paper.title or paper.filename}\n\n"
            await run_in_threadpool(note_path.write_text, note_content, "utf-8")

            # 逐篇提交：失败篇目回滚不影响已成功篇目（批量导入通常个位数文件，吞吐损失可忽略）
            db.commit()
            db.refresh(paper)

            # 后台自动处理
            if background_tasks:
                background_tasks.add_task(_process_paper_background, paper.id)

            imported.append(paper)
        except Exception as e:
            # 异常原文只入日志（宪法第 13 条），错误标记用通用文案
            logger.error(f"[import] 导入失败 {safe_name}，清理残留文件: {e}", exc_info=True)
            db.rollback()
            for path in (target_path, note_path):
                if path is None:
                    continue
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            errors.append({"filename": safe_name, "detail": "该文件导入失败，已清理残留文件"})
            continue

    return {
        "total": len(imported),
        "items": [PaperListItem.model_validate(p) for p in imported],
        "errors": errors,
    }


@router.get("", response_model=PaperListResponse)
def list_papers(
    skip: int = 0,
    limit: int = 20,
    status: Optional[str] = None,
    tag: Optional[str] = None,
    q: Optional[str] = None,
    db: Session = Depends(get_db),
):
    query = db.query(Paper)
    if status:
        query = query.filter(Paper.status == status)
    if tag:
        query = query.join(Paper.tags)
        tag_names = [t.strip() for t in tag.split(",") if t.strip()]
        if len(tag_names) == 1:
            query = query.filter(Tag.name == tag_names[0])
        else:
            query = query.filter(Tag.name.in_(tag_names))
    if q:
        like = f"%{q}%"
        query = query.filter(
            (Paper.title.ilike(like))
            | (Paper.authors.ilike(like))
            | (Paper.abstract.ilike(like))
        )

    # 限制单次最大返回数量，避免大数据量拖慢响应
    safe_limit = min(max(limit, 1), 200)
    total = query.count()
    items = query.order_by(Paper.created_at.desc()).offset(skip).limit(safe_limit).all()
    return PaperListResponse(total=total, items=items)


# 注意：所有固定路径（如 /import、/stats/overview、/batch/*、/tags/*）必须放在 /{paper_id} 之前，
# 否则会被 FastAPI 路径参数路由拦截。
@router.get("/stats/overview")
def paper_stats(db: Session = Depends(get_db)):
    """文献库统计与引用关系图数据。"""
    from collections import Counter
    from sqlalchemy import func

    papers = db.query(Paper).all()
    total = len(papers)

    # 按年份统计
    by_year = Counter()
    for p in papers:
        if p.year:
            by_year[str(p.year)] += 1

    # 按状态统计
    by_status = Counter()
    for p in papers:
        by_status[p.status or "unread"] += 1

    # 按标签统计
    by_tag = Counter()
    for p in papers:
        for t in p.tags:
            by_tag[t.name] += 1

    # 高频作者
    author_counter = Counter()
    for p in papers:
        if p.authors:
            for author in p.authors.split(","):
                name = author.strip()
                if name:
                    author_counter[name] += 1
    top_authors = [{"name": name, "count": count} for name, count in author_counter.most_common(10)]

    # 引用关系图：论文 -> 被大论文引用的章节
    nodes = [{"id": f"p{p.id}", "name": p.title or p.filename, "type": "paper"} for p in papers]
    links = []
    thesis_citations = db.query(ThesisCitation).filter(ThesisCitation.paper_id.isnot(None)).all()
    chapter_titles = {}
    for tc in thesis_citations:
        thesis = db.query(ThesisFile).filter(ThesisFile.id == tc.thesis_id).first()
        if thesis and tc.chapter_index is not None:
            ch = (thesis.chapter_structure or [])[tc.chapter_index] if tc.chapter_index < len(thesis.chapter_structure or []) else None
            ch_title = ch.get("title", f"第{tc.chapter_index + 1}章") if ch else f"第{tc.chapter_index + 1}章"
            ch_id = f"t{thesis.id}_ch{tc.chapter_index}"
            if ch_id not in {n["id"] for n in nodes}:
                nodes.append({"id": ch_id, "name": ch_title, "type": "chapter"})
            links.append({"source": f"p{tc.paper_id}", "target": ch_id, "value": 1})

    return {
        "total": total,
        "by_year": dict(sorted(by_year.items())),
        "by_status": dict(by_status),
        "by_tag": dict(by_tag.most_common(20)),
        "top_authors": top_authors,
        "citation_graph": {"nodes": nodes, "links": links},
    }


@router.get("/{paper_id}", response_model=PaperDetail)
def get_paper(paper_id: int, db: Session = Depends(get_db)):
    paper = db.query(Paper).filter(Paper.id == paper_id).first()
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")
    return paper


@router.get("/{paper_id}/citation-graph", response_model=CitationGraphResponse)
def get_citation_graph(paper_id: int, db: Session = Depends(get_db)):
    """引用图谱端点（Phase G G2）：以该文献为中心的 1 跳子图（出边 + 入边）。

    - nodes：中心文献 + 全部一跳邻居（id / title / year）；
    - edges：所有与中心相连的引用边（citing → cited，均对应 papers.id）；
    - paper_citations 表缺失或查询异常时降级为仅中心节点的空图（不抛 500）；
    - 文献不存在返回 404。
    """
    paper = db.query(Paper).filter(Paper.id == paper_id).first()
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")

    edges: List[Dict[str, int]] = []
    neighbor_ids: set = set()
    try:
        rows = db.execute(
            text(
                "SELECT citing_id, cited_id FROM paper_citations "
                "WHERE citing_id = :pid OR cited_id = :pid"
            ),
            {"pid": paper_id},
        ).fetchall()
        edges = [{"citing": r.citing_id, "cited": r.cited_id} for r in rows]
        for r in rows:
            neighbor_ids.add(r.citing_id)
            neighbor_ids.add(r.cited_id)
    except Exception as e:
        logger.warning(
            f"[citation-graph] 引用边查询失败，降级为仅中心节点的空图: "
            f"{type(e).__name__}: {e}"
        )

    neighbor_ids.discard(paper_id)
    nodes: List[Dict[str, Any]] = [
        {"id": paper.id, "title": paper.title, "year": paper.year}
    ]
    if neighbor_ids:
        neighbors = db.query(Paper).filter(Paper.id.in_(neighbor_ids)).all()
        nodes.extend(
            {"id": p.id, "title": p.title, "year": p.year} for p in neighbors
        )
    return CitationGraphResponse(nodes=nodes, edges=edges)


@router.put("/{paper_id}", response_model=PaperDetail)
def update_paper(paper_id: int, payload: PaperUpdate, db: Session = Depends(get_db)):
    paper = db.query(Paper).filter(Paper.id == paper_id).first()
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")

    update_data = payload.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(paper, key, value)

    db.commit()
    db.refresh(paper)
    return paper


@router.delete("/{paper_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_paper(paper_id: int, db: Session = Depends(get_db)):
    paper = db.query(Paper).filter(Paper.id == paper_id).first()
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")

    project_root = Path(__file__).resolve().parents[3]

    # 清理向量数据
    try:
        store = get_vector_store()
        store.delete_by_paper_id(paper_id)
    except Exception as e:
        logger.warning(f"[delete] 清理向量失败 paper {paper_id}: {e}")

    # 清理本地文件
    files_to_remove = [
        project_root / paper.file_path,
        get_notes_dir() / f"{paper_id}.md",
        get_summaries_dir() / f"{paper_id}.md",
    ]
    for file_path in files_to_remove:
        try:
            if file_path.exists():
                file_path.unlink()
        except Exception as e:
            logger.warning(f"[delete] 清理文件失败 {file_path}: {e}")

    db.query(Chunk).filter(Chunk.paper_id == paper_id).delete()
    # 级联清理大论文引用关联行，避免 thesis_citations 悬空指向已删文献（Batch7b-F10）
    db.query(ThesisCitation).filter(ThesisCitation.paper_id == paper_id).delete()
    db.delete(paper)
    db.commit()
    return


def _fix_tag_encoding(name: str) -> str:
    """修复可能被错误解码的中文标签名。"""
    if not name:
        return name
    try:
        encoded = name.encode("latin-1")
        # 若包含常见 UTF-8 中文高字节，则重新解码
        if any(b in encoded for b in [b"\xc3", b"\xe4", b"\xe5", b"\xe6", b"\xe7", b"\xe8", b"\xe9"]):
            return encoded.decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    return name


@router.post("/{paper_id}/tags", response_model=PaperDetail)
def add_tag_to_paper(paper_id: int, tag_name: str = Form(...), db: Session = Depends(get_db)):
    paper = db.query(Paper).filter(Paper.id == paper_id).first()
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")

    tag_name = _fix_tag_encoding(tag_name.strip())
    if not tag_name:
        raise HTTPException(status_code=400, detail="Tag name cannot be empty")

    tag = db.query(Tag).filter(Tag.name == tag_name).first()
    if not tag:
        tag = Tag(name=tag_name)
        db.add(tag)
        db.flush()

    if tag not in paper.tags:
        paper.tags.append(tag)
        db.commit()
        db.refresh(paper)
    return paper


@router.delete("/{paper_id}/tags/{tag_id}", response_model=PaperDetail)
def remove_tag_from_paper(paper_id: int, tag_id: int, db: Session = Depends(get_db)):
    paper = db.query(Paper).filter(Paper.id == paper_id).first()
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")

    tag = db.query(Tag).filter(Tag.id == tag_id).first()
    if not tag:
        raise HTTPException(status_code=404, detail="Tag not found")

    if tag in paper.tags:
        paper.tags.remove(tag)
        db.commit()
        db.refresh(paper)
    return paper


@router.get("/tags/all", response_model=List[TagResponse])
def list_all_tags(db: Session = Depends(get_db)):
    return db.query(Tag).order_by(Tag.name.asc()).all()


@router.put("/tags/{tag_id}", response_model=TagResponse)
def update_tag(tag_id: int, payload: TagCreate, db: Session = Depends(get_db)):
    tag = db.query(Tag).filter(Tag.id == tag_id).first()
    if not tag:
        raise HTTPException(status_code=404, detail="Tag not found")

    update_data = payload.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(tag, key, value)
    db.commit()
    db.refresh(tag)
    return tag


@router.get("/{paper_id}/read-progress")
def get_read_progress(paper_id: int, db: Session = Depends(get_db)):
    paper = db.query(Paper).filter(Paper.id == paper_id).first()
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")
    return {"paper_id": paper_id, "last_read_page": paper.last_read_page or 1}


@router.put("/{paper_id}/read-progress")
def update_read_progress(paper_id: int, page: int, db: Session = Depends(get_db)):
    paper = db.query(Paper).filter(Paper.id == paper_id).first()
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")
    paper.last_read_page = max(1, page)
    db.commit()
    return {"paper_id": paper_id, "last_read_page": paper.last_read_page}


@router.get("/{paper_id}/note")
def get_paper_note(paper_id: int):
    notes_dir = get_notes_dir()
    note_path = notes_dir / f"{paper_id}.md"
    if not note_path.exists():
        return {"content": ""}
    return {"content": note_path.read_text(encoding="utf-8")}


@router.post("/{paper_id}/note")
def save_paper_note(paper_id: int, content: str = Form(...)):
    notes_dir = get_notes_dir()
    note_path = notes_dir / f"{paper_id}.md"
    note_path.write_text(content, encoding="utf-8")
    return {"status": "ok"}


@router.get("/{paper_id}/pdf")
def get_pdf_file(paper_id: int, db: Session = Depends(get_db)):
    paper = db.query(Paper).filter(Paper.id == paper_id).first()
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")

    project_root = Path(__file__).resolve().parents[3]
    pdf_path = project_root / paper.file_path
    if not pdf_path.exists():
        raise HTTPException(status_code=404, detail="PDF file not found")

    def iterfile():
        with open(pdf_path, "rb") as f:
            yield from f

    # RFC 5987 编码文件名，支持中文
    encoded_filename = quote(paper.filename)
    disposition = f"inline; filename=\"{encoded_filename}\"; filename*=UTF-8''{encoded_filename}"
    return StreamingResponse(
        iterfile(),
        media_type="application/pdf",
        headers={"Content-Disposition": disposition},
    )


@router.post("/batch/delete", status_code=status.HTTP_204_NO_CONTENT)
def batch_delete_papers(payload: PaperBatchDelete, db: Session = Depends(get_db)):
    """批量删除论文及其关联数据。"""
    papers = db.query(Paper).filter(Paper.id.in_(payload.ids)).all()
    for paper in papers:
        delete_paper(paper.id, db)
    return None


@router.post("/batch/status")
def batch_update_status(payload: PaperBatchStatus, db: Session = Depends(get_db)):
    """批量修改论文状态。"""
    updated = db.query(Paper).filter(Paper.id.in_(payload.ids)).all()
    for paper in updated:
        paper.status = payload.status
    db.commit()
    return {"updated": len(updated)}


@router.post("/batch/tags")
def batch_update_tags(payload: PaperBatchTags, db: Session = Depends(get_db)):
    """批量为论文添加或移除标签。"""
    papers = db.query(Paper).filter(Paper.id.in_(payload.ids)).all()
    for name in payload.tag_names:
        tag = db.query(Tag).filter(Tag.name == name).first()
        if not tag:
            tag = Tag(name=name)
            db.add(tag)
            db.flush()
        for paper in papers:
            if payload.action == "add":
                if tag not in paper.tags:
                    paper.tags.append(tag)
            elif payload.action == "remove":
                if tag in paper.tags:
                    paper.tags.remove(tag)
    db.commit()
    return {"updated": len(papers)}


@router.post("/{paper_id}/process")
def process_paper(paper_id: int, db: Session = Depends(get_db)):
    """提取文本、分块、向量化。"""
    paper = db.query(Paper).filter(Paper.id == paper_id).first()
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")

    processor = PaperProcessor()
    try:
        result = processor.process(paper, db)
        paper.processed = "done"
        db.commit()
        return {"paper_id": paper_id, **result}
    except Exception as e:
        paper.processed = "error"
        db.commit()
        raise HTTPException(status_code=500, detail=f"处理失败: {e}")


@router.post("/{paper_id}/summarize")
async def summarize_paper(paper_id: int, db: Session = Depends(get_db)):
    """单篇 AI 深度概括，结果保存到笔记文件。"""
    paper = db.query(Paper).filter(Paper.id == paper_id).first()
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")

    if paper.processed != "done":
        raise HTTPException(status_code=400, detail="论文尚未处理完成，请稍后再试")

    chunks = (
        db.query(Chunk)
        .filter(Chunk.paper_id == paper_id)
        .order_by(Chunk.chunk_index.asc())
        .all()
    )
    if not chunks:
        raise HTTPException(status_code=400, detail="论文内容为空，无法生成概括")

    # 取所有 chunks 内容，按字符数截断到 6000，避免只取前 5 个导致信息缺失
    context = "\n\n".join([c.content for c in chunks])
    context = context[:6000]

    prompt = f"""请对以下学术论文进行深度概括，以 Markdown 格式输出：

- 标题：{paper.title or '未知'}
- 作者：{paper.authors or '未知'}
- 年份：{paper.year or '未知'}

论文内容片段：
{context}

请包含以下部分：
1. 研究背景与动机
2. 核心方法
3. 主要贡献
4. 实验与结果
5. 与结直肠癌 T 分期预测研究的关联
6. 可借鉴的思路
"""

    messages = [
        {"role": "system", "content": "你是专业的学术文献分析助手。"},
        {"role": "user", "content": prompt},
    ]

    try:
        logger.info(f"[Summarize] paper_id={paper_id} context_len={len(prompt)} 开始调用 LLM")
        summary = await llm_service.chat_completion(messages, timeout=300)
        logger.info(f"[Summarize] paper_id={paper_id} summary_len={len(summary)}")
    except Exception as e:
        # 异常脱敏（宪法第 13 条）：通用文案回前端，异常原文与堆栈只入日志
        logger.exception(f"[Summarize] paper_id={paper_id} LLM 调用失败")
        raise HTTPException(status_code=504, detail="AI 概括失败，请稍后再试") from e

    if summary.startswith("[调用 LLM 出错"):
        # llm_service 错误串可能携带异常原文（_format_error 兜底透传），同样只入日志不外发
        logger.warning(f"[Summarize] paper_id={paper_id} 返回错误内容: {summary}")
        raise HTTPException(status_code=504, detail="AI 概括失败，请稍后再试")

    summaries_dir = get_summaries_dir()
    summary_path = summaries_dir / f"{paper_id}.md"
    note_content = f"# {paper.title or paper.filename}\n\n{summary}\n"
    summary_path.write_text(note_content, encoding="utf-8")

    return {"paper_id": paper_id, "summary": summary}


@router.get("/{paper_id}/summary")
def get_paper_summary(paper_id: int, db: Session = Depends(get_db)):
    """读取由 AI 概括生成的笔记内容。"""
    paper = db.query(Paper).filter(Paper.id == paper_id).first()
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")

    summaries_dir = get_summaries_dir()
    summary_path = summaries_dir / f"{paper_id}.md"
    if not summary_path.exists():
        raise HTTPException(status_code=404, detail="尚未生成 AI 概括")

    content = summary_path.read_text(encoding="utf-8")
    # 去掉自动生成的标题行，只返回正文
    lines = content.split("\n")
    if lines and lines[0].startswith("# "):
        content = "\n".join(lines[1:]).strip()

    return {"paper_id": paper_id, "summary": content}


@router.get("/{paper_id}/annotations", response_model=List[PaperAnnotationResponse])
def list_annotations(paper_id: int, db: Session = Depends(get_db)):
    paper = db.query(Paper).filter(Paper.id == paper_id).first()
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")
    return (
        db.query(PaperAnnotation)
        .filter(PaperAnnotation.paper_id == paper_id)
        .order_by(PaperAnnotation.page_number.asc(), PaperAnnotation.created_at.asc())
        .all()
    )


@router.post("/{paper_id}/annotations", response_model=PaperAnnotationResponse)
def create_annotation(
    paper_id: int,
    payload: PaperAnnotationCreate,
    db: Session = Depends(get_db),
):
    paper = db.query(Paper).filter(Paper.id == paper_id).first()
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")

    annotation = PaperAnnotation(
        paper_id=paper_id,
        page_number=payload.page_number,
        selected_text=payload.selected_text,
        note=payload.note,
        color=payload.color,
    )
    db.add(annotation)
    db.commit()
    db.refresh(annotation)
    return annotation


@router.delete("/{paper_id}/annotations/{annotation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_annotation(paper_id: int, annotation_id: int, db: Session = Depends(get_db)):
    annotation = (
        db.query(PaperAnnotation)
        .filter(PaperAnnotation.id == annotation_id, PaperAnnotation.paper_id == paper_id)
        .first()
    )
    if not annotation:
        raise HTTPException(status_code=404, detail="Annotation not found")
    db.delete(annotation)
    db.commit()
    return None


@router.post("/{paper_id}/extract-metadata")
async def extract_metadata(paper_id: int, db: Session = Depends(get_db)):
    """使用 LLM 补全/校正论文元数据。"""
    paper = db.query(Paper).filter(Paper.id == paper_id).first()
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")

    project_root = Path(__file__).resolve().parents[3]
    pdf_path = project_root / paper.file_path
    if not pdf_path.exists():
        raise HTTPException(status_code=404, detail="PDF file not found")

    parser = PDFParser()
    enhanced = await parser.enhance_metadata_with_llm(str(pdf_path))

    update_data = {}
    if enhanced.get("title"):
        update_data["title"] = enhanced["title"]
        paper.title = enhanced["title"]
    if enhanced.get("authors"):
        paper.authors = enhanced["authors"]
    if enhanced.get("year"):
        paper.year = enhanced["year"]
    if enhanced.get("journal"):
        paper.journal = enhanced["journal"]
    if enhanced.get("abstract"):
        paper.abstract = enhanced["abstract"]
    if enhanced.get("doi"):
        paper.doi = enhanced["doi"]

    paper.metadata_json = {**(paper.metadata_json or {}), **enhanced}
    db.commit()
    db.refresh(paper)

    return PaperDetail.model_validate(paper)
