"""SkillRegistry 与模块级公开 API 的单元测试（纯内存，不调用任何 LLM）。"""

import pytest

from app.services.skills import (
    Skill,
    SkillRegistry,
    build_skill_prompt,
    get_skill_registry,
    list_skills,
)

# 默认 6 个 Skill 的 ID 与展示名（与原硬编码版本一致）
DEFAULT_SKILLS = {
    "translator": "学术翻译",
    "proofreader": "论文校对",
    "method_comparator": "方法对比",
    "outline_generator": "大纲生成",
    "data_analyst": "数据分析",
    "writing_assistant": "写作助手",
}


class TestDefaultSkills:
    """默认注册的 6 个 Skill 可完整列出。"""

    def test_list_skills_contains_all_defaults(self):
        skills = list_skills()
        assert len(skills) == 6
        assert {s["skill_id"] for s in skills} == set(DEFAULT_SKILLS)

    def test_display_names_match(self):
        skills = {s["skill_id"]: s["display_name"] for s in list_skills()}
        for skill_id, display_name in DEFAULT_SKILLS.items():
            assert skills[skill_id] == display_name

    def test_list_skills_fields_complete(self):
        for item in list_skills():
            assert set(item.keys()) == {"skill_id", "display_name", "description"}
            assert all(isinstance(v, str) and v for v in item.values())


class TestBuildSkillPrompt:
    """build_skill_prompt 的行为与原实现一致。"""

    def test_known_skill_returns_prompt(self):
        registry = get_skill_registry()
        skill = registry.get("translator")
        assert skill is not None
        prompt = build_skill_prompt("translator", "请翻译这段文字")
        assert prompt is not None
        # prompt 应包含角色设定、输出要求与用户输入
        assert skill.role in prompt
        assert skill.instruction in prompt
        assert "请翻译这段文字" in prompt

    def test_unknown_skill_returns_none(self):
        assert build_skill_prompt("not_a_skill", "任意输入") is None

    def test_none_and_empty_return_none(self):
        assert build_skill_prompt(None, "任意输入") is None
        assert build_skill_prompt("", "任意输入") is None


class TestSkillRegistry:
    """注册表自身的注册/查询/prompt 构建能力。"""

    @pytest.fixture()
    def registry(self):
        # 使用独立实例，避免污染全局单例
        return SkillRegistry()

    def test_register_then_get(self, registry):
        skill = Skill(
            skill_id="summarizer",
            display_name="摘要生成",
            description="生成文献摘要",
            role="你是一位文献摘要专家。",
            instruction="请为用户提供的文献生成结构化摘要。",
        )
        registry.register(skill)
        got = registry.get("summarizer")
        assert got is skill
        assert got.tools == []  # 预留工具字段默认为空列表

    def test_register_then_build_prompt(self, registry):
        registry.register(Skill(
            skill_id="summarizer",
            display_name="摘要生成",
            description="生成文献摘要",
            role="你是一位文献摘要专家。",
            instruction="请为用户提供的文献生成结构化摘要。",
        ))
        prompt = registry.build_prompt("summarizer", "这篇文献讲了什么")
        assert "你是一位文献摘要专家。" in prompt
        assert "请为用户提供的文献生成结构化摘要。" in prompt
        assert "这篇文献讲了什么" in prompt

    def test_get_unknown_returns_none(self, registry):
        assert registry.get("missing") is None
        assert registry.build_prompt("missing", "输入") is None

    def test_singleton_is_shared(self):
        assert get_skill_registry() is get_skill_registry()
