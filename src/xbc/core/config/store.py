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
        # 默认用哪个 Provider
        "provider": "ollama",
        # 全局模型名：用户直接操作的旋钮。
        # 优先于各 Provider 段里的 model —— 否则用户在 ai.model 里选了模型，
        # 却会被 ai.ollama.model 的内置默认值悄悄盖掉。留空则用 Provider 自己的设置。
        "model": "",
        "embedding_model": "",
        "ollama": {
            # 用 127.0.0.1 而不是 localhost：Windows 上 localhost 会先试 IPv6 ::1，
            # 失败后回退 IPv4，每次探测白等约 2 秒（实测）。
            "url": "http://127.0.0.1:11434",
            "model": "qwen2.5vl:3b",
            "embedding_model": "nomic-embed-text",
            "timeout": 180,
            # 以下组合来自 V18 原型的已验证参数
            "options": {"temperature": 0.1, "num_ctx": 8192, "num_predict": 700},
        },
        # API 模型接口预留：base_url 为空时**不注册**，不影响本地开箱体验。
        # 密钥放 secrets.json，这里只放它的键名。
        "openai_compatible": {
            "base_url": "",
            "model": "",
            "embedding_model": "",
            "api_key_secret": "openai_compatible_api_key",
            "timeout": 120,
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
