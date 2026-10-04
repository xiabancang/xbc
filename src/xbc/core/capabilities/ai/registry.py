"""Provider 注册表 —— 让"以后增加 DeepSeek / OpenAI / Claude"不必改内核别处。

## 怎么加一个新 Provider

1. 在 `providers/` 下加一个模块，定义 `ModelProvider` 子类；
2. 写一个工厂函数 `(ProviderSettings) -> ModelProvider | None`，
   返回 `None` 表示"没配置，跳过"；
3. 在 `providers/__init__.py` 里 `register_provider_factory("名字", 工厂)`。

**插件侧一行都不用改** —— 插件只认 `ctx.ai`。

第三方（比如某个企业内部网关）也可以调用 `register_provider_factory()` 自行登记，
内核不需要为它做任何修改。

## 模型名解析顺序

`ai.model`（全局）→ `ai.<provider>.model`（该 Provider 专属）→ Provider 内置默认。

全局项优先，是因为它才是用户直接操作的旋钮：配置里写 `ai.model: qwen` 就应该生效，
而不该被内置的默认模型名悄悄盖掉。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .types import AICapability

#: 工厂返回 None 表示"该 Provider 没配置，本轮跳过"
ProviderFactory = Callable[["ProviderSettings"], Any]

_FACTORIES: dict[str, ProviderFactory] = {}


@dataclass
class ProviderSettings:
    """交给工厂的一份"该 Provider 的全部输入"。"""

    name: str
    options: dict[str, Any] = field(default_factory=dict)
    #: `ai.model` —— 全局模型名（用户直接设置的旋钮）
    global_model: str = ""
    #: `ai.embedding_model` —— 全局向量模型名
    global_embedding_model: str = ""
    secrets: Any = None
    logger: Any = None

    def resolve_model(self, capability: AICapability) -> str:
        """按优先级解析模型名。"""
        if capability is AICapability.EMBEDDING:
            return self.global_embedding_model or str(self.options.get("embedding_model") or "")
        return self.global_model or str(self.options.get("model") or "")


def register_provider_factory(name: str, factory: ProviderFactory) -> None:
    """登记一个 Provider 工厂。同名会覆盖（便于测试替换）。"""
    if not name:
        raise ValueError("Provider 名不能为空")
    _FACTORIES[name] = factory


def unregister_provider_factory(name: str) -> None:
    _FACTORIES.pop(name, None)


def provider_factories() -> dict[str, ProviderFactory]:
    """当前已登记的工厂（按名字排序，保证装配顺序稳定）。"""
    return {name: _FACTORIES[name] for name in sorted(_FACTORIES)}


__all__ = [
    "ProviderFactory",
    "ProviderSettings",
    "provider_factories",
    "register_provider_factory",
    "unregister_provider_factory",
]
