from typing import List, Dict, Any, Optional

from sqlalchemy.orm import Session

from app.core.logger import logger
from app.core.config import config
from app.models import Paper, Chunk
from app.services.pdf_parser import PDFParser
from app.services.embedding import TextChunker
from app.services.reference_parser import rebuild_citation_edges
from app.services.retrieval import get_vector_store


class PaperProcessor:
    def __init__(self):
        self.parser = PDFParser()
        self.chunker = TextChunker()
        self.vector_store = get_vector_store()

    def process(self, paper: Paper, db: Session) -> Dict[str, Any]:
        pdf_path = config.runtime_root / paper.file_path

        if not pdf_path.exists():
            return {"status": "error", "message": "PDF file not found"}

        # 1. 提取文本
        pages = self.parser.extract_text(str(pdf_path))

        # 2. 分块
        chunks_data = self.chunker.chunk_pages(pages)

        # 3. 清除旧 chunks
        db.query(Chunk).filter(Chunk.paper_id == paper.id).delete()
        self.vector_store.delete_by_paper_id(paper.id)

        # 4. 保存 chunks 到 SQLite
        db_chunks = []
        for i, cd in enumerate(chunks_data):
            chunk = Chunk(
                paper_id=paper.id,
                content=cd["content"],
                page_number=cd["page_number"],
                chunk_index=i,
                section_title=cd.get("section_title"),
                chunk_type=cd.get("chunk_type", "paragraph"),
                token_count=cd.get("token_count"),
            )
            db.add(chunk)
            db_chunks.append(cd)

        # 4b. 摘要级 chunk（abstract 字段或首页启发式；无法生成则跳过）
        abstract_cd = self._build_abstract_chunk(paper, pages)
        if abstract_cd is not None:
            db.add(Chunk(
                paper_id=paper.id,
                content=abstract_cd["content"],
                page_number=abstract_cd.get("page_number"),
                chunk_index=-1,
                section_title=None,
                chunk_type="abstract",
                token_count=None,
            ))
            db_chunks.append(abstract_cd)

        db.commit()

        # 5. 向量化并写入 ChromaDB
        paper_metadata = {
            "title": paper.title,
            "authors": paper.authors,
            "year": paper.year,
        }
        self.vector_store.add_chunks(paper.id, db_chunks, paper_metadata)

        # 6. 参考文献解析建引用边（Phase G / G1；失败隔离：仅记 warning，不影响入库主流程）
        try:
            full_text = "\n".join((p.get("text") or "") for p in pages)
            rebuild_citation_edges(db, paper.id, full_text)
        except Exception as e:
            db.rollback()
            logger.warning(f"[references] 引用边构建失败 paper_id={paper.id}: {e}", exc_info=True)

        return {
            "status": "ok",
            "pages": len(pages),
            "chunks": len(db_chunks),
        }

    @staticmethod
    def _build_abstract_chunk(paper: Paper, pages: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """生成摘要级 chunk：abstract 字段非空时优先使用；否则取首页文本前 1500 字符；两者皆空返回 None。"""
        content = (paper.abstract or "").strip()
        page_number = None
        if not content:
            if not pages:
                return None
            first_text = (pages[0].get("text") or "").strip()
            if not first_text:
                return None
            content = first_text[:1500]
            page_number = pages[0].get("page_number")
        return {
            "id": f"p{paper.id}_c-1",  # 对齐 ChromaDB id = p{pid}_c{chunk_index} 不变式（eval 命中匹配依赖）
            "content": content,
            "page_number": page_number,
            "chunk_type": "abstract",
            "chunk_index": -1,
        }
