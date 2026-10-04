"""中文 CLIP（`chinese-clip-rn50`）的**本地 ONNX Provider**。

## 为什么在插件里

`ctx.ai` 的能力层负责"选哪个模型、怎么调、失败怎么报"；
这个 Provider 是**中文 CLIP 这个模型的本机实现**，注册进 `ctx.ai` 之后，
插件只通过 `ctx.ai.embed_images()` / `ctx.ai.embedding()` 使用它，
**不直接 import onnxruntime 做推理**。

## 部署形态（TASK-010 调研结论）

| 环节 | 依赖 |
|---|---|
| 导出（一次性，开发机） | torch + cn_clip + torchvision + timm + onnx |
| **运行** | **onnxruntime + numpy + pillow** —— **不需要 torch** |

## 声明两种能力，理由是同一条

Provider 同时声明：

- `AICapability.IMAGE_EMBEDDING` —— 把关键帧编码成 1024 维图片向量
- `AICapability.EMBEDDING` —— 把**中文查询**用同一模型的文本塔编码

**查询与图片必须出自同一个模型**，否则两者不在同一个向量空间、余弦相似度没有意义。
这也正是 `AICapability` 把"文本向量化"与"图片向量化"分成两个能力的原因：
别的 Provider（如 `nomic-embed-text`）只做文本，不该被误选来编码图片。

## 模型文件

`clip_model_dir` 指向含 `vision_model.onnx` / `text_model.onnx` / `vocab.txt` 的目录；
留空则用插件数据目录下的 `models/chinese-clip-rn50/`。
文件不全时 `available()` 返回 **False**（不抛异常），调用方会得到一句可照做的错误。
"""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path
from typing import Any

from xbc.core.capabilities.ai import (
    AICapability,
    AIError,
    AIUnavailable,
    EmbeddingRequest,
    EmbeddingResult,
    ImageEmbeddingRequest,
    ModelProvider,
)

#: 模型标识（写进库里，用于判断两个向量是否同一空间）
MODEL_ID = "chinese-clip-rn50"

#: 图像输入分辨率与上下文长度（来自官方 RN50 配置）
IMAGE_SIZE = 224
CONTEXT_LENGTH = 52

#: 官方 `image_transform` 用的归一化参数
_MEAN = (0.48145466, 0.4578275, 0.40821073)
_STD = (0.26862954, 0.26130258, 0.27577711)

#: BERT 特殊 token
_CLS, _SEP, _PAD, _UNK = "[CLS]", "[SEP]", "[PAD]", "[UNK]"
_MAX_CHARS_PER_WORD = 100


class ChineseClipUnavailable(AIUnavailable):
    """模型文件缺失 / 运行时依赖缺失。"""


# ---------------------------------------------------------------- BERT 分词
def _is_whitespace(char: str) -> bool:
    return char in " \t\n\r" or unicodedata.category(char) == "Zs"


def _is_control(char: str) -> bool:
    if char in "\t\n\r":
        return False
    return unicodedata.category(char) in ("Cc", "Cf")


def _is_punctuation(char: str) -> bool:
    code = ord(char)
    if 33 <= code <= 47 or 58 <= code <= 64 or 91 <= code <= 96 or 123 <= code <= 126:
        return True
    return unicodedata.category(char).startswith("P")


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return (
        0x4E00 <= code <= 0x9FFF
        or 0x3400 <= code <= 0x4DBF
        or 0x20000 <= code <= 0x2A6DF
        or 0x2A700 <= code <= 0x2B73F
        or 0x2B740 <= code <= 0x2B81F
        or 0x2B820 <= code <= 0x2CEAF
        or 0xF900 <= code <= 0xFAFF
        or 0x2F800 <= code <= 0x2FA1F
    )


class BertWordPieceTokenizer:
    """中文 BERT 的 WordPiece 分词器（纯 Python，无第三方依赖）。

    算法按 BERT 原论文/参考实现的标准定义独立实现：
    清洗控制字符 → 中日韩字符逐字切分 → 标点切分 → 去重音 + 小写 →
    贪心最长匹配的 WordPiece（续片段加 `##` 前缀）。

    与官方 `cn_clip` 的分词结果做过**逐条等价性验证**（见测试）。
    """

    def __init__(self, vocab_file: Path) -> None:
        self.vocab: dict[str, int] = {}
        with open(vocab_file, "r", encoding="utf-8") as handle:
            for index, line in enumerate(handle):
                token = line.rstrip("\n")
                if token and token not in self.vocab:
                    self.vocab[token] = index
        for required in (_CLS, _SEP, _PAD, _UNK):
            if required not in self.vocab:
                raise AIError(f"vocab.txt 缺少必需的特殊 token：{required}")
        self._unk_id = self.vocab[_UNK]

    # ---------- 基础切分 ----------
    @staticmethod
    def _clean(text: str) -> str:
        chars = []
        for char in text:
            if char == "\u0000" or char == "\ufffd" or _is_control(char):
                continue
            chars.append(" " if _is_whitespace(char) else char)
        return "".join(chars).strip()

    @staticmethod
    def _split_cjk(text: str) -> str:
        chars = []
        for char in text:
            if _is_cjk(char):
                chars.append(" ")
                chars.append(char)
                chars.append(" ")
            else:
                chars.append(char)
        return "".join(chars)

    @staticmethod
    def _strip_accents(text: str) -> str:
        return "".join(
            char for char in unicodedata.normalize("NFD", text)
            if unicodedata.category(char) != "Mn"
        )

    def _basic_tokenize(self, text: str) -> list[str]:
        text = self._strip_accents(text.lower())
        tokens: list[str] = []
        for chunk in self._split_cjk(self._clean(text)).split():
            current = []
            for char in chunk:
                if _is_punctuation(char):
                    if current:
                        tokens.append("".join(current))
                        current = []
                    tokens.append(char)
                else:
                    current.append(char)
            if current:
                tokens.append("".join(current))
        return tokens

    # ---------- WordPiece ----------
    def _wordpiece(self, token: str) -> list[str]:
        if len(token) > _MAX_CHARS_PER_WORD:
            return [_UNK]
        pieces: list[str] = []
        start = 0
        while start < len(token):
            end = len(token)
            matched = None
            while start < end:
                piece = token[start:end]
                if start > 0:
                    piece = "##" + piece
                if piece in self.vocab:
                    matched = piece
                    break
                end -= 1
            if matched is None:
                return [_UNK]
            pieces.append(matched)
            start = end
        return pieces

    # ---------- 对外 ----------
    def tokenize(self, text: str) -> list[str]:
        pieces: list[str] = []
        for token in self._basic_tokenize(text):
            pieces.extend(self._wordpiece(token))
        return pieces

    def convert_tokens_to_ids(self, tokens: list[str]) -> list[int]:
        return [self.vocab.get(token, self._unk_id) for token in tokens]

    def encode(self, text: str, context_length: int = CONTEXT_LENGTH) -> list[int]:
        """`[CLS] … [SEP]` + 补齐到固定长度（与官方 `tokenize()` 一致）。"""
        body = self.convert_tokens_to_ids(self.tokenize(text))[: context_length - 2]
        ids = [self.vocab[_CLS]] + body + [self.vocab[_SEP]]
        return ids + [self.vocab[_PAD]] * (context_length - len(ids))


# ---------------------------------------------------------------- 图像预处理
def preprocess_image(path: str, size: int = IMAGE_SIZE) -> Any:
    """按**官方** `image_transform` 预处理。

    ⚠️ 官方是 `Resize((size, size))` —— **直接拉成正方形**，不是标准 CLIP 的
    shortest-edge + center-crop。TASK-010 实测：用错预处理 top-1 从 10/10 掉到 7/10。
    （对照：amon-hen 的 MobileCLIP2 结论正好相反，它必须裁切。**按各自官方实现来。**）
    """
    import numpy as np
    from PIL import Image

    file = Path(str(path))
    if not file.is_file():
        raise AIError(f"图片文件不存在：{file}")

    image = Image.open(file).convert("RGB").resize((size, size), Image.BICUBIC)
    array = np.asarray(image, dtype=np.float32) / 255.0
    array = (array - np.asarray(_MEAN, dtype=np.float32)) / np.asarray(_STD, dtype=np.float32)
    return array.transpose(2, 0, 1)[None, ...]  # (1, 3, H, W)


# ---------------------------------------------------------------- Provider
class ChineseClipProvider(ModelProvider):
    """中文 CLIP 的本地 ONNX 实现。懒加载：构造时不碰磁盘、不加载模型。"""

    name = "chinese_clip"
    capabilities = frozenset({AICapability.IMAGE_EMBEDDING, AICapability.EMBEDDING})

    def __init__(self, model_dir: Path | str | None = None) -> None:
        self.model_dir = Path(model_dir) if model_dir else None
        self._vision: Any = None
        self._text: Any = None
        self._tokenizer: BertWordPieceTokenizer | None = None
        self._failed: str = ""

    # ---------- 就绪检查（必须便宜、不抛异常） ----------
    def _files(self) -> tuple[Path, Path, Path]:
        base = self.model_dir
        if base is None:
            raise ChineseClipUnavailable(
                "未配置中文 CLIP 模型目录。请在插件配置里设置 clip_model_dir，"
                "或用 scripts/export_chinese_clip_onnx.py 导出后放到插件数据目录的"
                " models/chinese-clip-rn50/ 下。"
            )
        return (
            base / "vision_model.onnx",
            base / "text_model.onnx",
            base / "vocab.txt",
        )

    def configured(self) -> bool:
        try:
            return all(path.is_file() for path in self._files())
        except ChineseClipUnavailable:
            return False

    def available(self) -> bool:
        """文件齐 + onnxruntime 可导入。**不抛异常**。"""
        if not self.configured():
            return False
        try:
            import onnxruntime  # noqa: F401
        except ImportError:
            return False
        return True

    def supports(self, capability: AICapability) -> bool:
        """模型没配好时**不声明能力**。

        否则 `AIService` 的路由会选到这个 Provider（它类上写了支持图片嵌入），
        然后必然失败 —— 而库里可能还有另一个真能干的 Provider。
        "声明支持"必须与"现在真能做"一致，路由才可信。
        """
        if capability in (AICapability.IMAGE_EMBEDDING, AICapability.EMBEDDING):
            return super().supports(capability) and self.configured()
        return super().supports(capability)

    def describe(self, *, probe: bool = True) -> dict[str, Any]:
        data = super().describe(probe=probe)
        data["model_dir"] = str(self.model_dir) if self.model_dir else ""
        if self._failed:
            data["error"] = self._failed
        return data

    # ---------- 懒加载 ----------
    def _ensure(self) -> None:
        if self._vision is not None:
            return
        if self._failed:
            raise ChineseClipUnavailable(self._failed)

        # **先查模型文件**再查运行时依赖：文件缺失是更常见、也更可操作的原因，
        # 报错应该先指向它，而不是甩一句"缺 onnxruntime"。
        vision_file, text_file, vocab_file = self._files()
        for path in (vision_file, text_file, vocab_file):
            if not path.is_file():
                self._failed = (
                    f"中文 CLIP 模型文件缺失：{path}\n"
                    "用 scripts/export_chinese_clip_onnx.py 导出，"
                    "或把 clip_model_dir 指向已导出的目录。"
                )
                raise ChineseClipUnavailable(self._failed)

        try:
            import onnxruntime as ort
        except ImportError as exc:
            self._failed = (
                "缺少 onnxruntime，无法运行本地中文 CLIP 模型：pip install onnxruntime"
            )
            raise ChineseClipUnavailable(self._failed) from exc

        options = ort.SessionOptions()
        options.log_severity_level = 3
        self._vision = ort.InferenceSession(
            str(vision_file), sess_options=options, providers=["CPUExecutionProvider"]
        )
        self._text = ort.InferenceSession(
            str(text_file), sess_options=options, providers=["CPUExecutionProvider"]
        )
        self._tokenizer = BertWordPieceTokenizer(vocab_file)

    # ---------- 能力实现 ----------
    @staticmethod
    def _normalise(matrix: Any) -> Any:
        import numpy as np

        norms = np.linalg.norm(matrix, axis=-1, keepdims=True)
        return matrix / np.maximum(norms, 1e-12)

    def embed_images(self, request: ImageEmbeddingRequest) -> EmbeddingResult:
        """关键帧 → 1024 维图片向量。"""
        if not request.images:
            return EmbeddingResult(vectors=[], provider=self.name, model=MODEL_ID)
        self._ensure()
        import numpy as np

        assert self._vision is not None
        input_name = self._vision.get_inputs()[0].name
        output_name = self._vision.get_outputs()[0].name

        vectors: list[list[float]] = []
        for path in request.images:
            batch = preprocess_image(path)
            raw = self._vision.run([output_name], {input_name: batch})[0]
            vectors.append(self._normalise(np.asarray(raw, dtype=np.float32))[0].tolist())
        return EmbeddingResult(vectors=vectors, provider=self.name, model=MODEL_ID)

    def embedding(self, request: EmbeddingRequest) -> EmbeddingResult:
        """中文查询文本 → 同一空间的 1024 维向量。"""
        if not request.texts:
            return EmbeddingResult(vectors=[], provider=self.name, model=MODEL_ID)
        self._ensure()
        import numpy as np

        assert self._text is not None and self._tokenizer is not None
        input_name = self._text.get_inputs()[0].name
        output_name = self._text.get_outputs()[0].name

        vectors: list[list[float]] = []
        for text in request.texts:
            ids = np.asarray([self._tokenizer.encode(str(text))], dtype=np.int64)
            raw = self._text.run([output_name], {input_name: ids})[0]
            vectors.append(self._normalise(np.asarray(raw, dtype=np.float32))[0].tolist())
        return EmbeddingResult(vectors=vectors, provider=self.name, model=MODEL_ID)


def find_model_dir(configured: str | None, data_dir: Path) -> Path | None:
    """解析模型目录：显式配置优先，其次插件数据目录下的默认位置。"""
    if configured:
        return Path(configured).expanduser()
    default = Path(data_dir) / "models" / MODEL_ID
    return default if default.is_dir() else None


__all__ = [
    "CONTEXT_LENGTH",
    "ChineseClipProvider",
    "ChineseClipUnavailable",
    "BertWordPieceTokenizer",
    "MODEL_ID",
    "find_model_dir",
    "preprocess_image",
]
