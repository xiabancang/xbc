"""Ollama 本地模型 Provider（TASK-007 第 3 项）。

对接 Ollama 的 HTTP API：

| 能力 | 端点 | 说明 |
|---|---|---|
| 可用性 / 模型列表 | `GET /api/tags` | |
| 文本生成 | `POST /api/generate` | `stream: false` |
| 视觉理解 | `POST /api/generate` | 多张图片走 `images`（base64） |
| 向量化 | `POST /api/embed` | 失败时回退到旧接口 `/api/embeddings` |

请求参数沿用 V18 原型中已验证可用的组合（`format=json`、`temperature=0.1`、
`num_ctx=8192`、`num_predict=700`），但现在全部可配置，不再写死在代码里。
"""

from __future__ import annotations

from typing import Any

from .provider import (
    AIProvider,
    base_url,
    encode_image,
    get_json,
    probe_available,
    request_json,
)
from .types import (
    AICapability,
    AIResponseError,
    AIUnavailable,
    EmbeddingResult,
    TextResult,
)


class OllamaProvider(AIProvider):
    name = "ollama"
    capabilities = frozenset({AICapability.TEXT, AICapability.VISION, AICapability.EMBEDDING})

    def __init__(
        self,
        url: str = "http://127.0.0.1:11434",
        model: str = "qwen2.5vl:3b",
        embedding_model: str = "nomic-embed-text",
        timeout: int = 180,
        options: dict[str, Any] | None = None,
        logger: Any = None,
    ) -> None:
        # 兼容老配置：url 里可能带着 /api/generate
        self.base = base_url(url) or "http://127.0.0.1:11434"
        self.model = str(model or "")
        self.embedding_model = str(embedding_model or "")
        self.timeout = int(timeout or 180)
        self.options = dict(options or {})
        self._log = logger

    # ---------- 端点 ----------
    @property
    def _tags_url(self) -> str:
        return f"{self.base}/api/tags"

    @property
    def _generate_url(self) -> str:
        return f"{self.base}/api/generate"

    @property
    def _embed_url(self) -> str:
        return f"{self.base}/api/embed"

    @property
    def _legacy_embed_url(self) -> str:
        return f"{self.base}/api/embeddings"

    # ---------- 可用性 ----------
    def configured(self) -> bool:
        """地址配好了就算配置齐备（不联网）。"""
        return bool(self.base)

    def available(self) -> bool:
        return probe_available(self._tags_url)

    def models(self) -> list[str]:
        try:
            data = get_json(self._tags_url, timeout=5)
        except Exception:  # noqa: BLE001 - 拿不到就返回空
            return []
        return [str(item.get("name", "")) for item in data.get("models", []) if item.get("name")]

    def default_model(self, capability: AICapability) -> str:
        return self.embedding_model if capability is AICapability.EMBEDDING else self.model

    # ---------- 文本 / 视觉 ----------
    def _generate(
        self,
        prompt: str,
        images: list[Any] | None,
        *,
        system: str | None,
        model: str | None,
        json_mode: bool,
        temperature: float | None,
        max_tokens: int | None,
    ) -> TextResult:
        chosen = model or self.model
        if not chosen:
            raise AIUnavailable("没有配置 Ollama 模型名（ai.ollama.model）")

        payload: dict[str, Any] = {
            "model": chosen,
            "prompt": prompt,
            "stream": False,
        }
        if system:
            payload["system"] = system
        if images:
            payload["images"] = [encode_image(image) for image in images]
        if json_mode:
            payload["format"] = "json"

        options = dict(self.options)
        if temperature is not None:
            options["temperature"] = temperature
        if max_tokens is not None:
            options["num_predict"] = max_tokens
        if options:
            payload["options"] = options

        if not self.available():
            raise AIUnavailable(
                f"本地模型服务不可用：{self._tags_url} 连不上。"
                "请确认 Ollama 已启动，或改用其他 ai.provider"
            )

        data = request_json(self._generate_url, payload=payload, timeout=self.timeout)
        text = data.get("response")
        if not isinstance(text, str):
            raise AIResponseError(f"Ollama 响应里没有 response 字段：{str(data)[:200]}")
        return TextResult(
            text=text,
            provider=self.name,
            model=chosen,
            usage={
                "prompt_tokens": data.get("prompt_eval_count"),
                "completion_tokens": data.get("eval_count"),
                "done_reason": data.get("done_reason"),
            },
        )

    def text_generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        model: str | None = None,
        json_mode: bool = False,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> TextResult:
        return self._generate(
            prompt, None,
            system=system, model=model, json_mode=json_mode,
            temperature=temperature, max_tokens=max_tokens,
        )

    def vision_analyze(
        self,
        prompt: str,
        images: list[Any],
        *,
        system: str | None = None,
        model: str | None = None,
        json_mode: bool = False,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> TextResult:
        if not images:
            raise AIResponseError("vision_analyze 至少需要一张图片")
        return self._generate(
            prompt, list(images),
            system=system, model=model, json_mode=json_mode,
            temperature=temperature, max_tokens=max_tokens,
        )

    # ---------- 向量化 ----------
    def embedding(self, texts: list[str], *, model: str | None = None) -> EmbeddingResult:
        items = [str(t) for t in texts]
        if not items:
            return EmbeddingResult(vectors=[], provider=self.name, model=model or self.embedding_model)

        chosen = model or self.embedding_model
        if not chosen:
            raise AIUnavailable(
                "没有配置 Ollama 向量模型名（ai.ollama.embedding_model）"
                "，例如 `ollama pull nomic-embed-text`"
            )

        try:
            data = request_json(
                self._embed_url,
                payload={"model": chosen, "input": items},
                timeout=self.timeout,
            )
            vectors = data.get("embeddings")
        except AIResponseError as exc:
            # "模型不存在"和"接口不存在"都是 404，但处理方式完全不同：
            # 前者要提示用户去 pull，后者才该回退到旧接口。
            message = str(exc)
            if "model" in message and "not found" in message:
                raise AIResponseError(
                    f"{message}\n提示：本地还没有这个向量模型，先执行 `ollama pull {chosen}`"
                ) from exc
            # 老版本 Ollama 没有 /api/embed，回退到逐条 /api/embeddings
            if self._log:
                self._log.debug("回退到旧向量接口：%s", exc)
            vectors = [self._legacy_embed(text, chosen) for text in items]

        if not isinstance(vectors, list) or len(vectors) != len(items):
            raise AIResponseError(
                f"Ollama 返回的向量数量与输入不一致（期望 {len(items)}，得到 {vectors!r:.80}）"
            )
        return EmbeddingResult(
            vectors=[list(map(float, vector)) for vector in vectors],
            provider=self.name,
            model=chosen,
        )

    def _legacy_embed(self, text: str, model: str) -> list[float]:
        data = request_json(
            self._legacy_embed_url,
            payload={"model": model, "prompt": text},
            timeout=self.timeout,
        )
        vector = data.get("embedding")
        if not isinstance(vector, list):
            raise AIResponseError("旧向量接口响应里没有 embedding 字段")
        return list(vector)
