"""参考文献解析与引用边构建（Phase G / G1）。

契约见 specs/phases/phase-g-graphrag/spec.md §3.1：
- 启发式定位全文最后一个独立 References/参考文献 标题行，取其后文本
- 按编号条目切分（[n] 与 n. 两种主流格式）
- 标题候选提取：引号内文本优先，否则取年份前的最长段
- difflib.SequenceMatcher 相似度 ≥ 0.85 建边；自引跳过；重复边去重
- 日志前缀 [references]
"""

import re
from difflib import SequenceMatcher
from typing import List, Optional, Tuple

from sqlalchemy.orm import Session

from app.core.logger import logger
from app.models import Paper, PaperCitation

# 标题模糊匹配阈值（spec §3.1）
MATCH_THRESHOLD = 0.85

# 独立参考文献标题行：整行仅含 References / Bibliography / 参考文献（可带句点）
_HEADING_RE = re.compile(r"^\s*(references|bibliography|参考文献)\s*\.?\s*$", re.IGNORECASE)
# 条目起始标记：[12] 或 12. / 12)
_BRACKET_START_RE = re.compile(r"^\s*\[\d{1,3}\]\s*")
_DOT_START_RE = re.compile(r"^\s*\d{1,3}[.)]\s+")
# 年份（19xx/20xx，可带字母后缀如 2020a）
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}[a-z]?\b")
# 引号对（直引号 / 中文弯引号 / 直角引号）
_QUOTE_RE = re.compile(r'["“「](.+?)["”」]')
# 分段符：中英文句读
_SEP_RE = re.compile(r"[.,;。，；]")
# 标题候选最短长度（过短片段不可能是标题）
_MIN_TITLE_LEN = 10


def extract_references_section(full_text: str) -> Optional[str]:
    """定位全文最后一个独立参考文献标题行，返回其后文本；未定位到返回 None。"""
    lines = (full_text or "").splitlines()
    last_idx = None
    for i, line in enumerate(lines):
        if _HEADING_RE.match(line):
            last_idx = i
    if last_idx is None:
        return None
    section = "\n".join(lines[last_idx + 1:]).strip()
    return section or None


def split_entries(section_text: str) -> List[str]:
    """按编号条目切分参考文献段；换行续接的条目合并为一行；段前杂行忽略。"""
    entries: List[str] = []
    current: List[str] = []
    for raw in section_text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if _BRACKET_START_RE.match(line) or _DOT_START_RE.match(line):
            if current:
                entries.append(" ".join(current))
            # 去掉起始编号标记
            line = _BRACKET_START_RE.sub("", line)
            line = _DOT_START_RE.sub("", line)
            current = [line]
        elif current:
            current.append(line)
    if current:
        entries.append(" ".join(current))
    return [e.strip() for e in entries if e.strip()]


def _clean_title(text: str) -> str:
    """清洗标题候选：压缩空白，去掉首尾标点。"""
    text = re.sub(r"\s+", " ", text).strip()
    return text.strip(" .,;:'\"“”‘’()[]<>")


def extract_title_candidate(entry: str) -> Optional[str]:
    """从单条参考文献中提取标题候选：优先引号内文本，否则取年份前的最长段。"""
    quote_m = _QUOTE_RE.search(entry)
    if quote_m:
        candidate = quote_m.group(1)
    else:
        year_m = _YEAR_RE.search(entry)
        head = entry[:year_m.start()] if year_m else entry
        segments = [s.strip() for s in _SEP_RE.split(head) if s.strip()]
        if not segments:
            return None
        candidate = max(segments, key=len)
    candidate = _clean_title(candidate)
    if len(candidate) < _MIN_TITLE_LEN:
        return None
    return candidate


def _normalize(text: str) -> str:
    """匹配前归一化：小写 + 压缩空白。"""
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def best_match(candidate: str, choices: List[Tuple[int, str]]) -> Optional[Tuple[int, float]]:
    """在 (paper_id, title) 候选集中找相似度最高的标题；≥ 阈值返回 (paper_id, ratio)，否则 None。"""
    norm_cand = _normalize(candidate)
    if not norm_cand:
        return None
    best_id, best_ratio = None, 0.0
    for pid, title in choices:
        norm_title = _normalize(title)
        if not norm_title:
            continue
        ratio = SequenceMatcher(None, norm_cand, norm_title).ratio()
        if ratio > best_ratio:
            best_id, best_ratio = pid, ratio
    if best_id is not None and best_ratio >= MATCH_THRESHOLD:
        return best_id, best_ratio
    return None


def rebuild_citation_edges(db: Session, paper_id: int, full_text: str) -> int:
    """重建某篇论文的出向引用边（幂等：先清出边再解析重建），返回建边数。"""
    # 先清该 paper 的全部出边，保证重复执行/回填幂等
    db.query(PaperCitation).filter(PaperCitation.citing_id == paper_id).delete()

    section = extract_references_section(full_text)
    if section is None:
        db.commit()
        logger.info(f"[references] paper_id={paper_id} 未定位到参考文献段，出边已清空")
        return 0
    entries = split_entries(section)

    # 候选标题集：排除自身（自引跳过）
    choices = [
        (pid, title)
        for pid, title in db.query(Paper.id, Paper.title)
        .filter(Paper.title.isnot(None))
        .all()
        if pid != paper_id
    ]

    cited_ids = set()
    for entry in entries:
        candidate = extract_title_candidate(entry)
        if not candidate:
            continue
        hit = best_match(candidate, choices)
        if hit is not None:
            cited_ids.add(hit[0])  # set 去重重复边

    for cited_id in sorted(cited_ids):
        db.add(PaperCitation(citing_id=paper_id, cited_id=cited_id))
    db.commit()
    logger.info(
        f"[references] paper_id={paper_id} 解析条目 {len(entries)} 条，建边 {len(cited_ids)} 条"
    )
    return len(cited_ids)
