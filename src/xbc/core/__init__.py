"""内核层：不依赖任何 GUI 库，可以独立测试和运行。

这是刻意的约束 —— 内核能脱离界面跑起来，
才谈得上"稳定的桌面软件内核"和"插件可被单独测试"。
"""

from .config import Config
from .context import AppContext, PluginContext
from .paths import AppPaths

__all__ = ["AppContext", "PluginContext", "Config", "AppPaths"]
