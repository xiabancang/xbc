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
    """把慢工具调用挪到后台线程，结果**回到主线程**。

    用法：`BackgroundCall(bridge, "library_scan", directory=...)`，
    然后 `call.start(on_done)`。`on_done` 收到 `ToolResult`。

    ## 这里踩过三个坑（TASK-012b 界面卡死的根因，都在这一个类里）

    ### 坑 1：`job` 必须是成员，不能是局部变量

    `job.moveToThread(thread)` **只改 C++ 线程亲和性，不增加 Python 引用**。
    写成局部变量的话，`start()` 一返回引用计数就归零 → C++ 对象被销毁 →
    `thread.started → job.run` 指向死对象 → **任务静默不执行**。
    而 `thread.quit` 也连在 `job.done` 上 → 线程永不退出 → 退出程序时卡死。

    ### 坑 2：回调的接收者必须是**主线程的 QObject**

    直接把用户的闭包连到 `job.done` 会走 **`DirectConnection`** ——
    信号在哪个线程发，回调就在哪个线程跑。而回调要碰控件
    （`setEnabled` / 往表格写数据），Qt 规定控件只能在所属线程访问。

    所以中间加一个 `self._relay`：`self` 是在主线程构造的 `QObject`，
    Qt 会自动用 `QueuedConnection` 把它投递到主线程。

    ### 坑 3：`thread.finished → thread.deleteLater` 与 `self._thread` 冲突

    C++ 对象被删了，Python 侧 `self._thread` 还指着它 ——
    之后任何 `self._thread.isRunning()`（`running` 属性 / `wait()`）都会碰到
    已销毁的 C++ 对象。而且这条路径**恰恰在"线程终于能正常结束"之后才会被走到**，
    所以坑 1 修好之后它才开始暴露。现在改成在线程结束时**主动清引用**。

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
        #: **强引用**后台任务对象 —— 见坑 1
        self._job: _CallJob | None = None
        #: 用户回调，在主线程执行
        self._on_done: Callable[[Any], None] | None = None

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.isRunning()

    def start(self, on_done: Callable[[Any], None]) -> None:
        thread = QThread()
        job = _CallJob(self._bridge, self._tool, self._arguments)
        job.moveToThread(thread)
        thread.started.connect(job.run)

        # 坑 2：接收者是 self（主线程 QObject）→ 自动 QueuedConnection → 主线程执行
        job.done.connect(self._relay)
        job.done.connect(thread.quit)
        job.done.connect(job.deleteLater)

        # 坑 3：不要 `thread.deleteLater()`，改为在线程结束时把引用放掉
        thread.finished.connect(self._on_thread_finished)

        self._on_done = on_done
        self._job = job          # 坑 1：保住 job
        self._thread = thread
        thread.start()

    def _relay(self, result: Any) -> None:
        """`job.done` 的接收者。**信号从工作线程发，本方法在主线程跑。**

        拿一次就把回调清掉 —— 一次调用只回一次，也避免持有闭包不放。
        """
        callback, self._on_done = self._on_done, None
        if callback is not None:
            callback(result)

    def _on_thread_finished(self) -> None:
        """线程真正结束了，才把引用放掉（坑 3）。"""
        self._job = None
        self._thread = None

    def wait(self, timeout_ms: int = 60_000) -> bool:
        """等它跑完。**只给测试用** —— 界面不应该阻塞等。"""
        thread = self._thread
        if thread is None:
            return True
        return bool(thread.wait(timeout_ms))
