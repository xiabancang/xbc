"""插件机制验证插件（V1 运行时版）。

它存在的唯一目的：证明运行时真的能跑 —— 发现、加载、激活、注册工具与技能、
调用、停用（作用域释放）、卸载，以及公共能力调用与错误隔离。

业务插件应该照抄这里的结构，再填业务逻辑。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

# 插件唯一允许 import 的内核模块：基类。其他内核内部实现都不应被插件依赖。
from xbc.core.contract.plugin import XbcPlugin


class HelloXbcPlugin(XbcPlugin):
    """演示完整生命周期与注册机制的插件。"""

    def __init__(self) -> None:
        super().__init__()
        self._greeting = "你好，夏半仓工具箱"
        self._pings = 0

    # ---------- 生命周期 ----------
    def on_load(self) -> None:
        # 此时上下文与配置已注入，但还没有注册任何东西
        self.log.info("on_load：数据目录 %s", self.ctx.data_dir)

    def apply(self, ctx: Any, config: dict) -> None:
        """注册阶段。所有注册自动挂到插件作用域，停用时统一释放。"""
        self._greeting = str(config.get("greeting") or self._greeting)

        # 1) 注册工具。风险等级会被内核用于授权（read 免授权，write 需授权）
        ctx.tools.register(
            "hello_probe",
            self.hello_probe,
            description="报告宿主提供了哪些能力、以及当前环境是否就绪",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            risk="read",
        )
        ctx.tools.register(
            "hello_greet",
            self.hello_greet,
            description="生成问候文本并写入插件自己的数据目录",
            input_schema={
                "type": "object",
                "properties": {"name": {"type": "string", "default": "世界"}},
            },
            risk="write",
        )
        ctx.tools.register(
            "hello_fail",
            self.hello_fail,
            description="故意失败，用于验证单个插件出错不会拖垮宿主",
            input_schema={"type": "object", "properties": {}},
            risk="read",
        )

        # 2) 注册本插件自带的技能（每个子目录一个 SKILL.md）
        if ctx.package_dir is not None:
            ctx.skills.register_dir(ctx.package_dir / "skills")

        # 3) 订阅事件 —— 用 ctx.effect 挂到作用域，停用时自动解除
        ctx.effect(lambda: ctx.events.on("hello/ping", self._on_ping, owner=ctx.plugin_id))

        self.log.info("apply：已注册工具与技能")

    def on_unload(self) -> None:
        self.log.info("on_unload：清理完成")

    # ---------- 工具实现 ----------
    def hello_probe(self) -> dict[str, Any]:
        """报告宿主能力与环境状态（不做任何可能失败的操作）。"""
        return {
            "core_version": self.ctx.core_version,
            "api_version": self.ctx.api_version,
            "declared_capabilities": list(self.ctx.capabilities),
            "plugin_data_dir": str(self.ctx.data_dir),
            "ffmpeg_available": bool(self.ctx.ffmpeg.available()),
            "ai_providers": self.ctx.ai.status(),
            "config": dict(self.ctx.config),
            "pings": self._pings,
        }

    def hello_greet(self, name: str = "世界") -> dict[str, Any]:
        """写一个问候文件到自己的数据目录（通过公共能力，不直接 open）。"""
        runs = int(self.ctx.settings.get("runs", 0) or 0) + 1
        self.ctx.settings.set("runs", runs)

        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        text = f"{self._greeting}，{name}！\n写入时间：{stamp}\n这是第 {runs} 次调用。\n"
        target = self.ctx.files.write_text(self.ctx.data_dir / "hello.txt", text)
        return {"file": str(target), "runs": runs, "message": text.strip()}

    def hello_fail(self) -> None:
        """故意失败：用于验证"单个插件出错不会拖垮宿主和其他插件"。"""
        raise RuntimeError("hello_xbc 故意抛出的异常，用于验证错误隔离")

    # ---------- 事件 ----------
    def _on_ping(self, **_: Any) -> None:
        self._pings += 1
