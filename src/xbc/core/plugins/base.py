"""插件基类与生命周期。

生命周期（内核严格按此顺序驱动，插件只能在钩子里做事）：

    DISCOVERED ──on_load──▶ LOADED ──on_start──▶ STARTED
                                                  │
                              STOPPED ◀──on_stop──┘
                                 │
                                 └──on_unload──▶ UNLOADED

任一步抛异常 → FAILED，并被内核记录；不会影响其他插件和宿主。
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Callable

from ..errors import PluginStateError
from .manifest import PluginManifest


class PluginState(str, Enum):
    DISCOVERED = "discovered"
    LOADED = "loaded"
    STARTED = "started"
    STOPPED = "stopped"
    FAILED = "failed"
    UNLOADED = "unloaded"


def action(func: Callable[..., Any]) -> Callable[..., Any]:
    """把一个方法标记为"可被宿主调用的动作"。

    只有被标记的方法才会出现在 plugin.actions() 里，
    宿主（CLI / 图形界面）据此生成按钮或命令。
    """
    func.__xbc_action__ = True  # type: ignore[attr-defined]
    return func


class XbcPlugin:
    """所有插件必须继承的基类。

    插件只能通过 self.ctx 访问宿主能力；不要 import 内核内部模块。
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

    # ---------- 生命周期钩子（子类按需覆盖） ----------
    def on_load(self) -> None:
        """代码已导入、上下文已注入。适合做只依赖配置的准备工作。"""

    def on_start(self) -> None:
        """启动：适合注册定时任务、建立连接、检查能力可用性。"""

    def on_stop(self) -> None:
        """停止：释放运行期资源。要求可重复调用、不抛异常。"""

    def on_unload(self) -> None:
        """卸载前的最后清理。"""

    # ---------- 动作 ----------
    def actions(self) -> dict[str, Callable[..., Any]]:
        """收集本插件所有被 @action 标记的方法。"""
        found: dict[str, Callable[..., Any]] = {}
        for name in dir(type(self)):
            if name.startswith("_"):
                continue
            attr = getattr(self, name, None)
            if callable(attr) and getattr(attr, "__xbc_action__", False):
                found[name] = attr
        return found

    def invoke(self, action_name: str, **kwargs: Any) -> Any:
        actions = self.actions()
        if action_name not in actions:
            raise PluginStateError(
                f"插件 {self.manifest.id if self.manifest else '?'} 没有动作 {action_name!r}；"
                f"可用动作: {sorted(actions)}"
            )
        return actions[action_name](**kwargs)
