import json
import re
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

import pdfplumber
from PyPDF2 import PdfReader

from app.core.logger import logger


class PDFParser:
    def parse_metadata(self, file_path: str) -> Dict[str, Any]:
        """解析 PDF 元数据，返回可序列化的字典。"""
        path = Path(file_path)
        metadata = {
            "title": None,
            "authors": None,
            "year": None,
            "journal": None,
            "abstract": None,
            "doi": None,
            "pages": 0,
            "filename": path.name,
        }

        # 1. PyPDF2 读取基础信息
        try:
            reader = PdfReader(str(path))
            info = reader.metadata or {}
            metadata["pages"] = len(reader.pages)

            title = info.get("/Title") or info.get("Title")
            if title and title.strip():
                metadata["title"] = title.strip()

            author = info.get("/Author") or info.get("Author")
            if author and author.strip():
                metadata["authors"] = author.strip()

            # 尝试从 metadata 的 subject 或 keywords 找 DOI
            subject = info.get("/Subject") or ""
            keywords = info.get("/Keywords") or ""
            doi = self._extract_doi(f"{subject} {keywords}")
            if doi:
                metadata["doi"] = doi
        except Exception:
            logger.warning(f"[PDFParser] 解析 XMP 元数据失败: {file_path}", exc_info=True)

        # 2. 分别用 PyPDF2 和 pdfplumber 提取前 3 页文本，择优补全元数据
        try:
            pypdf_front = self._extract_front_text_pypdf(path, max_pages=3)
            plumber_front = self._extract_front_text_plumber(path, max_pages=3)

            pypdf_meta = self._extract_from_front_text(path, pypdf_front)
            plumber_meta = self._extract_from_front_text(path, plumber_front)

            metadata = self._merge_metadata_candidates(metadata, pypdf_meta, plumber_meta)
        except Exception:
            logger.warning(f"[PDFParser] 双引擎提取元数据失败: {file_path}", exc_info=True)

        # 兜底：用文件名做标题
        if not metadata["title"]:
            metadata["title"] = path.stem.replace("_", " ")

        # 清理
        for key in ["title", "authors", "journal", "abstract", "doi"]:
            if metadata.get(key):
                metadata[key] = re.sub(r"\s+", " ", str(metadata[key])).strip()

        return metadata

    def _extract_from_front_text(self, path: Path, front_text: str) -> Dict[str, Any]:
        """从给定前页文本中提取元数据候选，并记录标题/作者所在行号。"""
        title, title_line_idx = self._infer_title_with_index(path, front_text)
        authors, authors_line_idx = self._infer_authors_with_index(path, front_text)
        return {
            "doi": self._extract_doi(front_text),
            "year": self._extract_year(front_text),
            "authors": authors,
            "authors_line_idx": authors_line_idx,
            "title": title,
            "title_line_idx": title_line_idx,
        }

    def _title_quality(self, title: Optional[str]) -> float:
        """评估标题质量：越像学术标题分越高。"""
        if not title:
            return 0.0
        title_indicators = {
            "with", "for", "of", "and", "based", "via", "using", "towards",
            "learning", "network", "networks", "model", "models", "analysis",
            "prediction", "segmentation", "classification", "detection",
            "multi", "cross", "hierarchical", "graph", "image", "pathology",
        }
        negative = {
            "we propose", "in this paper", "this study", "existing methods",
            "recent years", "however", "therefore", "thus", "models often",
        }
        score = 0.0
        lower = title.lower()
        score += sum(0.5 for ind in title_indicators if ind in lower)
        score -= sum(1.0 for neg in negative if neg in lower)
        # 长度适中
        if 20 <= len(title) <= 150:
            score += 1.0
        # 不以 The/This/These 开头
        if re.match(r"^(The|This|These|In|However|Therefore)\b", title, re.IGNORECASE):
            score -= 2.0
        return score

    def _merge_metadata_candidates(
        self,
        base: Dict[str, Any],
        meta_a: Dict[str, Any],
        meta_b: Dict[str, Any],
    ) -> Dict[str, Any]:
        """合并两个候选元数据，选择更完整、更合理的结果。"""
        result = dict(base)

        def _author_count(authors_str: Optional[str]) -> int:
            if not authors_str:
                return 0
            return len([p for p in authors_str.split(",") if p.strip()])

        # DOI：优先用非空的
        for meta in (meta_a, meta_b):
            if meta.get("doi") and not result.get("doi"):
                result["doi"] = meta["doi"]

        # Year：优先用非空的
        for meta in (meta_a, meta_b):
            if meta.get("year") and not result.get("year"):
                result["year"] = meta["year"]

        # Authors：选择作者数更多且更像人名的
        a_count = _author_count(meta_a.get("authors"))
        b_count = _author_count(meta_b.get("authors"))
        if a_count > 0 or b_count > 0:
            if a_count >= b_count and meta_a.get("authors"):
                result["authors"] = meta_a["authors"]
            elif meta_b.get("authors"):
                result["authors"] = meta_b["authors"]

        # Title：综合质量分和位置，不单纯看长度
        a_title = meta_a.get("title")
        b_title = meta_b.get("title")
        if a_title or b_title:
            a_score = self._title_quality(a_title) - (meta_a.get("title_line_idx", 999) * 0.01)
            b_score = self._title_quality(b_title) - (meta_b.get("title_line_idx", 999) * 0.01)
            best_title = a_title if a_score >= b_score else b_title
            if best_title and (not result.get("title") or self._title_quality(best_title) > self._title_quality(result["title"])):
                result["title"] = best_title

        return result

    async def enhance_metadata_with_llm(self, file_path: str) -> Dict[str, Any]:
        """使用 LLM 从前 3 页文本中提取结构化元数据。"""
        from app.services.llm import llm_service

        path = Path(file_path)
        front_text = self._extract_front_text(path, max_pages=3)
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
        result = await llm_service.chat_completion(messages, json_mode=True)
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
            logger.warning(f"[PDFParser] LLM 增强元数据解析失败: {e}", exc_info=True)
            return {}

    def extract_text(self, file_path: str) -> List[Dict[str, Any]]:
        """提取 PDF 全部文本，按页返回；自动处理双栏布局。"""
        pages = []
        with pdfplumber.open(file_path) as pdf:
            for i, page in enumerate(pdf.pages, start=1):
                text = self._extract_page_text(page)
                pages.append({
                    "page_number": i,
                    "text": text,
                    "width": page.width,
                    "height": page.height,
                })
        return pages

    def extract_text_full(self, file_path: str) -> str:
        """提取 PDF 全部文本并合并。"""
        parts = []
        with pdfplumber.open(file_path) as pdf:
            for page in pdf.pages:
                parts.append(self._extract_page_text(page))
        return "\n\n".join(parts)

    def _extract_page_text(self, page, x_tolerance: float = 1.5) -> str:
        """单页文本提取，自动检测并分栏。"""
        full_text = (page.extract_text(x_tolerance=x_tolerance) or "").strip()
        if not full_text:
            return ""

        # 非竖向页面或文本太少，直接返回整页结果
        if page.width >= page.height or len(full_text) < 200:
            return full_text

        # 尝试左右分栏提取
        mid = page.width / 2
        left_crop = page.crop((0, 0, mid, page.height))
        right_crop = page.crop((mid, 0, page.width, page.height))
        left_text = (left_crop.extract_text(x_tolerance=x_tolerance) or "").strip()
        right_text = (right_crop.extract_text(x_tolerance=x_tolerance) or "").strip()

        # 有一侧为空，视为单栏
        if not left_text or not right_text:
            return full_text

        full_lines = max(len(full_text.splitlines()), 1)
        split_lines = len(left_text.splitlines()) + len(right_text.splitlines())

        # 双栏特征：分栏后行数显著增加（左右文本不再被拼到同一行）
        if split_lines >= full_lines * 1.45:
            return left_text + "\n\n" + right_text

        return full_text

    def _extract_front_text(self, path: Path, max_pages: int = 3) -> str:
        """提取前 N 页文本，默认返回 pdfplumber 结果（保持向后兼容）。"""
        return self._extract_front_text_plumber(path, max_pages)

    def _extract_front_text_pypdf(self, path: Path, max_pages: int = 3) -> str:
        """使用 PyPDF2 提取前 N 页文本。"""
        try:
            reader = PdfReader(str(path))
            texts = []
            for i, page in enumerate(reader.pages[:max_pages], start=1):
                text = (page.extract_text() or "").strip()
                if text:
                    texts.append(text)
            return "\n\n".join(texts)
        except Exception:
            logger.warning(f"[PDFParser] PyPDF2 提取前页文本失败: {path}", exc_info=True)
            return ""

    def _extract_front_text_plumber(self, path: Path, max_pages: int = 3) -> str:
        """使用 pdfplumber 提取前 N 页文本，自动处理双栏。"""
        try:
            texts = []
            with pdfplumber.open(str(path)) as pdf:
                for i, page in enumerate(pdf.pages[:max_pages], start=1):
                    texts.append(self._extract_page_text(page))
            return "\n".join(texts)
        except Exception:
            logger.warning(f"[PDFParser] pdfplumber 提取前页文本失败: {path}", exc_info=True)
            return ""

    def _extract_doi(self, text: str) -> Optional[str]:
        if not text:
            return None
        pattern = re.compile(
            r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+",
            re.IGNORECASE,
        )
        match = pattern.search(text)
        return match.group(0) if match else None

    def _extract_year(self, text: str) -> Optional[int]:
        if not text:
            return None
        # 优先找版权年份 (c) 2024 或 Copyright 2024
        match = re.search(r"[Cc]opyright\s*[©]?\s*(\d{4})", text)
        if match:
            return int(match.group(1))
        # 兜底：找 1900-2099 年份
        years = re.findall(r"\b(19\d{2}|20\d{2})\b", text)
        if years:
            return int(years[0])
        return None

    def _infer_title(self, path: Path, front_text: str) -> Optional[str]:
        title, _ = self._infer_title_with_index(path, front_text)
        return title

    def _infer_title_with_index(self, path: Optional[Path], front_text: str) -> Tuple[Optional[str], int]:
        """从第一页推断标题：取前 3 页中位置靠前、长度合适的连续非噪声行作为标题。

        返回 (标题, 标题首行行号)。
        """
        if not front_text:
            return None, 999

        lines = [line.strip() for line in front_text.splitlines() if line.strip()]
        noise = {
            "abstract", "introduction", "keywords", "arxiv", "preprint",
            "journal", "proceedings", "conference", "university",
            "department", "email", "tel:", "fax:", "doi:", "https://",
            "figure", "table", "references", "acknowledgements", "related work",
            "original article", "article info", "received:", "accepted:", "published",
            "contents lists", "journal homepage", "all rights reserved", "copyright",
            "pattern analysis and applications", "medical image analysis",
        }

        # 收集候选行：长度合适、不是纯数字/标点、不包含噪声词
        candidates = []
        for i, line in enumerate(lines):
            if (
                len(line) > 10
                and len(line) < 300
                and not any(n in line.lower() for n in noise)
                and not re.match(r"^[\d\W]+$", line)
            ):
                candidates.append((i, line))

        if not candidates:
            return None, 999

        # 优先取最靠前的候选，并尝试与下一行合并成完整标题
        first_idx, first_line = candidates[0]
        title = first_line
        # 如果第二行紧接着且也是候选，且第一行不是以标点结尾，则合并
        if len(candidates) > 1 and candidates[1][0] == first_idx + 1:
            second_line = candidates[1][1]
            if not first_line.endswith((".", ":", "?", "!")):
                title = f"{first_line} {second_line}"
        return title, first_idx

    def _infer_authors(self, path: Path, front_text: str) -> Optional[str]:
        authors, _ = self._infer_authors_with_index(path, front_text)
        return authors

    def _infer_authors_with_index(self, path: Path, front_text: str) -> Tuple[Optional[str], int]:
        """从前 3 页推断作者列表，返回 (作者字符串, 作者块首行行号)。"""
        if not front_text:
            return None, 999

        # 1. 基于常见作者行模式（优先，通常能拿到完整人名）
        authors_from_pattern, line_idx = self._infer_authors_by_pattern(front_text)
        if authors_from_pattern:
            return ", ".join(authors_from_pattern), line_idx

        # 2. 基于邮箱反查作者名（兜底，可能只有缩写）
        authors_from_email = self._infer_authors_from_emails(front_text)
        if authors_from_email:
            return ", ".join(authors_from_email), 999

        return None, 999

    def _infer_authors_from_emails(self, text: str) -> List[str]:
        """通过机构邮箱前缀反推作者名。"""
        emails = re.findall(r"\b([a-zA-Z][\w.\-]*)@([\w\-]+\.[\w.\-]+)\b", text)
        if not emails:
            return []

        names = []
        seen = set()
        for local, domain in emails:
            # 仅处理学术/机构邮箱
            if not any(s in domain.lower() for s in [".edu", ".ac.", "university", "institute", "hospital", "lab", "org"]):
                continue
            name = self._email_local_to_name(local)
            if name and name.lower() not in seen:
                names.append(name)
                seen.add(name.lower())
        return names[:10]

    def _email_local_to_name(self, local: str) -> Optional[str]:
        """把邮箱前缀转换为人名，如 jdoe -> J. Doe，john.doe -> John Doe。"""
        if not local:
            return None
        # 去除数字后缀
        local = re.sub(r"\d+$", "", local)
        parts = re.split(r"[._\-]", local)
        parts = [p for p in parts if p]
        if not parts:
            return None

        # 首字母大写
        formatted = []
        for i, p in enumerate(parts):
            if len(p) == 1:
                formatted.append(p.upper() + ".")
            else:
                formatted.append(p.capitalize())
        return " ".join(formatted)

    def _strip_author_superscripts(self, text: str) -> str:
        """去除作者名后的上标字母/数字/星号/括号，如 Denga -> Deng, Womickb -> Womick。"""
        # 去掉空字符
        text = text.replace("\x00", "")
        # 去掉括号及其内容
        text = re.sub(r"\([^)]*\)", "", text)
        # 去掉独立的上标标记
        text = re.sub(r"[\d*†‡§¶]", "", text)
        # 去掉单词末尾的单字母上标（通常是 a,b,c,d）
        words = []
        for word in text.split():
            # 例如 "Denga", "Womickb", "Wilsonc,d"
            word = re.sub(r",[a-z]$", "", word)
            word = re.sub(r"([A-Za-z])[a-d]$", r"\1", word)
            if word:
                words.append(word)
        return " ".join(words)

    def _is_author_line(self, line: str) -> bool:
        """判断单行是否符合作者行特征。

        作者行通常包含 2 个以上 "名 姓" 模式，位于标题和摘要之间，长度较短。
        """
        cleaned = self._strip_author_superscripts(line).strip()
        if not (
            len(cleaned) > 5
            and len(cleaned) < 200
            and not re.search(r"@|University|School|Department|Institute|Hospital|Road|China|USA|Ltd\.|Inc\.|Received|Accepted|Published|Springer|Elsevier", cleaned)
        ):
            return False

        # 标题行通常很长且包含标题指示词，排除
        title_indicators = {"with", "for", "of", "based", "via", "using", "towards", "learning", "network", "networks"}
        lower = cleaned.lower()
        if len(cleaned) > 80 and any(ind in lower for ind in title_indicators):
            return False

        # 匹配人名实体：First Last / F. Last / First M. Last
        name_pattern = r"\b([A-Z][a-zA-Z]*(?:\s+[A-Z]\.)?(?:\s+[A-Z][a-zA-Z]+)+)\b"
        names = re.findall(name_pattern, cleaned)
        # 过滤掉常见非人名词组
        non_name_phrases = {
            "Multiple Instance", "Whole Slide", "Feature Drowning", "Computational Pathology",
            "Cancer Genome", "The Cancer", "Graph Convolution", "Hard Example",
        }
        names = [n for n in names if not any(phrase in n for phrase in non_name_phrases)]

        # 至少两个独立人名
        if len(names) >= 2:
            return True

        # 兜底：显式包含 and/逗号/中点，且能拆出 2 个以上有效人名片段
        if "," in cleaned or re.search(r"\band\b", cleaned, re.IGNORECASE) or "·" in cleaned:
            parts = re.split(r"[,;]|\band\b|\bAND\b|·", cleaned)
            parts = [p.strip() for p in parts if p.strip()]
            non_name_words = {
                "the", "of", "and", "with", "for", "in", "to", "a", "an", "is", "are", "was", "were",
                "has", "have", "had", "this", "that", "these", "those", "we", "our", "us", "they",
                "models", "methods", "approach", "approaches", "algorithm", "algorithms", "learning",
                "network", "networks", "data", "images", "image", "using", "based", "via", "from",
                "however", "therefore", "thus", "while", "where", "when", "which", "such", "can",
            }
            valid = 0
            for part in parts:
                words = part.split()
                if 1 <= len(words) <= 4:
                    lower_words = [w.lower() for w in words]
                    if not any(w in non_name_words for w in lower_words):
                        if all(re.match(r"^([A-Z]\.[A-Z]?|[A-Z][a-zA-Z\-]*)$", w) for w in words):
                            valid += 1
            return valid >= 2

        return False

    def _merge_author_lines(self, lines: List[str]) -> str:
        """合并被截断的多行作者行。"""
        if not lines:
            return ""
        merged = " ".join(lines)
        # 去除连字符断词
        merged = re.sub(r"(\w)-\s+(\w)", r"\1\2", merged)
        return merged

    def _infer_authors_by_pattern(self, text: str) -> Tuple[List[str], int]:
        """基于常见作者行模式提取作者，支持多行合并。

        返回 (作者列表, 作者块首行行号)。
        """
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            return [], 999

        # 模式 1: Author(s): ...
        for idx, line in enumerate(lines):
            match = re.match(r"(?i)^\s*authors?\s*[:：]\s*(.+)$", line)
            if match:
                return self._split_author_string(match.group(1)), idx

        # 模式 3: 定位标题和 Abstract/Introduction 之间的区域，合并后提取所有作者名
        abstract_index = None
        for i, line in enumerate(lines):
            if re.match(r"^(Abstract|摘要|1\s+Introduction|Introduction|Received|Keywords)\b", line, re.IGNORECASE):
                abstract_index = i
                break

        # 先定位标题行，作者应在标题之后、摘要之前
        title, title_idx = self._infer_title_with_index(None, "\n".join(lines))
        search_start = (title_idx + 1) if title_idx is not None and title_idx < 10 else 0
        search_end = abstract_index if abstract_index is not None else min(len(lines), 20)
        if search_end <= search_start:
            search_end = min(search_start + 10, len(lines))

        # 在标题和摘要之间寻找连续作者块，并合并截断行
        author_blocks = []
        i = search_start
        while i < search_end:
            if self._is_author_line(lines[i]):
                block = [lines[i]]
                block_start = i
                j = i + 1
                while j < search_end and self._is_author_line(lines[j]):
                    block.append(lines[j])
                    j += 1
                author_blocks.append((block_start, block))
                i = j
            else:
                i += 1

        if author_blocks:
            # 合并所有作者块中的行，再按分隔符拆分为人名
            all_text = " ".join(" ".join(block) for _, block in author_blocks)
            # 去除换行导致的连字符断词
            all_text = re.sub(r"(\w)-\s+(\w)", r"\1\2", all_text)
            authors = self._split_author_string(all_text)
            if authors:
                return authors, author_blocks[0][0]

        return [], 999

    def _split_author_string(self, s: str) -> List[str]:
        """把逗号或 and 分隔的作者字符串拆分为列表。"""
        if not s:
            return []
        # 统一分隔符：逗号、and、中文/英文中点
        s = s.replace(" and ", ", ").replace(" AND ", ", ").replace(" · ", ", ").replace("·", ", ")
        parts = [p.strip() for p in re.split(r"[,;]", s) if p.strip()]
        # 过滤机构、邮箱等噪声，并去除上标
        filtered = []
        for p in parts:
            if "@" in p or "University" in p or "Department" in p or "Hospital" in p:
                continue
            if re.match(r"^[\d\W]+$", p):
                continue
            p = self._strip_author_superscripts(p)
            p = re.sub(r"\s+", " ", p).strip()
            # 过滤单字母/空/纯数字
            if not p or len(p) < 2 or re.match(r"^[\d\W]+$", p):
                continue
            # 过滤不是人名的片段（要求至少包含两个单词，且首字母大写）
            words = p.split()
            if len(words) < 1:
                continue
            if not re.search(r"[A-Z][a-z]+", p):
                continue
            filtered.append(p)
        return filtered
