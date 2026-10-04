"""钩子契约（借鉴 pluggy）。

内核用 `@hookspec` 声明扩展点，插件用 `@hookimpl` 实现。
关键价值不在"能挂钩子"，而在**注册时校验签名** —— 插件把参数名写错、
或者实现了内核根本没有的钩子，都在注册那一刻就被发现，而不是运行到一半才炸。

我们只保留 pluggy 里真正用得上的一小部分：

- `@hookspec(firstresult=False)` —— 声明契约；`firstresult` 表示首个非 None 结果即返回
- `@hookimpl(tryfirst=False, trylast=False, optionalhook=False)` —— 实现契约
- 签名校验：实现的参数名必须是契约参数名的子集
- `check_pending()`：存在实现但无对应契约时报错（pluggy 同名机制）

**不实现** `hookwrapper` / `wrapper` / `historic`：当前没有使用场景，
按"最弱机制优先"原则不引入（方案 1.1 / 1.6）。
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, Callable

from ..errors import PluginError

_HOOKSPEC_ATTR = "__xbc_hookspec__"
_HOOKIMPL_ATTR = "__xbc_hookimpl__"


class HookValidationError(PluginError):
    """钩子实现的签名与契约不符，或实现了不存在的钩子。"""


def _normalize(func: Any, marker: str, opts: dict) -> Any:
    """既支持 @hookspec 直接装饰，也支持 @hookspec(...) 带参数。"""
    if func is None:
        return lambda f: _normalize(f, marker, opts)
    setattr(func, marker, opts)
    return func


def hookspec(func: Any = None, *, firstresult: bool = False) -> Any:
    """把一个函数标记为钩子契约。"""
    return _normalize(func, _HOOKSPEC_ATTR, {"firstresult": bool(firstresult)})


def hookimpl(
    func: Any = None,
    *,
    tryfirst: bool = False,
    trylast: bool = False,
    optionalhook: bool = False,
) -> Any:
    """把一个方法标记为钩子实现。

    `optionalhook=True` 时，即使内核没有对应契约也不报错 —— 这是
    **前向/后向兼容的关键**：新插件可以在老内核上安静地降级运行。
    """
    return _normalize(
        func,
        _HOOKIMPL_ATTR,
        {"tryfirst": bool(tryfirst), "trylast": bool(trylast), "optionalhook": bool(optionalhook)},
    )


class _Impl:
    __slots__ = ("plugin_id", "func", "opts", "argnames")

    def __init__(self, plugin_id: str, func: Callable, opts: dict) -> None:
        self.plugin_id = plugin_id
        self.func = func
        self.opts = opts
        self.argnames = _argnames(func)


class _Spec:
    __slots__ = ("name", "func", "opts", "argnames")

    def __init__(self, name: str, func: Callable, opts: dict) -> None:
        self.name = name
        self.func = func
        self.opts = opts
        self.argnames = _argnames(func)


def _argnames(func: Callable) -> list[str]:
    """取出函数除 self/cls 之外、且没有默认值的参数名。"""
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):  # pragma: no cover - 内建函数等
        return []
    names: list[str] = []
    for name, param in signature.parameters.items():
        if name in ("self", "cls"):
            continue
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        if param.default is param.empty:
            names.append(name)
    return names


class HookRelay:
    """钩子中继：收集契约与实现，校验签名，并按顺序调用。"""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._specs: dict[str, _Spec] = {}
        self._impls: dict[str, list[_Impl]] = {}
        self._log = logger or logging.getLogger("xbc")

    # ---------- 契约 ----------
    def add_specs(self, obj: Any) -> None:
        """从模块或类上收集 `@hookspec` 标记的函数。"""
        for name in dir(obj):
            if name.startswith("_"):
                continue
            attr = getattr(obj, name, None)
            opts = getattr(attr, _HOOKSPEC_ATTR, None)
            if opts is None or not callable(attr):
                continue
            self._specs[name] = _Spec(name, attr, opts)

    @property
    def specs(self) -> list[str]:
        return sorted(self._specs)

    # ---------- 实现 ----------
    def register(self, plugin_obj: Any, plugin_id: str = "") -> int:
        """收集插件上的 `@hookimpl`。签名不符抛 HookValidationError。"""
        owner = plugin_id or type(plugin_obj).__name__
        count = 0
        for name in dir(plugin_obj):
            if name.startswith("_"):
                continue
            attr = getattr(plugin_obj, name, None)
            opts = getattr(attr, _HOOKIMPL_ATTR, None)
            if opts is None or not callable(attr):
                continue
            self._validate(name, attr, opts, owner)
            self._impls.setdefault(name, []).append(_Impl(owner, attr, opts))
            count += 1
        return count

    def _validate(self, name: str, func: Callable, opts: dict, owner: str) -> None:
        spec = self._specs.get(name)
        if spec is None:
            if opts.get("optionalhook"):
                self._log.debug("插件 %s 实现了内核没有的钩子 %s（optionalhook，已忽略）", owner, name)
                return
            raise HookValidationError(
                f"插件 {owner} 实现了内核没有的钩子 {name!r}；"
                f"若这是为将来版本准备的，请加 optionalhook=True"
                f"（内核已知钩子: {self.specs}）"
            )
        allowed = set(spec.argnames)
        for arg in _argnames(func):
            if arg not in allowed:
                raise HookValidationError(
                    f"插件 {owner} 的钩子 {name!r} 有契约未声明的参数 {arg!r}；"
                    f"契约参数: {sorted(allowed)}"
                )

    def unregister(self, plugin_id: str) -> int:
        removed = 0
        for name in list(self._impls):
            keep = [impl for impl in self._impls[name] if impl.plugin_id != plugin_id]
            removed += len(self._impls[name]) - len(keep)
            if keep:
                self._impls[name] = keep
            else:
                del self._impls[name]
        return removed

    def check_pending(self) -> list[tuple[str, str]]:
        """列出"插件实现了、但内核没有对应契约"的**可选**钩子。

        非可选的未知钩子在 `register()` 时就已经被拒绝，所以这里剩下的
        全是 `optionalhook=True` 的。它们被允许运行（这是前向兼容的关键），
        但值得报告：说明插件期望的扩展点当前内核还不支持，属于版本错配的信号。
        """
        return sorted(
            (impl.plugin_id, name)
            for name, impls in self._impls.items()
            if name not in self._specs
            for impl in impls
            if impl.opts.get("optionalhook")
        )

    # ---------- 调用 ----------
    def impls(self, name: str) -> list[str]:
        return [impl.plugin_id for impl in self._impls.get(name, [])]

    def call(self, name: str, **kwargs: Any) -> list[Any]:
        """调用全部实现。返回结果列表；`firstresult` 契约返回首个非 None 值。

        **单个实现抛异常不会中断其余实现**（错误隔离，与插件生命周期一致）。
        """
        spec = self._specs.get(name)
        impls = self._ordered(name)
        if spec is not None and spec.opts.get("firstresult"):
            for impl in impls:
                result = self._invoke(impl, name, kwargs)
                if result is not None:
                    return result
            return None  # type: ignore[return-value]
        return [self._invoke(impl, name, kwargs) for impl in impls]

    def _ordered(self, name: str) -> list[_Impl]:
        impls = list(self._impls.get(name, []))
        # tryfirst 先执行，trylast 后执行；同类保持注册顺序（Python sort 稳定）
        impls.sort(key=lambda i: (not i.opts.get("tryfirst"), i.opts.get("trylast")))
        return impls

    def _invoke(self, impl: _Impl, name: str, kwargs: dict) -> Any:
        try:
            return impl.func(**kwargs)
        except Exception as exc:  # noqa: BLE001 - 错误隔离
            self._log.error("插件 %s 的钩子 %s 执行失败: %s", impl.plugin_id, name, exc)
            return None
