"""工作台：一眼看清当前装了些什么、素材库什么状态，以及去哪干活。

数字全部**现取**：插件/工具/技能来自 Runtime，素材库来自 `library_status` 工具。
本页不缓存、不推算、不美化。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QTextBrowser

from ...version import APP_NAME, CORE_VERSION
from .base import Page

__all__ = ["WorkbenchPage"]


class WorkbenchPage(Page):
    title = "工作台"

    def __init__(self, ctx: Any, bridge: Any, manager: Any = None,
                 parent: Any = None) -> None:
        super().__init__(ctx, bridge, manager, parent)
        self.root.addWidget(QLabel(f"{APP_NAME} v{CORE_VERSION} · 工作台"))

        self.summary = QTextBrowser()
        self.summary.setOpenExternalLinks(False)
        self.root.addWidget(self.summary, 1)

        buttons = QHBoxLayout()
        self.refresh_button = QPushButton("刷新")
        self.refresh_button.clicked.connect(self.on_refresh)
        buttons.addWidget(self.refresh_button)

        self.library_button = QPushButton("去素材库")
        self.library_button.clicked.connect(lambda: self.navigate.emit("library"))
        buttons.addWidget(self.library_button)

        self.match_button = QPushButton("去文案匹配")
        self.match_button.clicked.connect(lambda: self.navigate.emit("match"))
        buttons.addWidget(self.match_button)

        self.plugins_button = QPushButton("去插件中心")
        self.plugins_button.clicked.connect(lambda: self.navigate.emit("plugins"))
        buttons.addWidget(self.plugins_button)
        buttons.addStretch(1)
        self.root.addLayout(buttons)

        self.root.addWidget(self.status)

    # ---------------- 渲染 ----------------
    def refresh(self) -> None:
        records = self.manager.records() if self.manager is not None else []
        active = [r for r in records if r.state.value == "active"]
        failed = [r for r in records if r.error]
        tools = self.ctx.tool_registry.for_agent()
        skills = self.ctx.skill_catalog.specs()

        lines = [
            f"<b>数据目录</b>　{self.ctx.paths.root}",
            "",
            f"<b>插件</b>　共 {len(records)} 个，已启用 {len(active)} 个"
            + (f"，<b>失败 {len(failed)} 个</b>" if failed else ""),
            f"<b>工具</b>　{len(tools)} 个（Agent 可调用）",
            f"<b>技能</b>　{len(skills)} 个",
        ]
        for record in failed:
            lines.append(f"　⚠ {record.id}：{record.error}")

        lines.append("")
        lines.append("<b>素材库</b>")
        if not self.bridge.available("library_status"):
            lines.append("　工具 library_status 未注册 —— 请先在「插件中心」启用 video_analyzer")
        else:
            result = self.call("library_status")
            if not result.ok:
                lines.append(f"　读取失败：{result.message}")
            else:
                stats = result.value
                spaces = stats.get("spaces") or []
                lines.append(
                    f"　视频 {stats.get('videos_total')}　镜头 {stats.get('shots')}　"
                    f"向量 {stats.get('vectors')}　可审计 {stats.get('auditable')}"
                )
                lines.append(
                    "　向量空间：" + ("；".join(
                        f"{s.get('provider')}/{s.get('model')} {s.get('dim')}维"
                        for s in spaces) or "（无）")
                )
                image = stats.get("image_embedding") or {}
                lines.append(
                    "　图片嵌入（中文 CLIP）："
                    + ("可用" if image.get("available") else "不可用")
                )

        self.summary.setHtml("<br>".join(lines))
        self.say("已刷新（数字来自 Runtime 与工具，界面不缓存）")

    def on_refresh(self) -> None:
        self.refresh()
