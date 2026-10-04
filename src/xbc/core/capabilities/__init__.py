"""能力（Capability）层：**内核提供给插件**的公共能力。

这是插件与内核之间最重要的边界：
插件不直接 open() 文件、不直接 subprocess 调 ffmpeg、不直接发 HTTP 调模型，
而是通过 `ctx` 拿到这里的对象。

术语区分（方案 4.1）：
- **Capability**：内核给插件的（本模块）
- **Service**：插件给其他插件的（走注册表）
- **Tool**：插件给 Agent 的（走工具注册表）

`ai/` 是一个子包，因为 AI 能力本身是多 Provider 的：换模型只装配细节，
插件侧的调用方式不变。
"""

from .ai import (
    AICapability,
    AIError,
    AIProvider,
    AIResponseError,
    AIService,
    AIUnavailable,
    AIUnsupported,
    EmbeddingResult,
    OllamaProvider,
    OpenAICompatibleProvider,
    TextResult,
    build_ai_service,
)
from .events import EventBus
from .ffmpeg import FFmpegService
from .files import FileService
from .settings import SettingsCapability, mask_secret

__all__ = [
    "AICapability",
    "AIError",
    "AIProvider",
    "AIResponseError",
    "AIService",
    "AIUnavailable",
    "AIUnsupported",
    "EmbeddingResult",
    "EventBus",
    "FFmpegService",
    "FileService",
    "OllamaProvider",
    "OpenAICompatibleProvider",
    "SettingsCapability",
    "TextResult",
    "build_ai_service",
    "mask_secret",
]
