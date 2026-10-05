"""AI 能力层（Core Capability）。

插件通过 `ctx.ai` 使用；**业务插件不得直接调用模型**。

| 模块 | 职责 |
|---|---|
| `types.py` | 能力枚举（含**文本/图片两种向量化**与**语音合成**）、结果类型、错误类型 |
| `request.py` | 请求结构（`TextRequest` / `VisionRequest` / `EmbeddingRequest` / `ImageEmbeddingRequest` / `SpeechRequest` / `VoiceCloneRequest`） |
| `provider.py` | `ModelProvider` 抽象 + 视觉结果解析 + HTTP / 图片 / 代理工具 |
| `providers/` | 两个真实实现：`ollama`（本地）、`openai_compatible` |
| `service.py` | `AIService` 门面 + 显式装配 |

**没有注册表，没有"为将来预留"的接口。** 加第三个厂商 = 在 `providers/` 加一个模块
+ 在 `service.py` 的装配分支里加一段，**不改任何插件**。
"""

from .provider import (
    ModelProvider,
    base_url,
    encode_image,
    image_data_url,
    parse_vision_payload,
    request_json,
    vision_prompt,
)
from .providers import OllamaProvider, OpenAICompatibleProvider
from .request import (
    EmbeddingRequest,
    ImageEmbeddingRequest,
    SpeechRequest,
    TextRequest,
    VisionRequest,
    VoiceCloneRequest,
)
from .service import KNOWN_PROVIDERS, AIService, build_ai_service
from .types import (
    AICapability,
    AIError,
    AIResponseError,
    AIUnavailable,
    AIUnsupported,
    EmbeddingResult,
    SpeechResult,
    TextResult,
    VisionAnswer,
    VisionResult,
    VoiceProfile,
)

__all__ = [
    "AICapability",
    "AIError",
    "AIResponseError",
    "AIService",
    "AIUnavailable",
    "AIUnsupported",
    "EmbeddingRequest",
    "EmbeddingResult",
    "ImageEmbeddingRequest",
    "KNOWN_PROVIDERS",
    "ModelProvider",
    "OllamaProvider",
    "OpenAICompatibleProvider",
    "SpeechRequest",
    "SpeechResult",
    "TextRequest",
    "TextResult",
    "VisionAnswer",
    "VisionRequest",
    "VisionResult",
    "VoiceCloneRequest",
    "VoiceProfile",
    "base_url",
    "build_ai_service",
    "encode_image",
    "image_data_url",
    "parse_vision_payload",
    "request_json",
    "vision_prompt",
]
