"""内置 Provider 实现与登记。

## 增加一个内置 Provider 要做什么

1. 在本目录加一个模块，定义 `ModelProvider` 子类；
2. 写一个工厂 `(ProviderSettings) -> ModelProvider | None`（返回 `None` = 没配置，跳过）；
3. 在下面的 `_BUILTIN_FACTORIES` 里登记一行。

**插件侧完全不需要改动** —— 插件只认 `ctx.ai`，永远不会知道自己底下是哪个厂商。

## 已经预留但尚未内置的

`openai_compatible` 是一个**通道**而非单一厂商：DeepSeek / OpenAI / Claude /
自建网关都走它，只需改 `base_url` 与模型名。所以"再加一个厂商"通常根本不是加代码，
而是改配置。
"""

from __future__ import annotations

from typing import Any

from ..registry import ProviderSettings, register_provider_factory
from ..types import AICapability
from .ollama import OllamaProvider
from .openai_compatible import OpenAICompatibleProvider

__all__ = ["OllamaProvider", "OpenAICompatibleProvider"]


def _ollama_factory(settings: ProviderSettings) -> Any:
    """本地 Ollama：总是注册（本地优先，开箱即用）。"""
    options = settings.options
    return OllamaProvider(
        url=str(options.get("url", "http://127.0.0.1:11434") or "http://127.0.0.1:11434"),
        model=settings.resolve_model(AICapability.TEXT),
        embedding_model=settings.resolve_model(AICapability.EMBEDDING),
        timeout=int(options.get("timeout", 180) or 180),
        options=dict(options.get("options") or {}),
        logger=settings.logger,
    )


def _openai_compatible_factory(settings: ProviderSettings) -> Any:
    """OpenAI 兼容通道：**没配 base_url 就跳过**，不影响本地开箱体验。"""
    options = settings.options
    base = str(options.get("base_url", "") or "").strip()
    if not base:
        return None

    secret_name = str(options.get("api_key_secret", "") or "")
    api_key = ""
    if settings.secrets is not None and secret_name:
        api_key = settings.secrets.get_secret(secret_name) or ""

    return OpenAICompatibleProvider(
        base_url_value=base,
        model=settings.resolve_model(AICapability.TEXT),
        embedding_model=settings.resolve_model(AICapability.EMBEDDING),
        api_key=api_key,
        timeout=int(options.get("timeout", 120) or 120),
        logger=settings.logger,
    )


#: 内置 Provider 登记表。加一行就多一个厂商，插件无需改动。
_BUILTIN_FACTORIES = {
    "ollama": _ollama_factory,
    "openai_compatible": _openai_compatible_factory,
}

for _name, _factory in _BUILTIN_FACTORIES.items():
    register_provider_factory(_name, _factory)
