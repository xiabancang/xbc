"""最小图形宿主（PySide6）。

它只是内核的一个"消费者"：
插件列表、状态、动作按钮全部来自 PluginManager，
宿主自身不含任何插件逻辑，也不认识任何具体插件。

这就是"未来持续加插件而不需要频繁修改工具箱核心"的验证点：
加一个插件，这个界面不用改一行代码。
"""

from __future__ import annotations

import logging
from typing import Any

from ..core.plugins.base import PluginState
from ..version import APP_NAME, CORE_VERSION


class QtLogHandler(logging.Handler):
    """把内核日志搬进界面，用户能直接看到现场。"""

    def __init__(self, widget: Any) -> None:
        super().__init__()
        self.widget = widget
        self.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.widget.appendPlainText(self.format(record))
        except Exception:  # noqa: BLE001 - 界面写日志失败不能反过来影响内核
            pass


def _qtwidgets() -> Any:
    from PySide6 import QtWidgets

    return QtWidgets


def _qtcore() -> Any:
    from PySide6 import QtCore

    return QtCore


class HostWindow:
    """用组合而不是继承，避免界面类被 Qt 的继承层次绑死。"""

    def __init__(self, ctx: Any) -> None:
        qt = _qtwidgets()
        self.ctx = ctx
        self.manager = ctx.create_plugin_manager()

        self.window = qt.QMainWindow()
        self.window.setWindowTitle(f"{APP_NAME} v{CORE_VERSION}")
        self.window.resize(1000, 640)

        central = qt.QWidget()
        self.window.setCentralWidget(central)
        root = qt.QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        header = qt.QLabel(f"{APP_NAME} v{CORE_VERSION}　·　数据目录：{ctx.paths.root}")
        root.addWidget(header)

        body = qt.QHBoxLayout()
        root.addLayout(body, 1)

        # 左：插件列表
        left = qt.QVBoxLayout()
        left.addWidget(qt.QLabel("插件"))
        self.plugin_list = qt.QListWidget()
        self.plugin_list.currentItemChanged.connect(lambda *_: self.refresh_actions())
        left.addWidget(self.plugin_list, 1)

        button_row = qt.QHBoxLayout()
        for text, slot in (
            ("刷新", self.on_refresh),
            ("启动", self.on_start),
            ("停止", self.on_stop),
            ("卸载", self.on_unload),
        ):
            button = qt.QPushButton(text)
            button.clicked.connect(slot)
            button_row.addWidget(button)
        left.addLayout(button_row)

        left.addWidget(qt.QLabel("动作"))
        self.action_list = qt.QListWidget()
        left.addWidget(self.action_list, 1)
        invoke_button = qt.QPushButton("调用选中动作")
        invoke_button.clicked.connect(self.on_invoke)
        left.addWidget(invoke_button)

        body.addLayout(left, 2)

        # 右：日志
        right = qt.QVBoxLayout()
        right.addWidget(qt.QLabel("内核日志"))
        self.log_view = qt.QPlainTextEdit()
        self.log_view.setReadOnly(True)
        right.addWidget(self.log_view, 1)
        body.addLayout(right, 3)

        # 日志接入界面
        handler = QtLogHandler(self.log_view)
        logging.getLogger("xbc").addHandler(handler)
        self._handler = handler

        self.manager.discover()
        self.refresh_plugins()

    # ---------------- 刷新 ----------------
    def refresh_plugins(self) -> None:
        qt = _qtwidgets()
        previous = self._selected_id()

        # 重建列表期间屏蔽信号，避免逐个插入时反复触发 refresh_actions
        self.plugin_list.blockSignals(True)
        self.plugin_list.clear()
        for record in self.manager.records():
            item = qt.QListWidgetItem(f"{record.name}　[{record.state.value}]　{record.id}")
            item.setData(_qtcore().Qt.ItemDataRole.UserRole, record.id)
            if record.error:
                item.setToolTip(record.error)
            self.plugin_list.addItem(item)
        self.plugin_list.blockSignals(False)

        # 尽量保持原选中项；没有选中过就默认选第一个，
        # 否则右侧"动作"区在用户点击之前永远是空的。
        if self.plugin_list.count():
            target_row = 0
            for row in range(self.plugin_list.count()):
                if self.plugin_list.item(row).data(_qtcore().Qt.ItemDataRole.UserRole) == previous:
                    target_row = row
                    break
            self.plugin_list.setCurrentRow(target_row)

        self.refresh_actions()

    def refresh_actions(self) -> None:
        self.action_list.clear()
        record = self._selected()
        if record is None or record.instance is None:
            return
        for name in sorted(record.instance.actions()):
            self.action_list.addItem(name)

    def _selected_id(self) -> str | None:
        item = self.plugin_list.currentItem()
        if item is None:
            return None
        return item.data(_qtcore().Qt.ItemDataRole.UserRole)

    def _selected(self) -> Any:
        plugin_id = self._selected_id()
        if plugin_id is None:
            return None
        try:
            return self.manager.get(plugin_id)
        except Exception:  # noqa: BLE001
            return None

    # ---------------- 按钮 ----------------
    def on_refresh(self) -> None:
        self.manager.discover()
        self.refresh_plugins()

    def on_start(self) -> None:
        record = self._selected()
        if record is not None:
            self.manager.start(record.id)
            self.refresh_plugins()

    def on_stop(self) -> None:
        record = self._selected()
        if record is not None:
            self.manager.stop(record.id)
            self.refresh_plugins()

    def on_unload(self) -> None:
        record = self._selected()
        if record is not None:
            self.manager.unload(record.id)
            self.refresh_plugins()

    def on_invoke(self) -> None:
        qt = _qtwidgets()
        record = self._selected()
        item = self.action_list.currentItem()
        if record is None or item is None:
            return
        action = item.text()
        try:
            # 先加载再启动，保证动作可调用；参数无法从界面提供，故只支持无参动作
            self.manager.start(record.id)
            result = self.manager.invoke(record.id, action)
            qt.QMessageBox.information(self.window, "执行结果", f"{action}:\n{result}")
        except Exception as exc:  # noqa: BLE001 - 界面只负责把错误显示出来
            qt.QMessageBox.warning(self.window, "执行失败", f"{action} 失败:\n{exc}")
        self.refresh_plugins()


def run_shell(ctx: Any) -> int:
    """启动图形宿主。PySide6 在此处才被导入，内核因此保持与 Qt 无关。"""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    host = HostWindow(ctx)
    host.window.show()
    return int(app.exec())
