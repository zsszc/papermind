"""公开 RAG 评测 fixture 的校验与隔离数据库构建。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Union

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, _set_sqlite_pragma
from app.models import Chunk, Paper


@dataclass
class FixtureDatabase:
    """公开 fixture 的临时数据库及其生命周期。"""

    engine: Any
    session_factory: Any
    metadata: dict

    def close(self) -> None:
        self.engine.dispose()


def _validate_fixture(data: Any, path: Path) -> dict:
    if not isinstance(data, dict):
        raise ValueError(f"fixture 必须是 JSON 对象: {path}")
    for field in ("benchmark_id", "license", "papers"):
        if field not in data:
            raise ValueError(f"fixture 缺少字段 {field}: {path}")
    if not isinstance(data["papers"], list) or not data["papers"]:
        raise ValueError("fixture.papers 必须是非空列表")

    seen_uids: set[str] = set()
    for index, paper in enumerate(data["papers"], start=1):
        where = f"fixture 第 {index} 篇论文"
        if not isinstance(paper, dict):
            raise ValueError(f"{where} 必须是对象")
        uid = paper.get("paper_uid")
        if not isinstance(uid, str) or not uid.startswith("doi:") or len(uid) <= 4:
            raise ValueError(f"{where}.paper_uid 必须使用 doi:<doi>")
        if uid in seen_uids:
            raise ValueError(f"paper_uid 重复: {uid}")
        seen_uids.add(uid)
        if not isinstance(paper.get("title"), str) or not paper["title"].strip():
            raise ValueError(f"{where}.title 必须是非空字符串")
        chunks = paper.get("chunks")
        if not isinstance(chunks, list) or not chunks:
            raise ValueError(f"{where}.chunks 必须是非空列表")
        seen_indexes: set[int] = set()
        for chunk in chunks:
            chunk_index = chunk.get("chunk_index") if isinstance(chunk, dict) else None
            content = chunk.get("content") if isinstance(chunk, dict) else None
            if not isinstance(chunk_index, int) or isinstance(chunk_index, bool):
                raise ValueError(f"{where} chunk_index 必须是整数")
            if chunk_index in seen_indexes:
                raise ValueError(f"{where} chunk_index 重复: {chunk_index}")
            seen_indexes.add(chunk_index)
            if not isinstance(content, str) or len(content.strip()) < 20:
                raise ValueError(f"{where} chunk content 至少 20 个字符")
    return data


def load_fixture(path: Union[str, Path]) -> dict:
    """加载并严格校验公开 fixture JSON。"""
    path = Path(path)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent.parent / path
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"fixture 不是合法 JSON: {path}") from exc
    return _validate_fixture(data, path)


def open_fixture_database(path: Union[str, Path]) -> FixtureDatabase:
    """将公开 fixture seed 到独立内存 SQLite，绝不接触真实数据库。"""
    fixture = load_fixture(path)
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    event.listen(engine, "connect", _set_sqlite_pragma)
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = session_factory()
    try:
        for paper_id, item in enumerate(fixture["papers"], start=1):
            db.add(Paper(
                id=paper_id,
                title=item["title"],
                authors=item.get("authors", "PaperMind Evaluation Team"),
                year=item.get("year", 2026),
                journal=item.get("journal", "Synthetic Evaluation Corpus"),
                abstract=item.get("abstract"),
                doi=item["paper_uid"].removeprefix("doi:"),
                file_path=f"fixture/{paper_id}.txt",
                filename=f"fixture-{paper_id}.txt",
                source="synthetic",
                processed="done",
            ))
            db.flush()
            for chunk in item["chunks"]:
                db.add(Chunk(
                    paper_id=paper_id,
                    chunk_index=chunk["chunk_index"],
                    section_title=chunk.get("section_title"),
                    chunk_type=chunk.get("chunk_type", "paragraph"),
                    page_number=chunk.get("page_number"),
                    content=chunk["content"],
                ))
        db.commit()
    except Exception:
        db.rollback()
        engine.dispose()
        raise
    finally:
        db.close()
    return FixtureDatabase(
        engine=engine,
        session_factory=session_factory,
        metadata={
            "benchmark_id": fixture["benchmark_id"],
            "license": fixture["license"],
        },
    )
