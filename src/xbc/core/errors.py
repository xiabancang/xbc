"""内核错误类型。

约定：所有"可以预期"的失败都抛 XbcError 的子类，
这样宿主可以在插件边界上统一捕获并隔离，而不是让任意异常掀翻整个工具箱。
"""


class XbcError(Exception):
    """内核可预期错误的基类。"""


class ConfigError(XbcError):
    """配置文件读写或内容不合法。"""


class PluginError(XbcError):
    """插件相关错误的基类。"""


class PluginManifestError(PluginError):
    """plugin.json 缺失、格式错误或字段不合法。"""


class PluginApiMismatch(PluginError):
    """插件的 api_version 与内核 API 主版本不兼容。"""


class PluginLoadError(PluginError):
    """插件代码导入或实例化失败。"""


class PluginStateError(PluginError):
    """在非法状态下操作插件（例如未启动就调用动作）。"""


class ServiceUnavailable(XbcError):
    """公共能力当前不可用（如 FFmpeg 未安装、Ollama 未启动）。"""


class CapabilityDenied(XbcError):
    """插件请求了未在 manifest 中声明的能力。

    这是最小权限原则的落点：manifest 里没写的能力，插件拿不到。
    """
