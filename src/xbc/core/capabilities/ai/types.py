"""AI 能力的类型契约。

这一层是**插件与模型之间的唯一接口**，所以刻意把"能力"与"结果"都定义成显式类型，
插件不需要也不应该知道底下是 Ollama 还是别的什么。

三种能力（TASK-007 第 5/6/7 项）：

| 能力 | 方法 | 输入 | 输出 |
|---|---|---|---|
| 文本生成 | `text_generate` | 提示词 | `TextResult` |
| 视觉理解 | `vision_analyze` | 提示词 + 图片 | `TextResult` |
| 向量化 | `embedding` | 一段或多段文本 | `EmbeddingResult` |
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ...errors import XbcError


class AICapability(str, Enum):
    """Provider 可以声明自己支持的能力。"""

    TEXT = "text"
    VISION = "vision"
    EMBEDDING = "embedding"

    def __str__(self) -> str:  # pragma: no cover - 展示用
        return self.value


class AIError(XbcError):
    """AI 能力相关错误的基类。"""


class AIUnavailable(AIError):
    """没有可用的模型服务（未启动、连不上、超时）。"""


class AIUnsupported(AIError):
    """当前 Provider 不支持请求的能力。"""


class AIResponseError(AIError):
    """模型服务返回了无法理解的内容。"""


@dataclass
class TextResult:
    """文本类调用的结果。

    `text` 是模型输出的原文；`json()` 在 `json_mode=True` 时把原文解析成对象。
    带上 `provider` / `model` 是为了**可观测**：出问题时能说清是谁生成的。
    """

    text: str
    provider: str = ""
    model: str = ""
    usage: dict[str, Any] = field(default_factory=dict)

    def json(self) -> Any:
        """把 `text` 当 JSON 解析。解析失败抛 AIResponseError（而不是 JSONDecodeError）。"""
        try:
            return json.loads(self.text)
        except json.JSONDecodeError as exc:
            raise AIResponseError(
                f"模型输出不是合法 JSON（{exc}）：{self.text[:200]!r}"
            ) from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "provider": self.provider,
            "model": self.model,
            "usage": dict(self.usage),
        }


@dataclass
class EmbeddingResult:
    """向量化结果。`vectors[i]` 对应输入的第 i 段文本。"""

    vectors: list[list[float]]
    provider: str = ""
    model: str = ""

    @property
    def count(self) -> int:
        return len(self.vectors)

    @property
    def dimensions(self) -> int:
        return len(self.vectors[0]) if self.vectors else 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "count": self.count,
            "dimensions": self.dimensions,
            # 向量本身可能很长，摘要里只给维度与首元素，避免日志爆炸
            "vectors": self.vectors,
        }
