"""页面基类。

只提供三件东西：

1. `self.bridge` —— 调工具的唯一通道（`ToolBridge`）
2. `self.busy()` / `self.idle()` —— 慢操作时禁用整页
3. `self.background(name, arguments, render)` —— 把**工具调用**丢后台，
   回来后**在主线程**渲染

## 为什么要把"调"和"渲染"分开

`library_scan` 在真实素材上要几十秒。如果在主线程同步调，窗口会冻住。
但渲染必须回主线程。所以每个动作拆成两半：

```python
def scan(self) -> Any:                    # 同步版：给测试用
    result = self.call("library_scan", directory=...)
    self.render_scan(result)
    return result

def on_scan(self) -> None:                # 按钮版：工具在后台跑
    self.background("library_scan", {"directory": ...}, self.render_scan)
```

两边共用同一份**取参数**与**渲染**代码，所以按钮路径和测试路径不会分叉。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Callable

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from ..bridge import BackgroundCall, ToolBridge

__all__ = ["Page", "describe_space", "space_line"]

#: 模态的中文名 —— 界面上不出现 `image` / `text` 这种内部词
_MODALITY_LABELS = {"image": "图片", "text": "文本"}


def describe_space(space: Mapping[str, Any] | None) -> str:
    """把向量空间渲染成一行给人看的话（TASK-013c）。

    例：`向量空间：chinese_clip / chinese-clip-rn50 · 1024 维 · 图片`

    **纯展示**：只搬工具返回的字段，不认识的就如实写出来，不猜、不推算。
    """
    if not space:
        return "向量空间：未知"
    parts = [f"{space.get('provider') or '?'} / {space.get('model') or '?'}"]
    dim = space.get("dim")
    if dim:
        parts.append(f"{dim} 维")
    modality = str(space.get("modality") or "").lower()
    if modality:
        parts.append(_MODALITY_LABELS.get(modality, modality))
    return "向量空间：" + " · ".join(parts)


def space_line(space: Mapping[str, Any] | None, note: str = "") -> str:
    """一行空间描述；带降级提示时前面加 ⚠ 并接上原因。

    **提示文案来自工具返回的 `note`，界面不自己判断该不该降级** ——
    界面只负责显示，判断留在插件里（界面不含业务逻辑）。
    """
    text = describe_space(space)
    return f"⚠ {text}　——　{note}" if note else text


class Page(QWidget):
    """一个页面。**不含任何业务逻辑** —— 只收集输入、调工具、渲染结果。"""

    #: 导航里显示的名字
    title = "页面"

    #: 请求切换页面时发出（页面 key）
    navigate = Signal(str)

    def __init__(
        self,
        ctx: Any,
        bridge: ToolBridge,
        manager: Any = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.bridge = bridge
        #: 应用启动时装配的那个 PluginManager（界面**不自己再装配一个**）
        self.manager = manager
        self._call: BackgroundCall | None = None

        self.root = QVBoxLayout(self)
        self.status = QLabel("")
        self.status.setWordWrap(True)

    # ---------------- 子类实现 ----------------
    def refresh(self) -> None:
        """重新从工具/Runtime 取一遍数据并重绘。**不缓存状态。**"""

    # ---------------- 工具调用 ----------------
    def call(self, tool: str, **arguments: Any) -> Any:
        """同步调工具。返回 `ToolResult` 原样（`ok` / `value` / `message`）。

        第一个参数叫 `tool` —— 工具自己常有 `name` 参数，叫 `name` 会撞上。
        """
        return self.bridge.call(tool, **arguments)

    def background(
        self,
        tool: str,
        arguments: dict[str, Any],
        render: Callable[[Any], None],
    ) -> None:
        """把工具调用丢到后台线程，回来后在主线程渲染。

        跑的时候整页禁用 —— **一次只跑一个**，不做并发。
        """
        self.busy(f"正在执行 {tool} …")
        call = BackgroundCall(self.bridge, tool, **arguments)

        def done(result: Any) -> None:
            self.idle()
            render(result)

        self._call = call
        call.start(done)

    # ---------------- 忙碌状态 ----------------
    def busy(self, text: str = "") -> None:
        self.setEnabled(False)
        if text:
            self.status.setText(text)

    def idle(self) -> None:
        self.setEnabled(True)

    def say(self, text: str) -> None:
        self.status.setText(text)

    # ---------------- 组件的启用状态跟着工具走 ----------------
    def require(self, *names: str) -> tuple[bool, str]:
        """这些工具都注册了吗？没注册就说明还缺哪个插件。

        **问 Runtime，不猜**：界面不假设"素材库一定在"。
        """
        missing = self.bridge.missing(list(names))
        if not missing:
            return True, ""
        return False, f"缺少工具 {missing} —— 请先在「插件中心」启用对应插件"
