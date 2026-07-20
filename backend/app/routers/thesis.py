import os
import shutil
import re
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import ThesisFile, ThesisCitation, Paper
from app.schemas import (
    ThesisFileResponse,
    ThesisFileListResponse,
    ThesisAnalyzeRequest,
    ThesisAnalyzeResponse,
    ThesisCitationResponse,
    ThesisCitationMapResponse,
    ChapterCitationMapItem,
    ThesisCitationUpdate,
)
from app.services.docx_parser import DocxParser
from app.services.llm import llm_service
from app.core.logger import logger

router = APIRouter()


def get_thesis_dir() -> Path:
    project_root = Path(__file__).resolve().parents[3]
    thesis_dir = project_root / "my-thesis"
    thesis_dir.mkdir(parents=True, exist_ok=True)
    return thesis_dir


def _extract_surnames(authors_text: str) -> List[str]:
    """从引用文本中提取作者姓氏列表。"""
    text = authors_text.replace("et al.", "").replace("et al", "")
    parts = re.split(r",|\band\b|&", text)
    surnames = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        surname = part.split()[0]
        surname = re.sub(r"[^\w]", "", surname)
        if surname:
            surnames.append(surname.lower())
    return surnames


def find_paper_by_citation(citation_text: str, db: Session) -> Optional[Paper]:
    """根据引用标记反向查找文献库中的论文。"""
    # 尝试提取 [1] 中的数字
    numbers_match = re.search(r"\[(\d+)\]", citation_text)
    if numbers_match:
        # 目前只支持按序号匹配，序号与 paper.id 通常不对应，需要后续用户手动关联
        return None

    # 尝试匹配 (Author, year) 或 Author et al., 2024
    author_year_match = re.search(r"\(?([A-Za-z\s,\.&]+),?\s*(\d{4})", citation_text)
    if author_year_match:
        authors = author_year_match.group(1).strip()
        year = int(author_year_match.group(2))
        surnames = _extract_surnames(authors)
        if not surnames:
            return None
        papers = db.query(Paper).filter(Paper.year == year).all()
        for p in papers:
            if not p.authors:
                continue
            paper_authors_lower = p.authors.lower()
            if all(surname in paper_authors_lower for surname in surnames):
                return p
    return None


@router.post("/upload", response_model=ThesisFileResponse)
async def upload_thesis(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    if not file.filename or not file.filename.lower().endswith(".docx"):
        raise HTTPException(status_code=400, detail="只支持 .docx 文件")

    thesis_dir = get_thesis_dir()
    safe_name = Path(file.filename).name
    target_path = thesis_dir / safe_name

    counter = 1
    while target_path.exists():
        stem = Path(safe_name).stem
        suffix = Path(safe_name).suffix
        target_path = thesis_dir / f"{stem}_{counter}{suffix}"
        counter += 1

    with open(target_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    parser = DocxParser()
    parsed = parser.parse(str(target_path))

    thesis = ThesisFile(
        title=parsed.get("title") or target_path.stem,
        file_path=str(target_path.relative_to(Path(__file__).resolve().parents[3])),
        filename=target_path.name,
        chapter_structure=parsed.get("chapters", []),
        word_count=parsed.get("word_count"),
        metadata_json={"citations_detected": len(parsed.get("citations", []))},
    )
    db.add(thesis)
    db.flush()

    # 保存检测到的引用
    for c in parsed.get("citations", []):
        paper = find_paper_by_citation(c["citation_text"], db)
        chapter_index = None
        for idx, ch in enumerate(parsed.get("chapters", [])):
            if ch["start_paragraph"] <= c["paragraph_index"] <= ch["end_paragraph"]:
                chapter_index = idx
                break

        citation = ThesisCitation(
            thesis_id=thesis.id,
            paper_id=paper.id if paper else None,
            chapter_index=chapter_index,
            citation_text=c["citation_text"],
            context=c["context"],
            detected_auto=True,
        )
        db.add(citation)

    db.commit()
    db.refresh(thesis)
    return thesis


@router.get("", response_model=ThesisFileListResponse)
def list_thesis(db: Session = Depends(get_db)):
    items = db.query(ThesisFile).order_by(ThesisFile.created_at.desc()).all()
    return ThesisFileListResponse(total=len(items), items=items)


@router.get("/{thesis_id}", response_model=ThesisFileResponse)
def get_thesis(thesis_id: int, db: Session = Depends(get_db)):
    thesis = db.query(ThesisFile).filter(ThesisFile.id == thesis_id).first()
    if not thesis:
        raise HTTPException(status_code=404, detail="Thesis not found")
    return thesis


@router.delete("/{thesis_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_thesis(thesis_id: int, db: Session = Depends(get_db)):
    thesis = db.query(ThesisFile).filter(ThesisFile.id == thesis_id).first()
    if not thesis:
        raise HTTPException(status_code=404, detail="Thesis not found")

    # 删除关联引用
    db.query(ThesisCitation).filter(ThesisCitation.thesis_id == thesis_id).delete()
    db.delete(thesis)
    db.commit()

    # 可选：删除本地文件
    file_path = Path(__file__).resolve().parents[3] / thesis.file_path
    if file_path.exists():
        try:
            os.remove(file_path)
        except OSError:
            pass
    return None


@router.get("/{thesis_id}/citations")
def get_thesis_citations(thesis_id: int, db: Session = Depends(get_db)):
    thesis = db.query(ThesisFile).filter(ThesisFile.id == thesis_id).first()
    if not thesis:
        raise HTTPException(status_code=404, detail="Thesis not found")
    citations = db.query(ThesisCitation).filter(ThesisCitation.thesis_id == thesis_id).all()
    return citations


@router.put("/{thesis_id}/citations/{citation_id}")
def update_thesis_citation(
    thesis_id: int,
    citation_id: int,
    payload: ThesisCitationUpdate,
    db: Session = Depends(get_db),
):
    """手动关联/取消关联引用标记对应的文献。"""
    citation = (
        db.query(ThesisCitation)
        .filter(ThesisCitation.id == citation_id, ThesisCitation.thesis_id == thesis_id)
        .first()
    )
    if not citation:
        raise HTTPException(status_code=404, detail="Citation not found")

    if payload.paper_id is not None:
        paper = db.query(Paper).filter(Paper.id == payload.paper_id).first()
        if not paper:
            raise HTTPException(status_code=404, detail="Paper not found")

    citation.paper_id = payload.paper_id
    db.commit()
    db.refresh(citation)
    return citation


@router.get("/{thesis_id}/citation-map", response_model=ThesisCitationMapResponse)
def get_citation_map(thesis_id: int, db: Session = Depends(get_db)):
    """生成章节-文献映射视图数据，用于发现引用盲区。"""
    thesis = db.query(ThesisFile).filter(ThesisFile.id == thesis_id).first()
    if not thesis:
        raise HTTPException(status_code=404, detail="Thesis not found")

    citations = db.query(ThesisCitation).filter(ThesisCitation.thesis_id == thesis_id).all()
    chapters = thesis.chapter_structure or []

    # 预加载论文标题，避免 N+1
    paper_ids = {c.paper_id for c in citations if c.paper_id}
    papers = {p.id: p.title for p in db.query(Paper).filter(Paper.id.in_(paper_ids)).all()}

    total_citations = len(citations)
    matched_citations = sum(1 for c in citations if c.paper_id)

    chapter_items = []
    for idx, ch in enumerate(chapters):
        ch_citations = [c for c in citations if c.chapter_index == idx]
        ch_paper_ids = sorted({c.paper_id for c in ch_citations if c.paper_id})
        chapter_items.append(ChapterCitationMapItem(
            chapter_index=idx,
            chapter_title=ch.get("title", f"第{idx + 1}章"),
            level=ch.get("level", 1),
            paper_ids=ch_paper_ids,
            paper_titles={pid: papers.get(pid) for pid in ch_paper_ids},
            citation_count=len(ch_citations),
        ))

    return ThesisCitationMapResponse(
        thesis_id=thesis_id,
        total_citations=total_citations,
        matched_citations=matched_citations,
        chapters=chapter_items,
    )


@router.get("/{thesis_id}/chapters/{chapter_index}/text")
def get_chapter_text(
    thesis_id: int,
    chapter_index: int,
    db: Session = Depends(get_db),
):
    thesis = db.query(ThesisFile).filter(ThesisFile.id == thesis_id).first()
    if not thesis:
        raise HTTPException(status_code=404, detail="Thesis not found")

    parser = DocxParser()
    project_root = Path(__file__).resolve().parents[3]
    file_path = project_root / thesis.file_path
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Word file not found")

    parsed = parser.parse(str(file_path))
    chapters = parsed.get("chapters", [])
    paragraphs = parsed.get("paragraphs", [])

    if chapter_index < 0 or chapter_index >= len(chapters):
        raise HTTPException(status_code=400, detail="章节索引超出范围")

    chapter = chapters[chapter_index]
    text = parser.extract_chapter_text(paragraphs, chapter)
    return {
        "thesis_id": thesis_id,
        "chapter_index": chapter_index,
        "title": chapter["title"],
        "text": text,
    }


@router.post("/{thesis_id}/analyze", response_model=ThesisAnalyzeResponse)
async def analyze_thesis(
    thesis_id: int,
    request: ThesisAnalyzeRequest = ThesisAnalyzeRequest(),
    db: Session = Depends(get_db),
):
    thesis = db.query(ThesisFile).filter(ThesisFile.id == thesis_id).first()
    if not thesis:
        raise HTTPException(status_code=404, detail="Thesis not found")

    parser = DocxParser()
    project_root = Path(__file__).resolve().parents[3]
    file_path = project_root / thesis.file_path
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Word file not found")

    parsed = parser.parse(str(file_path))
    paragraphs = parsed.get("paragraphs", [])
    chapters = parsed.get("chapters", [])

    if request.chapter_index is not None:
        if request.chapter_index >= len(chapters):
            raise HTTPException(status_code=400, detail="章节索引超出范围")
        chapter = chapters[request.chapter_index]
        chapter_text = parser.extract_chapter_text(paragraphs, chapter)
        chapter_title = chapter["title"]
    else:
        # 默认分析第一章（通常是绪论）
        chapter = chapters[0] if chapters else None
        chapter_text = parser.extract_chapter_text(paragraphs, chapter) if chapter else ""
        chapter_title = chapter["title"] if chapter else thesis.title

    # 限制文本长度，避免 LLM 处理过慢
    chapter_text = chapter_text[:6000]

    if not chapter_text or len(chapter_text.strip()) < 30:
        logger.warning(f"[ThesisAnalyze] thesis_id={thesis_id} chapter_index={request.chapter_index} 文本过短，跳过分析")
        raise HTTPException(status_code=400, detail="章节内容为空或过短，无法生成评审意见")

    prompt = f"""你是一位资深的学术论文评审专家。请对以下毕业论文章节进行评审，指出逻辑漏洞、表述问题、学术规范问题，并给出具体修改建议。

章节标题：{chapter_title}

章节内容：
{chapter_text}

请按以下结构输出评审意见（Markdown 格式）：
1. 总体评价
2. 主要问题（分点列出）
3. 具体修改建议
4. 学术规范检查
"""

    messages = [
        {"role": "system", "content": "你是资深的学术论文评审专家，专注于医学图像分析和深度学习领域的论文写作指导。"},
        {"role": "user", "content": prompt},
    ]

    try:
        logger.info(f"[ThesisAnalyze] thesis_id={thesis_id} chapter_index={request.chapter_index} text_len={len(chapter_text)} 开始调用 LLM")
        suggestions = await llm_service.chat_completion(messages)
        logger.info(f"[ThesisAnalyze] thesis_id={thesis_id} 成功获取评审意见，长度={len(suggestions)}")
    except Exception as e:
        logger.exception(f"[ThesisAnalyze] thesis_id={thesis_id} LLM 调用失败")
        raise HTTPException(status_code=500, detail=f"AI 评审调用失败: {e}") from e

    try:
        citations_query = db.query(ThesisCitation).filter(ThesisCitation.thesis_id == thesis_id)
        if request.chapter_index is not None:
            citations_query = citations_query.filter(ThesisCitation.chapter_index == request.chapter_index)
        citations = citations_query.all()

        return ThesisAnalyzeResponse(
            thesis_id=thesis.id,
            chapter_title=chapter_title,
            suggestions=suggestions,
            citations=[ThesisCitationResponse.model_validate(c) for c in citations],
        )
    except Exception as e:
        logger.exception("[ThesisAnalyze] 序列化响应失败")
        raise HTTPException(status_code=500, detail=f"响应序列化失败: {e}") from e


@router.post("/{thesis_id}/suggest-citations")
async def suggest_citations(
    thesis_id: int,
    paragraph: str,
    db: Session = Depends(get_db),
):
    """针对输入段落推荐可引用的文献。"""
    thesis = db.query(ThesisFile).filter(ThesisFile.id == thesis_id).first()
    if not thesis:
        raise HTTPException(status_code=404, detail="Thesis not found")

    # 语义检索相关文献片段
    from app.services.retrieval import get_vector_store
    store = get_vector_store()
    retrieved = []
    if store.available():
        retrieved = store.search(query=paragraph, top_k=5)

    context = "\n\n".join([
        f"[{i}] {r.get('title') or '未知文献'}\n{r.get('content')}"
        for i, r in enumerate(retrieved, start=1)
    ])

    prompt = f"""你正在为一段毕业论文内容推荐可引用的文献。请基于以下文献片段，推荐 3-5 篇最相关的文献，并说明每篇文献可以在当前段落的哪个位置引用。

当前段落：
{paragraph}

候选文献片段：
{context}

请按以下格式输出（Markdown）：
1. 推荐引用 1：文献标题 + 可引用的具体内容
2. 推荐引用 2：...
"""

    messages = [
        {"role": "system", "content": "你是学术写作助手，擅长为论文段落推荐合适的参考文献。"},
        {"role": "user", "content": prompt},
    ]
    result = await llm_service.chat_completion(messages)

    return {
        "thesis_id": thesis_id,
        "paragraph": paragraph,
        "suggestions": result,
        "citations": retrieved,
    }
