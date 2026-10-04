"""作用域与「注册即资源」。

这是整个运行时最基础的一块。设计约束来自方案 3.4 / 1.1：

> **注册即资源** —— 插件每次注册（工具、技能、服务、事件订阅）都挂到一个
> 作用域上，并返回一个 disposer。停用插件时内核统一逆序释放，
> **插件自己不需要写清理代码**，也不可能"忘了清理"。

这样解决两个经典难题：
1. 停用插件后残留注册（工具还在、事件还在触发）；
2. 插件卸载顺序错乱导致的半死不活状态。

作用域可以嵌套：子作用域先于父作用域释放。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from ..errors import XbcError

Disposer = Callable[[], Any]


class ScopeDisposed(XbcError):
    """在已释放的作用域上继续注册。"""


class Scope:
    """一个可释放的注册容器。

    典型用法::

        scope = Scope("plugin:demo")
        scope.effect(lambda: registry.register("x", 1))   # 捕获返回值作为 disposer
        scope.dispose()                                    # 逆序释放，幂等
    """

    def __init__(self, name: str, parent: "Scope | None" = None, logger: Any = None) -> None:
        self.name = name
        self.parent = parent
        self._log = logger or logging.getLogger("xbc")
        self._disposers: list[Disposer] = []
        self._children: list[Scope] = []
        self._disposed = False
        if parent is not None:
            parent._children.append(self)

    # ---------- 注册 ----------
    def effect(self, register: Callable[[], Any]) -> Disposer:
        """执行一次注册并记录它的 disposer。

        `register` 应当返回一个可调用的释放函数；返回 None 视为无需释放。
        """
        if self._disposed:
            raise ScopeDisposed(f"作用域 {self.name} 已释放，不能再注册")
        result = register()
        disposer: Disposer = result if callable(result) else (lambda: None)
        self._disposers.append(disposer)
        return disposer

    def fork(self, name: str) -> "Scope":
        """派生子作用域。子作用域先于父作用域释放。"""
        if self._disposed:
            raise ScopeDisposed(f"作用域 {self.name} 已释放，不能再派生子作用域")
        return Scope(f"{self.name}/{name}", parent=self, logger=self._log)

    # ---------- 状态 ----------
    @property
    def disposed(self) -> bool:
        return self._disposed

    @property
    def effect_count(self) -> int:
        """当前持有的注册数量（测试用它断言"停用后无残留"）。"""
        return len(self._disposers) + sum(child.effect_count for child in self._children)

    # ---------- 释放 ----------
    def dispose(self) -> None:
        """释放全部注册。幂等：重复调用不做任何事。

        单个 disposer 失败**不影响其他 disposer**，只记日志 ——
        否则一个插件的清理 bug 会造成其他插件资源泄漏。
        """
        if self._disposed:
            return
        self._disposed = True

        for child in reversed(self._children):
            child.dispose()
        self._children.clear()

        for disposer in reversed(self._disposers):
            try:
                disposer()
            except Exception as exc:  # noqa: BLE001 - 释放失败必须被隔离
                self._log.error("作用域 %s 释放注册失败: %s", self.name, exc)
        self._disposers.clear()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        state = "disposed" if self._disposed else "active"
        return f"<Scope {self.name} {state} effects={len(self._disposers)}>"
