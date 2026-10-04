"""AI 能力的请求结构。

严格按规格，**只定义三个能力各自需要的东西，不多一分**：

| 能力 | 输入 |
|---|---|
| `text_generate` | `prompt`，可选 `system` |
| `vision_analyze` | 本地图片路径 |
| `embedding` | `texts` |

模型名来自配置（`ai.model` / `ai.embedding_model`），
温度、上下文长度、超时这类**参数**来自配置的 `ai.options` ——
所以请求结构里不放这些，避免出现"同一个参数有两个来源"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TextRequest:
    """文本生成请求。"""

    prompt: str
    system: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"prompt": self.prompt, "system": self.system}


@dataclass
class VisionRequest:
    """视觉理解请求。

    **第一版只接受本地图片路径**：不接受 base64 字符串，也不接受 http(s) URL。
    这个限制是刻意的 —— 让"图片来源"只有一种形态，行为可预期、可审计。

    `question` 为 `None` 时走**描述模式**（返回 `description` + `labels`）；
    非 `None` 时走**定向提问模式**（返回 `answer` + `labels`）。
    """

    images: list[str] = field(default_factory=list)
    question: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"images": [str(image) for image in self.images], "question": self.question}


@dataclass
class EmbeddingRequest:
    """向量化请求。"""

    texts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"texts": list(self.texts)}
