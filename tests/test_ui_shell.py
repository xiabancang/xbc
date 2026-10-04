"""桌面管理入口（Desktop Shell MVP）的测试。

TASK-004 的验收标准有两条：

1. **CLI 已有功能不能退化** —— 由 `test_plugin_runtime.py` 的 69 项用例保证，
   本文件不重复覆盖。
2. **桌面操作结果必须与 Runtime 状态一致** —— 本文件的核心。

第 2 条的测法很关键：**不硬编码界面应该显示什么**，而是拿 Runtime 的真实状态
去比对界面内容。这样测试证明的是"界面 == 运行时"，而不是"界面 == 我写死的期望"。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

# 让 Qt 在无显示环境下运行，不真的弹窗
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    HAS_PYSIDE6 = True
    PYSIDE6_ERROR = ""
except ImportError as exc:  # pragma: no cover - 取决于运行环境
    HAS_PYSIDE6 = False
    PYSIDE6_ERROR = str(exc)

from xbc.core.context import AppContext  # noqa: E402
from xbc.core.contract.plugin import PluginState  # noqa: E402
from xbc.core.runtime.manager import PluginManager  # noqa: E402

BUILTIN_PLUGINS = REPO_ROOT / "plugins"
_USER_ROLE = int(Qt.ItemDataRole.UserRole) if HAS_PYSIDE6 else 0


@unittest.skipUnless(HAS_PYSIDE6, f"未安装 PySide6: {PYSIDE6_ERROR}")
class ShellTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="xbc-shell-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.ctx = AppContext.create(root=self.root, console=False)
        self.addCleanup(self.ctx.close)
        self.app = QApplication.instance() or QApplication([])

        from xbc.ui.shell import HostWindow

        self.host = HostWindow(self.ctx)
        self.addCleanup(self.host.window.close)

    # ---------- 辅助：把界面内容读回来 ----------
    def plugin_rows(self) -> list[tuple[str, str]]:
        """返回界面上插件列表的 (plugin_id, 显示文本)。"""
        widget = self.host.plugin_list
        return [
            (widget.item(i).data(_USER_ROLE), widget.item(i).text())
            for i in range(widget.count())
        ]

    def tool_rows(self) -> list[str]:
        widget = self.host.tool_list
        return [widget.item(i).text() for i in range(widget.count())]

    def skill_rows(self) -> list[str]:
        widget = self.host.skill_list
        return [widget.item(i).text() for i in range(widget.count())]

    # ---------- 1/2. 插件列表与状态 ----------
    def test_window_lists_every_plugin_with_its_state(self) -> None:
        """界面上出现的插件与 Runtime 完全一致，且显示了真实状态。"""
        expected = {r.id: r for r in self.host.manager.records()}
        rows = dict(self.plugin_rows())

        self.assertEqual(set(rows), set(expected), "界面插件集合与 Runtime 不一致")
        for plugin_id, record in expected.items():
            self.assertIn(record.state.value, rows[plugin_id], f"{plugin_id} 未显示状态")

    def test_plugin_state_shown_is_the_runtime_state(self) -> None:
        """激活后界面必须显示 active —— 状态来自 Runtime，不是界面猜的。"""
        self.host.enable("text_toolbox")
        self.assertEqual(self.host.manager.get("text_toolbox").state, PluginState.ACTIVE)
        self.assertIn("active", dict(self.plugin_rows())["text_toolbox"])

    # ---------- 6/7. Tool 与 Skill 列表 ----------
    def test_tool_list_matches_runtime_registry(self) -> None:
        self.host.enable("text_toolbox")
        self.host.enable("hello_xbc")
        self.host.refresh()

        expected = {spec["name"] for spec in self.ctx.tool_registry.for_agent()}
        self.assertTrue(expected, "前置条件：应有已注册的工具")
        shown = self.tool_rows()
        self.assertEqual(len(shown), len(expected))
        for name in expected:
            self.assertTrue(
                any(row.startswith(name) for row in shown), f"工具 {name} 未出现在界面"
            )

    def test_skill_list_matches_runtime_catalog(self) -> None:
        self.host.enable("text_toolbox")
        self.host.refresh()

        expected = {spec.name for spec in self.ctx.skill_catalog.specs()}
        self.assertTrue(expected, "前置条件：应有已注册的技能")
        shown = self.skill_rows()
        self.assertEqual(len(shown), len(expected))
        for name in expected:
            self.assertTrue(any(row.startswith(name) for row in shown), f"技能 {name} 未出现")

    def test_lists_are_empty_when_nothing_is_active(self) -> None:
        """未激活时不应显示任何工具/技能（不能拿清单声明冒充已注册）。"""
        self.host.refresh()
        self.assertEqual(self.tool_rows(), [])
        self.assertEqual(self.skill_rows(), [])

    # ---------- 4/5. 启用与停用 ----------
    def test_enable_via_desktop_matches_runtime(self) -> None:
        """桌面"启用"的效果与 Runtime 状态一致，并且工具立即可见。"""
        self.host.enable("text_toolbox")

        record = self.host.manager.get("text_toolbox")
        self.assertEqual(record.state, PluginState.ACTIVE)
        self.assertTrue(record.enabled)
        self.assertIn("active", dict(self.plugin_rows())["text_toolbox"])
        self.assertTrue(self.tool_rows(), "启用后工具列表应立刻出现内容")
        self.assertTrue(self.skill_rows(), "启用后技能列表应立刻出现内容")

    def test_disable_via_desktop_matches_runtime(self) -> None:
        """桌面"停用"后：Runtime 状态、界面状态、工具/技能列表**同时**变化。"""
        self.host.enable("text_toolbox")
        self.assertTrue(self.tool_rows())

        self.host.disable("text_toolbox")

        record = self.host.manager.get("text_toolbox")
        self.assertFalse(record.enabled)
        self.assertEqual(record.state, PluginState.INACTIVE)
        self.assertIn("已禁用", dict(self.plugin_rows())["text_toolbox"])
        self.assertEqual(self.tool_rows(), [], "停用后工具列表必须清空（注册即资源）")
        self.assertEqual(self.skill_rows(), [], "停用后技能列表必须清空")

    def test_disable_only_affects_the_target_plugin(self) -> None:
        self.host.enable("hello_xbc")
        self.host.enable("text_toolbox")
        before = len(self.tool_rows())

        self.host.disable("text_toolbox")

        self.assertEqual(self.host.manager.get("hello_xbc").state, PluginState.ACTIVE)
        self.assertLess(len(self.tool_rows()), before)
        self.assertTrue(self.tool_rows(), "另一个插件的工具不应消失")

    def test_disabled_state_persists_like_the_cli(self) -> None:
        """桌面停用与 CLI 一样写入用户层配置，重启后仍生效。"""
        self.host.disable("text_toolbox")

        # 模拟重启：同一 root 上重新装配一个 Runtime
        ctx2 = AppContext.create(root=self.root, console=False)
        self.addCleanup(ctx2.close)
        manager2 = PluginManager(ctx2, [BUILTIN_PLUGINS], ctx2.logger)
        manager2.discover()

        self.assertFalse(manager2.get("text_toolbox").enabled)

    def test_desktop_and_cli_observe_the_same_state(self) -> None:
        """桌面启用后，另一个 Runtime 实例（即 CLI 的行为）观察到相同状态与工具集。

        注意：这里必须在桌面侧把两个插件都启用，才能与 `activate_all()` 对齐 ——
        差异只能来自"激活了哪些插件"，不能来自"对同一插件看法不同"。
        """
        self.host.enable("text_toolbox")
        self.host.enable("hello_xbc")

        ctx2 = AppContext.create(root=self.root, console=False)
        self.addCleanup(ctx2.close)
        manager2 = PluginManager(ctx2, [BUILTIN_PLUGINS], ctx2.logger)
        manager2.discover()
        manager2.activate_all()

        for plugin_id in ("text_toolbox", "hello_xbc"):
            self.assertEqual(
                manager2.get(plugin_id).state,
                self.host.manager.get(plugin_id).state,
                f"{plugin_id} 在两个 Runtime 实例中状态不一致",
            )
        self.assertEqual(
            {spec["name"] for spec in ctx2.tool_registry.for_agent()},
            {spec["name"] for spec in self.ctx.tool_registry.for_agent()},
        )

    def test_enable_unknown_plugin_raises_and_does_not_break_window(self) -> None:
        with self.assertRaises(Exception):
            self.host.enable("does_not_exist")
        # 界面仍可用
        self.host.refresh()
        self.assertTrue(self.plugin_rows())

    # ---------- 选中与按钮状态 ----------
    def test_selection_drives_buttons_and_detail(self) -> None:
        self.host.select_plugin("hello_xbc")
        self.assertTrue(self.host.enable_button.isEnabled())
        self.assertTrue(self.host.disable_button.isEnabled())
        # 详情来自清单（零成本：不需要加载插件代码）
        self.assertIn("能力", self.host.plugin_detail.text())
        self.assertIn("hello_xbc", dict(self.plugin_rows())["hello_xbc"])

    def test_failed_plugin_error_is_visible_in_list(self) -> None:
        """失败原因必须能在界面上看到，而不是被吞掉。"""
        fixture = self.root / "fixtures"
        broken = fixture / "broken_demo"
        broken.mkdir(parents=True)
        (broken / "plugin.json").write_text(
            '{"id":"broken_demo","name":"坏插件","version":"0.1.0",'
            '"spec_version":"1.0","api_version":"1.0","entry":"plugin.py:Demo"}',
            encoding="utf-8",
        )
        (broken / "plugin.py").write_text(
            "from xbc.core.contract.plugin import XbcPlugin\n"
            "class Demo(XbcPlugin):\n"
            "    def apply(self, ctx, config):\n"
            "        raise RuntimeError('apply 故意失败')\n",
            encoding="utf-8",
        )
        manager = PluginManager(self.ctx, [BUILTIN_PLUGINS, fixture], self.ctx.logger)
        manager.discover()
        from xbc.ui.shell import HostWindow

        host = HostWindow(self.ctx, manager=manager)
        self.addCleanup(host.window.close)
        host.enable("broken_demo")

        self.assertEqual(manager.get("broken_demo").state, PluginState.FAILED)
        rows = dict(
            (host.plugin_list.item(i).data(_USER_ROLE), host.plugin_list.item(i).text())
            for i in range(host.plugin_list.count())
        )
        self.assertIn("failed", rows["broken_demo"])
        self.assertIn("故意失败", rows["broken_demo"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
