"""批量导入 papers/ 目录中的 PDF 到 PaperMind（Phase A / Task A2）。

用法：
    # 干跑（只打印将导入什么，不上传）——改动后务必先干跑验证：
    cd backend && env -u PYTHONPATH venv/bin/python -u ../scripts/import_papers.py --dry-run
    # 实际导入：
    cd backend && env -u PYTHONPATH venv/bin/python -u ../scripts/import_papers.py

行为：
1. SQLite 直读已入库文件的 file_path，按内容 MD5 哈希判重；
2. 分批调用 POST /api/papers/import（复用 P1 异步上传管线）；
3. 轮询 SQLite chunks 表，等待每篇论文向量化完成；
4. 输出汇总表（成功 / 失败 / 每篇 chunk 数）。

注意：
- requests 必须 trust_env=False——本机系统代理会把 localhost 请求打出 502；
- 判重不能用 filename（导入接口会自动改名加 _1 后缀）也不能用 API 列表
  （PaperListItem 不含 file_path）——两次重复导入事故的教训；
- 必须用 python -u（无缓冲）运行，否则超时强杀时看不到已执行的上传。
"""

import hashlib
import sqlite3
import sys
import time
from pathlib import Path

import requests

API = "http://127.0.0.1:8000"
ROOT = Path(__file__).resolve().parents[1]
PAPERS_DIR = ROOT / "papers"
DB = ROOT / "data" / "papers.db"

# 与库内 demo-paper_1.pdf 内容重复（MD5 相同），跳过
SKIP_FILES = {"demo-paper.pdf"}
BATCH_SIZE = 4
POLL_INTERVAL = 15
POLL_TIMEOUT = 2400  # 向量化兜底 40 分钟


def _md5(path: Path) -> str:
    """计算文件内容 MD5（流式读取，避免大文件占内存）。"""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def main() -> int:
    dry_run = "--dry-run" in sys.argv

    # 1. 已入库文件的内容哈希集合（SQLite 直读 file_path——
    #    导入接口遇到同名文件会自动改名加 _1/_2 后缀，DB 里的 filename
    #    与磁盘原文件名永远对不上；API 列表项又不含 file_path，
    #    所以必须直读 DB 按内容哈希判重）
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = conn.execute("SELECT id, file_path FROM papers").fetchall()
    conn.close()
    existing_hashes = set()
    for _, fp_str in rows:
        fp = ROOT / (fp_str or "")
        if fp.is_file():
            existing_hashes.add(_md5(fp))
    print(f"[import] 库内已有 {len(rows)} 篇（{len(existing_hashes)} 个内容哈希）")
    if len(existing_hashes) != len(rows):
        missing = [pid for pid, fp_str in rows if not (ROOT / (fp_str or "")).is_file()]
        print(f"[import] 警告: {len(missing)} 条记录的 PDF 文件缺失，未纳入哈希判重: {missing}")

    # 2. 待导入清单（内容哈希判重 + 显式跳过名单）
    todo = []
    for p in sorted(PAPERS_DIR.glob("*.pdf")):
        if p.name in SKIP_FILES:
            continue
        if _md5(p) in existing_hashes:
            continue
        todo.append(p)
    print(f"[import] 待导入 {len(todo)} 篇: {[p.name for p in todo]}")
    if not todo:
        print("[import] 无新文件，退出")
        return 0
    if dry_run:
        print("[import] --dry-run 模式，不上传。退出。")
        return 0

    s = requests.Session()
    s.trust_env = False

    # 3. 分批上传
    imported: list[tuple[int, str]] = []
    failed: list[str] = []
    for i in range(0, len(todo), BATCH_SIZE):
        batch = todo[i : i + BATCH_SIZE]
        handles = []
        try:
            files = []
            for p in batch:
                fh = open(p, "rb")
                handles.append(fh)
                files.append(("files", (p.name, fh, "application/pdf")))
            print(f"[import] 上传批次 {i // BATCH_SIZE + 1}: {[p.name for p in batch]}")
            r = s.post(f"{API}/api/papers/import", files=files, timeout=600)
            r.raise_for_status()
            for it in r.json()["items"]:
                imported.append((it["id"], it["filename"]))
                print(f"  [OK] id={it['id']} {it['filename']} -> {(it.get('title') or '')[:50]}")
        except Exception as e:
            failed.extend(p.name for p in batch)
            print(f"  [FAIL] 批次失败: {e}")
        finally:
            for fh in handles:
                fh.close()

    # 4. 轮询向量化完成（chunks > 0 视为完成）
    print(f"[import] 等待向量化（{len(imported)} 篇，每 {POLL_INTERVAL}s 轮询）...")
    deadline = time.time() + POLL_TIMEOUT
    pending = {pid for pid, _ in imported}
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    chunk_counts: dict[int, int] = {}
    while pending and time.time() < deadline:
        time.sleep(POLL_INTERVAL)
        placeholders = ",".join("?" * len(pending))
        rows = conn.execute(
            f"SELECT paper_id, COUNT(*) FROM chunks WHERE paper_id IN ({placeholders}) GROUP BY paper_id",
            tuple(pending),
        ).fetchall()
        for pid, cnt in rows:
            if cnt > 0 and pid in pending:
                pending.discard(pid)
                chunk_counts[pid] = cnt
                name = next(n for i, n in imported if i == pid)
                print(f"  [向量化完成] id={pid} {name} chunks={cnt}")
        if pending:
            print(f"  ... 剩余 {len(pending)} 篇处理中")
    conn.close()

    # 5. 汇总
    print("\n===== 导入汇总 =====")
    print(f"成功导入: {len(imported)} 篇（其中向量化完成 {len(chunk_counts)} 篇）")
    for pid, name in imported:
        mark = f"chunks={chunk_counts[pid]}" if pid in chunk_counts else "向量化未完成"
        print(f"  id={pid:<3} {name:<45} {mark}")
    if pending:
        print(f"向量化超时未完成: {sorted(pending)}")
    if failed:
        print(f"上传失败: {failed}")
    return 0 if not failed and not pending else 1


if __name__ == "__main__":
    sys.exit(main())
