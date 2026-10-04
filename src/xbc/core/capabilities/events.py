"""事件能力。

方案 3.7 明确了只有三种通信方式，事件是第三种（M3）：

| 方式 | 方向 | 用途 |
|---|---|---|
| 能力注入 | 内核 → 插件 | 插件使用内核能力 |
| 服务查找 | 插件 ↔ 插件 | 插件使用其他插件的能力 |
| **事件广播** | 一对多 | **通知**（"某事发生了"） |

**事件只用于通知，不用于请求-响应。** 需要返回值请用 Service 或 Tool。
这样定位清楚，就不会演变成一套隐式 RPC。

订阅返回 disposer，挂到插件作用域上，停用时自动释放（注册即资源）。
"""

from __future__ import annotations

from typing import Any, Callable

Disposer = Callable[[], Any]


class EventBus:
    def __init__(self, logger: Any = None) -> None:
        self._handlers: dict[str, list[tuple[str, Callable[..., Any]]]] = {}
        self._log = logger

    def on(self, event: str, handler: Callable[..., Any], owner: str = "") -> Disposer:
        """订阅事件。返回 disposer。"""
        if not callable(handler):
            raise TypeError("事件处理器必须可调用")
        entry = (owner, handler)
        self._handlers.setdefault(event, []).append(entry)
        return lambda: self._off(event, entry)

    def _off(self, event: str, entry: tuple[str, Callable[..., Any]]) -> None:
        handlers = self._handlers.get(event)
        if handlers and entry in handlers:
            handlers.remove(entry)
            if not handlers:
                del self._handlers[event]

    def off_owner(self, owner: str) -> int:
        """移除某个 owner 的全部订阅（作用域释放的兜底）。"""
        removed = 0
        for event in list(self._handlers):
            keep = [h for h in self._handlers[event] if h[0] != owner]
            removed += len(self._handlers[event]) - len(keep)
            if keep:
                self._handlers[event] = keep
            else:
                del self._handlers[event]
        return removed

    def emit(self, event: str, **payload: Any) -> int:
        """广播事件，返回投递数量。

        **单个处理器抛异常不影响其他处理器**（错误隔离）。
        """
        handlers = list(self._handlers.get(event, []))
        delivered = 0
        for owner, handler in handlers:
            try:
                handler(**payload)
                delivered += 1
            except Exception as exc:  # noqa: BLE001 - 错误隔离
                if self._log:
                    self._log.error("事件 %s 的处理器（%s）失败: %s", event, owner or "?", exc)
        return delivered

    def listeners(self, event: str) -> int:
        return len(self._handlers.get(event, []))

    def events(self) -> list[str]:
        return sorted(self._handlers)

    def __len__(self) -> int:
        return sum(len(v) for v in self._handlers.values())
