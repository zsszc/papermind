import re
import random
from typing import Dict, List, Optional

from app.models import Paper, Tag
from app.services.llm import llm_service
from app.core.logger import logger

# 医学/影像/AI 领域常用标签池
_TAG_POOL = [
    "深度学习",
    "机器学习",
    "Transformer",
    "卷积神经网络",
    "计算机视觉",
    "医学影像",
    "MRI",
    "CT",
    "病理",
    "放射组学",
    "T分期",
    "结直肠癌",
    "胃癌",
    "肝癌",
    "肺癌",
    "乳腺癌",
    "预测",
    "分割",
    "多模态",
    "预后",
    "生存分析",
    "临床试验",
    "综述",
    "方法学",
    "可解释性",
    "迁移学习",
    "联邦学习",
    "数据增强",
    "注意力机制",
]

_TAG_COLORS = [
    "#1890ff",
    "#52c41a",
    "#faad14",
    "#eb2f96",
    "#722ed1",
    "#13c2c2",
    "#fa8c16",
    "#a0d911",
    "#f5222d",
    "#2f4554",
]

_KEYWORD_RULES = {
    "深度学习": ["deep learning", "neural network", "dnn"],
    "机器学习": ["machine learning", "classifier", "svm", "random forest"],
    "Transformer": ["transformer", "attention", "bert", "gpt"],
    "卷积神经网络": ["cnn", "convolutional neural network", "resnet", "unet"],
    "计算机视觉": ["computer vision", "image classification", "object detection"],
    "医学影像": ["medical imaging", "radiology", "radiological"],
    "MRI": ["magnetic resonance imaging", " mri ", "mr imaging"],
    "CT": ["computed tomography", " ct ", "ct scan"],
    "病理": ["pathology", "histopathology", "histological"],
    "放射组学": ["radiomics", "radiomic"],
    "T分期": ["t staging", "t stage", "t classification"],
    "结直肠癌": ["colorectal cancer", "rectal cancer", "colon cancer"],
    "预测": ["prediction", "predict", "prognosis"],
    "分割": ["segmentation", "segment"],
    "多模态": ["multimodal", "multi-modal"],
    "预后": ["survival", "prognostic"],
    "临床试验": ["clinical trial", "prospective cohort"],
    "综述": ["review", "survey"],
    "方法学": ["methodology", "framework", "pipeline"],
    "可解释性": ["explainable", "interpretable", "xai"],
}


class AutoTagService:
    """基于规则+LLM 为论文自动生成标签。"""

    def __init__(self):
        self.tag_pool = _TAG_POOL
        self.keyword_rules = _KEYWORD_RULES
        self.colors = _TAG_COLORS

    def _rule_based_tags(self, paper: Paper) -> List[str]:
        """基于标题、摘要、期刊的关键词规则匹配标签。"""
        text = " ".join(
            filter(
                None,
                [
                    paper.title or "",
                    paper.abstract or "",
                    paper.journal or "",
                ],
            )
        ).lower()
        matched = set()
        for tag, keywords in self.keyword_rules.items():
            for kw in keywords:
                if kw in text:
                    matched.add(tag)
                    break
        return list(matched)

    def _clean_tag_name(self, name: str) -> str:
        """清洗标签名：去除装饰字符、控制字符，确保 UTF-8 编码正确。"""
        if not name:
            return ""
        # 防御：若字符串被错误地以 latin-1 解码，尝试还原为 UTF-8
        try:
            encoded = name.encode("latin-1")
            if b"\xc3" in encoded or b"\xe4" in encoded or b"\xe5" in encoded or b"\xe6" in encoded:
                name = encoded.decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
        name = name.strip()
        # 去除常见的列表符号、引号
        name = re.sub(r"^[-\d•*·]+[.\\s]*", "", name)
        name = name.strip('"\'')
        # 去除控制字符
        name = "".join(ch for ch in name if ch.isprintable() or ch in " \t")
        return name.strip()

    def _build_llm_messages(self, paper: Paper) -> Optional[List[Dict[str, str]]]:
        """构造 LLM 标签抽取的消息；上下文为空时返回 None。"""
        context = "\n".join(
            filter(
                None,
                [
                    f"标题：{paper.title or ''}",
                    f"作者：{paper.authors or ''}",
                    f"期刊：{paper.journal or ''}",
                    f"摘要：{paper.abstract or ''}",
                ],
            )
        )
        if not context.strip():
            return None

        prompt = f"""请根据以下学术论文信息，从给定标签池中选出最相关的 3-5 个中文或英文标签。只返回标签名列表，不要解释，不要编号。

候选标签池（请尽量从中选择）：
{', '.join(self.tag_pool)}

如果论文内容明显超出标签池，可以补充 1-2 个新标签，但总数不超过 5 个。标签应简短，最好是 2-6 个字或 1-3 个英文单词。

论文信息：
{context[:1500]}

请按相关性从高到低输出，每行一个标签名，只输出标签："""

        return [
            {"role": "system", "content": "你是专业的学术文献分类助手，只输出标签名列表。"},
            {"role": "user", "content": prompt},
        ]

    def _parse_llm_tags_result(self, result: str) -> List[str]:
        """解析 LLM 输出为标签名列表（每行一个，最多 5 个）。"""
        tags = []
        for line in result.splitlines():
            line = self._clean_tag_name(line)
            if line:
                tags.append(line)
        return tags[:5]

    async def _llm_tags(self, paper: Paper) -> List[str]:
        """使用 LLM 从标题和摘要中提取领域标签（异步版）。"""
        messages = self._build_llm_messages(paper)
        if not messages:
            return []
        try:
            result = await llm_service.chat_completion(messages)
            return self._parse_llm_tags_result(result)
        except Exception:
            logger.warning("[AutoTag] LLM 生成标签失败", exc_info=True)
            return []

    def _llm_tags_sync(self, paper: Paper, timeout: Optional[int] = None) -> List[str]:
        """使用 LLM 从标题和摘要中提取领域标签（同步版，供后台线程使用）。"""
        messages = self._build_llm_messages(paper)
        if not messages:
            return []
        try:
            result = llm_service.chat_completion_sync(messages, timeout=timeout)
            return self._parse_llm_tags_result(result)
        except Exception:
            logger.warning("[AutoTag] LLM 生成标签失败（同步）", exc_info=True)
            return []

    def _collect_tags(self, paper: Paper, llm_tags: List[str], db) -> List[Tag]:
        """合并规则标签与 LLM 标签并落库，返回 Tag 对象列表（未 commit）。"""
        rule_tags = self._rule_based_tags(paper)

        # 合并去重，LLM 标签优先级高于规则标签
        ordered = []
        seen = set()
        for name in llm_tags + rule_tags:
            normalized = name.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            ordered.append(normalized)

        # 限制总数
        ordered = ordered[:5]

        tags = []
        for raw_name in ordered:
            name = self._clean_tag_name(raw_name)
            if not name:
                continue
            tag = db.query(Tag).filter(Tag.name == name).first()
            if not tag:
                tag = Tag(
                    name=name,
                    color=random.choice(self.colors),
                )
                db.add(tag)
                db.flush()
            if tag not in paper.tags:
                tags.append(tag)

        return tags

    async def generate_tags(self, paper: Paper, db) -> List[Tag]:
        """为论文生成标签，返回已关联到 db 的 Tag 对象列表（未 commit）。"""
        llm_tags = await self._llm_tags(paper)
        return self._collect_tags(paper, llm_tags, db)

    def generate_tags_sync(self, paper: Paper, db, timeout: Optional[int] = None) -> List[Tag]:
        """generate_tags 的同步入口：供后台线程（无事件循环）使用，内部走 chat_completion_sync。"""
        llm_tags = self._llm_tags_sync(paper, timeout=timeout)
        return self._collect_tags(paper, llm_tags, db)


auto_tag_service = AutoTagService()
