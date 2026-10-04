"""契约层：内核与插件之间的"合同"。

内核只根据这里的定义决定是否加载、能否激活、该注入什么能力，
判断过程不依赖插件代码本身。

- manifest.py  清单解析与校验（双版本字段、能力白名单、工具与技能声明）
- hookspec.py  钩子契约与注册时签名校验（借鉴 pluggy）
- plugin.py    插件基类与生命周期回调契约
"""

from .hookspec import (
    HookRelay,
    HookValidationError,
    hookimpl,
    hookspec,
)
from .manifest import (
    KNOWN_CAPABILITIES,
    MANIFEST_NAME,
    CommandDecl,
    PluginManifest,
    ToolDecl,
    is_api_compatible,
    is_spec_compatible,
    load_manifest,
    parse_manifest,
)
from .plugin import PluginState, XbcPlugin

__all__ = [
    "KNOWN_CAPABILITIES",
    "MANIFEST_NAME",
    "CommandDecl",
    "HookRelay",
    "HookValidationError",
    "PluginManifest",
    "PluginState",
    "ToolDecl",
    "XbcPlugin",
    "hookimpl",
    "hookspec",
    "is_api_compatible",
    "is_spec_compatible",
    "load_manifest",
    "parse_manifest",
]
