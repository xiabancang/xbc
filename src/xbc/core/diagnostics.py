"""开发环境自检。

把"开发环境是否就绪"变成**一条命令就能回答**的问题，而不是靠翻日志或凭感觉。
这是 TASK-001 的产出物之一：环境状态必须可观测，否则"恢复了"无法被证明。

分级语义：
- required   缺失则环境不可用（status=broken）
- recommended 缺失会降级，但内核仍可运行（status=degraded）
- optional   外部服务，随时可能不在，不影响环境健康
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from typing import Any

MIN_PYTHON = (3, 10)
GUI_MODULE = "PySide6"

REQUIRED = "required"
RECOMMENDED = "recommended"
OPTIONAL = "optional"


def _result(ok: bool, level: str, **details: Any) -> dict[str, Any]:
    return {"ok": bool(ok), "level": level, **details}


# ---------------- 各项检查 ----------------
def check_python() -> dict[str, Any]:
    version = sys.version_info
    return _result(
        version >= MIN_PYTHON,
        REQUIRED,
        version=sys.version.split()[0],
        required=f">={MIN_PYTHON[0]}.{MIN_PYTHON[1]}",
        executable=sys.executable,
        in_venv=sys.prefix != sys.base_prefix,
    )


def check_gui() -> dict[str, Any]:
    """图形宿主依赖 PySide6；内核本身不需要它。"""
    spec = importlib.util.find_spec(GUI_MODULE)
    if spec is None:
        return _result(False, RECOMMENDED, module=GUI_MODULE, reason="未安装")
    try:
        from importlib.metadata import version

        installed = version(GUI_MODULE)
    except Exception:  # noqa: BLE001 - 版本读不到不影响可用性判断
        installed = "unknown"
    return _result(True, RECOMMENDED, module=GUI_MODULE, version=installed)


def check_git() -> dict[str, Any]:
    """git 用于版本控制；没有它工程无法回滚，也无法协作。"""
    path = shutil.which("git")
    if path is None:
        return _result(False, RECOMMENDED, reason="PATH 中找不到 git")
    try:
        completed = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=15
        )
        return _result(True, RECOMMENDED, path=path, version=(completed.stdout or "").strip())
    except (OSError, subprocess.SubprocessError) as exc:
        return _result(False, RECOMMENDED, path=path, reason=str(exc))


def check_ffmpeg(ctx: Any) -> dict[str, Any]:
    """FFmpeg 是音视频类插件的前提，缺失时相关插件应自行降级。"""
    available = ctx.ffmpeg.available()
    return _result(
        available,
        RECOMMENDED,
        resolved_path=ctx.ffmpeg.ffmpeg,
        version=ctx.ffmpeg.version() if available else None,
    )


def check_ai(ctx: Any) -> dict[str, Any]:
    """本地模型服务属于外部依赖，未启动是正常状态。"""
    status = ctx.ai.status()
    return _result(any(status.values()), OPTIONAL, providers=status)


def check_data_dir(ctx: Any) -> dict[str, Any]:
    """数据目录必须可写 —— 这是内核启动的硬前提。"""
    probe = ctx.paths.root / ".write-probe"
    try:
        ctx.paths.root.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return _result(True, REQUIRED, root=str(ctx.paths.root), writable=True)
    except OSError as exc:
        return _result(False, REQUIRED, root=str(ctx.paths.root), writable=False, reason=str(exc))


def check_plugin_paths(ctx: Any) -> dict[str, Any]:
    paths = [str(p) for p in ctx.plugin_search_paths()]
    existing = [p for p in paths if __import__("pathlib").Path(p).is_dir()]
    return _result(bool(existing), REQUIRED, search_paths=paths, existing=existing)


# ---------------- 汇总 ----------------
def run_checks(ctx: Any) -> dict[str, Any]:
    checks = {
        "python": check_python(),
        "data_dir": check_data_dir(ctx),
        "plugin_paths": check_plugin_paths(ctx),
        "gui": check_gui(),
        "git": check_git(),
        "ffmpeg": check_ffmpeg(ctx),
        "ai": check_ai(ctx),
    }

    broken = [name for name, item in checks.items() if item["level"] == REQUIRED and not item["ok"]]
    degraded = [
        name
        for name, item in checks.items()
        if item["level"] == RECOMMENDED and not item["ok"]
    ]

    if broken:
        status = "broken"
    elif degraded:
        status = "degraded"
    else:
        status = "ok"

    return {
        "status": status,
        "healthy": status == "ok",
        "broken": broken,
        "degraded": degraded,
        "checks": checks,
    }


def exit_code(report: dict[str, Any]) -> int:
    """只有 required 项失败才算环境不可用。"""
    return 1 if report["status"] == "broken" else 0
