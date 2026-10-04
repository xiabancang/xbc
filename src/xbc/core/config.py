"""配置存储：单个 JSON 文件 + 原子写入。

为什么先用 JSON 而不是 pydantic-settings / TOML：
最小内核要能零第三方依赖直接跑起来。等到配置项复杂到需要类型校验时再引入，
不必现在付出复杂度（对应"不为了未来可能出现的需求过度设计"）。

配置文件位置由 AppPaths.config_file 决定，本模块不关心具体路径。
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
from pathlib import Path
from typing import Any

# 内核默认配置。这里是"默认值"的唯一来源，插件不应假设键一定存在。
DEFAULTS: dict[str, Any] = {
    "app": {
        "log_level": "INFO",
    },
    "ffmpeg": {
        # 可填绝对路径；填 "ffmpeg" 表示从 PATH 查找
        "ffmpeg_path": "ffmpeg",
        "ffprobe_path": "ffprobe",
    },
    "ai": {
        "provider": "ollama",
        "ollama": {
            "url": "http://localhost:11434/api/generate",
            "model": "qwen2.5vl:3b",
            "timeout": 180,
            # 以下组合来自 V18 原型的已验证参数
            "options": {"temperature": 0.1, "num_ctx": 8192, "num_predict": 700},
        },
    },
    # 插件自己的配置全部放在这里，按插件 id 分区
    "plugins": {},
}


def _deep_merge(base: dict, override: dict) -> dict:
    """以 base 为底，用 override 覆盖，返回新字典（不修改入参）。"""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


class Config:
    """内核配置对象。线程安全性不在当前范围内（内核目前单线程使用）。"""

    def __init__(self, path: Path | str, defaults: dict | None = None) -> None:
        self.path = Path(path)
        self.defaults = defaults if defaults is not None else DEFAULTS
        self._data: dict = copy.deepcopy(self.defaults)
        self.load()

    # ---------- 读写 ----------
    def load(self) -> None:
        if not self.path.exists():
            self._data = copy.deepcopy(self.defaults)
            return

        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # 配置损坏时：备份原文件（供人工排查）并退回默认值，保证内核仍能启动
            try:
                self.path.replace(self.path.with_name(self.path.name + ".broken"))
            except OSError:
                pass
            self._data = copy.deepcopy(self.defaults)
            return

        if not isinstance(raw, dict):
            raw = {}
        # 关键：用默认值兜底，旧配置缺少新键时不会 KeyError
        self._data = _deep_merge(self.defaults, raw)

    def save(self) -> None:
        """原子写入：先写同目录临时文件，再 os.replace 覆盖。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self._data, ensure_ascii=False, indent=2)

        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=".config-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(tmp_name, self.path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    # ---------- 访问 ----------
    def as_dict(self) -> dict:
        return copy.deepcopy(self._data)

    def get(self, dotted: str, default: Any = None) -> Any:
        """按 "ai.ollama.model" 这样的点号路径取值。"""
        node: Any = self._data
        for part in str(dotted).split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted: str, value: Any, save: bool = True) -> None:
        parts = str(dotted).split(".")
        node = self._data
        for part in parts[:-1]:
            nxt = node.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                node[part] = nxt
            node = nxt
        node[parts[-1]] = value
        if save:
            self.save()

    def plugin_section(self, plugin_id: str) -> "PluginSettings":
        return PluginSettings(self, plugin_id)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"Config(path={self.path})"


class PluginSettings:
    """插件配置命名空间：持久化在 config.json 的 plugins.<plugin_id> 下。

    这样每个插件的配置互相隔离，卸载插件时也不会污染内核配置。
    """

    def __init__(self, config: Config, plugin_id: str) -> None:
        self._config = config
        self._id = plugin_id
        self._config._data.setdefault("plugins", {}).setdefault(plugin_id, {})

    @property
    def raw(self) -> dict:
        return self._config._data.setdefault("plugins", {}).setdefault(self._id, {})

    def get(self, key: str, default: Any = None) -> Any:
        return self.raw.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.raw[key] = value
        self._config.save()

    def all(self) -> dict:
        return copy.deepcopy(self.raw)
