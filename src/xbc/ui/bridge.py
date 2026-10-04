"""界面 ↔ 工具之间的**唯一通道**。

## 界面不做业务逻辑

页面里不出现任何业务判断：不判断"这个素材该不该入库"、不判断"哪个镜头更匹配"、
不自己算时间码、不自己排序。**所有能力都通过 `ToolBridge.call("工具名", **参数)`
调现有工具** —— 与命令行 `run.py tool call` 走**完全相同**的代码路径。

界面也**不 import 任何插件模块**（`xbc_va_*` 之类一个都不碰）：
它只知道工具**名字**与**参数**，这正是 Agent 调工具的同一套契约。
所以"界面层无业务逻辑"是**结构性保证** —— 界面连业务代码都拿不到。

## 为什么要有异步

`library_scan` 在真实素材上要几十秒（每个镜头都要过一遍视觉模型）。
同步调用会把窗口冻住，用户以为程序死了。所以慢操作走后台线程，
但这层**不做任何调度智能**：一次只跑一个，跑的时候按钮全禁用。
"""

from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QObject, QThread, Signal

__all__ = ["BackgroundCall", "ToolBridge"]


class ToolBridge:
    """按名字调工具。界面唯一的对外出口。"""

    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx

    # ---------- 查询（只读，不改任何状态） ----------
    def names(self) -> list[str]:
        """当前已注册的工具名（来自 Runtime，不是清单声明）。"""
        return list(self._ctx.tool_registry.names())

    def spec(self, name: str) -> dict[str, Any] | None:
        spec = self._ctx.tool_registry.get(name)
        return spec.to_agent() if spec is not None else None

    def available(self, name: str) -> bool:
        """这个工具现在能不能用 —— **问 Runtime，不猜**。

        比如没装 `video_analyzer` 时就没有 `library_scan`，
        对应页面应该整块禁用而不是点了报错。
        """
        return self._ctx.tool_registry.get(name) is not None

    def missing(self, names: list[str]) -> list[str]:
        """这批工具里哪些还没注册 —— 用于给出"需要先启用哪个插件"的提示。"""
        return [name for name in names if not self.available(name)]

    # ---------- 调用 ----------
    def call(self, tool: str, **arguments: Any) -> Any:
        """调工具，**原样返回 `ToolResult`**。

        第一个参数叫 `tool` 而不是 `name` —— 因为不少工具自己就有个 `name` 参数
        （`script_match` / `match_show`），叫 `name` 会和它撞上。

        这一层刻意**不判断成功失败**：`ok` / `value` / `message` 由页面决定怎么呈现。
        想省事的页面可以用 `call_value()`。
        """
        return self._ctx.tool_registry.call(tool, arguments)

    def call_value(self, tool: str, **arguments: Any) -> Any:
        """调工具并直接返回 `value`；失败时抛 `ToolCallFailed`（带工具原文）。"""
        result = self.call(tool, **arguments)
        if not result.ok:
            raise ToolCallFailed(f"{tool} 调用失败：{result.message}")
        return result.value


class ToolCallFailed(RuntimeError):
    """工具返回 `ok=False`。消息里保留工具给的原文，界面直接展示。"""


class _CallJob(QObject):
    """在线程里跑一次工具调用。**一次一个**，不做并发。"""

    done = Signal(object)

    def __init__(self, bridge: ToolBridge, tool: str, arguments: dict[str, Any]) -> None:
        super().__init__()
        self._bridge = bridge
        self._tool = tool
        self._arguments = arguments

    def run(self) -> None:
        try:
            result: Any = self._bridge.call(self._tool, **self._arguments)
        except Exception as exc:  # noqa: BLE001 - 后台线程里不能让异常逃出去
            result = exc
        self.done.emit(result)


class BackgroundCall(QObject):
    """把慢工具调用挪到后台线程，结果回到主线程。

    用法：`BackgroundCall(bridge, "library_scan", directory=...)`，
    然后 `call.start(on_done)`。`on_done` 收到 `ToolResult` 或异常对象。

    **不做队列、不做并发**：调用方负责在跑的时候禁用按钮。
    """

    def __init__(
        self, bridge: ToolBridge, tool: str, **arguments: Any
    ) -> None:
        super().__init__()
        self._bridge = bridge
        self._tool = tool
        self._arguments = arguments
        self._thread: QThread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def start(self, on_done: Callable[[Any], None]) -> None:
        thread = QThread()
        job = _CallJob(self._bridge, self._tool, self._arguments)
        job.moveToThread(thread)
        thread.started.connect(job.run)
        job.done.connect(on_done)
        job.done.connect(thread.quit)
        job.done.connect(job.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._thread = thread
        thread.start()

    def wait(self, timeout_ms: int = 60_000) -> bool:
        """等它跑完。**只给测试用** —— 界面不应该阻塞等。"""
        if self._thread is None:
            return True
        return bool(self._thread.wait(timeout_ms))
