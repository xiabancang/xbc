"""AI 能力自检插件 —— 验收用，不交付。

## 它是干什么的

依次调用 Core 提供的三个 AI 能力，把每一步的结果原样报出来：

1. `ctx.ai.text_generate(...)`  → `TextResult`
2. `ctx.ai.vision_analyze(...)` → `VisionResult`
3. `ctx.ai.embedding(...)`      → `EmbeddingResult`

## 它为什么能证明"插件不知道 Provider 存在"

本文件里没有任何模型厂商、模型名、服务地址、端口，也没有任何网络库或模型 SDK 的
import —— 只有 `ctx.ai` 的三个方法。所以把配置里的 `ai.provider` 换成另一个实现，
本文件一个字节都不需要改。

这条不是靠"看一眼"保证的：`tests/test_ai_capability.py` 会

- 对这个目录下**所有文件**做哈希，两次不同 Provider 的运行结果必须逐字节一致；
- 用 `ast` 扫描 `plugins/` 下所有源码，出现模型 SDK 或网络库的 import 就失败。

## 关于错误

每一步都单独捕获 `AIError` 并记录 **provider 名 + 原因 + 检查建议**，然后继续跑下一步。
自检工具的价值就在于"把所有步骤的结果摆出来"，所以它不因为第一步失败就中断 ——
但失败会被**明确报出**，不会被吞掉。
"""

from __future__ import annotations

from typing import Any

from xbc.core.capabilities.ai import AIError
from xbc.core.contract.plugin import XbcPlugin

#: 默认提示词。刻意与任何业务无关 —— 这个插件的职责是验证能力层，不是办事。
DEFAULT_PROMPT = "用一句话介绍你自己。"
DEFAULT_TEXTS = ["第一段用于验证向量化的文本。", "第二段用于验证向量化的文本。"]


class AITestPlugin(XbcPlugin):
    """只通过 AI 能力层说话的插件。"""

    def apply(self, ctx: Any, config: dict) -> None:
        self._cfg = dict(config)

        ctx.tools.register(
            "ai_selftest",
            self.ai_selftest,
            description="依次调用文本生成、视觉理解、向量化，返回三步各自的结果",
            input_schema={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "文本生成用的提示词"},
                    "images": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "本地图片路径；留空则跳过视觉步骤",
                    },
                    "texts": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "向量化用的文本；留空用默认文本",
                    },
                },
            },
            output_schema={
                "type": "object",
                "required": ["provider", "steps"],
                "properties": {
                    "provider": {"type": "string"},
                    "steps": {"type": "array"},
                },
            },
            risk="read",
        )
        self.log.info("apply：AI 自检插件就绪")

    # ---------------- 工具 ----------------
    def ai_selftest(
        self,
        prompt: str | None = None,
        images: list[str] | None = None,
        texts: list[str] | None = None,
    ) -> dict[str, Any]:
        """依次调用三个能力，返回每一步的结果或明确的失败原因。"""
        chosen_prompt = prompt or str(self._cfg.get("text_prompt") or DEFAULT_PROMPT)
        chosen_texts = list(texts or DEFAULT_TEXTS)

        steps: list[dict[str, Any]] = []
        steps.append(self._text_step(chosen_prompt))
        steps.append(self._vision_step(list(images or [])))
        steps.append(self._embedding_step(chosen_texts))

        report = {
            "provider": self.ctx.ai.default_provider,
            "steps": steps,
            "ok": all(step["ok"] for step in steps),
        }
        self.log.info(
            "自检完成：provider=%s，成功 %d/%d",
            report["provider"],
            sum(1 for step in steps if step["ok"]),
            len(steps),
        )
        return report

    # ---------------- 三个步骤 ----------------
    def _text_step(self, prompt: str) -> dict[str, Any]:
        return self._run(
            "text_generate",
            lambda: self.ctx.ai.text_generate(prompt).to_dict(),
        )

    def _vision_step(self, images: list[str]) -> dict[str, Any]:
        if not images:
            return {
                "capability": "vision_analyze",
                "ok": False,
                "skipped": True,
                "error": "未提供本地图片路径，本步骤被跳过",
            }
        return self._run(
            "vision_analyze",
            lambda: self.ctx.ai.vision_analyze(images).to_dict(),
        )

    def _embedding_step(self, texts: list[str]) -> dict[str, Any]:
        def call() -> dict[str, Any]:
            result = self.ctx.ai.embedding(texts)
            return {
                **result.to_dict(),
                # 向量本身很长（本机 768 维），额外给一份前几个数值的预览
                "vectors_preview": [
                    [round(value, 5) for value in vector[:4]] for vector in result.vectors
                ],
            }

        return self._run("embedding", call)

    # ---------------- 内部 ----------------
    def _run(self, capability: str, call: Any) -> dict[str, Any]:
        try:
            result = call()
        except AIError as exc:
            self.log.error("%s 失败：%s", capability, exc)
            return {"capability": capability, "ok": False, "error": str(exc)}
        # 成功的步骤也要打印结果 —— 否则"打印结果"这件事只剩失败时才会发生
        self.log.info("%s 成功 → %s", capability, self._summarize(capability, result))
        return {"capability": capability, "ok": True, "result": result}

    @staticmethod
    def _summarize(capability: str, result: dict[str, Any]) -> str:
        """把一步的结果压成一行，便于在控制台直接看。"""
        if capability == "text_generate":
            text = str(result.get("text", "")).replace("\n", " ")
            return (
                f"model={result.get('model')} "
                f"usage={result.get('usage')} "
                f"text={text[:80]}"
            )
        if capability == "vision_analyze":
            description = str(result.get("description", "")).replace("\n", " ")
            return (
                f"model={result.get('model')} "
                f"description={description[:80]} "
                f"labels={result.get('labels')}"
            )
        return (
            f"model={result.get('model')} "
            f"count={result.get('count')} dim={result.get('dim')}"
        )
