"""Skill 注册与提示词组装服务。

Skill 系统采用轻量级 Prompt 路由：前端通过 skill 字段触发，后端在 system prompt
中注入对应的角色设定与输出要求。

本模块已实现可注册的 SkillRegistry（Skill-as-Tool 第一步）：Skill 以数据类描述，
注册表支持动态注册/查询，为后续 LangGraph 工具化（tools 字段）预留扩展点。
模块级公开函数 build_skill_prompt / list_skills 保持原有签名与返回结构不变，
内部委托给全局注册表单例，调用方（chat.py）无需改动。
"""

import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class Skill:
    """Skill 定义：角色设定 + 输出要求 + 预留的工具列表。"""

    skill_id: str
    display_name: str
    description: str
    role: str
    instruction: str
    # 预留给 LangGraph 阶段的工具列表，当前始终为空
    tools: List[str] = field(default_factory=list)


class SkillRegistry:
    """Skill 注册表：线程安全的注册/查询/prompt 构建。"""

    def __init__(self) -> None:
        self._skills: Dict[str, Skill] = {}
        self._lock = threading.Lock()

    def register(self, skill: Skill) -> None:
        """注册（或覆盖）一个 Skill。"""
        with self._lock:
            self._skills[skill.skill_id] = skill

    def get(self, skill_id: str) -> Optional[Skill]:
        """按 ID 查询 Skill，不存在返回 None。"""
        with self._lock:
            return self._skills.get(skill_id)

    def list(self) -> List[Skill]:
        """返回全部已注册 Skill（按注册顺序）。"""
        with self._lock:
            return list(self._skills.values())

    def build_prompt(self, skill_id: Optional[str], user_message: str) -> Optional[str]:
        """根据 Skill ID 构建 system prompt。返回 None 表示无需注入。"""
        if not skill_id:
            return None
        skill = self.get(skill_id)
        if not skill:
            return None
        return f"""{skill.role}

{skill.instruction}

当前用户输入：
{user_message}
"""


def _default_skills() -> List[Skill]:
    """内置的 6 个默认 Skill（内容与原硬编码版本保持一致）。"""
    return [
        Skill(
            skill_id="translator",
            display_name="学术翻译",
            description="请将用户提供的学术文本进行翻译。要求：",
            role="你是一位学术翻译专家，擅长中英文学术论文互译。",
            instruction="""请将用户提供的学术文本进行翻译。要求：
1. 保持学术术语准确
2. 保留原文的句式结构和专业表达
3. 直接输出翻译结果，不添加额外解释""",
        ),
        Skill(
            skill_id="proofreader",
            display_name="论文校对",
            description="请对用户提供的学术文本进行校对。要求：",
            role="你是一位严谨的学术论文校对专家。",
            instruction="""请对用户提供的学术文本进行校对。要求：
1. 指出语法、拼写、标点错误
2. 检查学术表达是否规范
3. 给出修改后的文本
4. 列出主要修改点""",
        ),
        Skill(
            skill_id="method_comparator",
            display_name="方法对比",
            description="请对比用户提到的两种或多种方法。要求：",
            role="你是一位计算机视觉/医学图像分析领域的专家，擅长对比不同研究方法。",
            instruction="""请对比用户提到的两种或多种方法。要求：
1. 从核心思想、网络结构、输入输出、优缺点等方面对比
2. 指出各方法适用的场景
3. 如果有文献库中的相关资料，请结合引用""",
        ),
        Skill(
            skill_id="outline_generator",
            display_name="大纲生成",
            description="请根据用户的研究主题和提供的文献，生成一份详细的论文大纲。要求：",
            role="你是一位学术论文写作导师，擅长帮助研究生梳理论文结构。",
            instruction="""请根据用户的研究主题和提供的文献，生成一份详细的论文大纲。要求：
1. 大纲层级清晰（章-节-小节）
2. 每一部分说明应包含的核心内容
3. 建议各部分应引用的关键文献""",
        ),
        Skill(
            skill_id="data_analyst",
            display_name="数据分析",
            description="请对用户提供的实验数据或结果进行分析。要求：",
            role="你是一位医学图像/机器学习实验数据分析专家。",
            instruction="""请对用户提供的实验数据或结果进行分析。要求：
1. 解释关键指标的含义（如 AUC、ACC、F1、CI 等）
2. 对比不同方法的结果
3. 指出数据中的趋势、异常和可改进之处
4. 如果需要，建议进一步的统计检验""",
        ),
        Skill(
            skill_id="writing_assistant",
            display_name="写作助手",
            description="请帮助用户改进学术写作。要求：",
            role="你是一位学术写作助手，帮助用户润色和扩展论文段落。",
            instruction="""请帮助用户改进学术写作。要求：
1. 使表达更学术化、更简洁
2. 保持原意不变
3. 如需补充，建议可引用的文献方向""",
        ),
    ]


_skill_registry_instance: Optional[SkillRegistry] = None
_skill_registry_lock = threading.Lock()


def get_skill_registry() -> SkillRegistry:
    """获取全局单例 SkillRegistry（带锁懒加载，默认注册 6 个内置 Skill）。"""
    global _skill_registry_instance
    if _skill_registry_instance is None:
        with _skill_registry_lock:
            if _skill_registry_instance is None:
                registry = SkillRegistry()
                for skill in _default_skills():
                    registry.register(skill)
                _skill_registry_instance = registry
    return _skill_registry_instance


def build_skill_prompt(skill: Optional[str], user_message: str) -> Optional[str]:
    """根据 Skill ID 构建 system prompt。返回 None 表示无需注入。"""
    return get_skill_registry().build_prompt(skill, user_message)


def list_skills() -> list:
    """返回所有可用 Skill 列表，供前端展示。"""
    return [
        {
            "skill_id": s.skill_id,
            "display_name": s.display_name,
            "description": s.description,
        }
        for s in get_skill_registry().list()
    ]
