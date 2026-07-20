"""Skill 提示词与参数组装服务。

当前 Skill 系统采用轻量级 Prompt 路由：前端通过 skill 字段触发，后端在 system prompt
中注入对应的角色设定与输出要求。后续可扩展为 YAML/数据库配置的插件化 Skill 注册表。
"""

from typing import Optional


SKILL_PROMPTS = {
    "translator": {
        "role": "你是一位学术翻译专家，擅长中英文学术论文互译。",
        "instruction": """请将用户提供的学术文本进行翻译。要求：
1. 保持学术术语准确
2. 保留原文的句式结构和专业表达
3. 直接输出翻译结果，不添加额外解释""",
    },
    "proofreader": {
        "role": "你是一位严谨的学术论文校对专家。",
        "instruction": """请对用户提供的学术文本进行校对。要求：
1. 指出语法、拼写、标点错误
2. 检查学术表达是否规范
3. 给出修改后的文本
4. 列出主要修改点""",
    },
    "method_comparator": {
        "role": "你是一位计算机视觉/医学图像分析领域的专家，擅长对比不同研究方法。",
        "instruction": """请对比用户提到的两种或多种方法。要求：
1. 从核心思想、网络结构、输入输出、优缺点等方面对比
2. 指出各方法适用的场景
3. 如果有文献库中的相关资料，请结合引用""",
    },
    "outline_generator": {
        "role": "你是一位学术论文写作导师，擅长帮助研究生梳理论文结构。",
        "instruction": """请根据用户的研究主题和提供的文献，生成一份详细的论文大纲。要求：
1. 大纲层级清晰（章-节-小节）
2. 每一部分说明应包含的核心内容
3. 建议各部分应引用的关键文献""",
    },
    "data_analyst": {
        "role": "你是一位医学图像/机器学习实验数据分析专家。",
        "instruction": """请对用户提供的实验数据或结果进行分析。要求：
1. 解释关键指标的含义（如 AUC、ACC、F1、CI 等）
2. 对比不同方法的结果
3. 指出数据中的趋势、异常和可改进之处
4. 如果需要，建议进一步的统计检验""",
    },
    "writing_assistant": {
        "role": "你是一位学术写作助手，帮助用户润色和扩展论文段落。",
        "instruction": """请帮助用户改进学术写作。要求：
1. 使表达更学术化、更简洁
2. 保持原意不变
3. 如需补充，建议可引用的文献方向""",
    },
}


def build_skill_prompt(skill: Optional[str], user_message: str) -> Optional[str]:
    """根据 Skill ID 构建 system prompt。返回 None 表示无需注入。"""
    if not skill:
        return None
    cfg = SKILL_PROMPTS.get(skill)
    if not cfg:
        return None
    return f"""{cfg['role']}

{cfg['instruction']}

当前用户输入：
{user_message}
"""


def list_skills() -> list:
    """返回所有可用 Skill 列表，供前端展示。"""
    display_names = {
        "translator": "学术翻译",
        "proofreader": "论文校对",
        "method_comparator": "方法对比",
        "outline_generator": "大纲生成",
        "data_analyst": "数据分析",
        "writing_assistant": "写作助手",
    }
    return [
        {
            "skill_id": k,
            "display_name": display_names.get(k, k),
            "description": v["instruction"].split("\n")[0],
        }
        for k, v in SKILL_PROMPTS.items()
    ]
