"""文案匹配页：输入文案 → 看 Top-N 候选 → 人工调整。

工具分工（本页只是把它们接到按钮上）：

| 动作 | 工具 |
|---|---|
| 输入文案、分段、每段推荐候选 | `script_match` |
| 查看结果（含已保存的列表） | `match_show` |
| 指定某段用哪个镜头 | `match_select` |
| 调候选顺序 | `match_reorder` |

**候选排序、相似度、分段全在工具里算**；本页连"哪个分数更高"都不判断。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
)

from .base import Page

__all__ = ["MatchPage"]

REQUIRED_TOOLS = ["script_match", "match_show", "match_select", "match_reorder"]


class MatchPage(Page):
    title = "文案匹配"

    def __init__(self, ctx: Any, bridge: Any, manager: Any = None,
                 parent: Any = None) -> None:
        super().__init__(ctx, bridge, manager, parent)
        self.root.addWidget(QLabel("文案匹配（每段推荐候选镜头，人来定稿）"))

        # ---- 输入 ----
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("结果名："))
        self.name_edit = QLineEdit("默认")
        name_row.addWidget(self.name_edit)

        name_row.addWidget(QLabel("每段候选数："))
        self.top_n_box = QSpinBox()
        self.top_n_box.setRange(1, 20)
        self.top_n_box.setValue(3)
        name_row.addWidget(self.top_n_box)

        self.match_button = QPushButton("开始匹配")
        self.match_button.clicked.connect(self.on_match)
        name_row.addWidget(self.match_button)
        name_row.addStretch(1)
        self.root.addLayout(name_row)

        self.script_edit = QPlainTextEdit()
        self.script_edit.setPlaceholderText(
            "粘贴一段文案。按句号 / 换行自动分段，每段推荐若干候选镜头。"
        )
        self.script_edit.setFixedHeight(110)
        self.root.addWidget(self.script_edit)

        # ---- 读取已有结果 ----
        open_row = QHBoxLayout()
        open_row.addWidget(QLabel("已有结果："))
        self.saved_box = QComboBox()
        open_row.addWidget(self.saved_box, 1)
        self.reload_button = QPushButton("刷新列表")
        self.reload_button.clicked.connect(self.on_list)
        open_row.addWidget(self.reload_button)
        self.open_button = QPushButton("读取")
        self.open_button.clicked.connect(self.on_open)
        open_row.addWidget(self.open_button)
        self.root.addLayout(open_row)

        # ---- 分段表 ----
        self.root.addWidget(QLabel("分段与选中镜头"))
        self.segment_table = QTableWidget(0, 4)
        self.segment_table.setHorizontalHeaderLabels(["段", "文案", "选中镜头", "失效"])
        self.segment_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.segment_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.segment_table.currentCellChanged.connect(self.on_segment_changed)
        self.root.addWidget(self.segment_table, 1)

        # ---- 候选表 + 调整 ----
        self.candidates_label = QLabel("候选（先在上面选一段）")
        self.root.addWidget(self.candidates_label)
        self.candidate_table = QTableWidget(0, 5)
        self.candidate_table.setHorizontalHeaderLabels(
            ["相似度", "视频", "镜头", "起-止", "来源"]
        )
        self.candidate_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.candidate_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.root.addWidget(self.candidate_table, 1)

        adjust_row = QHBoxLayout()
        self.select_button = QPushButton("设为首选")
        self.select_button.clicked.connect(self.on_select)
        adjust_row.addWidget(self.select_button)
        self.up_button = QPushButton("上移")
        self.up_button.clicked.connect(lambda: self.on_reorder("up"))
        adjust_row.addWidget(self.up_button)
        self.down_button = QPushButton("下移")
        self.down_button.clicked.connect(lambda: self.on_reorder("down"))
        adjust_row.addWidget(self.down_button)
        adjust_row.addStretch(1)
        self.root.addLayout(adjust_row)

        self.root.addWidget(self.status)
        self._shown: dict[str, Any] | None = None

    # ---------------- 刷新 ----------------
    def refresh(self) -> None:
        ok, why = self.require(*REQUIRED_TOOLS)
        for widget in (self.match_button, self.open_button, self.reload_button,
                       self.select_button, self.up_button, self.down_button):
            widget.setEnabled(ok)
        if not ok:
            self.say(why)
            return
        self.on_list()

    # ---------------- 列表 / 读取 ----------------
    def list_arguments(self) -> dict[str, Any]:
        return {}

    def list_saved(self) -> Any:
        result = self.call("match_show")
        self.render_list(result)
        return result

    def on_list(self) -> None:
        self.list_saved()

    def render_list(self, result: Any) -> None:
        if not result.ok:
            self.say(f"读取列表失败：{result.message}")
            return
        value = result.value or {}
        matches = value.get("matches") or []
        current = self.saved_box.currentText()
        self.saved_box.clear()
        for item in matches:
            self.saved_box.addItem(
                f"{item.get('name')}（{item.get('segments')} 段）", item.get("name")
            )
        if current:
            index = self.saved_box.findData(current)
            if index >= 0:
                self.saved_box.setCurrentIndex(index)
        self.say(f"已有 {len(matches)} 份匹配结果")

    def open_arguments(self) -> dict[str, Any]:
        name = self.saved_box.currentData() or self.saved_box.currentText()
        return {"name": str(name)}

    def open(self) -> Any:
        result = self.call("match_show", **self.open_arguments())
        self.render_open(result)
        return result

    def on_open(self) -> None:
        name = self.open_arguments()["name"]
        if not name:
            self.say("先选一份匹配结果")
            return
        self.background("match_show", self.open_arguments(), self.render_open)

    def render_open(self, result: Any) -> None:
        if not result.ok:
            self.say(f"读取失败：{result.message}")
            return
        # 重新渲染不该把人踢回第一段 —— 记下当前看的段，画完再还原
        keep_segment = self.current_segment_index() if self._shown else None

        self._shown = result.value
        segments = result.value.get("segments") or []
        self.segment_table.setRowCount(len(segments))
        for row, segment in enumerate(segments):
            stale = "是" if segment.get("selected_stale") else ""
            cells = [
                str(segment.get("index", row)),
                str(segment.get("text", "")),
                str(segment.get("selected_shot_id")),
                stale,
            ]
            for column, text in enumerate(cells):
                self.segment_table.setItem(row, column, QTableWidgetItem(text))
        self.candidate_table.setRowCount(0)
        stale_count = result.value.get("stale_candidates", 0)
        self.say(
            f"已读取「{result.value.get('name')}」：{len(segments)} 段"
            + (f"　⚠ 有 {stale_count} 个候选已失效（素材被替换或边界变了）"
               if stale_count else "")
        )
        if segments:
            row = 0
            if keep_segment is not None:
                found = next(
                    (i for i, s in enumerate(segments)
                     if int(s.get("index", i)) == keep_segment),
                    None,
                )
                if found is not None:
                    row = found
            self.segment_table.setCurrentCell(row, 0)
            # **必须显式重画候选表**：段号没变时 `currentCellChanged` 不会触发，
            # 而上面已经 `setRowCount(0)` 清空了 —— 只靠信号会留下一张空表。
            self.on_segment_changed()

    # ---------------- 匹配 ----------------
    def match_arguments(self) -> dict[str, Any]:
        return {
            "name": self.name_edit.text().strip(),
            "script": self.script_edit.toPlainText(),
            "top_n": int(self.top_n_box.value()),
        }

    def match(self) -> Any:
        result = self.call("script_match", **self.match_arguments())
        self.render_match(result)
        return result

    def on_match(self) -> None:
        arguments = self.match_arguments()
        if not arguments["name"]:
            self.say("请填结果名")
            return
        if not arguments["script"].strip():
            self.say("请粘贴文案")
            return
        self.background("script_match", arguments, self.render_match)

    def render_match(self, result: Any) -> None:
        if not result.ok:
            self.say(f"匹配失败：{result.message}")
            return
        value = result.value
        self.say(
            f"匹配完成：{value.get('segments')} 段 × {value.get('top_n')} 候选，"
            f"已存到 {value.get('path')}"
        )
        self.on_list()
        self.saved_box.setCurrentIndex(self.saved_box.findData(value.get("name")))
        self.render_open(_AsResult({
            "name": value.get("name"), "segments": [
                {
                    "index": segment.get("index"),
                    "text": segment.get("text"),
                    "selected_shot_key": segment.get("selected_shot_key"),
                    "selected_shot_id": segment.get("selected_shot_id"),
                    "selected_stale": False,
                    "candidates": segment.get("candidates") or [],
                }
                for segment in value.get("results") or []
            ],
            "stale_candidates": 0,
            "version": value.get("version"),
        }))

    # ---------------- 分段 → 候选 ----------------
    def current_segment_index(self) -> int | None:
        if self._shown is None:
            return None
        row = self.segment_table.currentRow()
        if row < 0:
            return None
        segments = self._shown.get("segments") or []
        if row >= len(segments):
            return None
        return int(segments[row].get("index", row))

    def on_segment_changed(self, *_: Any) -> None:
        index = self.current_segment_index()
        if index is None or self._shown is None:
            return
        segments = self._shown.get("segments") or []
        segment = next((s for s in segments if int(s.get("index", -1)) == index), None)
        if segment is None:
            return
        candidates = segment.get("candidates") or []
        self.candidates_label.setText(
            f"第 {index} 段「{segment.get('text', '')}」的候选"
            f"（选中 shot_id={segment.get('selected_shot_id')}）"
        )
        self.candidate_table.setRowCount(len(candidates))
        for row, candidate in enumerate(candidates):
            start = candidate.get("start")
            end = candidate.get("end")
            score = candidate.get("score")
            cells = [
                "" if score is None else f"{score:.4f}",
                Path(str(candidate.get("video_path", ""))).name,
                str(candidate.get("shot_id")),
                "" if start is None else f"{start:.2f}-{end:.2f}",
                "人工指定" if candidate.get("manual")
                else ("已失效" if candidate.get("stale") else "检索"),
            ]
            for column, text in enumerate(cells):
                self.candidate_table.setItem(row, column, QTableWidgetItem(text))

    def current_shot_id(self) -> int | None:
        row = self.candidate_table.currentRow()
        if row < 0:
            return None
        item = self.candidate_table.item(row, 2)
        if item is None or not item.text().isdigit():
            return None
        return int(item.text())

    # ---------------- 人工调整 ----------------
    def select_arguments(self) -> dict[str, Any] | None:
        index = self.current_segment_index()
        shot_id = self.current_shot_id()
        if index is None or shot_id is None:
            return None
        name = self._shown.get("name") if self._shown else None
        if not name:
            return None
        return {"name": str(name), "segment_index": index, "shot_id": shot_id}

    def select(self) -> Any:
        arguments = self.select_arguments()
        if arguments is None:
            return None
        result = self.call("match_select", **arguments)
        self.render_adjust(result)
        return result

    def on_select(self) -> None:
        arguments = self.select_arguments()
        if arguments is None:
            self.say("先选一段和一个候选镜头")
            return
        self.background("match_select", arguments, self.render_adjust)

    def reorder_arguments(self, direction: str) -> dict[str, Any] | None:
        arguments = self.select_arguments()
        if arguments is None:
            return None
        return {**arguments, "direction": direction}

    def reorder(self, direction: str) -> Any:
        arguments = self.reorder_arguments(direction)
        if arguments is None:
            return None
        result = self.call("match_reorder", **arguments)
        self.render_adjust(result)
        return result

    def on_reorder(self, direction: str) -> None:
        arguments = self.reorder_arguments(direction)
        if arguments is None:
            self.say("先选一段和一个候选镜头")
            return
        self.background("match_reorder", arguments, self.render_adjust)

    def render_adjust(self, result: Any) -> None:
        if not result.ok:
            self.say(f"调整失败：{result.message}")
            return
        name = result.value.get("name")
        # 人点完「设为首选」通常接着点「上移」—— 把候选选中行也还原
        keep_row = self.candidate_table.currentRow()
        keep_shot = self.current_shot_id()

        # 重新从工具读一遍，而不是自己改界面上的表格
        refreshed = self.call("match_show", name=name)
        self.render_open(refreshed)

        if keep_shot is not None:
            for row in range(self.candidate_table.rowCount()):
                item = self.candidate_table.item(row, 2)
                if item is not None and item.text() == str(keep_shot):
                    self.candidate_table.setCurrentCell(row, 0)
                    break
        elif 0 <= keep_row < self.candidate_table.rowCount():
            self.candidate_table.setCurrentCell(keep_row, 0)
        self.say(f"已保存对「{name}」的调整")


class _AsResult:
    """把 `script_match` 的返回值包成 `match_show` 的形状，复用同一套渲染。

    **只做形状适配，不做业务加工** —— 字段照抄，不补不删。
    """

    def __init__(self, value: Any) -> None:
        self.ok = True
        self.value = value
        self.message = ""
