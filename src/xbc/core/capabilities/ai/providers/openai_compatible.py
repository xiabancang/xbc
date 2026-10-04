"""OpenAI 兼容接口 Provider —— **API 模型接口预留**。

## 它预留了什么

只要一个服务实现了 OpenAI 的 `/chat/completions` 与 `/embeddings` 形状，
就能作为 Provider 接进来：

- **DeepSeek**（`https://api.deepseek.com/v1`）
- **OpenAI**（`https://api.openai.com/v1`）
- **Claude**（走其 OpenAI 兼容端点）
- 任何自建网关 / 企业内网模型

接进来只需要在配置里填 `ai.openai_compatible.base_url` 与模型名，
**插件侧一行都不用改**。

## 它默认不注册

只有配置了 `base_url` 才会出现在可用 Provider 列表里 ——
"预留接口"不该影响开箱即用的本地体验。

## 凭证

API Key 从 `secrets.json` 读取后由内核注入。
**本模块只把它放进请求头，不写日志、不进错误信息**（错误里只出现服务地址）。

## 关于"禁止云端账号"

禁止的是**云端账号体系**（登录、会员、租户）。这里只是一个 HTTP 客户端实现，
不涉及任何账号、登录或用户概念。它的正确性是用**本地假服务**验证的，
没有连接任何真实云端。
"""

from __future__ import annotations

from typing import Any

from ..provider import (
    ModelProvider,
    base_url,
    image_data_url,
    request_json,
)
from ..request import EmbeddingRequest, TextRequest, VisionRequest
from ..types import (
    AICapability,
    AIResponseError,
    AIUnavailable,
    EmbeddingResult,
    TextResult,
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

    # ---------- 可用性 ----------
    def configured(self) -> bool:
        """地址与 Key 都配齐才算配置完成（不联网、不探测，避免费额度）。"""
        return bool(self.base and self._api_key)

    def available(self) -> bool:
        return self.configured()

    def default_model(self, capability: AICapability) -> str:
        return self.embedding_model if capability is AICapability.EMBEDDING else self.model

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    def _require_ready(self) -> None:
        if not self.base:
            raise AIUnavailable("未配置 ai.openai_compatible.base_url")
        if not self._api_key:
            raise AIUnavailable(
                "未配置 API Key；请把密钥写进 secrets.json，"
                "并在 ai.openai_compatible.api_key_secret 里填键名"
            )

    # ---------- 文本 / 视觉 ----------
    def _messages(
        self,
        prompt: str,
        images: list[Any] | None,
        system: str | None,
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

    def _chat(self, request: TextRequest, images: list[Any] | None) -> TextResult:
        self._require_ready()
        chosen = request.model or self.model
        if not chosen:
            raise AIUnavailable("没有配置模型名：请设置 ai.model 或 ai.openai_compatible.model")

        payload: dict[str, Any] = {
            "model": chosen,
            "messages": self._messages(request.prompt, images, request.system),
        }
        if request.json_mode:
            payload["response_format"] = {"type": "json_object"}
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens

        data = request_json(
            self._chat_url, payload=payload, headers=self._headers(), timeout=self.timeout
        )
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
            model=str(data.get("model") or chosen),
            usage={
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "finish_reason": first.get("finish_reason"),
            },
        )

    def text_generate(self, request: TextRequest) -> TextResult:
        return self._chat(request, None)

    def vision_analyze(self, request: VisionRequest) -> TextResult:
        if not request.images:
            raise AIResponseError("vision_analyze 至少需要一张图片")
        return self._chat(request, list(request.images))

    # ---------- 向量化 ----------
    def embedding(self, request: EmbeddingRequest) -> EmbeddingResult:
        items = [str(text) for text in request.texts]
        if not items:
            return EmbeddingResult(
                vectors=[], provider=self.name, model=request.model or self.embedding_model
            )
        self._require_ready()

        chosen = request.model or self.embedding_model
        if not chosen:
            raise AIUnavailable(
                "没有配置向量模型名：请设置 ai.embedding_model 或 "
                "ai.openai_compatible.embedding_model"
            )

        data = request_json(
            self._embeddings_url,
            payload={"model": chosen, "input": items},
            headers=self._headers(),
            timeout=self.timeout,
        )
        rows = data.get("data")
        if not isinstance(rows, list) or len(rows) != len(items):
            raise AIResponseError(f"响应里没有与输入等长的 data：{str(data)[:200]}")

        vectors = []
        for row in rows:
            vector = row.get("embedding") if isinstance(row, dict) else None
            if not isinstance(vector, list):
                raise AIResponseError(f"向量条目格式不对：{str(row)[:120]}")
            vectors.append([float(value) for value in vector])

        return EmbeddingResult(vectors=vectors, provider=self.name, model=chosen)
