"""AI 能力层（Core Capability）—— TASK-007。

插件通过 `ctx.ai` 使用；**业务插件不得直接调用模型**。

| 模块 | 职责 |
|---|---|
| `types.py` | 能力枚举、结果类型、错误类型 |
| `request.py` | 请求结构（`TextRequest` / `VisionRequest` / `EmbeddingRequest`） |
| `provider.py` | `ModelProvider` 抽象 + 共用 HTTP/图片工具 |
| `registry.py` | Provider 注册表（加厂商不必改内核别处） |
| `providers/` | 内置实现：`ollama` / `openai_compatible` |
| `service.py` | `AIService` 门面 + 按配置装配的工厂 |
"""

from .provider import (
    AIProvider,
    ModelProvider,
    base_url,
    encode_image,
    image_data_url,
    request_json,
)
from .providers import OllamaProvider, OpenAICompatibleProvider
from .registry import (
    ProviderSettings,
    provider_factories,
    register_provider_factory,
    unregister_provider_factory,
)
from .request import EmbeddingRequest, TextRequest, VisionRequest
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
    "AIError",
    "AIProvider",
    "AIResponseError",
    "AIService",
    "AIUnavailable",
    "AIUnsupported",
    "AICapability",
    "EmbeddingRequest",
    "EmbeddingResult",
    "ModelProvider",
    "OllamaProvider",
    "OpenAICompatibleProvider",
    "ProviderSettings",
    "TextRequest",
    "TextResult",
    "VisionRequest",
    "base_url",
    "build_ai_service",
    "encode_image",
    "image_data_url",
    "provider_factories",
    "register_provider_factory",
    "request_json",
    "unregister_provider_factory",
]
