"""插件运行时。

- `scope.py`    作用域与 effect（注册即资源）
- `registry.py` 分层注册表与重名裁决
- `manager.py`  发现 / 加载 / 激活 / 停用 / 卸载的编排与错误隔离
"""

from .manager import PluginManager, PluginRecord
from .registry import Layer, LayeredRegistry, Registration
from .scope import Scope, ScopeDisposed

__all__ = [
    "Layer",
    "LayeredRegistry",
    "PluginManager",
    "PluginRecord",
    "Registration",
    "Scope",
    "ScopeDisposed",
]
