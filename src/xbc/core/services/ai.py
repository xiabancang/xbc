"""AI 能力：把"用哪个模型"与"插件怎么写"彻底解耦。

V18 原型把 Ollama 地址、模型名、超时和全部参数硬编码在一个函数里。
这里拆成 AIProvider 接口 + 具体 Provider，插件只依赖接口。

这样将来接云端 API（通义/OpenAI 等）时，只需要新增一个 Provider 并改配置，
插件代码一行都不用动 —— 这是插件化里最关键的一处解耦。
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..errors import ServiceUnavailable


@runtime_checkable
class AIProvider(Protocol):
    """AI 提供者接口。任何满足这三个成员的类都可以注册进来。"""

    name: str

    def available(self) -> bool:
        """当前是否真的可用（网络/服务/凭证都就绪）。"""

    def generate(
        self,
        prompt: str,
        images: list[Path | str] | None = None,
        *,
        json_mode: bool = False,
        options: dict | None = None,
    ) -> str:
        """返回模型输出的原始文本。"""


class OllamaProvider:
    """本地 Ollama 提供者。

    请求体沿用 V18 中已验证可用的组合（format=json、temperature=0.1、
    num_ctx=8192、num_predict=700），但全部改为可配置，不再写死在代码里。
    """

    name = "ollama"

    def __init__(
        self,
        url: str = "http://localhost:11434/api/generate",
        model: str = "qwen2.5vl:3b",
        timeout: int = 180,
        options: dict | None = None,
        logger: Any = None,
    ) -> None:
        self.url = url
        self.model = model
        self.timeout = int(timeout)
        self.options = dict(options or {})
        self._log = logger

    # ---------- 可用性 ----------
    @property
    def _tags_url(self) -> str:
        base = self.url.split("/api/", 1)[0].rstrip("/")
        return f"{base}/api/tags"

    def available(self) -> bool:
        """探测 Ollama 服务是否在跑。探测失败一律返回 False，不抛异常。"""
        try:
            with urllib.request.urlopen(self._tags_url, timeout=2) as response:
                return response.status == 200
        except Exception:  # noqa: BLE001 - 探测失败就是不可用
            return False

    def list_models(self) -> list[str]:
        try:
            with urllib.request.urlopen(self._tags_url, timeout=3) as response:
                data = json.loads(response.read().decode("utf-8"))
            return [m.get("name", "") for m in data.get("models", [])]
        except Exception:  # noqa: BLE001
            return []

    # ---------- 推理 ----------
    def generate(
        self,
        prompt: str,
        images: list[Path | str] | None = None,
        *,
        json_mode: bool = False,
        options: dict | None = None,
    ) -> str:
        if not self.available():
            raise ServiceUnavailable(
                f"本地模型服务不可用：{self._tags_url} 无法连接。"
                "请确认已启动 Ollama；或在 config.json 中改用其他 ai.provider"
            )

        payload: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
        }
        if images:
            payload["images"] = [
                base64.b64encode(Path(image).read_bytes()).decode("ascii")
                for image in images
            ]
        if json_mode:
            payload["format"] = "json"
        merged_options = {**self.options, **(options or {})}
        if merged_options:
            payload["options"] = merged_options

        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise ServiceUnavailable(f"调用本地模型失败: {exc}") from exc

        return str(data.get("response", ""))

    def generate_json(
        self,
        prompt: str,
        images: list[Path | str] | None = None,
        *,
        options: dict | None = None,
    ) -> Any:
        """要求模型输出 JSON 并解析。解析失败会抛 ValueError。"""
        raw = self.generate(prompt, images, json_mode=True, options=options)
        return json.loads(raw)


class AIService:
    """AI 能力门面：插件通过它拿当前可用的 Provider，不关心具体是谁。"""

    def __init__(self, logger: Any = None) -> None:
        self._providers: dict[str, AIProvider] = {}
        self._default: str | None = None
        self._log = logger

    def register(self, provider: AIProvider, default: bool = False) -> None:
        self._providers[provider.name] = provider
        if default or self._default is None:
            self._default = provider.name

    def provider(self, name: str | None = None) -> AIProvider:
        key = name or self._default
        if not key or key not in self._providers:
            raise ServiceUnavailable(
                f"没有可用的 AI 提供者（请求的是 {key!r}，已注册: {sorted(self._providers)}）"
            )
        return self._providers[key]

    def providers(self) -> list[str]:
        return sorted(self._providers)

    def status(self) -> dict[str, bool]:
        """诊断用：每个 Provider 当前是否可用。"""
        result: dict[str, bool] = {}
        for name, provider in self._providers.items():
            try:
                result[name] = bool(provider.available())
            except Exception:  # noqa: BLE001
                result[name] = False
        return result

    def available(self) -> bool:
        try:
            return bool(self.provider().available())
        except ServiceUnavailable:
            return False

    def generate(
        self,
        prompt: str,
        images: list[Path | str] | None = None,
        *,
        provider: str | None = None,
        json_mode: bool = False,
        options: dict | None = None,
    ) -> str:
        return self.provider(provider).generate(
            prompt, images, json_mode=json_mode, options=options
        )


def build_ai_service(config: Any, logger: Any = None) -> AIService:
    """按配置装配 AI 服务。内核里唯一知道"有哪些 Provider 实现"的地方。"""
    service = AIService(logger=logger)
    provider_name = config.get("ai.provider", "ollama")

    if provider_name == "ollama":
        ollama_cfg = config.get("ai.ollama", {}) or {}
        service.register(
            OllamaProvider(
                url=ollama_cfg.get("url", "http://localhost:11434/api/generate"),
                model=ollama_cfg.get("model", "qwen2.5vl:3b"),
                timeout=int(ollama_cfg.get("timeout", 180)),
                options=dict(ollama_cfg.get("options") or {}),
                logger=logger,
            ),
            default=True,
        )
    # 将来：elif provider_name == "qwen_cloud": service.register(QwenCloudProvider(...))

    return service
