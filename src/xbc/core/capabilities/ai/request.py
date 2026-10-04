"""AI 能力的请求结构。

把参数收进显式对象，而不是散在方法签名里，有两个实际好处：

1. **扩展不破坏 Provider** —— 以后要加 `top_p` / `stop` / `seed`，只需在
   dataclass 上加一个字段，不必修改每个 Provider 的方法签名；
2. **请求可以被记录与回放** —— 排查问题时能说清"到底发出去了什么"。
   `to_dict()` 会**剔除图片原始字节**，避免把几 MB 的 base64 倒进日志。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TextRequest:
    """文本生成请求。"""

    prompt: str
    system: str | None = None
    model: str | None = None
    json_mode: bool = False
    temperature: float | None = None
    max_tokens: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt": self.prompt,
            "system": self.system,
            "model": self.model,
            "json_mode": self.json_mode,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }


@dataclass
class VisionRequest(TextRequest):
    """视觉理解请求。`images` 里可以是路径字符串、`Path` 或原始字节。"""

    images: list[Any] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        # 只报数量与类型，不倒出图片内容
        data["images"] = [
            {"kind": "bytes", "size": len(image)} if isinstance(image, (bytes, bytearray))
            else {"kind": "path", "value": str(image)}
            for image in self.images
        ]
        return data


@dataclass
class EmbeddingRequest:
    """向量化请求。"""

    texts: list[str]
    model: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"texts": list(self.texts), "model": self.model}
