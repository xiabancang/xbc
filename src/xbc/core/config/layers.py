"""插件配置的三层装配。

方案 3.6 定义的三层：

| 层 | 位置 | 谁改 | 升级时 |
|---|---|---|---|
| L1 默认层 | 插件 `config_schema` 里的 `default` | 插件作者 | 随笔更新 |
| L2 宿主层 | `<仓库>/config/plugins.json` | 内核维护者 | 被新版本替换 |
| L3 用户层 | `%LOCALAPPDATA%/夏半仓工具箱/config/plugins.json` | 用户 | **保留，不覆盖** |

**合并语义：整对象替换，不做深合并。**
理由是深合并会制造两种难以解释的状态 ——"我明明改了 A，为什么 B 还是旧值"、
"升级后我的配置被悄悄改了"。整体替换语义简单、可预测。

注意区分两个层面：

- **行（row）字段**按字段覆盖：用户行只写 `config` 时，`enabled` 仍沿用宿主行；
- **`config` 对象**整体替换：用户行一旦提供 `config`，就用它完整取代低层的 `config`。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .store import read_json, write_json


@dataclass
class PluginRow:
    """配置文件中一个插件对应的行。"""

    plugin_id: str
    enabled: bool | None = None
    config: dict | None = None
    layer: str = ""  # config 来自哪一层：default / host / user

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        if self.enabled is not None:
            data["enabled"] = self.enabled
        if self.config is not None:
            data["config"] = self.config
        return data


def config_defaults(schema: dict | None) -> dict:
    """L1：从 config_schema 的 properties[*].default 收集默认值。

    只处理顶层属性（当前插件的配置都是扁平的），保持实现简单可预测。
    """
    if not isinstance(schema, dict):
        return {}
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return {}
    defaults: dict[str, Any] = {}
    for key, spec in properties.items():
        if isinstance(spec, dict) and "default" in spec:
            defaults[key] = copy.deepcopy(spec["default"])
    return defaults


def load_rows(path: Path | str) -> dict[str, dict]:
    """读一个配置文件，返回 {plugin_id: {enabled?, config?}}。"""
    data = read_json(Path(path))
    if not isinstance(data, dict):
        return {}
    plugins = data.get("plugins", {})
    if not isinstance(plugins, dict):
        return {}
    rows: dict[str, dict] = {}
    for plugin_id, row in plugins.items():
        if isinstance(row, dict):
            rows[plugin_id] = row
    return rows


def merge_rows(host_rows: dict[str, dict], user_rows: dict[str, dict]) -> dict[str, dict]:
    """按字段合并两层的行。

    - `enabled`：用户层有就用用户层，否则用宿主层；
    - `config`：**整体替换**（用户层一旦提供，就完全取代低层），不做深合并。
    """
    merged: dict[str, dict] = {pid: dict(row) for pid, row in host_rows.items()}
    for plugin_id, user_row in user_rows.items():
        base = merged.setdefault(plugin_id, {})
        if "enabled" in user_row:
            base["enabled"] = user_row["enabled"]
        if "config" in user_row:
            base["config"] = user_row["config"]  # 整体替换
    return merged


def effective_config(
    manifest: Any,
    host_rows: dict[str, dict],
    user_rows: dict[str, dict],
) -> tuple[dict, str]:
    """算出某个插件的最终有效配置，并**准确报告它来自哪一层**。

    返回 `(配置值, 来源)`，来源 ∈ {"default", "host", "user"}。

    判定顺序即优先级：用户层 > 宿主层 > 默认层；某一层一旦提供 `config`，
    就整体取代低层（不做深合并）。
    """
    plugin_id = getattr(manifest, "id", "")

    user_config = user_rows.get(plugin_id, {}).get("config")
    if isinstance(user_config, dict):
        return copy.deepcopy(user_config), "user"

    host_config = host_rows.get(plugin_id, {}).get("config")
    if isinstance(host_config, dict):
        return copy.deepcopy(host_config), "host"

    return config_defaults(getattr(manifest, "config_schema", None)), "default"


def resolve_row(plugin_id: str, merged: dict[str, dict], default_enabled: bool = True) -> PluginRow:
    """把合并后的行转成 `PluginRow`，并判断 `enabled`。"""
    row = merged.get(plugin_id, {})
    enabled = row.get("enabled")
    config = row.get("config")
    return PluginRow(
        plugin_id=plugin_id,
        enabled=bool(enabled) if isinstance(enabled, bool) else default_enabled,
        config=copy.deepcopy(config) if isinstance(config, dict) else None,
    )


def write_row(path: Path | str, plugin_id: str, **fields: Any) -> None:
    """写入/更新用户层配置中的一行（用于 enable / disable / 配置编辑）。

    只改用户层文件，**不动宿主层** —— 宿主层随内核发布，不该被用户操作改写。
    """
    target = Path(path)
    data = read_json(target)
    if not isinstance(data, dict):
        data = {}
    plugins = data.setdefault("plugins", {})
    if not isinstance(plugins, dict):
        plugins = {}
        data["plugins"] = plugins
    row = plugins.setdefault(plugin_id, {})
    if not isinstance(row, dict):
        row = {}
        plugins[plugin_id] = row
    for key, value in fields.items():
        if value is None:
            row.pop(key, None)
        else:
            row[key] = value
    write_json(target, data)
