"""插件基类与生命周期状态。

生命周期（内核严格按此顺序驱动，插件只能在回调里做事）：

    DISCOVERED ──load──▶ LOADED ──activate──▶ ACTIVE
                                              │
                          INACTIVE ◀─deactivate┘
                             │
                             └──unload──▶ UNLOADED

    任意环节失败 → FAILED（记录可读原因，可重试；不影响其他插件与宿主）

关键约定：

- `load` 只做"导入代码 + 实例化 + 校验配置"，**不注册任何东西**；
- `apply` 才是注册阶段，插件在这里通过 `ctx` 注册工具、技能、服务；
- `deactivate` 由内核释放作用域完成，插件**不需要写清理代码** ——
  这正是"注册即资源"（Scope/effect）带来的好处。
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from .manifest import PluginManifest


class PluginState(str, Enum):
    DISCOVERED = "discovered"
    LOADED = "loaded"
    ACTIVE = "active"
    INACTIVE = "inactive"
    FAILED = "failed"
    UNLOADED = "unloaded"


class XbcPlugin:
    """所有插件必须继承的基类。

    插件只能通过 `self.ctx` 访问宿主能力；不要 import 内核内部实现。
    """

    manifest: PluginManifest | None = None

    def __init__(self) -> None:
        self.ctx: Any = None
        self.log: Any = None
        self._state: PluginState = PluginState.DISCOVERED

    # ---------- 内核调用，插件不要覆盖 ----------
    def bind(self, manifest: PluginManifest, context: Any, logger: Any) -> None:
        self.manifest = manifest
        self.ctx = context
        self.log = logger

    @property
    def state(self) -> PluginState:
        return self._state

    # ---------- 生命周期回调（子类按需覆盖） ----------
    def on_load(self) -> None:
        """代码已导入、上下文已注入。适合做只依赖配置的准备工作。"""

    def apply(self, ctx: Any, config: dict) -> None:
        """注册阶段：在这里注册工具、技能、服务。

        所有注册都应通过 `ctx.effect(...)` 或注册方法返回的 disposer 挂到
        当前作用域上，内核会在 deactivate 时统一释放。
        """

    def on_unload(self) -> None:
        """模块移出内存前的最后清理。"""

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        plugin_id = self.manifest.id if self.manifest else "?"
        return f"<{type(self).__name__} id={plugin_id} state={self._state.value}>"
