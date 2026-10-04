"""AI 能力层（Core Capability）—— TASK-007。

插件通过 `ctx.ai` 使用；**业务插件不得直接调用模型**。

| 模块 | 职责 |
|---|---|
| `types.py` | 能力枚举、结果类型、错误类型 |
| `provider.py` | `AIProvider` 抽象 + 共用 HTTP/图片工具 |
| `ollama.py` | 本地 Ollama Provider（文本 / 视觉 / 向量） |
| `openai_compat.py` | OpenAI 兼容 Provider（API 接口预留，默认不注册） |
| `service.py` | `AIService` 门面 + 按配置装配的工厂 |
"""

from .ollama import OllamaProvider
from .openai_compat import OpenAICompatibleProvider
from .provider import AIProvider, base_url, encode_image, image_data_url, request_json
from .service import AIService, build_ai_service
from .types import (
    AICapability,
    AIError,
    AIResponseError,
    AIUnavailable,
    AIUnsupported,
    EmbeddingResult,
    TextResult,
)

__all__ = [
    "AICapability",
    "AIError",
    "AIProvider",
    "AIResponseError",
    "AIService",
    "AIUnavailable",
    "AIUnsupported",
    "EmbeddingResult",
    "OllamaProvider",
    "OpenAICompatibleProvider",
    "TextResult",
    "base_url",
    "build_ai_service",
    "encode_image",
    "image_data_url",
    "request_json",
]
