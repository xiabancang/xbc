"""技能（Skill）子系统。

Skill = 声明式指令（不是代码），按需加载，带调用策略。
"""

from .catalog import (
    SKILL_FILE,
    FileSystemSkillProvider,
    SkillCatalog,
    SkillContent,
    SkillProvider,
    SkillSpec,
    parse_front_matter,
)

__all__ = [
    "SKILL_FILE",
    "FileSystemSkillProvider",
    "SkillCatalog",
    "SkillContent",
    "SkillProvider",
    "SkillSpec",
    "parse_front_matter",
]
