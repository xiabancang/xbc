"""技能（Skill）注册表。

Skill 是**声明式指令**，不是代码。方案 4.4 的调用策略矩阵在这里落地：

| model_invocable | user_invocable | 含义 |
|---|---|---|
| ✅ | ✅ | 模型能发现并加载；用户也能用 `/名字` 直接调用 |
| ✅ | ❌ | 只给模型用 |
| ❌ | ✅ | 只给用户用（危险流程应由用户显式触发） |
| ❌ | ❌ | 都不给（保留内容但不生效） |

两个关键设计（学 DeepSeek Harness）：

1. **目录只给"名称 + 限长描述"，正文按需加载** —— 控制上下文成本；
   并明确告诉模型"不得仅凭摘要推断指令"。
2. **提供方（provider）负责"技能从哪来"**，注册表负责合并、裁决与校验。
   注册表不含任何具体技能内容。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from ..errors import SkillError

SKILL_FILE = "SKILL.md"
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,63}$")
_DEFAULT_DESCRIPTION_MAX = 500


@dataclass
class SkillSpec:
    """技能的元数据（不含正文）。"""

    name: str
    description: str
    path: Path | None = None
    provider: str = ""
    plugin_id: str = ""
    model_invocable: bool = True
    user_invocable: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "provider": self.provider,
            "plugin_id": self.plugin_id,
            "model_invocable": self.model_invocable,
            "user_invocable": self.user_invocable,
            "path": str(self.path) if self.path else "",
        }


@dataclass
class SkillContent:
    """加载后的技能正文。"""

    name: str
    body: str
    path: Path | None = None
    resources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": str(self.path) if self.path else "",
            "resources": list(self.resources),
        }


class SkillProvider(Protocol):
    """技能提供方。只负责"技能从哪来"，不参与裁决。"""

    name: str

    def list(self) -> list[SkillSpec]:
        """列出本提供方可见的技能元数据。"""

    def get(self, name: str) -> str | None:
        """按名加载正文；不存在返回 None。"""


def parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    """解析 `---` 包裹的极简 front matter（`key: value`），返回 (元数据, 正文)。

    刻意不引入 PyYAML：内核零第三方依赖。只支持扁平的 `key: value`。
    """
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    end = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            end = index
            break
    if end is None:
        return {}, text

    meta: dict[str, str] = {}
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip().strip('"').strip("'")
    return meta, "\n".join(lines[end + 1 :]).lstrip("\n")


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() not in ("false", "0", "no", "off")


class FileSystemSkillProvider:
    """从磁盘发现技能：扫描若干根目录，每个技能是一个含 SKILL.md 的子目录。"""

    name = "filesystem"

    def __init__(self, roots: list[Path | str], logger: Any = None) -> None:
        self._roots = [Path(r) for r in roots]
        self._log = logger

    def _iter_skill_dirs(self) -> list[Path]:
        found: list[Path] = []
        for root in self._roots:
            if not root.is_dir():
                continue
            for child in sorted(root.iterdir()):
                if child.is_dir() and (child / SKILL_FILE).is_file():
                    found.append(child)
        return found

    def list(self) -> list[SkillSpec]:
        specs: list[SkillSpec] = []
        for folder in self._iter_skill_dirs():
            try:
                meta, _ = parse_front_matter((folder / SKILL_FILE).read_text(encoding="utf-8"))
            except OSError as exc:
                if self._log:
                    self._log.warning("读取技能失败 %s: %s", folder, exc)
                continue
            name = meta.get("name") or folder.name
            specs.append(
                SkillSpec(
                    name=name,
                    description=meta.get("description", ""),
                    path=folder,
                    provider=self.name,
                    model_invocable=_as_bool(meta.get("model_invocable"), True),
                    user_invocable=_as_bool(meta.get("user_invocable"), True),
                )
            )
        return specs

    def get(self, name: str) -> str | None:
        for folder in self._iter_skill_dirs():
            try:
                text = (folder / SKILL_FILE).read_text(encoding="utf-8")
            except OSError:
                continue
            meta, body = parse_front_matter(text)
            if (meta.get("name") or folder.name) == name:
                return body
        return None

    def resources(self, name: str) -> list[str]:
        for folder in self._iter_skill_dirs():
            try:
                meta, _ = parse_front_matter((folder / SKILL_FILE).read_text(encoding="utf-8"))
            except OSError:
                continue
            if (meta.get("name") or folder.name) == name:
                return sorted(
                    str(p.relative_to(folder))
                    for p in folder.rglob("*")
                    if p.is_file() and p.name != SKILL_FILE
                )
        return []


class SkillCatalog:
    """合并多来源的技能目录。注册表不含技能内容。"""

    def __init__(
        self,
        logger: Any = None,
        description_max_length: int = _DEFAULT_DESCRIPTION_MAX,
    ) -> None:
        self._log = logger
        self._description_max = max(3, int(description_max_length))
        self._providers: dict[str, SkillProvider] = {}
        self._inline: dict[str, tuple[SkillSpec, Callable[[], str] | str]] = {}

    # ---------- 注册 ----------
    def register_provider(self, provider: SkillProvider) -> Callable[[], None]:
        self._providers[provider.name] = provider
        return lambda: self._providers.pop(provider.name, None)

    def register(self, spec: SkillSpec, body: Callable[[], str] | str) -> Callable[[], None]:
        """运行时注册（插件用 `ctx.skills.register(...)` 贡献技能）。"""
        self._validate_name(spec.name)
        if spec.name in self._inline:
            raise SkillError(f"技能 {spec.name!r} 已被运行时注册占用（先到先得）")
        self._inline[spec.name] = (spec, body)
        return lambda: self._inline.pop(spec.name, None)

    @staticmethod
    def _validate_name(name: str) -> None:
        if not _NAME_RE.match(name):
            raise SkillError(
                f"非法技能名 {name!r}：只允许小写字母开头、由小写字母/数字/连字符组成，长度 2-64"
            )

    # ---------- 查询 ----------
    def _collect(self) -> dict[str, SkillSpec]:
        merged: dict[str, SkillSpec] = {}
        for provider in self._providers.values():
            try:
                for spec in provider.list():
                    self._validate_name(spec.name)
                    merged.setdefault(spec.name, spec)  # 先到先得
            except Exception as exc:  # noqa: BLE001 - 单个提供方失败不影响其他来源
                if self._log:
                    self._log.warning("技能提供方 %s 列举失败: %s", provider.name, exc)
        for name, (spec, _) in self._inline.items():
            merged.setdefault(name, spec)
        return merged

    def specs(self, *, for_model: bool | None = None) -> list[SkillSpec]:
        specs = sorted(self._collect().values(), key=lambda s: s.name)
        if for_model is None:
            return specs
        key = "model_invocable" if for_model else "user_invocable"
        return [s for s in specs if getattr(s, key)]

    def names(self) -> list[str]:
        return [s.name for s in self.specs()]

    def __contains__(self, name: object) -> bool:
        return str(name) in self._collect()

    def __len__(self) -> int:
        return len(self._collect())

    # ---------- 目录与加载 ----------
    def catalog_text(self, *, for_model: bool = True) -> str:
        """给模型的技能目录：只有名称与限长描述，**不含正文**。"""
        specs = self.specs(for_model=for_model)
        if not specs:
            return ""
        lines = [
            "以下是可用的 skill（技能）。它们是**指令**，不是可执行动作。",
            "在着手相关任务前，先用 skill 工具按**精确名称**加载完整指令；",
            "**不要仅凭下面的摘要推断指令内容**。",
            "",
        ]
        for spec in specs:
            description = spec.description
            if len(description) > self._description_max:
                description = description[: self._description_max - 3] + "..."
            lines.append(f"- {spec.name}: {description}")
        return "\n".join(lines)

    def load(self, name: str) -> SkillContent:
        """按名加载正文。找不到抛 SkillError。"""
        self._validate_name(name)
        merged = self._collect()
        spec = merged.get(name)
        if spec is None:
            raise SkillError(f"技能 {name!r} 不存在或已不可用；可用技能: {sorted(merged)}")

        if name in self._inline:
            spec_inline, source = self._inline[name]
            body = source() if callable(source) else source
            resources: list[str] = []
            # 运行时注册的技能如果带目录，同样提供资源清单（供界面预览）
            if spec_inline.path is not None and Path(spec_inline.path).is_dir():
                folder = Path(spec_inline.path)
                resources = sorted(
                    str(p.relative_to(folder))
                    for p in folder.rglob("*")
                    if p.is_file() and p.name != SKILL_FILE
                )
            return SkillContent(name=name, body=body, path=spec_inline.path, resources=resources)

        provider = self._providers.get(spec.provider)
        if provider is None:
            raise SkillError(f"技能 {name!r} 的提供方 {spec.provider!r} 已不可用")
        body = provider.get(name)
        if body is None:
            raise SkillError(f"技能 {name!r} 在提供方 {spec.provider!r} 中已不可用（可能在发现后被删除）")
        resources = getattr(provider, "resources", None)
        return SkillContent(
            name=name,
            body=body,
            path=spec.path,
            resources=resources(name) if callable(resources) else [],
        )
