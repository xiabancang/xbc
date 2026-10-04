"""配置与密钥能力。

区分两个东西（很重要，别混）：

- **`ctx.config`**：用户/宿主给插件的配置，来自三层装配（默认 / 宿主 / 用户），
  插件只读。
- **`ctx.settings`**：插件自己维护的运行期键值（如"调用过几次"），插件可读写。

**密钥处理**（方案 3.6 / 3.10 S4）：

- 密钥存在独立的 `secrets.json`，**不进插件配置文件、不进仓库**；
- 读取时通过本能力，日志与错误信息一律脱敏；
- 长期目标是"宿主代持 + 代理出站"，让插件**根本不持有长期密钥**；
  V1 先做到"不落盘到插件目录 + 强制脱敏"。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..config.store import Config, PluginSettings, read_json


def mask_secret(value: str | None, keep: int = 2) -> str:
    """把密钥变成可安全打印的形式：前 keep 位 + 星号 + 后 keep 位。"""
    if not value:
        return ""
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}{'*' * (len(value) - keep * 2)}{value[-keep:]}"


class SettingsCapability:
    """插件配置与密钥的门面。"""

    def __init__(self, config: Config, secrets_path: Path | str | None = None, logger: Any = None) -> None:
        self._config = config
        self._secrets_path = Path(secrets_path) if secrets_path else None
        self._log = logger

    # ---------- 插件自维护配置 ----------
    def for_plugin(self, plugin_id: str) -> PluginSettings:
        return self._config.plugin_section(plugin_id)

    # ---------- 密钥 ----------
    def get_secret(self, name: str) -> str | None:
        """读取密钥。找不到返回 None；**永远不要把返回值写进日志**。"""
        if self._secrets_path is None:
            return None
        data = read_json(self._secrets_path)
        if not isinstance(data, dict):
            return None
        value = data.get(name)
        return value if isinstance(value, str) and value else None

    def masked(self, name: str) -> str:
        """密钥的脱敏形式，供展示。"""
        return mask_secret(self.get_secret(name))

    def has_secret(self, name: str) -> bool:
        return self.get_secret(name) is not None
