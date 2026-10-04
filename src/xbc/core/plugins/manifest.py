"""插件清单（plugin.json）的解析与校验。

清单是"合同"：内核只根据清单决定是否加载、能否兼容、该注入哪些能力，
判断过程不依赖插件代码本身。因此清单必须先于代码被严格校验。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...version import CORE_API_VERSION, api_major
from ..errors import PluginManifestError

MANIFEST_NAME = "plugin.json"

_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
_REQUIRED_FIELDS = ("id", "name", "version", "api_version", "entry")

# 内核已知的能力名。清单里只能声明这些，写错会被明确拒绝（而不是静默失效）。
KNOWN_CAPABILITIES: tuple[str, ...] = ("files", "settings", "ffmpeg", "ai")


@dataclass
class PluginManifest:
    id: str
    name: str
    version: str
    api_version: str
    entry: str
    description: str = ""
    author: str = ""
    capabilities: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    path: Path | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def module(self) -> str:
        """entry 中冒号前的部分，例如 "plugin.py" 或 "mypkg.main"。"""
        return self.entry.split(":", 1)[0]

    @property
    def class_name(self) -> str:
        """entry 中冒号后的类名。"""
        return self.entry.split(":", 1)[1]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "api_version": self.api_version,
            "entry": self.entry,
            "description": self.description,
            "author": self.author,
            "capabilities": list(self.capabilities),
            "actions": list(self.actions),
        }


def is_api_compatible(plugin_api_version: str, core_api_version: str = CORE_API_VERSION) -> bool:
    """主版本号一致即兼容。"""
    return api_major(plugin_api_version) == api_major(core_api_version)


def _require_str(data: dict, key: str, plugin_dir: Path) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PluginManifestError(f"{plugin_dir / MANIFEST_NAME}: 字段 {key!r} 必须是非空字符串")
    return value.strip()


def _parse_str_list(data: dict, key: str, plugin_dir: Path) -> tuple[str, ...]:
    value = data.get(key, [])
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise PluginManifestError(f"{plugin_dir / MANIFEST_NAME}: 字段 {key!r} 必须是字符串数组")
    return tuple(v.strip() for v in value if v.strip())


def parse_manifest(data: Any, plugin_dir: Path) -> PluginManifest:
    """校验并构造清单对象。任何不合法都抛 PluginManifestError。"""
    if not isinstance(data, dict):
        raise PluginManifestError(f"{plugin_dir / MANIFEST_NAME}: 顶层必须是 JSON 对象")

    for key in _REQUIRED_FIELDS:
        if key not in data:
            raise PluginManifestError(
                f"{plugin_dir / MANIFEST_NAME}: 缺少必填字段 {key!r}（必填: {', '.join(_REQUIRED_FIELDS)}）"
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

    # 注意：这里只校验"结构合法"，不断言版本兼容。
    # 兼容性由 PluginManager 在加载阶段判断，这样不兼容的插件也能被登记为
    # FAILED 记录并显示原因，而不是从列表里静默消失。
    api_version = _require_str(data, "api_version", plugin_dir)

    capabilities = _parse_str_list(data, "capabilities", plugin_dir)
    unknown = [c for c in capabilities if c not in KNOWN_CAPABILITIES]
    if unknown:
        raise PluginManifestError(
            f"{plugin_dir / MANIFEST_NAME}: 未知能力 {unknown}，可用能力: {list(KNOWN_CAPABILITIES)}"
        )

    return PluginManifest(
        id=plugin_id,
        name=_require_str(data, "name", plugin_dir),
        version=_require_str(data, "version", plugin_dir),
        api_version=api_version,
        entry=entry,
        description=str(data.get("description", "") or ""),
        author=str(data.get("author", "") or ""),
        capabilities=capabilities,
        actions=_parse_str_list(data, "actions", plugin_dir),
        path=plugin_dir,
        extra={
            k: v
            for k, v in data.items()
            if k
            not in {
                "id", "name", "version", "api_version", "entry",
                "description", "author", "capabilities", "actions",
            }
        },
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
