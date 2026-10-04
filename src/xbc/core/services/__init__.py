"""公共能力层（capabilities）。

这是插件与内核之间最重要的边界：
插件不直接 open() 文件、不直接 subprocess 调 ffmpeg、不直接发 HTTP，
而是通过 AppContext 拿到这里的服务对象。

好处是将来可以统一在服务层加：超时、重试、限流、审计、密钥管理、权限校验，
而不需要改任何插件。
"""

from .ai import AIService, AIProvider, OllamaProvider, build_ai_service
from .ffmpeg import FFmpegService
from .files import FileService

__all__ = [
    "AIService",
    "AIProvider",
    "OllamaProvider",
    "build_ai_service",
    "FFmpegService",
    "FileService",
]
