"""一次性回填：为存量论文重建引用边（Phase G G1 配套）。

对 papers 表中有 PDF 的每篇论文：解析全文 → rebuild_citation_edges（幂等，先清出边再重建）。
用法：cd backend && env -u PYTHONPATH venv/bin/python ../scripts/backfill_citations.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.database import SessionLocal, ensure_schema  # noqa: E402
from app.models import Paper, PaperCitation  # noqa: E402
from app.services.pdf_parser import PDFParser  # noqa: E402
from app.services.reference_parser import rebuild_citation_edges  # noqa: E402


def main() -> None:
    ensure_schema()  # 确保 paper_citations 表存在（幂等）
    db = SessionLocal()
    parser = PDFParser()
    ok = edges = 0
    try:
        papers = db.query(Paper).filter(Paper.file_path.isnot(None)).all()
        for paper in papers:
            pdf = Path(paper.file_path)
            if not pdf.is_absolute():
                pdf = Path(__file__).resolve().parents[1] / pdf
            if not pdf.exists():
                print(f"[backfill] id={paper.id} PDF 缺失，跳过")
                continue
            try:
                full_text = parser.extract_text_full(str(pdf))
                n = rebuild_citation_edges(db, paper.id, full_text)
                edges += n
                ok += 1
                print(f"[backfill] id={paper.id}《{(paper.title or '')[:30]}》 -> {n} 条引用边")
            except Exception as e:
                db.rollback()
                print(f"[backfill] id={paper.id} 失败: {e}")
        total = db.query(PaperCitation).count()
        print(f"[backfill] 完成：成功 {ok}/{len(papers)}，本次建边 {edges}，全表边数 {total}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
