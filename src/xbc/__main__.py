"""命令行入口：内核的**唯一**操作界面（TASK-003 不做图形界面）。

    python -m xbc env                     环境健康检查
    python -m xbc doctor                  完整体检（环境 + 插件运行时）
    python -m xbc plugin list             列出插件（不导入代码）
    python -m xbc plugin show <id>        查看插件详情
    python -m xbc plugin enable <id>      启用（持久化到用户层配置）
    python -m xbc plugin disable <id>     禁用（持久化）
    python -m xbc tool list               列出工具（Agent 视角）
    python -m xbc tool call <名称> [--kwargs JSON] [--yes]
    python -m xbc skill list              列出技能目录
    python -m xbc skill load <名称>       加载技能正文
    python -m xbc smoke                   端到端冒烟（验收用）
    python -m xbc ui                      旧的占位图形宿主（不在 TASK-003 范围）

有了它，插件机制的正确性完全不依赖界面 —— 这正是"内核与界面解耦"的收益。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .version import APP_NAME, API_SPEC_VERSION, CORE_API_VERSION, CORE_VERSION


def _print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _jsonable(value: Any) -> Any:
    if isinstance(value, (dict, list, str, int, float, bool)) or value is None:
        return value
    return str(value)


def _manager(ctx: Any) -> Any:
    manager = ctx.create_plugin_manager()
    manager.discover()
    return manager


# ---------------- 环境与诊断 ----------------
def cmd_env(ctx: Any, args: argparse.Namespace) -> int:
    from .core.diagnostics import exit_code, run_checks

    report = run_checks(ctx)
    report["info"] = {
        "app": APP_NAME,
        "core_version": CORE_VERSION,
        "api_version": CORE_API_VERSION,
        "spec_version": API_SPEC_VERSION,
        "python": sys.version.split()[0],
        "data_root": str(ctx.paths.root),
    }
    _print_json(report)
    return exit_code(report)


def cmd_doctor(ctx: Any, args: argparse.Namespace) -> int:
    from .core.diagnostics import doctor, doctor_exit_code

    manager = _manager(ctx)
    manager.load_all()
    manager.activate_all()  # 体检要反映"真正跑起来"的状态，否则工具/技能永远是 0
    report = doctor(ctx, manager)
    report["plugin_detail"] = [
        {"id": r.id, "state": r.state.value, "enabled": r.enabled,
         "error": r.error, "blocked_reason": r.blocked_reason}
        for r in manager.records()
    ]
    _print_json(report)
    return doctor_exit_code(report)


# ---------------- 插件 ----------------
def cmd_plugin_list(ctx: Any, args: argparse.Namespace) -> int:
    manager = _manager(ctx)
    # 注意：**不导入插件代码**，只读清单 —— 这就是 DISCOVERED 状态的"零成本"
    _print_json(manager.summary())
    return 0


def cmd_plugin_show(ctx: Any, args: argparse.Namespace) -> int:
    manager = _manager(ctx)
    record = manager.get(args.plugin_id)
    data = record.to_dict()
    data["config"] = ctx.effective_config_for(record.manifest)
    # 未加载时 record.config_source 还是默认值，这里按层直接算，避免报错层次
    data["config_source"] = ctx.config_source_for(record.manifest)
    data["config_search_paths"] = [str(p) for p in ctx.config_search_paths()]
    data["icon_path"] = str(record.manifest.icon_path() or "")
    _print_json(data)
    return 0


def cmd_plugin_enable(ctx: Any, args: argparse.Namespace) -> int:
    manager = _manager(ctx)
    record = manager.enable(args.plugin_id)
    _print_json(record.to_dict())
    return 0 if record.state.value == "active" else 1


def cmd_plugin_disable(ctx: Any, args: argparse.Namespace) -> int:
    manager = _manager(ctx)
    record = manager.disable(args.plugin_id)
    _print_json(record.to_dict())
    return 0


# ---------------- 工具 ----------------
def cmd_tool_list(ctx: Any, args: argparse.Namespace) -> int:
    manager = _manager(ctx)
    manager.load_all()
    manager.activate_all()
    _print_json(
        {
            "count": len(ctx.tool_registry),
            "tools": ctx.tool_registry.for_agent(),
            "shadowed": ctx.tool_registry.shadowed(),
            "plugin_states": {r.id: r.state.value for r in manager.records()},
        }
    )
    return 0


def cmd_tool_call(ctx: Any, args: argparse.Namespace) -> int:
    manager = _manager(ctx)
    manager.load_all()
    manager.activate_all()
    try:
        kwargs = json.loads(args.kwargs) if args.kwargs else {}
    except json.JSONDecodeError as exc:
        print(f"--kwargs 不是合法 JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(kwargs, dict):
        print("--kwargs 必须是 JSON 对象", file=sys.stderr)
        return 2

    result = ctx.tool_registry.call(args.tool_name, kwargs)
    _print_json({"tool": args.tool_name, "arguments": kwargs, **result.to_dict()})
    return 0 if result.ok else 1


# ---------------- 技能 ----------------
def cmd_skill_list(ctx: Any, args: argparse.Namespace) -> int:
    manager = _manager(ctx)
    manager.load_all()
    manager.activate_all()
    _print_json(
        {
            "count": len(ctx.skill_catalog),
            "for_model": [s.to_dict() for s in ctx.skill_catalog.specs(for_model=True)],
            "for_user": [s.to_dict() for s in ctx.skill_catalog.specs(for_model=False)],
        }
    )
    return 0


def cmd_skill_load(ctx: Any, args: argparse.Namespace) -> int:
    manager = _manager(ctx)
    manager.load_all()
    manager.activate_all()
    content = ctx.skill_catalog.load(args.skill_name)
    _print_json({**content.to_dict(), "body": content.body})
    return 0


# ---------------- 端到端冒烟 ----------------
def cmd_smoke(ctx: Any, args: argparse.Namespace) -> int:
    """发现 → 加载 → 激活 → 调用只读工具 → 停用 → 卸载。验收用。"""
    from .core.tools.schema import defaults_from

    manager = _manager(ctx)
    manager.load_all()
    manager.activate_all()

    call_results: dict[str, Any] = {}
    for record in manager.records():
        if record.state.value != "active":
            continue
        for name in record.tools_registered:
            spec = ctx.tool_registry.get(name)
            if spec is None or spec.risk != "read":
                continue
            arguments = defaults_from(spec.input_schema)
            required = set(spec.input_schema.get("required", []))
            if not required.issubset(arguments):
                call_results[name] = f"跳过（缺少必填参数 {sorted(required - set(arguments))}）"
                continue
            result = ctx.tool_registry.call(name, arguments)
            call_results[name] = result.to_dict()

    report = {
        "core_version": CORE_VERSION,
        "plugins": [r.to_dict() for r in manager.records()],
        "failed": [r.id for r in manager.records() if r.state.value == "failed"],
        "tool_calls": call_results,
        "tools_total": len(ctx.tool_registry),
        "skills_total": len(ctx.skill_catalog),
        "services_total": len(ctx.service_registry),
        "shadowed": ctx.tool_registry.shadowed(),
    }

    # 停用 → 卸载，并验证"注册即资源"确实清空
    manager.deactivate_all()
    tools_after_deactivate = len(ctx.tool_registry)
    skills_after_deactivate = len(ctx.skill_catalog)
    for record in manager.records():
        manager.unload(record.id)
    report["after_deactivate"] = {
        "tools": tools_after_deactivate,
        "skills": skills_after_deactivate,
    }

    _print_json(report)
    return 0 if report["plugins"] and not report["failed"] else 1


def cmd_ui(ctx: Any, args: argparse.Namespace) -> int:
    """旧的占位图形宿主。TASK-003 不做 UI，此处仅为保持入口不失效。"""
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
    parser.add_argument("--yes", action="store_true", help="对 write/destructive 工具预先授权")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("env", help="环境健康检查").set_defaults(func=cmd_env)
    sub.add_parser("doctor", help="完整体检（环境 + 插件运行时）").set_defaults(func=cmd_doctor)

    plugin = sub.add_parser("plugin", help="插件管理").add_subparsers(dest="plugin_action", required=True)
    plugin.add_parser("list", help="列出插件（不导入代码）").set_defaults(func=cmd_plugin_list)
    show = plugin.add_parser("show", help="查看插件详情")
    show.add_argument("plugin_id")
    show.set_defaults(func=cmd_plugin_show)
    enable = plugin.add_parser("enable", help="启用插件")
    enable.add_argument("plugin_id")
    enable.set_defaults(func=cmd_plugin_enable)
    disable = plugin.add_parser("disable", help="禁用插件")
    disable.add_argument("plugin_id")
    disable.set_defaults(func=cmd_plugin_disable)

    tool = sub.add_parser("tool", help="工具").add_subparsers(dest="tool_action", required=True)
    tool.add_parser("list", help="列出工具（Agent 视角）").set_defaults(func=cmd_tool_list)
    call = tool.add_parser("call", help="调用工具")
    call.add_argument("tool_name")
    call.add_argument("--kwargs", default=None, help="JSON 对象形式的参数")
    call.set_defaults(func=cmd_tool_call)

    skill = sub.add_parser("skill", help="技能").add_subparsers(dest="skill_action", required=True)
    skill.add_parser("list", help="列出技能目录").set_defaults(func=cmd_skill_list)
    load = skill.add_parser("load", help="加载技能正文")
    load.add_argument("skill_name")
    load.set_defaults(func=cmd_skill_load)

    sub.add_parser("smoke", help="端到端冒烟（验收用）").set_defaults(func=cmd_smoke)
    sub.add_parser("ui", help="旧的占位图形宿主").set_defaults(func=cmd_ui)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    args = build_parser().parse_args(argv)

    from .core.context import AppContext

    approver = (lambda *_: True) if getattr(args, "yes", False) else None
    ctx = AppContext.create(root=args.root, console=not args.quiet, approver=approver)
    try:
        return int(args.func(ctx, args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
