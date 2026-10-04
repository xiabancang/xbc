"""AI 能力门面（TASK-007 第 1 项）。

## 插件唯一能用的 AI 入口

业务插件**不允许**直接调模型、直接发 HTTP、直接 import 某个模型的 SDK。
它们只能通过 `ctx.ai`（本对象）说话：

```python
result = ctx.ai.text_generate("写一句自我介绍")
info = ctx.ai.vision_analyze("描述这张图", images=[path], json_mode=True)
vectors = ctx.ai.embedding(["第一段", "第二段"])
```

这样做的实际收益（不是口号）：

1. **换模型不改插件** —— Provider 是装配细节，配置里换个名字就行；
2. 超时、重试策略、密钥、用量统计都在一处，不必每个插件重写；
3. 出问题时能说清"是谁、用什么模型、生成了什么"。

## 能力路由

`provider=None` 时按**能力**挑：先看默认 Provider 支不支持，不支持就找其他注册了的。
如果一个都没有，抛 `AIUnsupported` 并列出**谁支持这个能力** —— 而不是等网络请求失败。

## 装配

`build_ai_service()` 遍历**注册表**里的工厂（`registry.py`），
每个工厂自己决定"这轮要不要出现"。所以增加厂商是加一个工厂 + 登记一行，
内核其他地方与插件都不需要动。
"""

from __future__ import annotations

from typing import Any, Callable

from . import providers as _builtin_providers  # noqa: F401 - 导入即完成内置 Provider 登记
from .registry import ProviderSettings, provider_factories
from .request import EmbeddingRequest, TextRequest, VisionRequest
from .types import (
    AICapability,
    AIError,
    AIUnavailable,
    AIUnsupported,
    EmbeddingResult,
    TextResult,
)

Disposer = Callable[[], Any]


class AIService:
    """AI 能力门面。一个进程一份，由 `AppContext` 装配。"""

    def __init__(self, logger: Any = None) -> None:
        self._providers: dict[str, Any] = {}
        self._default: str | None = None
        self._log = logger

    # ---------------- 装配 ----------------
    def register(self, provider: Any, *, default: bool = False) -> Disposer:
        """注册一个 Provider。返回 disposer（可挂到作用域上）。"""
        if not provider.name:
            raise AIError("Provider 必须有 name")
        self._providers[provider.name] = provider
        if default or self._default is None:
            self._default = provider.name
        return lambda: self._providers.pop(provider.name, None)

    def set_default(self, name: str) -> None:
        if name not in self._providers:
            raise AIUnavailable(f"未注册的 Provider：{name!r}；已注册: {self.providers()}")
        self._default = name

    @property
    def default_provider(self) -> str:
        return self._default or ""

    def providers(self) -> list[str]:
        return sorted(self._providers)

    def provider(self, name: str | None = None, *, capability: AICapability | None = None) -> Any:
        """取一个 Provider。指定 `capability` 时会校验它确实支持该能力。"""
        if name is not None:
            found = self._providers.get(name)
            if found is None:
                raise AIUnavailable(f"没有名为 {name!r} 的 Provider；已注册: {self.providers()}")
            if capability is not None and not found.supports(capability):
                raise AIUnsupported(
                    f"Provider {name!r} 不支持 {capability}；"
                    f"支持 {capability} 的有: {self._providers_for(capability)}"
                )
            return found

        if capability is None:
            if self._default and self._default in self._providers:
                return self._providers[self._default]
            raise AIUnavailable(f"没有可用的 AI Provider；已注册: {self.providers()}")

        candidates = self._providers_for(capability)
        if not candidates:
            raise AIUnsupported(
                f"没有 Provider 支持 {capability}；已注册: {self.providers()}"
            )
        if self._default in candidates:
            return self._providers[self._default]
        return self._providers[candidates[0]]

    def _providers_for(self, capability: AICapability) -> list[str]:
        return sorted(name for name, p in self._providers.items() if p.supports(capability))

    # ---------------- 诊断 ----------------
    def capabilities(self, name: str | None = None) -> list[str]:
        provider = self.provider(name)
        return sorted(str(c) for c in provider.capabilities)

    def supports(self, capability: AICapability | str, provider: str | None = None) -> bool:
        want = capability if isinstance(capability, AICapability) else AICapability(str(capability))
        try:
            return self.provider(provider).supports(want)
        except AIError:
            return False

    def status(self, *, probe: bool = False) -> dict[str, dict[str, Any]]:
        """每个 Provider 的能力与状态。

        **默认不联网**（`probe=False`）：只报告"注册了谁、各自支持什么、配置齐不齐"。
        需要真实可用性时显式传 `probe=True`（`doctor` 就是这么做的）。

        早先的版本默认做活性探测，结果一次状态查询要 4 秒 —— 状态查询会被放在
        热路径上，这个代价不能接受。
        """
        report: dict[str, dict[str, Any]] = {}
        for name, provider in sorted(self._providers.items()):
            try:
                report[name] = provider.describe(probe=probe)
            except Exception as exc:  # noqa: BLE001 - 诊断不该因为一个 Provider 崩掉
                report[name] = {
                    "name": name, "available": False, "configured": False,
                    "capabilities": [], "models": [], "error": str(exc),
                }
            report[name]["default"] = name == self._default
        return report

    def available(self, capability: AICapability | None = None) -> bool:
        """**会真的连一下服务**。明确问"现在能不能用"时才调用。"""
        try:
            return bool(self.provider(capability=capability).available())
        except AIError:
            return False

    # ---------------- 三个能力入口 ----------------
    def text_generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        json_mode: bool = False,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> TextResult:
        """文本生成。"""
        chosen = self.provider(provider, capability=AICapability.TEXT)
        return chosen.text_generate(
            TextRequest(
                prompt=prompt,
                system=system,
                model=model,
                json_mode=json_mode,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        )

    def vision_analyze(
        self,
        prompt: str,
        images: list[Any],
        *,
        system: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        json_mode: bool = False,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> TextResult:
        """视觉理解。

        `images` 可以是路径字符串、`Path` 或原始字节；由内核负责读文件与编码。

        **注意**：传路径时，内核会替插件读文件。因此需要"看图"的插件
        应当同时声明 `files` 能力 —— 这是当前版本已知且接受的取舍
        （与 `ffmpeg` 能力接受路径一致）。
        """
        chosen = self.provider(provider, capability=AICapability.VISION)
        return chosen.vision_analyze(
            VisionRequest(
                prompt=prompt,
                system=system,
                model=model,
                json_mode=json_mode,
                temperature=temperature,
                max_tokens=max_tokens,
                images=list(images),
            )
        )

    def embedding(
        self,
        texts: list[str] | str,
        *,
        provider: str | None = None,
        model: str | None = None,
    ) -> EmbeddingResult:
        """向量化。传单个字符串也可以。"""
        items = [texts] if isinstance(texts, str) else list(texts)
        chosen = self.provider(provider, capability=AICapability.EMBEDDING)
        return chosen.embedding(EmbeddingRequest(texts=items, model=model))

    # ---------------- 兼容旧接口 ----------------
    def generate(
        self,
        prompt: str,
        images: list[Any] | None = None,
        *,
        provider: str | None = None,
        json_mode: bool = False,
        options: dict | None = None,
    ) -> str:
        """旧版接口（返回纯文本）。保留是为了不破坏已有调用方。

        新代码请用 `text_generate` / `vision_analyze`，它们会返回带
        provider / model / usage 的结构化结果。
        """
        temperature = (options or {}).get("temperature")
        max_tokens = (options or {}).get("num_predict")
        if images:
            return self.vision_analyze(
                prompt, images,
                provider=provider, json_mode=json_mode,
                temperature=temperature, max_tokens=max_tokens,
            ).text
        return self.text_generate(
            prompt,
            provider=provider, json_mode=json_mode,
            temperature=temperature, max_tokens=max_tokens,
        ).text


def build_ai_service(config: Any, logger: Any = None, secrets: Any = None) -> AIService:
    """按配置装配 AI 服务。

    遍历注册表里的工厂：每个工厂拿到自己的配置段与全局模型名，
    自己决定"这轮要不要出现"（返回 None 就是跳过）。

    加一个厂商 = 加一个工厂 + 登记一行；**本函数不需要改**。
    """
    service = AIService(logger=logger)
    provider_name = str(config.get("ai.provider", "ollama") or "ollama")
    global_model = str(config.get("ai.model", "") or "")
    global_embedding_model = str(config.get("ai.embedding_model", "") or "")

    for name, factory in provider_factories().items():
        settings = ProviderSettings(
            name=name,
            options=dict(config.get(f"ai.{name}", {}) or {}),
            global_model=global_model,
            global_embedding_model=global_embedding_model,
            secrets=secrets,
            logger=logger,
        )
        try:
            provider = factory(settings)
        except Exception as exc:  # noqa: BLE001 - 一个 Provider 装配失败不该拖垮内核
            if logger is not None:
                logger.error("Provider %s 装配失败，已跳过：%s", name, exc)
            continue
        if provider is None:
            continue
        service.register(provider, default=(name == provider_name))

    if provider_name and provider_name not in service.providers() and logger is not None:
        logger.warning(
            "ai.provider 指定了 %r，但它没有注册成功（多半是没配置齐全）；"
            "当前可用：%s",
            provider_name, service.providers() or "（无）",
        )

    return service
