"""主界面：左侧导航 + 右侧页面。

## 界面在这里只做两件事

1. **装壳**：导航切页
2. **转发**：把 `ctx` 与启动时装配的 `PluginManager` 交给各页

业务能力一律通过 `ToolBridge` 调工具；插件管理直接用那个 manager
（与 CLI 同一方法）。**主窗口本身不认识任何业务概念。**
"""

from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMainWindow,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..version import APP_NAME, CORE_VERSION
from .bridge import ToolBridge
from .pages import LibraryPage, MatchPage, PluginsPage, WorkbenchPage

__all__ = ["PAGES", "MainWindow", "run_app"]

#: 导航顺序与页面类。**没有"为将来预留"的入口** —— 每一项都有页面实现。
PAGES = [
    ("workbench", "工作台", WorkbenchPage),
    ("library", "素材库", LibraryPage),
    ("match", "文案匹配", MatchPage),
    ("plugins", "插件中心", PluginsPage),
]


class MainWindow:
    """主窗口。用组合而不是继承，与 TASK-004 的 `HostWindow` 一致。"""

    def __init__(self, ctx: Any, manager: Any = None) -> None:
        self.ctx = ctx
        self.manager = manager
        self.bridge = ToolBridge(ctx)

        self.window = QMainWindow()
        self.window.setWindowTitle(f"{APP_NAME} v{CORE_VERSION}")
        self.window.resize(1180, 760)

        central = QWidget()
        self.window.setCentralWidget(central)
        row = QHBoxLayout(central)

        # ---- 左侧导航 ----
        left = QVBoxLayout()
        left.addWidget(QLabel(f"{APP_NAME}　AI 软件工具平台"))
        self.nav = QListWidget()
        self.nav.setFixedWidth(150)
        left.addWidget(self.nav, 1)
        self.footer = QLabel("本地模式")
        left.addWidget(self.footer)
        row.addLayout(left)

        # ---- 右侧页面 ----
        right = QVBoxLayout()
        self.title = QLabel("")
        right.addWidget(self.title)
        self.stack = QStackedWidget()
        right.addWidget(self.stack, 1)
        row.addLayout(right, 1)

        # ---- 建页 ----
        self.pages: dict[str, Any] = {}
        for key, label, page_class in PAGES:
            page = page_class(ctx, self.bridge, manager)
            self.pages[key] = page
            self.nav.addItem(label)
            self.stack.addWidget(page)
            if hasattr(page, "navigate"):
                page.navigate.connect(self.show_page)

        self.nav.currentRowChanged.connect(self.on_nav_changed)
        self.show_page(PAGES[0][0])

    # ---------------- 导航 ----------------
    def keys(self) -> list[str]:
        return [key for key, _label, _cls in PAGES]

    def current_key(self) -> str:
        return self.keys()[self.nav.currentRow()]

    def show_page(self, key: str) -> bool:
        """切到某个页面。切过去时**重新取数**（界面不缓存）。"""
        if key not in self.pages:
            return False
        row = self.keys().index(key)
        if self.nav.currentRow() != row:
            self.nav.setCurrentRow(row)
            return True
        self._activate(key)
        return True

    def on_nav_changed(self, row: int) -> None:
        if 0 <= row < len(PAGES):
            self._activate(self.keys()[row])

    def _activate(self, key: str) -> None:
        self.stack.setCurrentWidget(self.pages[key])
        label = next(lbl for k, lbl, _cls in PAGES if k == key)
        self.title.setText(f"{APP_NAME} › {label}")
        page = self.pages[key]
        refresh = getattr(page, "refresh", None)
        if callable(refresh):
            refresh()


def run_app(ctx: Any, manager: Any = None) -> int:
    """启动主界面。"""
    app = QApplication.instance() or QApplication([])
    window = MainWindow(ctx, manager)
    window.window.show()
    return int(app.exec())
