"""一次性脚本：重处理全部论文，生成摘要级 chunk（Phase B-B2 激活）。

直接调用 PaperProcessor.process 对每篇已处理论文重新分块+向量化。
运行：cd backend && env -u PYTHONPATH venv/bin/python ../scripts/reprocess_abstract_chunks.py
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.database import SessionLocal
from app.models import Paper
from app.services.processor import PaperProcessor


def main():
    db = SessionLocal()
    processor = PaperProcessor()
    papers = db.query(Paper).order_by(Paper.id).all()
    print(f"[reprocess] 共 {len(papers)} 篇待重处理")
    ok, fail = 0, 0
    t0 = time.time()
    for p in papers:
        try:
            result = processor.process(p, db)
            status = result.get("status") if isinstance(result, dict) else result
            print(f"[reprocess] id={p.id}《{(p.title or '')[:30]}》 -> {status}")
            ok += 1
        except Exception as e:
            print(f"[reprocess] id={p.id} 失败: {type(e).__name__}: {e}")
            fail += 1
            db.rollback()
    print(f"[reprocess] 完成：成功 {ok} 失败 {fail}，耗时 {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
