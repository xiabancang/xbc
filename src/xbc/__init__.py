"""夏半仓工具箱（XBC）。

一个可插件化的 Windows 本地工具平台内核。
内核不依赖任何 GUI 库：图形界面只是它的一个消费者。
"""

from .version import APP_NAME, APP_ID, CORE_API_VERSION, CORE_VERSION

__version__ = CORE_VERSION

__all__ = ["APP_NAME", "APP_ID", "CORE_VERSION", "CORE_API_VERSION", "__version__"]
