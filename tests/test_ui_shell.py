"""图形宿主的最小集成测试。

PySide6 没安装时整体跳过：
内核测试不依赖界面，界面测试也不该拖累内核测试。

用 QT_QPA_PLATFORM=offscreen 在无显示环境下运行，不会真的弹出窗口。
"""

from __future__ import annotations

import logging
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

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication

    HAS_PYSIDE6 = True
    PYSIDE6_ERROR = ""
except ImportError as exc:  # pragma: no cover - 取决于运行环境
    HAS_PYSIDE6 = False
    PYSIDE6_ERROR = str(exc)

from xbc.core.context import AppContext  # noqa: E402


@unittest.skipUnless(HAS_PYSIDE6, f"未安装 PySide6: {PYSIDE6_ERROR}")
class ShellTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="xbc-ui-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.ctx = AppContext.create(root=self.root, console=False)
        self.addCleanup(self.ctx.close)
        self.app = QApplication.instance() or QApplication([])

    def test_host_renders_plugins_without_knowing_them(self) -> None:
        """宿主必须能只靠内核数据渲染插件与动作，不含任何插件专属代码。"""
        from xbc.ui.shell import HostWindow

        host = HostWindow(self.ctx)
        self.addCleanup(host.window.close)
        self.addCleanup(logging.getLogger("xbc").removeHandler, host._handler)

        labels = [
            host.plugin_list.item(i).text() for i in range(host.plugin_list.count())
        ]
        self.assertTrue(
            any("hello_xbc" in text for text in labels), f"插件列表里没有 hello_xbc: {labels}"
        )

        host.manager.activate("hello_xbc")
        host.refresh_plugins()

        actions = [
            host.action_list.item(i).text() for i in range(host.action_list.count())
        ]
        self.assertIn("hello_probe", actions)
        self.assertIn("hello_greet", actions)


if __name__ == "__main__":
    unittest.main(verbosity=2)
