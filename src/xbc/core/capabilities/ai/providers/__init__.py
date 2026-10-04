"""内置 Provider 实现。

只有两个，都是**真实实现**：

| Provider | 说明 |
|---|---|
| `ollama` | 本地模型（第一实现，本地优先） |
| `openai_compatible` | 兼容 OpenAI 形状的通道（第二实现，用于验证可替换性） |

**这里没有 Provider 注册表。** 内核只装配这两个名字，加第三个厂商要改
`service.py` 里那一处显式装配 —— 但**仍然不需要改任何插件**。

这是刻意的：为一个尚不存在的需求预先搭一套注册机制，正属于任务书禁止的
"为未来预留的扩展点"。
"""

from __future__ import annotations

from .ollama import OllamaProvider
from .openai_compatible import OpenAICompatibleProvider

__all__ = ["OllamaProvider", "OpenAICompatibleProvider"]
