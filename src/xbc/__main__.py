"""命令行入口：让内核可以脱离图形界面被验证。

    python -m xbc env                  环境自检（ffmpeg / AI / 目录）
    python -m xbc list [--load]        列出发现的插件
    python -m xbc start <插件id>       启动插件
    python -m xbc stop <插件id>        停止插件
    python -m xbc invoke <插件id> <动作>
    python -m xbc smoke                全流程冒烟测试（建议先跑这个）
    python -m xbc ui                   启动图形宿主（需要 PySide6）

有了它，插件机制的正确性不依赖界面就能被验证 ——
这也是内核与界面解耦后立刻得到的好处。
"""

from __future__ import annotations

import argparse
import inspect
import json
import sys
from typing import Any

from .version import APP_NAME, CORE_API_VERSION, CORE_VERSION


def _print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _jsonable(value: Any) -> Any:
    if isinstance(value, (dict, list, str, int, float, bool)) or value is None:
        return value
    return str(value)


def _callable_without_args(func: Any) -> bool:
    """判断动作能否不带参数调用（冒烟测试只调用这类动作）。"""
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return False
    for parameter in signature.parameters.values():
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            continue
        if parameter.default is parameter.empty:
            return False
    return True


def _build_manager(ctx: Any) -> Any:
    manager = ctx.create_plugin_manager()
    manager.discover()
    return manager


# ---------------- 子命令 ----------------
def cmd_env(ctx: Any, args: argparse.Namespace) -> int:
    """开发环境自检：一条命令回答"环境是否就绪"，并在不可用时返回非零退出码。"""
    from .core.diagnostics import exit_code, run_checks

    report = run_checks(ctx)
    report["info"] = {
        "app": APP_NAME,
        "core_version": CORE_VERSION,
        "api_version": CORE_API_VERSION,
        "python": sys.version.split()[0],
        "data_root": str(ctx.paths.root),
        "config_file": str(ctx.paths.config_file),
        "logs_dir": str(ctx.paths.logs_dir),
    }
    _print_json(report)
    return exit_code(report)


def cmd_list(ctx: Any, args: argparse.Namespace) -> int:
    manager = _build_manager(ctx)
    if args.load:
        manager.load_all()
    _print_json(manager.summary())
    return 0


def cmd_start(ctx: Any, args: argparse.Namespace) -> int:
    manager = _build_manager(ctx)
    record = manager.start(args.plugin_id)
    _print_json(record.to_dict())
    return 0 if record.state.value == "started" else 1


def cmd_stop(ctx: Any, args: argparse.Namespace) -> int:
    manager = _build_manager(ctx)
    manager.load_all()
    manager.start(args.plugin_id)
    record = manager.stop(args.plugin_id)
    _print_json(record.to_dict())
    return 0


def cmd_invoke(ctx: Any, args: argparse.Namespace) -> int:
    manager = _build_manager(ctx)
    manager.start(args.plugin_id)
    kwargs = json.loads(args.kwargs) if args.kwargs else {}
    if not isinstance(kwargs, dict):
        print("--kwargs 必须是 JSON 对象", file=sys.stderr)
        return 2
    result = manager.invoke(args.plugin_id, args.action, **kwargs)
    _print_json({"plugin": args.plugin_id, "action": args.action, "result": _jsonable(result)})
    return 0


def cmd_smoke(ctx: Any, args: argparse.Namespace) -> int:
    """全流程冒烟：发现 → 加载 → 启动 → 调用动作 → 停止 → 卸载。"""
    manager = _build_manager(ctx)
    manager.load_all()
    manager.start_all()

    report: list[dict] = []
    action_failures = 0
    for record in manager.records():
        entry: dict[str, Any] = {
            "id": record.id,
            "state": record.state.value,
            "error": record.error,
            "actions": {},
        }
        if record.state.value == "started" and record.instance is not None:
            for name, func in sorted(record.instance.actions().items()):
                if not _callable_without_args(func):
                    entry["actions"][name] = "跳过（需要参数）"
                    continue
                try:
                    entry["actions"][name] = _jsonable(manager.invoke(record.id, name))
                except Exception as exc:  # noqa: BLE001 - 冒烟测试要收集失败而不是中断
                    action_failures += 1
                    entry["actions"][name] = f"失败: {exc}"
        report.append(entry)

    manager.stop_all()
    for record in manager.records():
        manager.unload(record.id)

    failed = [e for e in report if e["state"] != "started"]
    _print_json(
        {
            "core_version": CORE_VERSION,
            "plugin_count": len(report),
            "failed_count": len(failed),
            # 注意：hello_xbc 的 fail_on_purpose 是"故意失败"，用于验证错误隔离，
            # 因此这里单独统计动作失败数，退出码只反映插件是否成功加载并启动。
            "action_failure_count": action_failures,
            "plugins": report,
        }
    )
    return 0 if report and not failed else 1


def cmd_ui(ctx: Any, args: argparse.Namespace) -> int:
    try:
        from .ui.shell import run_shell
    except ImportError as exc:
        print(f"无法启动图形界面（需要 PySide6）: {exc}", file=sys.stderr)
        return 2
    return run_shell(ctx)


# ---------------- 参数解析 ----------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="xbc", description=f"{APP_NAME} 命令行")
    parser.add_argument("--root", default=None, help="覆盖数据根目录（默认 %%LOCALAPPDATA%%/夏半仓工具箱）")
    parser.add_argument("--quiet", action="store_true", help="不输出内核日志到控制台")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("env", help="开发环境健康检查").set_defaults(func=cmd_env)

    list_parser = subparsers.add_parser("list", help="列出插件")
    list_parser.add_argument("--load", action="store_true", help="同时执行加载")
    list_parser.set_defaults(func=cmd_list)

    start_parser = subparsers.add_parser("start", help="启动插件")
    start_parser.add_argument("plugin_id")
    start_parser.set_defaults(func=cmd_start)

    stop_parser = subparsers.add_parser("stop", help="停止插件")
    stop_parser.add_argument("plugin_id")
    stop_parser.set_defaults(func=cmd_stop)

    invoke_parser = subparsers.add_parser("invoke", help="调用插件动作")
    invoke_parser.add_argument("plugin_id")
    invoke_parser.add_argument("action")
    invoke_parser.add_argument("--kwargs", default=None, help="JSON 对象形式的动作参数")
    invoke_parser.set_defaults(func=cmd_invoke)

    subparsers.add_parser("smoke", help="全流程冒烟测试").set_defaults(func=cmd_smoke)
    subparsers.add_parser("ui", help="启动图形宿主").set_defaults(func=cmd_ui)
    return parser


def main(argv: list[str] | None = None) -> int:
    # 保证中文在任何 Windows 控制台编码下都能输出
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    parser = build_parser()
    args = parser.parse_args(argv)

    from .core.context import AppContext

    ctx = AppContext.create(root=args.root, console=not args.quiet)
    try:
        return int(args.func(ctx, args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
