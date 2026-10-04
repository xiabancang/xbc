"""内核对外暴露的扩展点契约。

插件可以实现这些钩子来参与内核行为。它们**不是**插件之间的通信方式
（那是 Service），而是内核自己留出的横切扩展点。

按"最弱机制优先"原则（方案 1.1），这里的钩子都是**只读通知型**：
只能观察，不能改写内核决策。真正需要改写时再加更强的机制。
"""

from __future__ import annotations

from .hookspec import hookspec


class KernelHooks:
    """内核扩展点契约。参数名即契约，插件实现时不能写错（注册时校验）。"""

    @hookspec
    def on_plugin_state_changed(self, plugin_id: str, old_state: str, new_state: str) -> None:
        """插件状态发生变化时调用。用于审计与界面刷新。"""

    @hookspec
    def on_tool_called(self, tool_name: str, arguments: dict) -> None:
        """工具被调用时调用（在执行之前）。用于审计与统计。

        注意：它是**通知**，不参与放行决策 —— 权限必须在执行点由内核强制，
        不能依赖插件实现（方案 3.10 S2）。
        """


__all__ = ["KernelHooks"]
