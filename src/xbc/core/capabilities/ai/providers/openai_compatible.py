"""OpenAI 兼容 Provider —— 第二实现，用来验证「Provider 可替换」。

只要一个服务实现了 OpenAI 的 `/chat/completions` 与 `/embeddings` 形状，
就能接进来：DeepSeek、OpenAI、Claude、自建网关，都走这一条通道，
差别只在 `base_url` 与模型名。

**它默认不出现**：只有配置了 `ai.openai_compatible.base_url` 才会被装配。
"本地模型优先"是硬约束，云端通道不该在用户没要求时冒出来。

## 凭证

API Key 从 `secrets.json` 读取后由内核注入。**本模块只把它放进请求头**，
不写日志、不进错误信息（错误里只出现服务地址）。

## 它是真实实现，不是预留接口

正确性由 `tests/test_ai_capability.py` 里的**本地 mock server** 逐字段验证：
请求路径、`Authorization` 头、多模态 content 结构、响应解析、缺密钥时不发请求。
全程不连接任何外部服务。
"""

from __future__ import annotations

from typing import Any

from ..provider import (
    ModelProvider,
    base_url,
    image_data_url,
    parse_vision_payload,
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


class OpenAICompatibleProvider(ModelProvider):
    name = "openai_compatible"
    capabilities = frozenset({AICapability.TEXT, AICapability.VISION, AICapability.EMBEDDING})

    def __init__(
        self,
        base_url_value: str = "",
        model: str = "",
        embedding_model: str = "",
        api_key: str = "",
        timeout: int = 120,
        logger: Any = None,
    ) -> None:
        self.base = base_url(base_url_value)
        self.model = str(model or "")
        self.embedding_model = str(embedding_model or "")
        self._api_key = str(api_key or "")
        self.timeout = int(timeout or 120)
        self._log = logger

    # ---------- 端点 ----------
    @property
    def _chat_url(self) -> str:
        return f"{self.base}/chat/completions"

    @property
    def _embeddings_url(self) -> str:
        return f"{self.base}/embeddings"

    # ---------- 错误信息（provider 名 + 原因 + 检查建议）----------
    def _unavailable(self, reason: str) -> AIUnavailable:
        return AIUnavailable(
            f"provider={self.name} 不可用\n"
            f"原因：{reason}\n"
            "检查建议：1) 确认 ai.openai_compatible.base_url 正确且服务可访问；"
            "2) 确认 secrets.json 里的 API Key 有效；"
            "3) 或把 config.json 的 ai.provider 改回 ollama。"
        )

    def _require_model(self, model: str, *, embedding: bool = False) -> str:
        if model:
            return model
        key = "ai.embedding_model" if embedding else "ai.model"
        example = '"embedding_model": "<向量模型名>"' if embedding else '"model": "<模型名>"'
        raise AIUnavailable(
            f"provider={self.name} 缺少模型配置\n"
            f"原因：{key} 未设置，无法确定要调用哪个模型\n"
            f"检查建议：在 config.json 里设置 {key}（例如 {example}），"
            "注意要写**该服务方**的模型名。"
        )

    # ---------- 可用性 ----------
    def configured(self) -> bool:
        """地址与 Key 都配齐才算配置完成（不联网、不探测，避免白费额度）。"""
        return bool(self.base and self._api_key)

    def available(self) -> bool:
        return self.configured()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    def _require_ready(self) -> None:
        if not self.base:
            raise self._unavailable("未配置 ai.openai_compatible.base_url")
        if not self._api_key:
            raise self._unavailable(
                "未配置 API Key（secrets.json 里没有对应的键）"
            )

    # ---------- 调用 ----------
    def _post(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return request_json(
                url, payload=payload, headers=self._headers(), timeout=self.timeout
            )
        except AIUnavailable as exc:
            raise self._unavailable(str(exc)) from exc

    def _messages(
        self, prompt: str, images: list[str] | None, system: str | None
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        if images:
            content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
            for image in images:
                content.append({"type": "image_url", "image_url": {"url": image_data_url(image)}})
            messages.append({"role": "user", "content": content})
        else:
            messages.append({"role": "user", "content": prompt})
        return messages

    def _chat(
        self, prompt: str, *, system: str | None, images: list[str] | None,
        model: str, json_mode: bool,
    ) -> TextResult:
        payload: dict[str, Any] = {"model": model, "messages": self._messages(prompt, images, system)}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        data = self._post(self._chat_url, payload)
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise AIResponseError(f"响应里没有 choices：{str(data)[:200]}")
        first = choices[0] if isinstance(choices[0], dict) else {}
        message = first.get("message")
        text = message.get("content") if isinstance(message, dict) else None
        if not isinstance(text, str):
            raise AIResponseError(f"响应里没有 message.content：{str(first)[:200]}")

        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        return TextResult(
            text=text,
            provider=self.name,
            model=str(data.get("model") or model),
            usage={
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
            },
        )

    # ---------- 三种能力 ----------
    def text_generate(self, request: TextRequest) -> TextResult:
        self._require_ready()
        model = self._require_model(self.model)
        return self._chat(
            request.prompt, system=request.system, images=None, model=model, json_mode=False
        )

    def vision_analyze(self, request: VisionRequest) -> VisionResult | VisionAnswer:
        """描述模式返回 `VisionResult`，定向提问模式返回 `VisionAnswer`。"""
        if not request.images:
            raise AIResponseError("vision_analyze 至少需要一张本地图片路径")
        self._require_ready()
        model = self._require_model(self.model)
        generated = self._chat(
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
        self._require_ready()
        model = self._require_model(self.embedding_model, embedding=True)

        data = self._post(self._embeddings_url, {"model": model, "input": items})
        rows = data.get("data")
        if not isinstance(rows, list) or len(rows) != len(items):
            raise AIResponseError(f"响应里没有与输入等长的 data：{str(data)[:200]}")

        vectors = []
        for row in rows:
            vector = row.get("embedding") if isinstance(row, dict) else None
            if not isinstance(vector, list):
                raise AIResponseError(f"向量条目格式不对：{str(row)[:120]}")
            vectors.append([float(value) for value in vector])

        return EmbeddingResult(vectors=vectors, provider=self.name, model=model)
