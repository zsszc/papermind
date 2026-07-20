import re
from pathlib import Path
from typing import Dict, Any, List, Optional

from docx import Document


class DocxParser:
    """解析 Word 大论文，提取章节结构和引用。"""

    # 忽略的样式：目录、页眉页脚、题注等
    IGNORE_STYLES = {"toc 1", "toc 2", "toc 3", "toc 4", "toc 5", "toc 6", "toc 7", "toc 8", "toc 9",
                     "Table of Contents", "Header", "Footer", "Caption", "题注"}

    def parse(self, file_path: str) -> Dict[str, Any]:
        doc = Document(file_path)

        paragraphs = []
        for i, para in enumerate(doc.paragraphs):
            text = para.text.strip()
            if not text:
                continue
            style = (para.style.name if para.style else "Normal").strip()
            if style.lower() in {s.lower() for s in self.IGNORE_STYLES}:
                continue

            # 计算段落主要字号
            sizes = [run.font.size.pt for run in para.runs if run.font.size]
            font_size = max(sizes) if sizes else None

            paragraphs.append({
                "index": i,
                "text": text,
                "style": style,
                "font_size": font_size,
                "is_heading": self._is_heading(style, text),
            })

        # 提取章节结构
        chapters = self._extract_chapters(paragraphs)

        # 统计字数
        full_text = "\n".join([p["text"] for p in paragraphs])
        word_count = len(full_text.replace(" ", ""))

        # 检测引用标记
        citations = self._extract_citations(paragraphs, chapters)

        return {
            "title": self._extract_title(paragraphs),
            "paragraphs": paragraphs,
            "chapters": chapters,
            "word_count": word_count,
            "citations": citations,
        }

    def _is_heading(self, style: str, text: str) -> bool:
        """判断段落是否为标题。"""
        # 明确的标题样式
        if any(s in style for s in ["Heading", "标题"]):
            return True
        # 中文章节编号：第1章、第一章
        if re.match(r"^第[一二三四五六七八九十0-9]+章", text):
            return True
        # 节编号：1.1、1.1.1
        if re.match(r"^\d+(\.\d+)+\s+", text):
            return True
        return False

    def _extract_title(self, paragraphs: List[Dict[str, Any]]) -> Optional[str]:
        """从封面提取论文标题。优先根据字号判断。"""
        noise_keywords = [
            "作者", "学院", "指导教师", "学位论文", "硕士学位", "学士学位", "博士学位",
            "论文题目", "专业", "学号", "答辩日期", "湖州师范学院", "University",
            "A Dissertation", "Submitted to", "Thesis", "By", "Supervisor", "摘要",
            "本文", "本章", "关键词", "研究方向",
        ]

        # 策略1：找封面页字号最大的标题段落（>= 16pt），合并连续段落
        title_parts = []
        prev_index = -2
        for p in paragraphs[:30]:
            text = p["text"]
            if any(k in text for k in noise_keywords):
                continue
            if p["font_size"] and p["font_size"] >= 16:
                if p["index"] == prev_index + 1:
                    title_parts[-1] += text
                else:
                    title_parts.append(text)
                prev_index = p["index"]

        if title_parts:
            # 取最长的合并标题
            best = max(title_parts, key=len)
            if 12 <= len(best) <= 120:
                return best

        # 策略2：兜底，按文本特征匹配
        candidates = []
        for p in paragraphs[:25]:
            text = p["text"]
            if any(k in text for k in noise_keywords):
                continue
            if any(c in text for c in [':', '：', '/', '\\', '。', '！', '？', '.', ',', '，', '=']):
                continue
            if len(text) >= 15 and len(text) <= 80 and p["style"] in ("Normal", "Title", "标题"):
                candidates.append(text)

        if candidates:
            return max(candidates, key=len)
        return None

    def _extract_chapters(self, paragraphs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        chapters = []
        current = None

        for p in paragraphs:
            if not p["is_heading"]:
                continue

            text = self._clean_heading_text(p["text"])
            level = self._heading_level(p["style"], text)

            if level <= 0:
                continue

            # 排除目录
            if text in ("目录", "目  录", "目 录", "Table of Contents", "Contents"):
                continue

            if current:
                current["end_paragraph"] = p["index"] - 1
            current = {
                "title": text,
                "level": level,
                "start_paragraph": p["index"],
                "end_paragraph": p["index"],
            }
            chapters.append(current)

        if current and paragraphs:
            current["end_paragraph"] = paragraphs[-1]["index"]
        return chapters

    def _clean_heading_text(self, text: str) -> str:
        """清理标题：去掉页码、换行等。"""
        # 去掉制表符后的页码
        text = re.split(r"\t+", text)[0]
        # 去掉末尾页码 " ... 7"
        text = re.sub(r"\s+\.\.\.\s*\d+$", "", text)
        text = re.sub(r"\s+\d+$", "", text)
        return text.strip()

    def _heading_level(self, style: str, text: str) -> int:
        """判断标题级别。"""
        # 第X章 = 一级
        if re.match(r"^第[一二三四五六七八九十0-9]+章", text):
            return 1
        # Heading 1 / 标题 1
        if "Heading 1" in style or "标题 1" in style:
            return 1
        # Heading 2 / 标题 2
        if "Heading 2" in style or "标题 2" in style:
            return 2
        # Heading 3 / 标题 3
        if "Heading 3" in style or "标题 3" in style:
            return 3
        # Heading 4 / 标题 4
        if "Heading 4" in style or "标题 4" in style:
            return 4
        # 1.1 = 二级
        if re.match(r"^\d+\.\d+\.\d+\.\d+\s", text):
            return 4
        if re.match(r"^\d+\.\d+\.\d+\s", text):
            return 3
        if re.match(r"^\d+\.\d+\s", text):
            return 2
        return 2

    def _extract_citations(self, paragraphs: List[Dict[str, Any]], chapters: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """检测引用标记，如 [1]、(Zhou et al., 2024) 等。"""
        citations = []

        # [1], [1,2], [1-3]
        bracket_pattern = re.compile(r"\[(\d+(?:\s*[,-]\s*\d+)*)\]")
        # (Zhou et al., 2024) 或 (Zhang and Li, 2023)
        paren_pattern = re.compile(r"\(([A-Z][a-zA-Z\s,\.]+(?:et al\.)?,\s*\d{4}[a-z]?)\)")

        for p in paragraphs:
            text = p["text"]
            chapter_index = self._find_chapter_index(p["index"], chapters)

            for match in bracket_pattern.finditer(text):
                citations.append({
                    "paragraph_index": p["index"],
                    "citation_text": match.group(0),
                    "raw_numbers": match.group(1),
                    "context": text,
                    "chapter_index": chapter_index,
                })
            for match in paren_pattern.finditer(text):
                citations.append({
                    "paragraph_index": p["index"],
                    "citation_text": match.group(0),
                    "raw_numbers": match.group(1),
                    "context": text,
                    "chapter_index": chapter_index,
                })
        return citations

    def _find_chapter_index(self, paragraph_index: int, chapters: List[Dict[str, Any]]) -> Optional[int]:
        for idx, ch in enumerate(chapters):
            if ch["start_paragraph"] <= paragraph_index <= ch["end_paragraph"]:
                return idx
        return None

    def extract_chapter_text(self, paragraphs: List[Dict[str, Any]], chapter: Dict[str, Any]) -> str:
        """提取某个章节的完整文本。"""
        start = chapter["start_paragraph"]
        end = chapter["end_paragraph"]
        return "\n".join([p["text"] for p in paragraphs if start <= p["index"] <= end])
