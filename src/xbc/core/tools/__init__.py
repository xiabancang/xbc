"""工具（Tool）子系统。

- `schema.py`   JSON Schema 子集校验器（自研，保持内核零第三方依赖）
- `registry.py` 工具注册、入参/出参校验、风险授权、统一错误信封
"""

from .registry import (
    CODE_DENIED,
    CODE_EXEC_FAILED,
    CODE_INVALID_ARGS,
    CODE_INVALID_OUTPUT,
    CODE_NOT_FOUND,
    RISKS_REQUIRING_CONSENT,
    ToolRegistry,
    ToolResult,
    ToolSpec,
)
from .schema import SchemaError, defaults_from, validate

__all__ = [
    "CODE_DENIED",
    "CODE_EXEC_FAILED",
    "CODE_INVALID_ARGS",
    "CODE_INVALID_OUTPUT",
    "CODE_NOT_FOUND",
    "RISKS_REQUIRING_CONSENT",
    "SchemaError",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "defaults_from",
    "validate",
]
