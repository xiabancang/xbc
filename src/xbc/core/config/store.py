"""配置存储：单个 JSON 文件 + 原子写入。

为什么用 JSON 而不是 YAML：
**内核零第三方依赖**是硬约束，解析 YAML 需要引入 PyYAML。
方案里写的是 `plugins.yml`，V1 实际使用 `plugins.json` —— 概念不变（三层补丁），
只换载体。这是本 MVP 相对方案的一处**有意偏差**，已在测试报告中记录。
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
from pathlib import Path
from typing import Any

# 内核默认配置。"默认值"的唯一来源，插件不应假设某个键一定存在。
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
        # 用哪个 Provider。内核只实现了这两个名字（见 capabilities/ai/service.py）。
        "provider": "ollama",
        # 模型名。**Core 里不写死任何模型名** —— 留空时调用会报明确错误
        # （含 provider 名、原因、检查建议），而不是静默失败。
        "model": "",
        # 向量模型与对话模型通常不是同一个，所以分开配。
        "embedding_model": "",
        # Provider 参数，原样交给 Provider：
        # temperature / num_ctx / num_predict / timeout …
        "options": {},
        # 各 Provider 的**连接信息**（不是参数）。
        # 用 127.0.0.1 而非 localhost：Windows 上 localhost 会先试 IPv6 ::1、
        # 失败再回退 IPv4，每次探测白等约 2 秒（实测 2.07s vs 0.004s）。
        "ollama": {"url": "http://127.0.0.1:11434"},
        "openai_compatible": {
            "base_url": "",
            "api_key_secret": "openai_compatible_api_key",
        },
    },
    "plugins": {},
}


def _deep_merge(base: dict, override: dict) -> dict:
    """以 base 为底，用 override 覆盖，返回新字典（不修改入参）。

    仅用于**内核自身配置**（app / ffmpeg / ai）：新增配置项时旧文件不会 KeyError。
    **插件配置不使用它** —— 插件配置是整对象替换，见 layers.py。
    """
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def read_json(path: Path) -> Any:
    """读 JSON；文件不存在或损坏返回 None（并保留损坏文件供排查）。"""
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        try:
            path.replace(path.with_name(path.name + ".broken"))
        except OSError:
            pass
        return None


def write_json(path: Path, data: Any) -> None:
    """原子写入：先写同目录临时文件，再 os.replace 覆盖。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


class Config:
    """内核配置对象。线程安全性不在当前范围内（内核目前单线程使用）。"""

    def __init__(self, path: Path | str, defaults: dict | None = None) -> None:
        self.path = Path(path)
        self.defaults = defaults if defaults is not None else DEFAULTS
        self._data: dict = copy.deepcopy(self.defaults)
        self.load()

    # ---------- 读写 ----------
    def load(self) -> None:
        raw = read_json(self.path)
        if not isinstance(raw, dict):
            self._data = copy.deepcopy(self.defaults)
            return
        self._data = _deep_merge(self.defaults, raw)

    def save(self) -> None:
        write_json(self.path, self._data)

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

    def remove_plugin_section(self, plugin_id: str, save: bool = True) -> bool:
        """删除某个插件的运行期配置分区（彻底卸载时用）。

        返回是否真的删掉了东西。**通过本对象删除**而不是直接改文件，
        这样内存里的副本与磁盘保持一致 —— 否则长驻宿主会拿着过期配置继续跑。
        """
        plugins = self._data.get("plugins")
        if not isinstance(plugins, dict) or plugin_id not in plugins:
            return False
        plugins.pop(plugin_id, None)
        if save:
            self.save()
        return True

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"Config(path={self.path})"


class PluginSettings:
    """插件配置命名空间：持久化在 config.json 的 plugins.<plugin_id> 下。

    这是**运行期**的插件私有配置（插件自己设置的键值）。
    用户可编辑的插件配置走 layers.py 的三层装配，两者用途不同：
    - PluginSettings：插件运行时自己维护的状态（如调用次数）
    - layers：用户/宿主对插件行为的配置（如 frame_count）
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
