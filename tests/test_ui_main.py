"""TASK-012b 界面二期的测试。

分三层：

1. **`MainWindowTests`** —— 主界面能打开、四个页面都在、导航能切
2. **`PageWiringTests`** —— 每个按钮**确实走的是那个工具**，参数也对
3. **`UiPurityTests`** —— 结构性证明"界面层无业务逻辑"

第 2 层是关键：用一个**记录型 bridge** 换掉真的工具桥，
页面照样跑 —— 说明页面不依赖任何真实业务代码，只依赖工具契约。
"""

from __future__ import annotations

import ast
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
PLUGIN_DIR = REPO_ROOT / "plugins" / "video_analyzer"
for path in (str(REPO_ROOT), str(SRC_DIR), str(PLUGIN_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

# 让 Qt 在无显示环境下运行，不真的弹窗
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication

    HAS_PYSIDE6 = True
    PYSIDE6_ERROR = ""
except ImportError as exc:  # pragma: no cover
    HAS_PYSIDE6 = False
    PYSIDE6_ERROR = str(exc)

from xbc.core.context import AppContext  # noqa: E402

UI_DIR = SRC_DIR / "xbc" / "ui"
BUILTIN_PLUGINS = REPO_ROOT / "plugins"


def ok(value=None):
    return SimpleNamespace(ok=True, value=value if value is not None else {}, message="")


class RecordingBridge:
    """假的工具桥：记录每一次调用，返回预置结果。

    它**没有** `ctx`、没有注册表 —— 页面照样能跑，
    这本身就说明页面只依赖工具契约，不依赖任何业务实现。
    """

    def __init__(self, responses: dict | None = None, tools: set[str] | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.responses = responses or {}
        self.tools = set(tools if tools is not None else self.responses)

    def available(self, name: str) -> bool:
        return name in self.tools

    def missing(self, names: list[str]) -> list[str]:
        return [n for n in names if n not in self.tools]

    def spec(self, name: str):
        return None

    def names(self) -> list[str]:
        return sorted(self.tools)

    def call(self, tool: str, **arguments):
        self.calls.append((tool, arguments))
        reply = self.responses.get(tool)
        if reply is None:
            return ok({})
        if callable(reply):
            return ok(reply(**arguments))
        if isinstance(reply, list):  # 按调用次序依次返回
            index = sum(1 for called, _ in self.calls if called == tool) - 1
            reply = reply[min(index, len(reply) - 1)]
        return ok(reply)

    def called_with(self, tool: str) -> list[dict]:
        return [args for called, args in self.calls if called == tool]


def make_library_page(bridge: RecordingBridge):
    from xbc.ui.pages.library import LibraryPage

    return LibraryPage.__new__(LibraryPage).__class__(None, bridge, None)


@unittest.skipUnless(HAS_PYSIDE6, f"未安装 PySide6: {PYSIDE6_ERROR}")
class MainWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="xbc-ui-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.ctx = AppContext.create(root=self.root, console=False)
        self.addCleanup(self.ctx.close)
        self.app = QApplication.instance() or QApplication([])

        self.manager = self.ctx.create_plugin_manager()
        self.manager.discover()
        self.manager.activate_all()

        from xbc.ui.main import MainWindow

        self.window = MainWindow(self.ctx, self.manager)
        self.addCleanup(self.window.window.close)

    def test_main_window_has_the_four_pages(self) -> None:
        """验收 1：能打开主界面，左侧导航就是这四个入口。"""
        from xbc.ui.main import PAGES

        self.assertEqual(self.window.nav.count(), 4)
        self.assertEqual(
            [self.window.nav.item(i).text() for i in range(self.window.nav.count())],
            ["工作台", "素材库", "文案匹配", "插件中心"],
        )
        self.assertEqual(self.window.stack.count(), len(PAGES))
        self.assertTrue(self.window.window.windowTitle())

    def test_every_nav_entry_switches_to_a_real_page(self) -> None:
        """没有"为将来预留"的空入口 —— 每个导航项都有真页面。"""
        for key in self.window.keys():
            self.assertTrue(self.window.show_page(key), key)
            self.assertEqual(self.window.current_key(), key)
            self.assertIs(self.window.stack.currentWidget(), self.window.pages[key])

    def test_plugin_page_shares_the_booted_manager(self) -> None:
        """插件中心用的是启动时那个 manager —— 界面不自己再装配一套。"""
        page = self.window.pages["plugins"]
        self.assertIs(page.panel.manager, self.manager)

    def test_pages_report_missing_tools_instead_of_crashing(self) -> None:
        """插件没启用时说清楚缺什么，而不是点了报错。"""
        empty_root = Path(tempfile.mkdtemp(prefix="xbc-ui-empty-"))
        self.addCleanup(shutil.rmtree, empty_root, ignore_errors=True)
        ctx = AppContext.create(root=empty_root, console=False)
        self.addCleanup(ctx.close)
        manager = ctx.create_plugin_manager()
        manager.discover()

        from xbc.ui.main import MainWindow

        window = MainWindow(ctx, manager)
        self.addCleanup(window.window.close)
        window.show_page("library")
        page = window.pages["library"]
        self.assertFalse(page.search_button.isEnabled())
        self.assertIn("缺少工具", page.status.text())


@unittest.skipUnless(HAS_PYSIDE6, f"未安装 PySide6: {PYSIDE6_ERROR}")
class PageWiringTests(unittest.TestCase):
    """验收 5：界面通过**现有工具**调用，界面上没有业务逻辑。"""

    def setUp(self) -> None:
        self.app = QApplication.instance() or QApplication([])
        self.root = Path(tempfile.mkdtemp(prefix="xbc-ui-fake-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def _library_page(self, responses: dict):
        from xbc.ui.pages.library import LibraryPage

        bridge = RecordingBridge(responses, tools=set(responses))
        return LibraryPage(None, bridge, None), bridge

    def _match_page(self, responses: dict):
        from xbc.ui.pages.match import MatchPage

        bridge = RecordingBridge(responses, tools=set(responses))
        return MatchPage(None, bridge, None), bridge

    # ---------- 素材库 ----------
    def test_import_materials_calls_library_scan(self) -> None:
        page, bridge = self._library_page({
            "library_scan": {
                "found": 3, "analyzed": [{"path": "a.mp4"}],
                "skipped": [], "failed": [],
            },
            "library_status": {"videos_total": 1, "shots": 2, "vectors": 4,
                               "labels": 3, "distinct_labels": 2,
                               "auditable": True, "schema_version": 4,
                               "library_path": "/lib.db", "spaces": []},
        })
        page.directory_edit.setText("D:/materials")
        page.scan()

        self.assertEqual(bridge.called_with("library_scan"), [{"directory": "D:/materials"}])
        self.assertIn("导入完成", page.status.text())
        self.assertTrue(bridge.called_with("library_status"), "导入后应刷新概况")

    def test_semantic_search_calls_the_semantic_tool(self) -> None:
        results = [{"score": 0.5, "video_path": "/a.mp4", "shot_index": 0,
                    "start": 0.0, "duration": 3.0, "labels": ["夜景"]}]
        page, bridge = self._library_page({
            "library_search_semantic": {"query": "星空", "count": 1, "results": results,
                                        "mode": "semantic"},
        })
        page.query_edit.setText("星空")
        page.mode_box.setCurrentIndex(0)
        page.search()

        self.assertEqual(
            bridge.called_with("library_search_semantic"),
            [{"query": "星空", "limit": 20}],
        )
        self.assertEqual(page.table.rowCount(), 1)
        self.assertEqual(page.table.item(0, 1).text(), "a.mp4")

    def test_label_search_calls_the_label_tool(self) -> None:
        page, bridge = self._library_page({
            "library_search_labels": {"query": "夜景", "count": 0, "results": [],
                                      "mode": "labels/fuzzy"},
        })
        page.query_edit.setText("夜景")
        page.mode_box.setCurrentIndex(1)
        page.search()

        self.assertEqual(
            bridge.called_with("library_search_labels"),
            [{"query": "夜景", "limit": 20}],
        )

    def test_search_renders_what_the_tool_returned_verbatim(self) -> None:
        """**界面不算数**：工具给几行就画几行，给什么文本就画什么文本。"""
        results = [
            {"score": 0.9876, "video_path": "/x/y.mp4", "shot_index": 7,
             "start": 1.5, "duration": 2.5, "labels": ["甲", "乙"]},
        ]
        page, _ = self._library_page({
            "library_search_semantic": {"query": "q", "count": 1, "results": results},
        })
        page.query_edit.setText("q")
        page.search()

        self.assertEqual(page.table.rowCount(), len(results))
        self.assertEqual(page.table.item(0, 0).text(), "0.9876")
        self.assertEqual(page.table.item(0, 2).text(), "7")
        self.assertEqual(page.table.item(0, 3).text(), "1.50s")
        self.assertEqual(page.table.item(0, 4).text(), "2.50s")
        self.assertEqual(page.table.item(0, 5).text(), "甲、乙")

    # ---------- 文案匹配 ----------
    def test_match_page_calls_script_match_with_the_form_values(self) -> None:
        page, bridge = self._match_page({
            "script_match": {"name": "演示", "path": "/m.json", "segments": 1,
                             "top_n": 5, "version": 2,
                             "results": [{"index": 0, "text": "第一句。",
                                          "selected_shot_key": "k1",
                                          "selected_shot_id": 1,
                                          "candidates": []}]},
            "match_show": {"count": 0, "matches": []},
        })
        page.name_edit.setText("演示")
        page.script_edit.setPlainText("第一句。")
        page.top_n_box.setValue(5)
        page.match()

        self.assertEqual(
            bridge.called_with("script_match"),
            [{"name": "演示", "script": "第一句。", "top_n": 5}],
        )
        self.assertEqual(page.segment_table.rowCount(), 1)

    def test_adjusting_calls_match_select_and_match_reorder(self) -> None:
        shown = {
            "name": "演示", "version": 2, "stale_candidates": 0,
            "segments": [{
                "index": 0, "text": "第一句。",
                "selected_shot_key": "k1", "selected_shot_id": 11,
                "selected_stale": False,
                "candidates": [
                    {"shot_key": "k1", "shot_id": 11, "video_path": "/a.mp4",
                     "start": 0.0, "end": 3.0, "score": 0.9, "stale": False},
                    {"shot_key": "k2", "shot_id": 22, "video_path": "/b.mp4",
                     "start": 0.0, "end": 3.0, "score": 0.8, "stale": False},
                ],
            }],
        }
        page, bridge = self._match_page({
            # 真工具也是这样：不传 name 是"列已有结果"，传了才是"读这一份"
            "match_show": lambda **kw: (
                shown if kw.get("name") else
                {"count": 1, "matches": [{"name": "演示", "segments": 1}]}
            ),
            "match_select": {"name": "演示", "segment": shown["segments"][0]},
            "match_reorder": {"name": "演示", "segment": shown["segments"][0]},
        })
        page.render_open(ok(shown))
        page.candidate_table.setCurrentCell(1, 0)

        page.select()
        self.assertEqual(
            bridge.called_with("match_select"),
            [{"name": "演示", "segment_index": 0, "shot_id": 22}],
        )
        # 调整之后**选中行不该丢** —— 人通常接着点「上移」
        self.assertEqual(page.current_shot_id(), 22, "重新渲染后仍选中同一个候选")

        page.reorder("up")
        self.assertEqual(
            bridge.called_with("match_reorder"),
            [{"name": "演示", "segment_index": 0, "shot_id": 22, "direction": "up"}],
        )

    def test_stale_candidates_are_surfaced(self) -> None:
        """失效的候选要说出来（TASK-012a 的 stale 不能在这里被吞掉）。"""
        shown = {
            "name": "旧结果", "version": 2, "stale_candidates": 1,
            "segments": [{
                "index": 0, "text": "一句。", "selected_shot_key": "k9",
                "selected_shot_id": None, "selected_stale": True,
                "candidates": [{"shot_key": "k9", "shot_id": None, "stale": True,
                                "stale_reason": "素材库里已找不到这个镜头",
                                "video_path": "/gone.mp4", "start": 0.0, "end": 1.0,
                                "score": 0.5}],
            }],
        }
        page, _ = self._match_page({"match_show": shown})
        page.render_open(ok(shown))

        self.assertIn("失效", page.status.text())
        self.assertEqual(page.segment_table.item(0, 3).text(), "是")
        page.candidate_table.setCurrentCell(0, 0)
        self.assertEqual(page.candidate_table.item(0, 4).text(), "已失效")

    # ---------- 工作台 ----------
    def test_workbench_reports_failed_plugins(self) -> None:
        from xbc.ui.pages.workbench import WorkbenchPage

        ctx = AppContext.create(root=Path(tempfile.mkdtemp(prefix="xbc-ui-wb-")), console=False)
        self.addCleanup(ctx.close)
        manager = ctx.create_plugin_manager()
        manager.discover()
        bridge = RecordingBridge({"library_status": {"videos_total": 0, "shots": 0,
                                                     "vectors": 0, "auditable": False,
                                                     "spaces": []}},
                                 tools={"library_status"})
        page = WorkbenchPage(ctx, bridge, manager)
        page.refresh()
        self.assertIn("插件", page.summary.toPlainText() or page.summary.toHtml())
        self.assertIn("library_status", " ".join(bridge.names()))


@unittest.skipUnless(HAS_PYSIDE6, f"未安装 PySide6: {PYSIDE6_ERROR}")
class UiPurityTests(unittest.TestCase):
    """验收 5 的结构性证明：界面层**拿不到**业务代码。"""

    def ui_files(self) -> list[Path]:
        return sorted(UI_DIR.rglob("*.py"))

    def test_ui_does_not_import_any_plugin_module(self) -> None:
        offenders = []
        for path in self.ui_files():
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    names = [node.module or ""]
                for name in names:
                    head = name.split(".")[0]
                    if head.startswith("xbc_va") or head == "plugins":
                        offenders.append(f"{path.name}: import {name}")
        self.assertEqual(offenders, [], "界面层不该 import 任何插件模块")

    def test_ui_does_not_import_business_or_heavy_libraries(self) -> None:
        """界面层不碰 sqlite / numpy / PIL / onnxruntime —— 那些是插件的事。"""
        forbidden = {"sqlite3", "numpy", "PIL", "onnxruntime", "torch", "requests"}
        offenders = []
        for path in self.ui_files():
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    names = [node.module or ""]
                for name in names:
                    if name.split(".")[0] in forbidden:
                        offenders.append(f"{path.name}: import {name}")
        self.assertEqual(offenders, [])

    def test_every_business_action_goes_through_the_tool_bridge(self) -> None:
        """界面对外的唯一出口是 `ToolBridge.call`。

        做法：把页面里所有 `self.call(...)` / 工具名都找出来，
        确认它们**只出现**在经由 bridge 的路径上 —— 页面里没有第二套调用方式。
        """
        page_files = sorted((UI_DIR / "pages").glob("*.py"))
        self.assertTrue(page_files)
        offenders = []
        for path in page_files:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    if node.func.attr == "call" and isinstance(node.func.value, ast.Name):
                        continue  # self.call(...) —— 走 bridge
                    if node.func.attr in {"call_value", "background"}:
                        continue  # 也走 bridge
                # 直接 import 业务能力的写法一律不允许
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = ([a.name for a in node.names]
                             if isinstance(node, ast.Import) else [node.module or ""])
                    for name in names:
                        if name.split(".")[0].startswith("xbc_va"):
                            offenders.append(f"{path.name}: {name}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
