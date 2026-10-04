"""AI 能力探针：TASK-007 的验收插件。

## 它证明什么

这个插件的全部意义在于**它里面没有任何"模型"的痕迹**：

- 不 import `ollama` / `openai` / `requests` / `urllib` / `http.client`
- 不知道 Provider 叫什么、模型叫什么、跑在哪个端口
- 只调用 `ctx.ai.text_generate` / `vision_analyze` / `embedding`

所以「**替换模型 Provider，不修改插件代码**」不是承诺，而是这个文件的直接结果：
把配置里的 `ai.provider` 从 `ollama` 换成 `openai_compatible`，
本文件一个字节都不需要改。

有测试专门守着这条线：`tests/test_ai_capability.py` 里有一条会扫描
所有插件的源码，发现直接调用模型/HTTP 就失败。
"""

from __future__ import annotations

from typing import Any

from xbc.core.contract.plugin import XbcPlugin


class AIProbePlugin(XbcPlugin):
    """只通过 AI 能力层说话的插件。"""

    def __init__(self) -> None:
        super().__init__()
        self._cfg: dict[str, Any] = {}

    def apply(self, ctx: Any, config: dict) -> None:
        self._cfg = dict(config)

        ctx.tools.register(
            "ai_status",
            self.ai_status,
            description="查看已注册的 AI Provider、各自支持的能力与默认 Provider",
            input_schema={
                "type": "object",
                "properties": {
                    "probe": {
                        "type": "boolean",
                        "description": "是否真实探测模型服务（会联网，默认 false）",
                    }
                },
            },
            output_schema={
                "type": "object",
                "required": ["providers", "default_provider", "configured", "probed"],
                "properties": {
                    "providers": {"type": "object"},
                    "default_provider": {"type": "string"},
                    "configured": {"type": "boolean"},
                    "probed": {"type": "boolean"},
                    "available": {"type": ["boolean", "null"]},
                },
            },
            risk="read",
        )

        text_schema = {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "minLength": 1},
                "system": {"type": "string"},
                "json_mode": {"type": "boolean"},
                "provider": {"type": "string"},
            },
            "required": ["prompt"],
        }
        text_output = {
            "type": "object",
            "required": ["text", "provider", "model"],
            "properties": {
                "text": {"type": "string"},
                "provider": {"type": "string"},
                "model": {"type": "string"},
                "usage": {"type": "object"},
            },
        }

        ctx.tools.register(
            "ai_text", self.ai_text,
            description="调用 AI 能力层的文本生成（text_generate）",
            input_schema=text_schema, output_schema=text_output, risk="read",
        )
        ctx.tools.register(
            "ai_vision", self.ai_vision,
            description="调用 AI 能力层的视觉理解（vision_analyze）",
            input_schema={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "minLength": 1},
                    "images": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                    "json_mode": {"type": "boolean"},
                    "provider": {"type": "string"},
                },
                "required": ["prompt", "images"],
            },
            output_schema=text_output,
            risk="read",
        )
        ctx.tools.register(
            "ai_embed", self.ai_embed,
            description="调用 AI 能力层的向量化（embedding）",
            input_schema={
                "type": "object",
                "properties": {
                    "texts": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                    "provider": {"type": "string"},
                },
                "required": ["texts"],
            },
            output_schema={
                "type": "object",
                "required": ["vectors", "provider", "model", "count", "dimensions"],
                "properties": {
                    "vectors": {"type": "array"},
                    "provider": {"type": "string"},
                    "model": {"type": "string"},
                    "count": {"type": "integer"},
                    "dimensions": {"type": "integer"},
                },
            },
            risk="read",
        )

        self.log.info("apply：AI 探针就绪，配置 = %s", self._cfg)

    # ---------------- 工具 ----------------
    def ai_status(self, probe: bool = False) -> dict[str, Any]:
        """报告能力层的现状。

        **默认不联网** —— 状态查询要便宜。需要"现在到底能不能用"时传 `probe=true`。
        """
        providers = self.ctx.ai.status(probe=probe)
        return {
            "providers": providers,
            "default_provider": self.ctx.ai.default_provider,
            "configured": any(info.get("configured") for info in providers.values()),
            "probed": bool(probe),
            "available": (
                any(info.get("available") for info in providers.values()) if probe else None
            ),
        }

    def ai_text(
        self,
        prompt: str,
        system: str | None = None,
        json_mode: bool = False,
        provider: str | None = None,
    ) -> dict[str, Any]:
        result = self.ctx.ai.text_generate(
            self._with_prefix(prompt),
            system=system,
            json_mode=json_mode,
            provider=provider or self._preferred(),
        )
        return result.to_dict()

    def ai_vision(
        self,
        prompt: str,
        images: list[str],
        json_mode: bool = False,
        provider: str | None = None,
    ) -> dict[str, Any]:
        result = self.ctx.ai.vision_analyze(
            self._with_prefix(prompt),
            images,
            json_mode=json_mode,
            provider=provider or self._preferred(),
        )
        return result.to_dict()

    def ai_embed(self, texts: list[str], provider: str | None = None) -> dict[str, Any]:
        result = self.ctx.ai.embedding(texts, provider=provider or self._preferred())
        return result.to_dict()

    # ---------------- 内部 ----------------
    def _with_prefix(self, prompt: str) -> str:
        prefix = str(self._cfg.get("prompt_prefix") or "")
        return f"{prefix}{prompt}" if prefix else prompt

    def _preferred(self) -> str | None:
        value = str(self._cfg.get("preferred_provider") or "").strip()
        return value or None
