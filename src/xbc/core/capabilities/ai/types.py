"""AI 能力的类型契约。

这一层是**插件与模型之间的唯一接口**，所以刻意把"能力"与"结果"都定义成显式类型，
插件不需要也不应该知道底下是哪家的模型。

三种能力：

| 能力 | 方法 | 输入 | 输出 |
|---|---|---|---|
| 文本生成 | `text_generate` | 提示词 | `TextResult` |
| 视觉理解 | `vision_analyze` | 本地图片路径 | `VisionResult` |
| 向量化 | `embedding` | 一段或多段文本 | `EmbeddingResult` |

--------------------------------------------------------------------------
**关于向量的跨 Provider 约束（重要，不要绕过）**

`EmbeddingResult.vectors` **不允许跨 Provider 混用**，也不允许跨模型混用。

原因是它们根本不在同一个空间里：

- 不同 Provider 的向量**维度**可能不同（768 / 1536 / 1024 …），连加减都不合法；
- 即使维度碰巧相同，不同模型的**语义坐标系也不同** —— 在 A 模型里
  "余弦相似度 0.85"与在 B 模型里的 0.85 完全不是一回事。

所以：**只有在 `provider` 与 `model` 都相同的前提下，两组向量才可比较。**

结果对象里同时带上 `provider` / `model` / `dim` 就是为了让调用方**能**做这个判断 ——
拿它们当空间标识，不一致就直接拒绝比较，而不是算出一个看似合理、实则无意义的相似度。
--------------------------------------------------------------------------
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
    """没有可用的模型服务，或必需的配置缺失（未配置 provider / model、连不上、超时）。

    消息里必须能回答三件事：**是哪个 provider、什么原因、该怎么办**。
    """


class AIUnsupported(AIError):
    """当前 Provider 不支持请求的能力。"""


class AIResponseError(AIError):
    """模型服务返回了无法理解的内容。"""


@dataclass
class TextResult:
    """文本生成的结果。

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
class VisionResult:
    """视觉理解的结果。

    **刻意不是一个字符串。** 下游拿到的是"这段画面是什么 + 有哪些标签"，
    而不是一段散文 —— 只返回字符串会让调用方被迫自己再去解析自然语言，
    那等于把提示词工程推给每一个插件。

    `raw` 保留模型原始输出，便于排查为什么解析成了这个结果。
    """

    description: str
    labels: list[str] = field(default_factory=list)
    provider: str = ""
    model: str = ""
    raw: str = ""
    usage: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "description": self.description,
            "labels": list(self.labels),
            "provider": self.provider,
            "model": self.model,
            "raw": self.raw,
            "usage": dict(self.usage),
        }


@dataclass
class EmbeddingResult:
    """向量化的结果。

    `vectors[i]` 对应输入的第 i 段文本。`dim` 是向量维度。

    **跨 Provider / 跨模型不可混用** —— 详见本模块开头那段说明。
    比较两组向量之前，先确认 `provider` 与 `model` 一致。
    """

    vectors: list[list[float]]
    provider: str = ""
    model: str = ""

    @property
    def count(self) -> int:
        return len(self.vectors)

    @property
    def dim(self) -> int:
        """向量维度。空结果返回 0。

        它是算出来的而不是存下来的 —— 存下来就可能与实际向量不符。
        """
        return len(self.vectors[0]) if self.vectors else 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "vectors": self.vectors,
            "dim": self.dim,
            "count": self.count,
            "provider": self.provider,
            "model": self.model,
        }
