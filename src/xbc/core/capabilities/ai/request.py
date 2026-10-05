"""AI 能力的请求结构。

严格按规格，**只定义三个能力各自需要的东西，不多一分**：

| 能力 | 输入 |
|---|---|
| `text_generate` | `prompt`，可选 `system` |
| `vision_analyze` | 本地图片路径 |
| `embedding` | `texts`（文本） |
| `embed_images` | `images`（本地图片路径） |
| `synthesize` | `text` + 可选 `voice` + `output_path` |
| `clone_voice` | `reference_audio`（本地语音文件路径） + 可选 `name` |

**文本向量化与图片向量化是两个请求类型，不是一个。**
沿用 TASK-007 定下的原则：**一个字段不承载两种语义** ——
`EmbeddingRequest` 里放 `images` 会让"这段文本"和"这张图"混在一个列表里，
调用方与实现方都得靠猜。两者的向量也不可互相比较，分开才能让约束说得清。

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
    """文本向量化请求。"""

    texts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"texts": list(self.texts)}


@dataclass
class ImageEmbeddingRequest:
    """图片向量化请求。

    与 `VisionRequest` 一样，**只接受本地图片路径** —— 不接受 base64、
    不接受 http(s) URL，保持"图片来源只有一种形态"。

    与 `EmbeddingRequest` 的关系：**请求是两种（文本 / 图片），结果是同一个**
    （`EmbeddingResult`）。两者的向量属于**不同的向量空间，绝对不可互相比较**。
    """

    images: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"images": [str(image) for image in self.images]}


@dataclass
class SpeechRequest:
    """语音合成请求。

    `voice` **留空时用实现方的默认音色**（内置音色或默认参考语音）——
    调用方不必先知道有哪些声音才能出声。

    `output_path` 必须由调用方给出：产物落在哪里是**调用方的决定**，
    实现方不该自己挑目录（那样产物位置就不可预期、不可审计）。

    语速、音高这类**参数**来自配置，所以请求结构里不放它们 ——
    与其它能力同一条规矩：一个参数只有一个来源。
    """

    text: str
    output_path: str
    voice: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "output_path": self.output_path,
            "voice": self.voice,
        }


@dataclass
class VoiceCloneRequest:
    """音色登记请求。

    **只接受本地语音文件路径** —— 与 `vision_analyze` / `embed_images` 同一条规矩：
    "素材来源只有一种形态"，行为可预期、可审计。

    这里**不做持久化**：请求只要求实现方确认"这段语音能不能当音色用"，
    并返回它的规格与指纹（`VoiceProfile`）。声音库由调用方自己管。
    """

    reference_audio: str
    name: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"reference_audio": self.reference_audio, "name": self.name}
