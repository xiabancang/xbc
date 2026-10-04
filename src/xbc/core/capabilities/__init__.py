"""能力（Capability）层：**内核提供给插件**的公共能力。

这是插件与内核之间最重要的边界：
插件不直接 open() 文件、不直接 subprocess 调 ffmpeg、不直接发 HTTP，
而是通过 `ctx` 拿到这里的对象。

术语区分（方案 4.1）：
- **Capability**：内核给插件的（本模块）
- **Service**：插件给其他插件的（走注册表）
- **Tool**：插件给 Agent 的（走工具注册表）

好处是将来可以统一在能力层加：超时、重试、限流、审计、密钥代理、权限校验，
而不需要改任何插件。
"""

from .ai import AIService, AIProvider, OllamaProvider, build_ai_service
from .events import EventBus
from .ffmpeg import FFmpegService
from .files import FileService
from .settings import SettingsCapability, mask_secret

__all__ = [
    "AIService",
    "AIProvider",
    "EventBus",
    "FFmpegService",
    "FileService",
    "OllamaProvider",
    "SettingsCapability",
    "build_ai_service",
    "mask_secret",
]
