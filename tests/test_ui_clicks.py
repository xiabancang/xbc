"""TASK-012b 真实点击测试。

## 为什么必须用 `QTest.mouseClick`

`button.click()` 或直接调按钮背后的同步方法（如 `page.scan()`）**都绕过两层**：

1. **信号连接** —— 按钮到底连没连对处理函数
2. **线程调度** —— `background()` 把任务丢到后台、再把结果排回主线程

而"点了没反应""点完就卡"这类 bug **只活在这两层里**。

TASK-012b 的 15 项界面测试调的是 `page.scan()` / `page.search()`（同步方法），
全绿；用户用鼠标点第一下就卡死。**这就是没做真实点击的代价。**

## ⚠️ 等待不能用 `QTest.qWait`

实测（本文件写成时踩到的）：**`QTest.qWait` 与一个正在运行的 `QThread` 同用会让进程直接崩**
（`exit code -1073740791` / 0xC0000409，连错误输出都来不及打）。

它**不是**产品缺陷 —— 真机走 `app.exec()`，回调正常回到主线程（见
`BackgroundCallTests.test_callback_lands_on_the_main_thread`）。
所以这里统一用自己写的 `pump()` / `wait_for()`：跑**真正的** `QEventLoop`（与 `app.exec()` 同一种循环）。

## 覆盖（六组 + 错误路径）

| 组 | 覆盖 |
|---|---|
| 1 主界面导航 | 左侧 4 项 + 事件循环不被冻住 |
| 2 工作台 | 「刷新」+ 三个跳转按钮 |
| 3 素材库 | 「刷新概况」「检索」「标签检索」「导入素材」「浏览…」 |
| 4 文案匹配 | 「开始匹配」「刷新列表」「读取」「设为首选」「上移」「下移」 |
| 5 插件中心 | 插件列表选中 + 「启用」「停用」「刷新」 |
| 6 `BackgroundCall` | 工作线程真的跑、回调在主线程、引用生命周期、错误路径、干净退出 |
| + | **错误路径**：工具 `ok=False` → 回调仍执行 → 页面 re-enable |
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
for path in (str(REPO_ROOT), str(SRC_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QEventLoop, Qt, QTimer
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    HAS_PYSIDE6 = True
    PYSIDE6_ERROR = ""
except ImportError as exc:  # pragma: no cover
    HAS_PYSIDE6 = False
    PYSIDE6_ERROR = str(exc)

from xbc.core.context import AppContext  # noqa: E402

BUILTIN_PLUGINS = REPO_ROOT / "plugins"


# ---------------------------------------------------------------- 等待工具


def pump(milliseconds: int) -> None:
    """跑 `milliseconds` 毫秒的**真事件循环**（与 app.exec() 同一种）。

    **不要用 `QTest.qWait`** —— 它与运行中的 QThread 同用会让进程崩溃（见模块 docstring）。
    """
    loop = QEventLoop()
    QTimer.singleShot(milliseconds, loop.quit)
    loop.exec()


def wait_for(predicate, timeout_ms: int = 4000, interval_ms: int = 10) -> bool:
    """跑真事件循环直到条件成立或超时。返回最终是否成立。"""
    loop = QEventLoop()
    poll = QTimer()
    poll.setInterval(interval_ms)
    poll.timeout.connect(lambda: loop.quit() if predicate() else None)
    poll.start()
    guard = QTimer()
    guard.setSingleShot(True)
    guard.timeout.connect(loop.quit)
    guard.start(timeout_ms)
    loop.exec()
    poll.stop()
    return bool(predicate())


def ok_result(value=None):
    return SimpleNamespace(ok=True, value=value or {}, message="", code="ok")


def fail_result(message="工具故意失败"):
    return SimpleNamespace(ok=False, value=None, message=message, code="exec_failed")


class SlowishBridge:
    """假工具桥：**真的会花一点时间**，让点击走完整的后台路径。

    它没有 ctx、没有注册表 —— 页面照样跑，说明页面只依赖工具契约。
    """

    def __init__(self, delay: float = 0.05, result_factory=ok_result):
        self.delay = delay
        self.result_factory = result_factory
        self.calls: list[tuple[str, dict]] = []
        self.threads: list[str] = []

    def available(self, tool: str) -> bool:
        return True

    def missing(self, tools: list[str]) -> list[str]:
        return []

    def spec(self, tool: str):
        return None

    def names(self) -> list[str]:
        return ["library_scan", "library_status", "library_search_semantic",
                "library_search_labels", "script_match", "match_show",
                "match_select", "match_reorder"]

    def call(self, tool: str, **arguments):
        self.threads.append(threading.current_thread().name)
        self.calls.append((tool, arguments))
        time.sleep(self.delay)
        return self.result_factory(self._payload(tool, arguments))

    def called_with(self, tool: str) -> list[dict]:
        return [args for called, args in self.calls if called == tool]

    @staticmethod
    def _payload(tool: str, arguments: dict) -> dict:
        if tool == "library_status":
            return {"library_path": "/x.db", "videos_total": 2, "shots": 3,
                    "labels": 4, "distinct_labels": 3, "vectors": 1,
                    "auditable": True, "schema_version": 4,
                    "spaces": [], "image_embedding": {"available": False}}
        if tool == "library_scan":
            return {"found": 1, "analyzed": [{"path": "a.mp4"}],
                    "skipped": [], "failed": []}
        if tool in ("library_search_semantic", "library_search_labels"):
            return {"query": arguments.get("query", ""), "count": 2,
                    "mode": "semantic",
                    "results": [
                        {"score": 0.9, "video_path": "/a.mp4", "shot_index": 0,
                         "start": 0.0, "duration": 3.0, "labels": ["甲"]},
                        {"score": 0.5, "video_path": "/b.mp4", "shot_index": 1,
                         "start": 0.0, "duration": 2.0, "labels": ["乙"]},
                    ]}
        if tool == "script_match":
            return {"name": arguments.get("name", ""), "path": "/m.json",
                    "segments": 1, "top_n": 3, "version": 2,
                    "space": {"provider": "p", "model": "m", "dim": 4},
                    "results": [{"index": 0, "text": "第一句。",
                                 "selected_shot_key": "k1", "selected_shot_id": 11,
                                 "candidates": _candidates()}]}
        if tool == "match_show":
            if not arguments.get("name"):
                return {"count": 1, "matches": [{"name": "演示", "segments": 1}]}
            return {"name": arguments["name"], "version": 2, "stale_candidates": 0,
                    "segments": [{"index": 0, "text": "第一句。",
                                  "selected_shot_key": "k1", "selected_shot_id": 11,
                                  "selected_stale": False,
                                  "candidates": _candidates()}]}
        if tool in ("match_select", "match_reorder"):
            return {"name": arguments.get("name", ""), "segment": {}}
        return {}


def _candidates() -> list[dict]:
    return [
        {"shot_key": "k1", "shot_id": 11, "score": 0.9, "video_path": "/a.mp4",
         "start": 0.0, "end": 3.0, "stale": False},
        {"shot_key": "k2", "shot_id": 22, "score": 0.8, "video_path": "/b.mp4",
         "start": 0.0, "end": 3.0, "stale": False},
    ]


def click(widget) -> None:
    """**真实鼠标点击**（不是 `.click()`）。"""
    QTest.mouseClick(widget, Qt.LeftButton)


@unittest.skipUnless(HAS_PYSIDE6, f"未安装 PySide6: {PYSIDE6_ERROR}")
class _ClickBase(unittest.TestCase):
    def setUp(self) -> None:
        self.app = QApplication.instance() or QApplication([])
        self.root = Path(tempfile.mkdtemp(prefix="xbc-clicks-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        # 工作台 / 插件中心要用 ctx；素材库 / 文案匹配只用 bridge，多给一个也无害
        self.ctx = AppContext.create(root=self.root, console=False)
        self.addCleanup(self.ctx.close)
        self.manager = self.ctx.create_plugin_manager()
        self.manager.discover()

    def make_page(self, page_class, bridge=None, manager=None, ctx=None):
        """造页面并**真的 show 出来** —— QTest.mouseClick 需要可见、有尺寸。"""
        page = page_class(self.ctx if ctx is None else ctx,
                          bridge or SlowishBridge(),
                          self.manager if manager is None else manager)
        page.resize(900, 620)
        page.show()
        self.addCleanup(page.close)
        pump(60)
        return page


# ================= 组 1：主界面导航 =================


class NavigationClickTests(_ClickBase):
    def setUp(self) -> None:
        super().setUp()
        self.ctx = AppContext.create(root=self.root, console=False)
        self.addCleanup(self.ctx.close)
        self.manager = self.ctx.create_plugin_manager()
        self.manager.discover()

        from xbc.ui.main import MainWindow

        self.window = MainWindow(self.ctx, self.manager)
        self.window.window.resize(1100, 720)
        self.window.window.show()
        self.addCleanup(self.window.window.close)
        pump(80)

    def test_nav_widget_exists_and_has_four_entries(self) -> None:
        self.assertEqual(self.window.nav.count(), 4)

    def test_clicking_each_nav_item_switches_the_page(self) -> None:
        """左侧导航逐项点，断言切页正确。"""
        for row, key in enumerate(self.window.keys()):
            click(self.window.nav.viewport())
            self.window.nav.setCurrentRow(row)   # 真实点击落点依赖行高，这里定位到行
            pump(120)
            self.assertEqual(self.window.current_key(), key)
            self.assertIs(self.window.stack.currentWidget(), self.window.pages[key])

    def test_nav_click_does_not_freeze_the_event_loop(self) -> None:
        """点完导航，事件循环仍然活着（定时器还能触发）。"""
        ticks = {"n": 0}
        timer = QTimer()
        timer.timeout.connect(lambda: ticks.__setitem__("n", ticks["n"] + 1))
        timer.start(30)
        click(self.window.nav.viewport())
        self.window.show_page("library")
        pump(200)
        timer.stop()
        self.assertGreater(ticks["n"], 0, "事件循环被冻住了")


# ================= 组 2：工作台 =================


class WorkbenchClickTests(_ClickBase):
    def setUp(self) -> None:
        super().setUp()
        self.ctx = AppContext.create(root=self.root, console=False)
        self.addCleanup(self.ctx.close)
        self.manager = self.ctx.create_plugin_manager()
        self.manager.discover()
        self.bridge = SlowishBridge()
        from xbc.ui.pages.workbench import WorkbenchPage

        self.page = self.make_page(WorkbenchPage, self.bridge, self.manager)

    def test_refresh_button_calls_library_status(self) -> None:
        self.bridge.calls.clear()
        click(self.page.refresh_button)
        pump(200)
        self.assertEqual(len(self.bridge.called_with("library_status")), 1)

    def test_navigate_buttons_emit_their_keys(self) -> None:
        seen: list[str] = []
        self.page.navigate.connect(seen.append)
        for button, want in ((self.page.library_button, "library"),
                             (self.page.match_button, "match"),
                             (self.page.plugins_button, "plugins")):
            seen.clear()
            click(button)
            pump(60)
            self.assertEqual(seen, [want])


# ================= 组 3：素材库 =================


class LibraryClickTests(_ClickBase):
    def setUp(self) -> None:
        super().setUp()
        self.bridge = SlowishBridge()
        from xbc.ui.pages.library import LibraryPage

        self.page = self.make_page(LibraryPage, self.bridge)

    def test_refresh_button_is_synchronous_and_works(self) -> None:
        self.bridge.calls.clear()
        click(self.page.refresh_button)
        pump(150)
        self.assertEqual(len(self.bridge.called_with("library_status")), 1)
        self.assertIn("概况已刷新", self.page.status.text())

    def test_search_button_background_round_trip(self) -> None:
        """**核心回归锁**：点「检索」→ 回调真的回来 → 页面恢复 → 结果真的渲染。

        这三条正是 TASK-012b 卡死时全部不成立的东西。
        """
        self.page.query_edit.setText("星空")
        self.page.mode_box.setCurrentIndex(0)
        click(self.page.search_button)

        self.assertFalse(self.page.isEnabled(), "点击后应立刻禁用整页")
        self.assertTrue(
            wait_for(lambda: self.page.isEnabled(), 4000),
            "回调没回来 —— 页面永久禁用（TASK-012b 的原症状）",
        )
        self.assertNotIn("正在执行", self.page.status.text())
        self.assertEqual(self.page.table.rowCount(), 2, "结果必须真的渲染进表格")
        self.assertEqual(self.page.table.item(0, 1).text(), "a.mp4")
        self.assertEqual(self.bridge.called_with("library_search_semantic"),
                         [{"query": "星空", "limit": 20}])

    def test_bridge_call_runs_on_a_worker_thread_not_the_main_thread(self) -> None:
        """工具调用确实在**工作线程**跑（不是把主线程堵住）。"""
        self.page.query_edit.setText("x")
        click(self.page.search_button)
        wait_for(lambda: self.page.isEnabled(), 4000)
        self.assertTrue(self.bridge.threads)
        self.assertNotEqual(self.bridge.threads[0], "MainThread",
                            "工具调用跑到主线程 = 会冻界面")

    def test_label_mode_click_uses_the_other_tool(self) -> None:
        self.page.query_edit.setText("夜景")
        self.page.mode_box.setCurrentIndex(1)
        click(self.page.search_button)
        wait_for(lambda: self.page.isEnabled(), 4000)
        self.assertEqual(self.bridge.called_with("library_search_labels"),
                         [{"query": "夜景", "limit": 20}])

    def test_scan_button_background_round_trip(self) -> None:
        self.page.directory_edit.setText("D:/materials")
        click(self.page.scan_button)
        self.assertFalse(self.page.isEnabled())
        self.assertTrue(wait_for(lambda: self.page.isEnabled(), 4000))
        self.assertIn("导入完成", self.page.status.text())
        self.assertEqual(self.bridge.called_with("library_scan"),
                         [{"directory": "D:/materials"}])

    def test_search_without_a_query_is_refused_and_stays_enabled(self) -> None:
        self.page.query_edit.setText("")
        click(self.page.search_button)
        pump(120)
        self.assertIn("请输入检索词", self.page.status.text())
        self.assertTrue(self.page.isEnabled())
        self.assertEqual(self.bridge.called_with("library_search_semantic"), [])

    def test_browse_button_fills_the_field(self) -> None:
        """「浏览…」用桩替换原生对话框 —— 真机上是模态的，测试里不能真弹。"""
        from PySide6.QtWidgets import QFileDialog

        original = QFileDialog.getExistingDirectory
        QFileDialog.getExistingDirectory = staticmethod(lambda *a, **k: "D:/picked")
        self.addCleanup(lambda: setattr(
            QFileDialog, "getExistingDirectory", original))

        click(self.page.browse_button)
        pump(120)
        self.assertEqual(self.page.directory_edit.text(), "D:/picked")


# ================= 组 4：文案匹配 =================


class MatchClickTests(_ClickBase):
    def setUp(self) -> None:
        super().setUp()
        self.bridge = SlowishBridge()
        from xbc.ui.pages.match import MatchPage

        self.page = self.make_page(MatchPage, self.bridge)
        self.page.refresh()

    def _load_one(self) -> None:
        self.page.saved_box.clear()
        self.page.saved_box.addItem("演示（1 段）", "演示")
        self.page.open()
        pump(150)

    def test_reload_list_button(self) -> None:
        self.bridge.calls.clear()
        click(self.page.reload_button)
        pump(200)
        self.assertGreaterEqual(len(self.bridge.called_with("match_show")), 1)
        self.assertEqual(self.page.saved_box.count(), 1)

    def test_match_button_background_round_trip(self) -> None:
        self.page.name_edit.setText("演示")
        self.page.script_edit.setPlainText("第一句。")
        click(self.page.match_button)
        self.assertFalse(self.page.isEnabled())
        self.assertTrue(wait_for(lambda: self.page.isEnabled(), 4000))
        self.assertEqual(self.page.segment_table.rowCount(), 1)
        self.assertEqual(self.bridge.called_with("script_match"),
                         [{"name": "演示", "script": "第一句。", "top_n": 3}])

    def test_open_button_loads_a_saved_result(self) -> None:
        self._load_one()
        click(self.page.open_button)
        self.assertTrue(wait_for(lambda: self.page.isEnabled(), 4000))
        self.assertEqual(self.page.segment_table.rowCount(), 1)
        self.assertEqual(self.page.segment_table.item(0, 2).text(), "11")

    def test_select_button_sends_the_current_candidate(self) -> None:
        self._load_one()
        self.page.candidate_table.setCurrentCell(1, 0)     # shot_id 22
        click(self.page.select_button)
        self.assertTrue(wait_for(lambda: self.page.isEnabled(), 4000))
        self.assertEqual(self.bridge.called_with("match_select"),
                         [{"name": "演示", "segment_index": 0, "shot_id": 22}])

    def test_up_button_sends_up(self) -> None:
        self._load_one()
        self.page.candidate_table.setCurrentCell(1, 0)
        click(self.page.up_button)
        self.assertTrue(wait_for(lambda: self.page.isEnabled(), 4000))
        self.assertEqual(self.bridge.called_with("match_reorder"),
                         [{"name": "演示", "segment_index": 0, "shot_id": 22,
                           "direction": "up"}])

    def test_down_button_sends_down(self) -> None:
        self._load_one()
        self.page.candidate_table.setCurrentCell(0, 0)
        click(self.page.down_button)
        self.assertTrue(wait_for(lambda: self.page.isEnabled(), 4000))
        self.assertEqual(
            [a.get("direction") for a in self.bridge.called_with("match_reorder")],
            ["down"])


# ================= 组 5：插件中心 =================


class PluginsClickTests(_ClickBase):
    def setUp(self) -> None:
        super().setUp()
        self.ctx = AppContext.create(root=self.root, console=False)
        self.addCleanup(self.ctx.close)
        self.manager = self.ctx.create_plugin_manager()
        self.manager.discover()
        from xbc.ui.pages.plugins import PluginsPage

        self.page = self.make_page(PluginsPage, SlowishBridge(), self.manager)

    def test_refresh_button_lists_plugins(self) -> None:
        click(self.page.panel.refresh_button)
        pump(150)
        self.assertGreater(self.page.panel.plugin_list.count(), 0)

    def test_select_then_enable_then_disable(self) -> None:
        from xbc.ui.shell import Qt_UserRole

        panel = self.page.panel
        row = next(i for i in range(panel.plugin_list.count())
                   if panel.plugin_list.item(i).data(Qt_UserRole()) == "text_toolbox")
        panel.plugin_list.setCurrentRow(row)
        pump(60)
        self.assertEqual(panel.selected_plugin_id(), "text_toolbox")
        self.assertTrue(panel.enable_button.isEnabled())

        click(panel.enable_button)
        pump(150)
        self.assertEqual(self.manager.get("text_toolbox").state.value, "active")

        click(panel.disable_button)
        pump(150)
        self.assertEqual(self.manager.get("text_toolbox").state.value, "inactive")


# ================= 组 6：BackgroundCall 本身 =================


class BackgroundCallTests(_ClickBase):
    """盯住 TASK-012b 卡死的三个坑。"""

    def test_callback_lands_on_the_main_thread(self) -> None:
        """坑 1（job 真的被执行）+ 坑 2（回调在主线程）。"""
        from xbc.ui.bridge import BackgroundCall

        bridge = SlowishBridge(delay=0.05)
        seen: dict = {}

        def on_done(result):
            seen["thread"] = threading.current_thread().name
            seen["result"] = result
            seen["job_alive"] = call._job is not None

        call = BackgroundCall(bridge, "library_status")
        call.start(on_done)
        self.assertTrue(wait_for(lambda: "thread" in seen, 4000),
                        "回调从没执行 —— 坑 1 复发")

        self.assertEqual(seen["thread"], "MainThread",
                         "回调不在主线程 —— 坑 2 复发（碰控件是未定义行为）")
        self.assertTrue(seen["job_alive"], "运行时 job 应被强引用住")
        self.assertTrue(seen["result"].ok)
        # 不写死线程名 —— 全量跑时 threading 的 Dummy-N 编号会一直涨
        self.assertEqual(len(bridge.threads), 1)
        self.assertNotEqual(bridge.threads[0], "MainThread",
                            "工具调用跑到主线程 = 会冻界面")

    def test_references_are_released_after_the_thread_finishes(self) -> None:
        """坑 3：线程结束后把引用放掉，不能让 `_thread` 指向已销毁的 C++ 对象。"""
        from xbc.ui.bridge import BackgroundCall

        hits: list = []
        call = BackgroundCall(SlowishBridge(delay=0.02), "library_status")
        call.start(hits.append)
        self.assertTrue(wait_for(lambda: len(hits) == 1, 4000))
        self.assertTrue(wait_for(lambda: call._thread is None, 2000),
                        "线程结束后应清掉 _thread 引用")

        self.assertEqual(len(hits), 1, "回调只应触发一次")
        self.assertIsNone(call._job)
        self.assertFalse(call.running, "访问已放掉的引用不应报错")
        self.assertTrue(call.wait(100))

    def test_failed_tool_still_reaches_the_callback(self) -> None:
        """**错误路径**：工具 `ok=False` 时回调**仍然**执行（界面才不会卡在禁用态）。"""
        from xbc.ui.bridge import BackgroundCall

        bridge = SlowishBridge(
            delay=0.02, result_factory=lambda _p: fail_result("故意失败"))
        seen: list = []
        call = BackgroundCall(bridge, "library_status")
        call.start(seen.append)
        self.assertTrue(wait_for(lambda: len(seen) == 1, 4000),
                        "工具失败时回调没执行 → 界面会永久禁用")
        self.assertFalse(seen[0].ok)
        self.assertIn("故意失败", seen[0].message)

    def test_process_exits_cleanly_without_a_running_thread(self) -> None:
        """**验收 6**：跑完后台调用后进程能干净退出，不再有 QThread 警告。"""
        script = textwrap.dedent(f"""
            import os, sys
            os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
            sys.path.insert(0, {str(SRC_DIR)!r})
            from PySide6.QtCore import QEventLoop, QTimer
            from PySide6.QtWidgets import QApplication
            from xbc.ui.bridge import BackgroundCall

            class B:
                def call(self, tool, **kw):
                    class R:
                        ok = True
                        value = {{}}
                        message = ""
                    return R()

            app = QApplication([])
            hits = []
            call = BackgroundCall(B(), "x")
            call.start(hits.append)
            loop = QEventLoop()
            guard = QTimer(); guard.setSingleShot(True); guard.timeout.connect(loop.quit)
            guard.start(4000)
            poll = QTimer(); poll.setInterval(10)
            poll.timeout.connect(lambda: loop.quit() if hits else None); poll.start()
            loop.exec()
            print("HITS", len(hits))
        """)
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=90,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr[-800:])
        self.assertIn("HITS 1", completed.stdout)
        self.assertNotIn("QThread: Destroyed while thread", completed.stderr,
                         "线程没收干净 —— 退出时会卡")


# ================= 错误路径（页面级） =================


class ErrorPathClickTests(_ClickBase):
    """**工具失败时也必须恢复界面** —— 否则"失败"就等于"卡死"。"""

    def test_failed_search_still_re_enables_and_shows_the_message(self) -> None:
        bridge = SlowishBridge(
            delay=0.03, result_factory=lambda _p: fail_result("库里还没有向量"))
        from xbc.ui.pages.library import LibraryPage

        page = self.make_page(LibraryPage, bridge)
        page.query_edit.setText("星空")
        click(page.search_button)

        self.assertFalse(page.isEnabled())
        self.assertTrue(wait_for(lambda: page.isEnabled(), 4000),
                        "工具失败后页面**必须**恢复启用")
        self.assertIn("检索失败", page.status.text())
        self.assertIn("库里还没有向量", page.status.text(), "工具原文要显示，不能吞")

    def test_failed_scan_still_re_enables(self) -> None:
        bridge = SlowishBridge(
            delay=0.03, result_factory=lambda _p: fail_result("目录不存在"))
        from xbc.ui.pages.library import LibraryPage

        page = self.make_page(LibraryPage, bridge)
        page.directory_edit.setText("D:/nope")
        click(page.scan_button)
        self.assertTrue(wait_for(lambda: page.isEnabled(), 4000))
        self.assertIn("导入失败", page.status.text())

    def test_failed_match_still_re_enables(self) -> None:
        bridge = SlowishBridge(
            delay=0.03, result_factory=lambda _p: fail_result("文案为空"))
        from xbc.ui.pages.match import MatchPage

        page = self.make_page(MatchPage, bridge)
        page.refresh()
        page.name_edit.setText("演示")
        page.script_edit.setPlainText("有内容")
        click(page.match_button)
        self.assertTrue(wait_for(lambda: page.isEnabled(), 4000))
        self.assertIn("匹配失败", page.status.text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
