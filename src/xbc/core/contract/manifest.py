"""插件清单（plugin.json）的解析与校验。

清单是"合同"：内核只根据清单决定是否加载、能否兼容、该注入哪些能力，
判断过程**不依赖插件代码**。因此清单必须先于代码被严格校验。

设计要点（对应《Plugin Runtime 技术方案 V1》3.2 / 3.9）：

- **双版本字段**：`spec_version`（清单规范版本，内核不认识的字段可降级忽略）
  与 `api_version`（内核 API 契约，主版本必须匹配）。前者保证新插件在老内核上
  至少能被读出基本信息，后者保证真不兼容时明确拒绝。
- **元数据可静态读取**：名称/描述/图标/命令索引都不需要导入代码，
  这样插件管理器与命令面板不会随插件增多而变慢。
- **图标路径必须落在插件目录内**：拒绝绝对路径、URL 与越界路径。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...version import API_SPEC_VERSION, CORE_API_VERSION, api_major
from ..errors import PluginManifestError

MANIFEST_NAME = "plugin.json"

_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
_REQUIRED_FIELDS = ("id", "name", "version", "api_version", "entry")

# 内核已知的能力名。清单里只能声明这些，写错会被明确拒绝（而不是静默失效）。
KNOWN_CAPABILITIES: tuple[str, ...] = ("files", "settings", "ffmpeg", "ai", "events")

# 工具的风险等级。注意：这只是**提示**，内核不以它作为放行依据
# （参考 MCP 对 annotations 的规定：不可信来源的声明不得作为决策依据）。
KNOWN_RISKS: tuple[str, ...] = ("read", "write", "destructive")

# 命令面板的匹配类型
KNOWN_MATCH_TYPES: tuple[str, ...] = ("text", "files", "regex", "window")


@dataclass(frozen=True)
class ToolDecl:
    """清单里声明的工具（供零成本展示与权限预览；实际注册在代码里）。"""

    name: str
    description: str = ""
    risk: str = "read"
    input_schema: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "risk": self.risk,
        }


@dataclass(frozen=True)
class CommandDecl:
    """命令面板的声明式索引：宿主只读清单即可建立索引，命中后才激活插件。"""

    code: str
    label: str
    match: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "label": self.label, "match": dict(self.match)}


@dataclass
class PluginManifest:
    id: str
    name: str
    version: str
    entry: str
    spec_version: str = API_SPEC_VERSION
    api_version: str = CORE_API_VERSION
    description: str = ""
    author: str = ""
    icon: str = ""
    capabilities: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()
    tools: tuple[ToolDecl, ...] = ()
    skills: tuple[str, ...] = ()
    commands: tuple[CommandDecl, ...] = ()
    config_schema: dict[str, Any] = field(default_factory=dict)
    path: Path | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def module(self) -> str:
        """entry 中冒号前的部分，例如 "plugin.py"。"""
        return self.entry.split(":", 1)[0]

    @property
    def class_name(self) -> str:
        """entry 中冒号后的类名。"""
        return self.entry.split(":", 1)[1]

    def icon_path(self) -> Path | None:
        """图标的绝对路径（已确认落在插件目录内）；无图标或路径不合法则 None。"""
        if not self.icon or self.path is None:
            return None
        candidate = (self.path / self.icon).resolve()
        try:
            candidate.relative_to(self.path.resolve())
        except ValueError:
            return None
        return candidate if candidate.is_file() else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "spec_version": self.spec_version,
            "api_version": self.api_version,
            "entry": self.entry,
            "description": self.description,
            "author": self.author,
            "capabilities": list(self.capabilities),
            "requires": list(self.requires),
            "tools": [t.to_dict() for t in self.tools],
            "skills": list(self.skills),
            "commands": [c.to_dict() for c in self.commands],
        }


# ---------------- 兼容判断 ----------------
def is_api_compatible(plugin_api_version: str, core_api_version: str = CORE_API_VERSION) -> bool:
    """API 契约：主版本号一致即兼容。"""
    return api_major(plugin_api_version) == api_major(core_api_version)


def is_spec_compatible(plugin_spec_version: str, core_spec_version: str = API_SPEC_VERSION) -> bool:
    """清单规范版本：只用于提示，不做拒绝（未知字段降级忽略）。"""
    return api_major(plugin_spec_version) <= api_major(core_spec_version)


# ---------------- 解析辅助 ----------------
def _require_str(data: dict, key: str, plugin_dir: Path) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PluginManifestError(f"{plugin_dir / MANIFEST_NAME}: 字段 {key!r} 必须是非空字符串")
    return value.strip()


def _opt_str(data: dict, key: str) -> str:
    value = data.get(key)
    return value.strip() if isinstance(value, str) else ""


def _str_list(data: dict, key: str, plugin_dir: Path) -> tuple[str, ...]:
    value = data.get(key, [])
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise PluginManifestError(f"{plugin_dir / MANIFEST_NAME}: 字段 {key!r} 必须是字符串数组")
    return tuple(v.strip() for v in value if v.strip())


def _parse_icon(data: dict, plugin_dir: Path) -> str:
    icon = _opt_str(data, "icon")
    if not icon:
        return ""
    if "://" in icon or Path(icon).is_absolute() or ".." in Path(icon).parts:
        raise PluginManifestError(
            f"{plugin_dir / MANIFEST_NAME}: icon 必须是插件目录内的相对路径，当前为 {icon!r}"
        )
    return icon


def _parse_tools(data: dict, plugin_dir: Path) -> tuple[ToolDecl, ...]:
    raw = data.get("tools", [])
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise PluginManifestError(f"{plugin_dir / MANIFEST_NAME}: 字段 'tools' 必须是数组")
    tools: list[ToolDecl] = []
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"].strip():
            raise PluginManifestError(f"{plugin_dir / MANIFEST_NAME}: 每个 tool 必须有非空 name")
        risk = str(item.get("risk", "read")).strip() or "read"
        if risk not in KNOWN_RISKS:
            raise PluginManifestError(
                f"{plugin_dir / MANIFEST_NAME}: tool {item['name']!r} 的 risk={risk!r} 非法，"
                f"可用: {list(KNOWN_RISKS)}"
            )
        schema = item.get("input_schema", {})
        if not isinstance(schema, dict):
            raise PluginManifestError(
                f"{plugin_dir / MANIFEST_NAME}: tool {item['name']!r} 的 input_schema 必须是对象"
            )
        tools.append(
            ToolDecl(
                name=item["name"].strip(),
                description=str(item.get("description", "") or ""),
                risk=risk,
                input_schema=schema,
            )
        )
    return tuple(tools)


def _parse_commands(data: dict, plugin_dir: Path) -> tuple[CommandDecl, ...]:
    raw = data.get("commands", [])
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise PluginManifestError(f"{plugin_dir / MANIFEST_NAME}: 字段 'commands' 必须是数组")
    commands: list[CommandDecl] = []
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("code"), str) or not item["code"].strip():
            raise PluginManifestError(f"{plugin_dir / MANIFEST_NAME}: 每个 command 必须有非空 code")
        match = item.get("match", {}) or {}
        if not isinstance(match, dict):
            raise PluginManifestError(
                f"{plugin_dir / MANIFEST_NAME}: command {item['code']!r} 的 match 必须是对象"
            )
        match_type = str(match.get("type", "text"))
        if match_type not in KNOWN_MATCH_TYPES:
            raise PluginManifestError(
                f"{plugin_dir / MANIFEST_NAME}: command {item['code']!r} 的 match.type={match_type!r} 非法，"
                f"可用: {list(KNOWN_MATCH_TYPES)}"
            )
        commands.append(
            CommandDecl(
                code=item["code"].strip(),
                label=str(item.get("label", "") or item["code"]).strip(),
                match=dict(match),
            )
        )
    return tuple(commands)


def parse_manifest(data: Any, plugin_dir: Path) -> PluginManifest:
    """校验并构造清单对象。任何不合法都抛 PluginManifestError。

    注意：这里只校验"结构合法"，不断言 API 版本兼容。
    兼容性由运行时在加载阶段判断，这样不兼容的插件也能被登记为 FAILED 记录
    并显示原因，而不是从列表里静默消失。
    """
    if not isinstance(data, dict):
        raise PluginManifestError(f"{plugin_dir / MANIFEST_NAME}: 顶层必须是 JSON 对象")

    for key in _REQUIRED_FIELDS:
        if key not in data:
            raise PluginManifestError(
                f"{plugin_dir / MANIFEST_NAME}: 缺少必填字段 {key!r}"
                f"（必填: {', '.join(_REQUIRED_FIELDS)}）"
            )

    plugin_id = _require_str(data, "id", plugin_dir)
    if not _ID_RE.match(plugin_id):
        raise PluginManifestError(
            f"{plugin_dir / MANIFEST_NAME}: 非法 id={plugin_id!r}，"
            "只允许小写字母开头、由小写字母/数字/下划线组成，长度 2-32"
        )

    entry = _require_str(data, "entry", plugin_dir)
    if entry.count(":") != 1:
        raise PluginManifestError(
            f"{plugin_dir / MANIFEST_NAME}: entry 必须形如 'plugin.py:ClassName'，当前为 {entry!r}"
        )
    module_part, class_part = entry.split(":", 1)
    if not module_part.strip() or not class_part.strip():
        raise PluginManifestError(f"{plugin_dir / MANIFEST_NAME}: entry 的模块名或类名不能为空")

    capabilities = _str_list(data, "capabilities", plugin_dir)
    unknown = [c for c in capabilities if c not in KNOWN_CAPABILITIES]
    if unknown:
        raise PluginManifestError(
            f"{plugin_dir / MANIFEST_NAME}: 未知能力 {unknown}，可用能力: {list(KNOWN_CAPABILITIES)}"
        )

    config_schema = data.get("config_schema", {}) or {}
    if not isinstance(config_schema, dict):
        raise PluginManifestError(f"{plugin_dir / MANIFEST_NAME}: config_schema 必须是对象")

    _known_keys = {
        "id", "name", "version", "spec_version", "api_version", "entry",
        "description", "author", "icon", "capabilities", "requires",
        "tools", "skills", "commands", "config_schema",
    }

    return PluginManifest(
        id=plugin_id,
        name=_require_str(data, "name", plugin_dir),
        version=_require_str(data, "version", plugin_dir),
        entry=entry,
        spec_version=_opt_str(data, "spec_version") or API_SPEC_VERSION,
        api_version=_require_str(data, "api_version", plugin_dir),
        description=_opt_str(data, "description"),
        author=_opt_str(data, "author"),
        icon=_parse_icon(data, plugin_dir),
        capabilities=capabilities,
        requires=_str_list(data, "requires", plugin_dir),
        tools=_parse_tools(data, plugin_dir),
        skills=_str_list(data, "skills", plugin_dir),
        commands=_parse_commands(data, plugin_dir),
        config_schema=config_schema,
        path=plugin_dir,
        extra={k: v for k, v in data.items() if k not in _known_keys},
    )


def load_manifest(plugin_dir: Path | str) -> PluginManifest:
    """从插件目录读取 plugin.json。"""
    folder = Path(plugin_dir)
    manifest_path = folder / MANIFEST_NAME
    if not manifest_path.is_file():
        raise PluginManifestError(f"缺少清单文件: {manifest_path}")

    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PluginManifestError(f"{manifest_path}: 读取或解析失败: {exc}") from exc

    return parse_manifest(data, folder)
