"""Ollama Provider —— 本地模型的第一实现（真实可用）。

对接 Ollama 的 HTTP API：

| 能力 | 端点 |
|---|---|
| 可用性 / 模型列表 | `GET /api/tags` |
| 文本生成 | `POST /api/generate`（`stream: false`） |
| 视觉理解 | `POST /api/generate`（`images` 传 base64） |
| 向量化 | `POST /api/embed`，失败时回退旧接口 `/api/embeddings` |

生成参数（`temperature` / `num_ctx` / `num_predict`）来自配置的 `ai.options`，
不在代码里写死。
"""

from __future__ import annotations

from typing import Any

from ..provider import (
    ModelProvider,
    base_url,
    encode_image,
    get_json,
    parse_vision_payload,
    probe_available,
    request_json,
    vision_prompt,
)
from ..request import EmbeddingRequest, TextRequest, VisionRequest
from ..types import (
    AICapability,
    AIResponseError,
    AIUnavailable,
    EmbeddingResult,
    TextResult,
    VisionAnswer,
    VisionResult,
)

#: 这些键属于"怎么连"而不是"怎么生成"，不能塞进 Ollama 的 options
_NON_GENERATION_KEYS = {
    "timeout", "url", "base_url", "api_key_secret", "model", "embedding_model",
}


class OllamaProvider(ModelProvider):
    name = "ollama"
    capabilities = frozenset({AICapability.TEXT, AICapability.VISION, AICapability.EMBEDDING})

    def __init__(
        self,
        url: str = "http://127.0.0.1:11434",
        model: str = "",
        embedding_model: str = "",
        timeout: int = 180,
        options: dict[str, Any] | None = None,
        logger: Any = None,
    ) -> None:
        # 兼容更早的配置形态：url 里可能带着 /api/generate
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

    # ---------- 错误信息（provider 名 + 原因 + 检查建议）----------
    def _unavailable(self, reason: str) -> AIUnavailable:
        return AIUnavailable(
            f"provider={self.name} 不可用\n"
            f"原因：{reason}\n"
            f"检查建议：1) 确认 Ollama 已启动（`ollama serve`）；"
            f"2) 确认 {self.base} 可访问；"
            f"3) 或把 config.json 的 ai.provider 改为其他 Provider。"
        )

    def _require_model(self, model: str, *, embedding: bool = False) -> str:
        if model:
            return model
        key = "ai.embedding_model" if embedding else "ai.model"
        # 示例值按能力区分：把对话模型的示例给向量模型用，会让人照做之后继续失败。
        example = '"embedding_model": "<向量模型名>"' if embedding else '"model": "<模型名>"'
        raise AIUnavailable(
            f"provider={self.name} 缺少模型配置\n"
            f"原因：{key} 未设置，无法确定要调用哪个模型\n"
            f"检查建议：在 config.json 里设置 {key}（例如 {example}），"
            "并用 `ollama list` 查看本地已下载的模型。"
        )

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

    # ---------- 调用 ----------
    def _post(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        """统一的出站调用，把"连不上"翻译成带排查建议的错误。

        **不做事前探测**：那会白多一个往返；而且本机实测"连没人监听的端口"不会立刻
        被拒、而是挂满超时（2s），代价直接加到每次调用上。服务没起时，下面的 HTTP
        调用本来就会立刻给出答案。
        """
        try:
            return request_json(url, payload=payload, timeout=self.timeout)
        except AIUnavailable as exc:
            raise self._unavailable(str(exc)) from exc

    def _generation_options(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in self.options.items()
            if key not in _NON_GENERATION_KEYS
        }

    def _generate(
        self,
        prompt: str,
        *,
        system: str | None,
        images: list[str] | None,
        model: str,
        json_mode: bool,
    ) -> TextResult:
        payload: dict[str, Any] = {"model": model, "prompt": prompt, "stream": False}
        if system:
            payload["system"] = system
        if images:
            payload["images"] = [encode_image(image) for image in images]
        if json_mode:
            payload["format"] = "json"

        options = self._generation_options()
        if options:
            payload["options"] = options

        data = self._post(self._generate_url, payload)
        text = data.get("response")
        if not isinstance(text, str):
            raise AIResponseError(f"Ollama 响应里没有 response 字段：{str(data)[:200]}")
        return TextResult(
            text=text,
            provider=self.name,
            model=str(data.get("model") or model),
            usage={
                "prompt_tokens": data.get("prompt_eval_count"),
                "completion_tokens": data.get("eval_count"),
            },
        )

    # ---------- 三种能力 ----------
    def text_generate(self, request: TextRequest) -> TextResult:
        model = self._require_model(self.model)
        return self._generate(
            request.prompt, system=request.system, images=None, model=model, json_mode=False
        )

    def vision_analyze(self, request: VisionRequest) -> VisionResult | VisionAnswer:
        """描述模式返回 `VisionResult`，定向提问模式返回 `VisionAnswer`。"""
        if not request.images:
            raise AIResponseError("vision_analyze 至少需要一张本地图片路径")
        model = self._require_model(self.model)
        generated = self._generate(
            vision_prompt(request.question), system=None, images=list(request.images),
            model=model, json_mode=True,
        )
        if request.question:
            answer, labels = parse_vision_payload(generated.text, field="answer")
            return VisionAnswer(
                question=request.question,
                answer=answer,
                labels=labels,
                provider=self.name,
                model=generated.model,
                raw=generated.text,
                usage=generated.usage,
            )
        description, labels = parse_vision_payload(generated.text)
        return VisionResult(
            description=description,
            labels=labels,
            provider=self.name,
            model=generated.model,
            raw=generated.text,
            usage=generated.usage,
        )

    def embedding(self, request: EmbeddingRequest) -> EmbeddingResult:
        items = [str(text) for text in request.texts]
        if not items:
            return EmbeddingResult(vectors=[], provider=self.name, model=self.embedding_model)

        model = self._require_model(self.embedding_model, embedding=True)

        try:
            data = self._post(self._embed_url, {"model": model, "input": items})
            vectors = data.get("embeddings")
        except AIResponseError as exc:
            # "模型不存在"和"接口不存在"都是 404，但处理方式完全不同：
            # 前者要提示用户去 pull，后者才该回退到旧接口。
            message = str(exc)
            if "model" in message and "not found" in message:
                raise AIResponseError(
                    f"{message}\n检查建议：本地还没有这个向量模型，先执行 `ollama pull {model}`"
                ) from exc
            if self._log:
                self._log.debug("回退到旧向量接口：%s", exc)
            vectors = [self._legacy_embed(text, model) for text in items]

        if not isinstance(vectors, list) or len(vectors) != len(items):
            raise AIResponseError(
                f"Ollama 返回的向量数量与输入不一致（期望 {len(items)}，得到 {str(vectors)[:80]}）"
            )
        return EmbeddingResult(
            vectors=[list(map(float, vector)) for vector in vectors],
            provider=self.name,
            model=model,
        )

    def _legacy_embed(self, text: str, model: str) -> list[float]:
        data = self._post(self._legacy_embed_url, {"model": model, "prompt": text})
        vector = data.get("embedding")
        if not isinstance(vector, list):
            raise AIResponseError("旧向量接口响应里没有 embedding 字段")
        return list(vector)
