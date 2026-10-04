"""运行期目录的唯一来源。

设计约束：除了本文件，工程里不应再出现任何硬编码路径。
V18 原型把 "%LOCALAPPDATA%/AI企业内容操作系统" 写在全局常量里，
导致数据位置无法配置、无法备份、无法做便携模式；这里把它收敛成一个对象。

所有目录都支持用环境变量 XBC_HOME 整体重定向 —— 单元测试靠它隔离，
将来做"绿色便携版"也靠它。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

APP_DIR_NAME = "夏半仓工具箱"

# 插件 id 会参与拼路径，必须限制字符集，避免出现 ../ 之类的路径穿越
_SAFE_ID = re.compile(r"^[a-z][a-z0-9_]{1,31}$")


def assert_safe_id(value: str, what: str = "plugin_id") -> str:
    """校验一个将被用作目录名的标识符。"""
    if not isinstance(value, str) or not _SAFE_ID.match(value):
        raise ValueError(
            f"非法的 {what}={value!r}：只允许小写字母开头、由小写字母/数字/下划线组成，长度 2-32"
        )
    return value


def app_root() -> Path:
    """应用根目录。XBC_HOME 优先，否则用 %LOCALAPPDATA%。"""
    override = os.environ.get("XBC_HOME")
    if override and override.strip():
        return Path(override).expanduser().resolve()
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return (Path(base) / APP_DIR_NAME).resolve()


class AppPaths:
    """集中管理全部目录。

    插件拿不到 AppPaths 本体，只能通过 AppContext 拿到自己的数据目录，
    这样插件无法越界写别的插件或内核的目录。
    """

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root).resolve() if root is not None else app_root()

    # ---------- 内核目录 ----------
    @property
    def config_file(self) -> Path:
        return self.root / "config.json"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def cache_dir(self) -> Path:
        return self.root / "cache"

    @property
    def models_dir(self) -> Path:
        """**Core 级共享模型目录**：`<数据根>/models/<模型标识>/`。

        为什么不放插件数据目录：插件数据目录是**按插件隔离**的，
        两个插件要用同一个模型就会各存一份（中文 CLIP 一份 294MB）。
        模型是**跨插件共享资源**，所以放在这里，与 TASK-006 定下的
        "用户数据与插件目录分离"同一条原则 —— 它既不随 `.xbcplugin` 包分发，
        也不属于任何一个插件。

        插件**不应该自己拼这个路径**：Core 在注册 Provider 时通过
        `ModelProvider.bind_models()` 把位置交给实现方。
        """
        return self.root / "models"

    @property
    def user_plugins_dir(self) -> Path:
        """用户安装的插件目录（将来插件商城的落地位置）。"""
        return self.root / "plugins"

    @property
    def user_config_dir(self) -> Path:
        """用户层配置目录（跨版本存活，绝不覆盖）。"""
        return self.root / "config"

    @property
    def user_plugins_config(self) -> Path:
        """用户层的插件配置（方案 3.6 的 L3）。"""
        return self.user_config_dir / "plugins.json"

    @property
    def secrets_file(self) -> Path:
        """密钥文件：不进插件目录、不进仓库。"""
        return self.user_config_dir / "secrets.json"

    @property
    def user_skills_dir(self) -> Path:
        """用户自己的 Skill 目录。"""
        return self.root / "skills"

    @property
    def installed_ledger(self) -> Path:
        """插件安装台账：安装时间、来源、升级历史。

        注意它**不是版本的权威来源** —— 版本以插件目录里的 `plugin.json` 为准
        （见 PluginInstaller.installed 的说明）。
        """
        return self.root / "installed.json"

    # ---------- 插件私有空间 ----------
    def plugin_data_dir(self, plugin_id: str) -> Path:
        return self.data_dir / assert_safe_id(plugin_id)

    def plugin_cache_dir(self, plugin_id: str) -> Path:
        return self.cache_dir / assert_safe_id(plugin_id)

    def ensure(self) -> "AppPaths":
        """创建全部基础目录（幂等）。"""
        for path in (
            self.root,
            self.logs_dir,
            self.data_dir,
            self.cache_dir,
            self.models_dir,
            self.user_plugins_dir,
            self.user_config_dir,
            self.user_skills_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
        return self

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"AppPaths(root={self.root})"


def builtin_plugins_dir() -> Path | None:
    """内置插件目录。

    源码运行时是 <仓库根>/plugins（即本文件向上四层）。
    打包成 exe 后目录结构会变，可用 XBC_BUILTIN_PLUGINS 显式指定；
    找不到时返回 None（内核仍能只加载用户插件目录）。
    """
    override = os.environ.get("XBC_BUILTIN_PLUGINS")
    if override and override.strip():
        candidate = Path(override).expanduser().resolve()
        return candidate if candidate.is_dir() else None

    candidate = Path(__file__).resolve().parents[3] / "plugins"
    return candidate if candidate.is_dir() else None


def builtin_config_dir() -> Path | None:
    """宿主层配置目录（随内核发布）。

    源码运行时是 <仓库根>/config。打包后目录结构会变，可用
    XBC_BUILTIN_CONFIG 显式指定；找不到时返回 None。
    """
    override = os.environ.get("XBC_BUILTIN_CONFIG")
    if override and override.strip():
        candidate = Path(override).expanduser().resolve()
        return candidate if candidate.is_dir() else None

    candidate = Path(__file__).resolve().parents[3] / "config"
    return candidate if candidate.is_dir() else None
