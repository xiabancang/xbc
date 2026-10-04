"""最小验证插件。

它存在的唯一目的：证明插件机制真的能跑 ——
发现、加载、启动、调用动作、停止、卸载六个环节，以及公共能力调用。

它刻意做得"不像业务"：没有任何视频/内容处理逻辑。
真正的业务插件（AI视频助手等）应该照抄这里的结构，再填业务。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

# 插件唯一允许 import 的内核模块：基类与动作装饰器。
# 其他内核内部实现都不应该被插件依赖。
from xbc.core.plugins.base import XbcPlugin, action


class HelloXbcPlugin(XbcPlugin):
    """演示完整生命周期的插件。"""

    def __init__(self) -> None:
        super().__init__()
        self._greeting = "你好，夏半仓工具箱"

    # ---------- 生命周期 ----------
    def on_load(self) -> None:
        # 此时配置与上下文已注入，但还没有"运行"
        self._greeting = str(self.ctx.settings.get("greeting", self._greeting))
        self.log.info("on_load：配置已读取，数据目录 %s", self.ctx.data_dir)

    def on_start(self) -> None:
        runs = int(self.ctx.settings.get("runs", 0) or 0)
        self.log.info("on_start：这是第 %s 次启动", runs + 1)

    def on_stop(self) -> None:
        self.log.info("on_stop：本插件不持有资源，无需释放")

    def on_unload(self) -> None:
        self.log.info("on_unload：清理完成")

    # ---------- 动作 ----------
    @action
    def hello(self, name: str = "世界") -> dict[str, Any]:
        """写一个问候文件到自己的数据目录，并累计调用次数。

        这里刻意走 ctx.files 而不是直接 open()：
        公共能力层将来要统一做审计、沙箱和编码处理。
        """
        self.ctx.files.ensure_dir(self.ctx.data_dir)

        runs = int(self.ctx.settings.get("runs", 0) or 0) + 1
        self.ctx.settings.set("runs", runs)  # 持久化到 config.json 的 plugins.hello_xbc

        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        text = (
            f"{self._greeting}，{name}！\n"
            f"写入时间：{stamp}\n"
            f"这是第 {runs} 次调用。\n"
        )
        target = self.ctx.files.write_text(self.ctx.data_dir / "hello.txt", text)

        return {"file": str(target), "runs": runs, "message": text.strip()}

    @action
    def probe(self) -> dict[str, Any]:
        """报告宿主提供了什么、当前环境是否就绪（不做任何可能失败的操作）。"""
        return {
            "core_version": self.ctx.core_version,
            "api_version": self.ctx.api_version,
            "declared_capabilities": list(self.ctx.capabilities),
            "plugin_data_dir": str(self.ctx.data_dir),
            "ffmpeg_available": bool(self.ctx.ffmpeg.available()),
            "ai_providers": self.ctx.ai.status(),
        }

    @action
    def fail_on_purpose(self) -> None:
        """故意失败：用于验证"单个插件出错不会拖垮宿主和其他插件"。"""
        raise RuntimeError("hello_xbc 故意抛出的异常，用于验证错误隔离")
