from pathlib import Path
from typing import List, Dict, Any

from sqlalchemy.orm import Session

from app.models import Paper, Chunk
from app.services.pdf_parser import PDFParser
from app.services.embedding import TextChunker
from app.services.retrieval import get_vector_store


class PaperProcessor:
    def __init__(self):
        self.parser = PDFParser()
        self.chunker = TextChunker()
        self.vector_store = get_vector_store()

    def process(self, paper: Paper, db: Session) -> Dict[str, Any]:
        project_root = Path(__file__).resolve().parents[3]
        pdf_path = project_root / paper.file_path

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

        db.commit()

        # 5. 向量化并写入 ChromaDB
        paper_metadata = {
            "title": paper.title,
            "authors": paper.authors,
            "year": paper.year,
        }
        self.vector_store.add_chunks(paper.id, db_chunks, paper_metadata)

        return {
            "status": "ok",
            "pages": len(pages),
            "chunks": len(db_chunks),
        }
