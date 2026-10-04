"""工具（Tool）注册表。

工具是** Agent 唯一能执行的东西**：一个带 JSON Schema 的可执行动作。

设计要点（方案 3.10 / M5）：

- 入参与出参都用 JSON Schema 校验；
- **错误二分**（学 MCP）：协议错误（工具不存在、参数不合法）与业务失败
  （执行抛异常）用不同的 `code` 表达，让 Agent 能够自纠；
- **风险等级只是提示**。内核**不依据插件自报的等级放行** ——
  `write` / `destructive` 工具必须经过授权回调，否则拒绝执行。
  这条来自 MCP 对 annotations 的规定：不可信来源的声明不得作为决策依据。

异常设计：模式名与分层注册表共用（内核 / 插件 / 用户三层 + 先到先得裁决）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ..contract.manifest import KNOWN_RISKS
from .schema import SchemaError, validate

# 错误码：协议级
CODE_NOT_FOUND = "tool_not_found"
CODE_INVALID_ARGS = "invalid_arguments"
CODE_DENIED = "consent_denied"
# 错误码：执行级
CODE_EXEC_FAILED = "execution_failed"
CODE_INVALID_OUTPUT = "invalid_output"

#: 需要用户授权的风险等级
RISKS_REQUIRING_CONSENT = ("write", "destructive")


@dataclass
class ToolSpec:
    """一个可被调用的工具。"""

    name: str
    handler: Callable[..., Any]
    description: str = ""
    input_schema: dict = field(default_factory=dict)
    output_schema: dict = field(default_factory=dict)
    risk: str = "read"
    owner: str = ""
    plugin_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "risk": self.risk,
            "owner": self.owner,
            "input_schema": self.input_schema,
        }

    def to_agent_dict(self) -> dict[str, Any]:
        """给 Agent 看的工具描述（对齐 MCP 的 tools/list 形态）。

        `annotations` 里放风险等级与归属 —— 但**它只是提示**。方案 3.10 已明确：
        内核不依据插件自报的等级放行，授权必须在执行点强制。
        （MCP 对 annotations 的规定与此一致：不可信来源的声明不得作为决策依据。）
        """
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
            "annotations": {"risk": self.risk, "owner": self.owner},
        }


@dataclass
class ToolResult:
    """统一的错误信封 `{code, message, detail}`。"""

    ok: bool
    value: Any = None
    code: str = ""
    message: str = ""
    detail: Any = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"ok": self.ok}
        if self.ok:
            data["value"] = self.value
        else:
            data["code"] = self.code
            data["message"] = self.message
            if self.detail is not None:
                data["detail"] = self.detail
        return data


class ToolRegistry:
    """工具注册与调用。分层裁决复用 LayeredRegistry 的规则。"""

    def __init__(self, logger: Any = None, hooks: Any = None, approver: Callable[..., bool] | None = None) -> None:
        from ..runtime.registry import LayeredRegistry  # 局部导入，避免循环依赖

        self._log = logger
        self._hooks = hooks
        self._approver = approver
        self._registry = LayeredRegistry("工具", logger)

    # ---------- 注册 ----------
    def register(
        self,
        spec: ToolSpec,
        *,
        layer: int | None = None,
        rank: int = 0,
    ) -> Callable[[], None]:
        from ..runtime.registry import Layer

        if spec.risk not in KNOWN_RISKS:
            raise ValueError(f"工具 {spec.name!r} 的 risk={spec.risk!r} 非法，可用: {list(KNOWN_RISKS)}")
        if not callable(spec.handler):
            raise ValueError(f"工具 {spec.name!r} 的 handler 不可调用")
        return self._registry.register(
            spec.name,
            spec,
            layer=Layer.PLUGIN if layer is None else layer,
            rank=rank,
            owner=spec.owner or spec.plugin_id,
        )

    # ---------- 查询 ----------
    def get(self, name: str) -> ToolSpec | None:
        return self._registry.get(name)

    def names(self) -> list[str]:
        return self._registry.names()

    def specs(self) -> list[ToolSpec]:
        return [self._registry.get(name) for name in self.names()]

    def for_agent(self) -> list[dict[str, Any]]:
        """Agent 看到的工具目录。"""
        return [spec.to_agent_dict() for spec in self.specs()]

    def shadowed(self) -> list[dict[str, Any]]:
        return [entry.to_dict() for entry in self._registry.shadowed()]

    def __contains__(self, name: object) -> bool:
        return str(name) in self._registry

    def __len__(self) -> int:
        return len(self._registry)

    # ---------- 调用 ----------
    def call(self, name: str, arguments: dict | None = None) -> ToolResult:
        """调用工具。**任何失败都返回 ToolResult，不抛异常**（对齐协议错误二分）。"""
        args = dict(arguments or {})
        spec = self._registry.get(name)
        if spec is None:
            return ToolResult(
                ok=False,
                code=CODE_NOT_FOUND,
                message=f"工具 {name!r} 不存在；可用工具: {self.names()}",
            )

        # 1) 入参校验（协议错误）
        try:
            validate(args, spec.input_schema or {"type": "object"})
        except SchemaError as exc:
            return ToolResult(ok=False, code=CODE_INVALID_ARGS, message=str(exc))

        # 2) 授权（内核强制，不信任插件自报等级）
        allowed, reason = self._check_consent(spec, args)
        if not allowed:
            return ToolResult(ok=False, code=CODE_DENIED, message=reason, detail={"risk": spec.risk})

        # 3) 通知（只读观察，不参与放行）
        if self._hooks is not None:
            self._hooks.call("on_tool_called", tool_name=spec.name, arguments=args)

        # 4) 执行
        try:
            value = spec.handler(**args)
        except TypeError as exc:
            # 参数名与 schema 不一致，属于插件契约错误 → 协议错误
            return ToolResult(ok=False, code=CODE_INVALID_ARGS, message=f"参数与实现不匹配: {exc}")
        except Exception as exc:  # noqa: BLE001 - 业务失败，让 Agent 能自纠
            if self._log:
                self._log.error("工具 %s 执行失败: %s", spec.name, exc)
            return ToolResult(ok=False, code=CODE_EXEC_FAILED, message=str(exc))

        # 5) 出参校验（可选）
        if spec.output_schema:
            try:
                validate(value, spec.output_schema)
            except SchemaError as exc:
                return ToolResult(
                    ok=False,
                    code=CODE_INVALID_OUTPUT,
                    message=f"工具返回值不符合 output_schema: {exc}",
                    detail={"value": value},
                )

        return ToolResult(ok=True, value=value)

    def _check_consent(self, spec: ToolSpec, arguments: dict) -> tuple[bool, str]:
        if spec.risk not in RISKS_REQUIRING_CONSENT:
            return True, ""
        if self._approver is None:
            return False, (
                f"工具 {spec.name!r} 的风险等级为 {spec.risk}，需要用户授权，"
                "但当前没有可用的授权回调（命令行可用 --yes 显式授权）"
            )
        try:
            granted = bool(self._approver(spec.name, spec.risk, arguments))
        except Exception as exc:  # noqa: BLE001 - 授权回调出错按拒绝处理
            return False, f"授权过程出错，按拒绝处理: {exc}"
        if not granted:
            return False, f"用户拒绝了工具 {spec.name!r}（风险等级 {spec.risk}）的执行授权"
        return True, ""
