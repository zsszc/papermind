import csv
import io
from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Paper
from app.core.config import config
from app.services.backup import auto_backup, cleanup_old_backups, create_backup

router = APIRouter()

EXPORT_COLUMNS = [
    ("id", "ID"),
    ("title", "标题"),
    ("authors", "作者"),
    ("year", "年份"),
    ("journal", "期刊/会议"),
    ("doi", "DOI"),
    ("status", "阅读状态"),
    ("tags", "标签"),
    ("filename", "文件名"),
    ("created_at", "导入时间"),
]


def _paper_to_row(paper: Paper) -> dict:
    tags = ", ".join(t.name for t in paper.tags) if paper.tags else ""
    return {
        "id": paper.id,
        "title": paper.title or "",
        "authors": paper.authors or "",
        "year": paper.year or "",
        "journal": paper.journal or "",
        "doi": paper.doi or "",
        "status": paper.status or "",
        "tags": tags,
        "filename": paper.filename or "",
        "created_at": paper.created_at.strftime("%Y-%m-%d %H:%M:%S") if paper.created_at else "",
    }


def _format_citation(paper: Paper, fmt: str) -> str:
    authors = paper.authors or "匿名"
    title = paper.title or "未命名"
    year = paper.year or "n.d."
    journal = paper.journal or ""

    if fmt == "APA":
        return f"{authors} ({year}). {title}. {journal}".strip()
    if fmt == "MLA":
        return f'{authors}. "{title}." {journal}, {year}.'.strip()
    # 默认 GB/T 7714
    return f"{authors}. {title}[J]. {journal}, {year}.".strip()


@router.get("/papers/csv")
def export_papers_csv(db: Session = Depends(get_db)):
    """导出文献元数据为 CSV。"""
    papers = db.query(Paper).order_by(Paper.created_at.desc()).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([label for _, label in EXPORT_COLUMNS])
    for paper in papers:
        row = _paper_to_row(paper)
        writer.writerow([row[key] for key, _ in EXPORT_COLUMNS])

    output.seek(0)
    filename = f"papermind_papers_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return StreamingResponse(
        io.BytesIO(output.getvalue().encode("utf-8-sig")),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/papers/excel")
def export_papers_excel(db: Session = Depends(get_db)):
    """导出文献元数据为 Excel。"""
    try:
        import openpyxl
    except ImportError:
        raise HTTPException(status_code=500, detail="缺少 openpyxl，无法导出 Excel")

    papers = db.query(Paper).order_by(Paper.created_at.desc()).all()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "文献列表"
    ws.append([label for _, label in EXPORT_COLUMNS])
    for paper in papers:
        row = _paper_to_row(paper)
        ws.append([row[key] for key, _ in EXPORT_COLUMNS])

    # 简单调整列宽
    ws.column_dimensions["A"].width = 8
    ws.column_dimensions["B"].width = 50
    ws.column_dimensions["C"].width = 30
    ws.column_dimensions["D"].width = 10
    ws.column_dimensions["E"].width = 30
    ws.column_dimensions["F"].width = 25
    ws.column_dimensions["G"].width = 12
    ws.column_dimensions["H"].width = 20
    ws.column_dimensions["I"].width = 25
    ws.column_dimensions["J"].width = 20

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    filename = f"papermind_papers_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/papers/bib")
def export_papers_bib(format: str = "GB/T 7714", db: Session = Depends(get_db)):
    """导出引用列表为格式化文本。"""
    fmt = format.upper()
    if fmt not in {"GB/T 7714", "APA", "MLA"}:
        fmt = config.get("export.citation_format", "GB/T 7714")
    # 统一处理别名
    if "GB" in fmt or "7714" in fmt:
        fmt = "GB/T 7714"

    papers = db.query(Paper).order_by(Paper.created_at.desc()).all()
    lines = []
    for i, paper in enumerate(papers, start=1):
        lines.append(f"[{i}] {_format_citation(paper, fmt)}")

    content = "\n".join(lines)
    output = io.BytesIO(content.encode("utf-8"))
    filename = f"papermind_citations_{fmt.replace('/', '_')}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    return StreamingResponse(
        output,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/backup")
def export_backup():
    """打包全量数据为 zip 备份。"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"papermind_backup_{timestamp}.zip"
    backup_data = create_backup(include_config=False)

    return StreamingResponse(
        io.BytesIO(backup_data),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/backup/auto")
def trigger_auto_backup():
    """触发一次自动备份，并清理旧备份。"""
    backup_path = auto_backup()
    cleanup_old_backups()
    return {"status": "ok", "path": str(backup_path)}
