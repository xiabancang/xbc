"""插件中心页：直接复用 TASK-004 的 `PluginManagerPanel`。

**没有第二个插件管理实现** —— 页里装的就是那个面板本身。
所以"桌面看到的状态 == Runtime 状态"这条保证在二期里依然成立。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

__all__ = ["PluginsPage"]


class PluginsPage(QWidget):
    """插件中心。**不是 `Page` 子类** —— 它不需要工具桥，直接用 Runtime。

    插件管理本来就不走工具（工具是给 Agent 的），
    所以这里复用 `PluginManagerPanel`，与 CLI 的 `plugin enable/disable` 同一方法。
    """

    title = "插件中心"

    def __init__(self, ctx: Any, bridge: Any, manager: Any = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from ..shell import PluginManagerPanel

        self.bridge = bridge
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("插件中心（启用 / 停用直接调用 Runtime，与命令行同一方法）"))
        # 复用启动时装配的那个 manager —— 界面**不自己再 discover 一遍**
        self.panel = PluginManagerPanel(ctx, manager)
        layout.addWidget(self.panel, 1)

    # 主窗口切换过来时调用；与 Page 同名，便于统一处理
    def refresh(self) -> None:
        self.panel.refresh()
