"""素材库页：导入素材、看概况、按标签 / 语义检索。

**这里没有一行业务逻辑**：入库、检索、排序、时间码全是工具算的，
本页只管"把参数收上来、把 `results` 铺进表格"。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
)

from .base import Page, space_line

__all__ = ["LibraryPage"]

#: 本页需要的工具。缺任何一个就在页面上说清楚，而不是点了才报错。
REQUIRED_TOOLS = [
    "library_scan",
    "library_status",
    "library_search_semantic",
    "library_search_labels",
]


class LibraryPage(Page):
    title = "素材库"

    def __init__(self, ctx: Any, bridge: Any, manager: Any = None,
                 parent: Any = None) -> None:
        super().__init__(ctx, bridge, manager, parent)
        self.root.addWidget(QLabel("素材库"))

        # ---- 导入素材 ----
        import_row = QHBoxLayout()
        import_row.addWidget(QLabel("素材目录："))
        self.directory_edit = QLineEdit()
        self.directory_edit.setPlaceholderText("选择本地视频素材目录（.mp4 / .mov / .mkv …）")
        import_row.addWidget(self.directory_edit, 1)

        self.browse_button = QPushButton("浏览…")
        self.browse_button.clicked.connect(self.on_browse)
        import_row.addWidget(self.browse_button)

        self.scan_button = QPushButton("导入素材")
        self.scan_button.clicked.connect(self.on_scan)
        import_row.addWidget(self.scan_button)

        self.refresh_button = QPushButton("刷新概况")
        self.refresh_button.clicked.connect(self.on_refresh_clicked)
        import_row.addWidget(self.refresh_button)
        self.root.addLayout(import_row)

        # ---- 概况 ----
        self.summary = QLabel("（还没读概况）")
        self.summary.setWordWrap(True)
        self.root.addWidget(self.summary)

        # ---- 检索 ----
        search_row = QHBoxLayout()
        search_row.addWidget(QLabel("检索："))
        self.query_edit = QLineEdit()
        self.query_edit.setPlaceholderText("例如：星空 / 夜景 / 彩条测试卡")
        self.query_edit.returnPressed.connect(self.on_search)
        search_row.addWidget(self.query_edit, 1)

        self.mode_box = QComboBox()
        self.mode_box.addItems(["语义检索", "标签检索"])
        search_row.addWidget(self.mode_box)

        self.search_button = QPushButton("检索")
        self.search_button.clicked.connect(self.on_search)
        search_row.addWidget(self.search_button)
        self.root.addLayout(search_row)

        # ---- 本次检索用的向量空间（TASK-013c）----
        # 常驻显示：状态栏会被下一条消息覆盖，这个不会被覆盖。
        self.space_label = QLabel("向量空间：（还没检索过）")
        self.space_label.setWordWrap(True)
        self.root.addWidget(self.space_label)

        # ---- 结果表 ----
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["相似度", "视频", "镜头", "起始", "时长", "标签"]
        )
        # 视频名与标签是最有信息量的两列，其余按内容自适应 —— 别让标签被截掉
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.Stretch)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.root.addWidget(self.table, 1)

        self.root.addWidget(self.status)

    # ---------------- 刷新 ----------------
    def refresh(self) -> None:
        ok, why = self.require(*REQUIRED_TOOLS)
        for widget in (self.scan_button, self.refresh_button, self.search_button):
            widget.setEnabled(ok)
        if not ok:
            self.say(why)
            self.summary.setText("（素材库插件未启用）")
            return
        self.show_status()

    # ---------------- 素材库概况 ----------------
    def on_refresh_clicked(self) -> None:
        self.show_status()

    def show_status(self, note: str = "概况已刷新") -> dict[str, Any] | None:
        """读概况。失败就把工具的原文显示出来，不吞。"""
        result = self.call("library_status")
        if not result.ok:
            self.say(f"读取素材库概况失败：{result.message}")
            return None
        stats = result.value
        self.render_status(stats, note=note)
        return stats

    def render_status(self, stats: dict[str, Any], note: str = "概况已刷新") -> None:
        spaces = stats.get("spaces") or []
        space_text = "；".join(
            f"{s.get('provider')}/{s.get('model')} {s.get('dim')}维 "
            f"{s.get('frames', 0)}帧"
            for s in spaces
        ) or "（无向量空间）"
        image = stats.get("image_embedding") or {}
        image_text = "可用" if image.get("available") else "不可用（图片检索会降级）"
        self.summary.setText(
            f"库：{stats.get('library_path')}\n"
            f"视频 {stats.get('videos_total')}　镜头 {stats.get('shots')}　"
            f"标签 {stats.get('labels')}（去重 {stats.get('distinct_labels')}）　"
            f"向量 {stats.get('vectors')}　可审计 {stats.get('auditable')}　"
            f"schema v{stats.get('schema_version')}\n"
            f"向量空间：{space_text}　图片嵌入：{image_text}"
            + (f"\n注意：{stats.get('note')}" if stats.get("note") else "")
        )
        self.say(note)

    # ---------------- 导入素材 ----------------
    def on_browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择素材目录", "")
        if folder:
            self.directory_edit.setText(folder)

    def scan_arguments(self) -> dict[str, Any]:
        return {"directory": self.directory_edit.text().strip()}

    def scan(self) -> Any:
        """同步导入（测试用）。"""
        result = self.call("library_scan", **self.scan_arguments())
        self.render_scan(result)
        return result

    def on_scan(self) -> None:
        arguments = self.scan_arguments()
        if not arguments["directory"]:
            self.say("请先选择素材目录")
            return
        self.background("library_scan", arguments, self.render_scan)

    def render_scan(self, result: Any) -> None:
        if not result.ok:
            self.say(f"导入失败：{result.message}")
            return
        value = result.value
        summary = (
            f"导入完成：发现 {value.get('found')} 个视频，"
            f"新入库 {len(value.get('analyzed') or [])} 个，"
            f"跳过 {len(value.get('skipped') or [])} 个，"
            f"失败 {len(value.get('failed') or [])} 个"
        )
        # 概况跟着刷，但**不要把导入结果这条消息盖掉**
        self.show_status(note=summary)

    # ---------------- 检索 ----------------
    def search_arguments(self) -> dict[str, Any]:
        semantic = self.mode_box.currentIndex() == 0
        arguments: dict[str, Any] = {"query": self.query_edit.text().strip(), "limit": 20}
        if semantic:
            return {"__tool__": "library_search_semantic", **arguments}
        return {"__tool__": "library_search_labels", **arguments}

    def search(self) -> Any:
        arguments = self.search_arguments()
        tool = arguments.pop("__tool__")
        result = self.call(tool, **arguments)
        self.render_search(result)
        return result

    def on_search(self) -> None:
        arguments = self.search_arguments()
        tool = arguments.pop("__tool__")
        if not arguments["query"]:
            self.say("请输入检索词")
            return
        self.background(tool, arguments, self.render_search)

    def render_search(self, result: Any) -> None:
        if not result.ok:
            self.say(f"检索失败：{result.message}")
            return
        value = result.value
        results = value.get("results") or []
        self.table.setRowCount(len(results))
        for row, item in enumerate(results):
            score = item.get("score")
            duration = item.get("duration")
            cells = [
                "" if score is None else f"{score:.4f}",
                Path(str(item.get("video_path", ""))).name,
                str(item.get("shot_index", "")),
                f"{item.get('start', 0):.2f}s",
                "" if duration is None else f"{duration:.2f}s",
                "、".join(item.get("labels") or []),
            ]
            for column, text in enumerate(cells):
                self.table.setItem(row, column, QTableWidgetItem(text))
        note = value.get("note")
        self.space_label.setText(space_line(value.get("embedding"), note or ""))
        self.say(
            f"「{value.get('query')}」命中 {value.get('count', len(results))} 个镜头"
            f"（{value.get('mode', '')}）" + (f"　{note}" if note else "")
        )
