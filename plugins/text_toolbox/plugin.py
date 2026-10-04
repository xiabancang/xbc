"""文本工具箱：第一个真实的业务插件（不含 AI、不含音视频）。

它的作用是**把运行时的每条链路都跑一遍真实逻辑**，而不是只做 hello world：

- `text_defaults` 证明**三层配置**装配正确（默认层 / 宿主层 / 用户层）；
- `text_stats` / `text_dedupe` 是纯函数式只读工具，证明工具契约与 JSON Schema 校验；
- `text_export` 是 `write` 风险工具，证明**内核侧的风险强制**（不授权就拒绝）；
- `skills/text-cleanup` 证明技能与工具的分工：技能是说明书，工具才是动作。
"""

from __future__ import annotations

import re
from typing import Any

from xbc.core.contract.plugin import XbcPlugin

_WORD_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")


class TextToolboxPlugin(XbcPlugin):
    """纯本地文本处理插件。"""

    def __init__(self) -> None:
        super().__init__()
        self._config: dict[str, Any] = {}

    # ---------- 生命周期 ----------
    def on_load(self) -> None:
        self.log.info("on_load：文本工具箱准备就绪")

    def apply(self, ctx: Any, config: dict) -> None:
        self._config = dict(config)

        ctx.tools.register(
            "text_defaults",
            self.text_defaults,
            description="查看本插件当前生效的配置",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            risk="read",
        )
        ctx.tools.register(
            "text_stats",
            self.text_stats,
            description="统计文本的字符数、行数、词数、空行数与最长行长度",
            input_schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            output_schema={
                "type": "object",
                "required": ["chars", "lines", "words", "blank_lines", "longest_line"],
                "properties": {
                    "chars": {"type": "integer"},
                    "lines": {"type": "integer"},
                    "words": {"type": "integer"},
                    "blank_lines": {"type": "integer"},
                    "longest_line": {"type": "integer"},
                },
            },
            risk="read",
        )
        ctx.tools.register(
            "text_dedupe",
            self.text_dedupe,
            description="按行去重并保留首次出现的顺序",
            input_schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            risk="read",
        )
        ctx.tools.register(
            "text_export",
            self.text_export,
            description="按当前配置清洗文本并导出到插件数据目录",
            input_schema={
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "filename": {"type": "string", "default": "cleaned.txt"},
                },
                "required": ["text"],
            },
            risk="write",
        )

        if ctx.package_dir is not None:
            ctx.skills.register_dir(ctx.package_dir / "skills")

        # 每次激活发一个通知，演示事件（只用于通知，不用于请求-响应）
        ctx.events.emit("text_toolbox/activated", tools=len(ctx.tools.names()))
        self.log.info("apply：文本工具箱已激活，配置 = %s", self._config)

    # ---------- 工具实现 ----------
    def text_defaults(self) -> dict[str, Any]:
        """返回当前生效配置。用于验证三层配置是否按预期生效。"""
        return {
            "config": dict(self._config),
            "strip_blank_lines": bool(self._config.get("strip_blank_lines", True)),
            "max_lines": int(self._config.get("max_lines", 1000)),
        }

    def text_stats(self, text: str) -> dict[str, int]:
        lines = text.splitlines()
        words = _WORD_RE.findall(text)
        longest = max((len(line) for line in lines), default=0)
        return {
            "chars": len(text),
            "lines": len(lines),
            "words": len(words),
            "blank_lines": sum(1 for line in lines if not line.strip()),
            "longest_line": longest,
        }

    def text_dedupe(self, text: str) -> dict[str, Any]:
        seen: set[str] = set()
        kept: list[str] = []
        removed = 0
        for line in text.splitlines():
            if line in seen:
                removed += 1
                continue
            seen.add(line)
            kept.append(line)
        return {"text": "\n".join(kept), "kept": len(kept), "removed": removed}

    def text_export(self, text: str, filename: str = "cleaned.txt") -> dict[str, Any]:
        """按配置清洗并导出。这是 write 风险工具 —— 未授权时内核会拒绝执行。"""
        cleaned = self._clean(text)
        self.ctx.files.ensure_dir(self.ctx.data_dir)
        target = self.ctx.files.write_text(self.ctx.data_dir / filename, cleaned)

        runs = int(self.ctx.settings.get("exports", 0) or 0) + 1
        self.ctx.settings.set("exports", runs)

        return {"file": str(target), "bytes": len(cleaned.encode("utf-8")), "exports": runs}

    # ---------- 内部 ----------
    def _clean(self, text: str) -> str:
        lines = text.splitlines()
        if self._config.get("strip_blank_lines", True):
            lines = [line for line in lines if line.strip()]

        deduped: list[str] = []
        seen: set[str] = set()
        for line in lines:
            if line in seen:
                continue
            seen.add(line)
            deduped.append(line)

        max_lines = int(self._config.get("max_lines", 1000))
        if max_lines > 0:
            deduped = deduped[:max_lines]
        return "\n".join(deduped) + ("\n" if deduped else "")
