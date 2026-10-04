"""插件管理面板 + 独立窗口。

TASK-004 交付的是**独立窗口**（`HostWindow`）；TASK-012b 的主界面需要把同一套
插件管理**嵌进页面**，所以在这里拆成两层：

- `PluginManagerPanel(QWidget)` —— 真正的内容。可以在独立窗口里，也可以在页面里
- `HostWindow` —— 独立窗口，`centralWidget` 就是那个面板；
  公开属性/方法**全部转发**给面板，所以 TASK-004 的界面 API 一字未变

## 范围（TASK-004 定下的，没变）

| 必须实现 | 禁止 |
|---|---|
| PySide6 窗口 | 登录系统 |
| 显示插件列表 / 状态 | 云端 |
| 启用 / 停用插件 | 商城 |
| 查看 Tool / Skill 列表 | 支付 |
| | **UI 美化** |

**"不做 UI 美化"是硬约束**：不写任何 `setStyleSheet`、不设字体、不设图标、
不用自绘控件。全部使用 Qt 默认外观。

## 一致性保证

界面**不缓存任何状态**，也不自己判断"能不能启用"。每次刷新都重新从 Runtime 读取：

- 插件列表 ← `PluginManager.records()`
- 工具列表 ← `ctx.tool_registry.for_agent()`
- 技能列表 ← `ctx.skill_catalog.specs()`

启用/停用直接调用 `PluginManager.enable()` / `disable()` —— 与 CLI **同一个方法**。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core.contract.plugin import PluginState
from ..version import APP_NAME, CORE_VERSION

#: 插件在列表里的显示顺序（按状态，便于一眼看出问题）
_STATE_ORDER = {
    PluginState.ACTIVE: 0,
    PluginState.LOADED: 1,
    PluginState.DISCOVERED: 2,
    PluginState.INACTIVE: 3,
    PluginState.FAILED: 4,
    PluginState.UNLOADED: 5,
}


def Qt_UserRole() -> int:
    """插件 id 在列表项里存放的 role（用整数，便于测试断言）。"""
    return int(Qt.ItemDataRole.UserRole)


class PluginManagerPanel(QWidget):
    """插件管理面板。纯 Qt 控件，不含任何业务判断。"""

    def __init__(self, ctx: Any, manager: Any = None) -> None:
        super().__init__()
        self.ctx = ctx
        self.manager = manager if manager is not None else ctx.create_plugin_manager()
        self.manager.discover()

        root = QVBoxLayout(self)

        # ---- 顶部：标题 + 数据目录 ----
        root.addWidget(QLabel(f"{APP_NAME} v{CORE_VERSION}　数据目录：{ctx.paths.root}"))

        body = QHBoxLayout()
        root.addLayout(body, 1)

        # ---- 左：插件列表 + 操作 ----
        left = QVBoxLayout()
        left.addWidget(QLabel("插件"))
        self.plugin_list = QListWidget()
        self.plugin_list.currentItemChanged.connect(self._on_selection_changed)
        left.addWidget(self.plugin_list, 1)

        buttons = QHBoxLayout()
        self.refresh_button = QPushButton("刷新")
        self.enable_button = QPushButton("启用")
        self.disable_button = QPushButton("停用")
        self.refresh_button.clicked.connect(self.on_refresh)
        self.enable_button.clicked.connect(self.on_enable)
        self.disable_button.clicked.connect(self.on_disable)
        for button in (self.refresh_button, self.enable_button, self.disable_button):
            buttons.addWidget(button)
        left.addLayout(buttons)

        self.plugin_detail = QLabel("")
        self.plugin_detail.setWordWrap(True)
        left.addWidget(self.plugin_detail)

        body.addLayout(left, 1)

        # ---- 右：工具列表 + 技能列表 ----
        right = QVBoxLayout()

        self.tools_label = QLabel("Tool（Agent 可调用）")
        right.addWidget(self.tools_label)
        self.tool_list = QListWidget()
        right.addWidget(self.tool_list, 1)

        self.skills_label = QLabel("Skill（按需加载的指令）")
        right.addWidget(self.skills_label)
        self.skill_list = QListWidget()
        right.addWidget(self.skill_list, 1)

        body.addLayout(right, 1)

        # ---- 底部：操作结果 ----
        self.message = QPlainTextEdit()
        self.message.setReadOnly(True)
        self.message.setFixedHeight(90)
        root.addWidget(self.message)

        self.refresh()
        self._log("就绪。启用/停用会直接调用 Runtime，与命令行行为一致。")

    # ---------------- 从 Runtime 读取（不缓存） ----------------
    def refresh(self) -> None:
        """重新从 Runtime 读取插件 / 工具 / 技能，并重绘三个列表。"""
        self.refresh_plugins()
        self.refresh_tools()
        self.refresh_skills()

    def refresh_plugins(self) -> None:
        records = sorted(
            self.manager.records(),
            key=lambda r: (_STATE_ORDER.get(r.state, 99), r.id),
        )
        self.plugin_list.clear()
        for record in records:
            self.plugin_list.addItem(self._plugin_item(record))
        self._on_selection_changed()

    def _plugin_item(self, record: Any) -> QListWidgetItem:
        flags = "启用" if record.enabled else "已禁用"
        text = f"{record.name}（{record.id}）　{record.state.value}　{flags}"
        if record.error:
            text += f"　⚠ {record.error}"
        elif record.blocked_reason:
            text += f"　· {record.blocked_reason}"
        item = QListWidgetItem(text)
        item.setData(Qt_UserRole(), record.id)
        return item

    def refresh_tools(self) -> None:
        specs = self.ctx.tool_registry.for_agent()
        self.tool_list.clear()
        for spec in specs:
            risk = spec.get("annotations", {}).get("risk", "?")
            owner = spec.get("annotations", {}).get("owner", "?")
            description = (spec.get("description") or "").strip()
            self.tool_list.addItem(f"{spec['name']}　[{risk}]　{owner}　{description}")
        self.tools_label.setText(f"Tool（Agent 可调用）　共 {len(specs)} 个")

    def refresh_skills(self) -> None:
        specs = self.ctx.skill_catalog.specs()
        self.skill_list.clear()
        for spec in specs:
            description = (spec.description or "").strip()
            self.skill_list.addItem(
                f"{spec.name}　[model={spec.model_invocable} user={spec.user_invocable}]　{description}"
            )
        self.skills_label.setText(f"Skill（按需加载的指令）　共 {len(specs)} 个")

    # ---------------- 选中与详情 ----------------
    def selected_plugin_id(self) -> str | None:
        item = self.plugin_list.currentItem()
        return item.data(Qt_UserRole()) if item is not None else None

    def select_plugin(self, plugin_id: str) -> bool:
        for row in range(self.plugin_list.count()):
            if self.plugin_list.item(row).data(Qt_UserRole()) == plugin_id:
                self.plugin_list.setCurrentRow(row)
                return True
        return False

    def _on_selection_changed(self, *_: Any) -> None:
        plugin_id = self.selected_plugin_id()
        has_selection = plugin_id is not None
        # 按钮可用性只反映"有没有选中"，不预测能否启用（那由 Runtime 决定）
        self.enable_button.setEnabled(has_selection)
        self.disable_button.setEnabled(has_selection)
        if not has_selection:
            self.plugin_detail.setText("")
            return

        record = next(
            (r for r in self.manager.records() if r.id == plugin_id),
            None,
        )
        if record is None:
            self.plugin_detail.setText("")
            return
        manifest = record.manifest
        lines = [
            f"版本 {manifest.version}　API {manifest.api_version}　清单规范 {manifest.spec_version}",
            f"能力：{', '.join(manifest.capabilities) or '（无）'}",
            f"配置来源：{record.config_source}　配置：{self.ctx.effective_config_for(manifest)}",
        ]
        if record.error:
            lines.append(f"错误：{record.error}")
        self.plugin_detail.setText("\n".join(lines))

    # ---------------- 操作（与 CLI 同一方法） ----------------
    def on_refresh(self) -> None:
        self.manager.discover()
        self.refresh()
        self._log("已刷新（重新扫描插件目录并从 Runtime 读取状态）")

    def on_enable(self) -> None:
        plugin_id = self.selected_plugin_id()
        if plugin_id:
            self.enable(plugin_id)

    def on_disable(self) -> None:
        plugin_id = self.selected_plugin_id()
        if plugin_id:
            self.disable(plugin_id)

    def enable(self, plugin_id: str) -> Any:
        """启用插件。与 CLI 的 `plugin enable` 走**完全相同**的代码路径。"""
        record = self.manager.enable(plugin_id)
        self._after_operation("启用", record)
        return record

    def disable(self, plugin_id: str) -> Any:
        """停用插件。与 CLI 的 `plugin disable` 走**完全相同**的代码路径。"""
        record = self.manager.disable(plugin_id)
        self._after_operation("停用", record)
        return record

    def _after_operation(self, action: str, record: Any) -> None:
        # 操作后**重新从 Runtime 读取**，而不是自己改界面
        self.refresh()
        self.select_plugin(record.id)
        detail = f"{action} {record.id} → 状态 {record.state.value}"
        if record.error:
            detail += f"　错误：{record.error}"
        elif record.blocked_reason:
            detail += f"　{record.blocked_reason}"
        self._log(detail)

    # ---------------- 日志 ----------------
    def _log(self, text: str) -> None:
        self.message.appendPlainText(text)


class HostWindow:
    """插件管理的**独立窗口**（TASK-004 的入口，行为与 API 保持不变）。

    内容全在 `PluginManagerPanel` 里；这里只做窗口装壳 + 属性转发。
    新界面请直接用 `PluginManagerPanel` 或 `ui.main.MainWindow`。
    """

    def __init__(self, ctx: Any, manager: Any = None) -> None:
        self.ctx = ctx
        self.manager = manager if manager is not None else ctx.create_plugin_manager()

        self.panel = PluginManagerPanel(ctx, self.manager)

        self.window = QMainWindow()
        self.window.setWindowTitle(f"{APP_NAME} v{CORE_VERSION} — 插件管理")
        self.window.resize(960, 600)
        self.window.setCentralWidget(self.panel)

    def __getattr__(self, name: str) -> Any:
        """把面板的公开属性/方法转发出去 —— TASK-004 的界面 API 因此一字未变。"""
        panel = self.__dict__.get("panel")
        if panel is not None and hasattr(panel, name):
            return getattr(panel, name)
        raise AttributeError(name)


def run_shell(ctx: Any) -> int:
    """启动桌面管理入口（TASK-004 的独立窗口）。"""
    app = QApplication.instance() or QApplication([])
    host = HostWindow(ctx)
    host.window.show()
    return int(app.exec())
