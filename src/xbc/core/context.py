"""AppContext 与 PluginContext。

拆成两个对象，是"最小权限"的落点（方案 3.4 / 3.7）：

- **AppContext**：内核自己用的完整上下文（路径、配置、全部能力、注册表、钩子）
- **PluginContext**：交给插件的**受限视图**，只暴露该插件在清单里声明过的能力

因此插件拿不到 `AppPaths`、拿不到别的插件的数据目录，
访问未声明的能力会直接抛 `CapabilityDenied`（而不是悄悄返回 None）。

术语区分（方案 4.1）：

- `ctx.tools` / `ctx.skills` / `ctx.services` 是**注册机制**（插件向外贡献东西）
- `ctx.files` / `ctx.ai` / `ctx.ffmpeg` / `ctx.events` / `ctx.settings` 是**能力**
  （内核向插件提供，需要声明）

注册机制里的每一次注册都会**自动挂到插件作用域**，停用时统一释放 ——
插件即使忘了处理 disposer，也不会留下残留。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from ..version import API_SPEC_VERSION, CORE_API_VERSION, CORE_VERSION
from .capabilities import (
    AIService,
    EventBus,
    FFmpegService,
    FileService,
    SettingsCapability,
    build_ai_service,
)
from .config import (
    Config,
    PluginSettings,
    effective_config,
    load_rows,
    merge_rows,
)
from .contract.hookspec import HookRelay
from .contract.kernel_hooks import KernelHooks
from .contract.manifest import PluginManifest
from .errors import CapabilityDenied
from .logging_setup import plugin_logger, setup_logging, shutdown_logging
from .paths import AppPaths, builtin_config_dir, builtin_plugins_dir
from .runtime.registry import Layer, LayeredRegistry
from .runtime.scope import Disposer, Scope
from .skills import SKILL_FILE, FileSystemSkillProvider, SkillCatalog, SkillSpec, parse_front_matter
from .tools import ToolRegistry, ToolSpec

# ---------------- 插件侧注册门面 ----------------


class _ToolFacade:
    """插件注册工具的门面。每次注册自动挂到插件作用域。"""

    def __init__(self, registry: ToolRegistry, scope: Scope, plugin_id: str) -> None:
        self._registry = registry
        self._scope = scope
        self._plugin_id = plugin_id

    def register(
        self,
        name: str,
        handler: Callable[..., Any],
        *,
        description: str = "",
        input_schema: dict | None = None,
        output_schema: dict | None = None,
        risk: str = "read",
        rank: int = 0,
    ) -> Disposer:
        spec = ToolSpec(
            name=name,
            handler=handler,
            description=description,
            input_schema=input_schema or {"type": "object"},
            output_schema=output_schema or {},
            risk=risk,
            owner=self._plugin_id,
            plugin_id=self._plugin_id,
        )
        disposer = self._registry.register(spec, rank=rank)
        self._scope.effect(lambda: disposer)  # 注册即资源：自动挂载
        return disposer

    def names(self) -> list[str]:
        return [s.name for s in self._registry.specs() if s.plugin_id == self._plugin_id]


class _SkillFacade:
    """插件注册技能的门面。"""

    def __init__(self, catalog: SkillCatalog, scope: Scope, plugin_id: str) -> None:
        self._catalog = catalog
        self._scope = scope
        self._plugin_id = plugin_id

    def register(
        self,
        name: str,
        description: str,
        body: str | Callable[[], str],
        *,
        model_invocable: bool = True,
        user_invocable: bool = True,
        path: Path | None = None,
    ) -> Disposer:
        spec = SkillSpec(
            name=name,
            description=description,
            path=path,
            provider="runtime",
            plugin_id=self._plugin_id,
            model_invocable=model_invocable,
            user_invocable=user_invocable,
        )
        disposer = self._catalog.register(spec, body)
        self._scope.effect(lambda: disposer)
        return disposer

    def register_dir(self, directory: Path | str) -> list[Disposer]:
        """把一个目录下的技能（每个子目录一个 SKILL.md）注册进来。"""
        folder = Path(directory)
        if not folder.is_dir():
            return []
        disposers: list[Disposer] = []
        for child in sorted(folder.iterdir()):
            skill_file = child / SKILL_FILE
            if not (child.is_dir() and skill_file.is_file()):
                continue
            try:
                text = skill_file.read_text(encoding="utf-8")
            except OSError:
                continue
            meta, body = parse_front_matter(text)
            name = meta.get("name") or child.name
            disposers.append(
                self.register(
                    name=name,
                    description=meta.get("description", ""),
                    body=body,
                    model_invocable=meta.get("model_invocable", "true").lower() != "false",
                    user_invocable=meta.get("user_invocable", "true").lower() != "false",
                    path=child,
                )
            )
        return disposers


class _ServiceFacade:
    """插件对外提供/查找服务（插件 ↔ 插件）。"""

    def __init__(self, registry: LayeredRegistry, scope: Scope, plugin_id: str) -> None:
        self._registry = registry
        self._scope = scope
        self._plugin_id = plugin_id

    def provide(self, name: str, value: Any, *, rank: int = 0) -> Disposer:
        disposer = self._registry.register(
            name, value, layer=Layer.PLUGIN, rank=rank, owner=self._plugin_id
        )
        self._scope.effect(lambda: disposer)
        return disposer

    def get(self, name: str, default: Any = None) -> Any:
        return self._registry.get(name, default)

    def names(self) -> list[str]:
        return self._registry.names()


# ---------------- 插件上下文 ----------------


class PluginContext:
    """插件视角的上下文。未在清单中声明的能力不可用。"""

    def __init__(
        self,
        app: "AppContext",
        manifest: PluginManifest,
        scope: Scope,
        config: dict[str, Any],
        logger: Any,
    ) -> None:
        self._app = app
        self.manifest = manifest
        self.plugin_id = manifest.id
        self.capabilities = tuple(manifest.capabilities)
        self.scope = scope
        self.logger = logger
        self.config: dict[str, Any] = config

        self.tools = _ToolFacade(app.tool_registry, scope, manifest.id)
        self.skills = _SkillFacade(app.skill_catalog, scope, manifest.id)
        self.services = _ServiceFacade(app.service_registry, scope, manifest.id)

    # ---------- 任何人都有的信息 ----------
    @property
    def core_version(self) -> str:
        return CORE_VERSION

    @property
    def api_version(self) -> str:
        return CORE_API_VERSION

    @property
    def spec_version(self) -> str:
        return API_SPEC_VERSION

    @property
    def data_dir(self) -> Path:
        """插件私有数据目录，内核保证与其他插件隔离。"""
        return self._app.paths.plugin_data_dir(self.plugin_id)

    @property
    def cache_dir(self) -> Path:
        return self._app.paths.plugin_cache_dir(self.plugin_id)

    @property
    def package_dir(self) -> Path | None:
        return self.manifest.path

    def effect(self, register: Callable[[], Any]) -> Disposer:
        """注册一个自定义资源并返回 disposer。"""
        return self.scope.effect(register)

    def get(self, name: str, default: Any = None) -> Any:
        """查找其他插件提供的服务。"""
        return self._app.service_registry.get(name, default)

    # ---------- 需要声明才有的能力 ----------
    def _capability(self, name: str, service: Any) -> Any:
        if name not in self.capabilities:
            raise CapabilityDenied(
                f"插件 {self.plugin_id} 未声明能力 {name!r}；"
                f"请在 plugin.json 的 capabilities 中加入 {name!r}"
                f"（当前已声明: {list(self.capabilities)}）"
            )
        return service

    @property
    def files(self) -> FileService:
        return self._capability("files", self._app.files)

    @property
    def ffmpeg(self) -> FFmpegService:
        return self._capability("ffmpeg", self._app.ffmpeg)

    @property
    def ai(self) -> AIService:
        return self._capability("ai", self._app.ai)

    @property
    def events(self) -> EventBus:
        return self._capability("events", self._app.events)

    @property
    def settings(self) -> PluginSettings:
        self._capability("settings", True)
        return self._app.settings.for_plugin(self.plugin_id)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"PluginContext(plugin_id={self.plugin_id!r}, capabilities={list(self.capabilities)})"


# ---------------- 内核上下文 ----------------


class AppContext:
    """内核运行上下文。整个进程只应创建一个。"""

    def __init__(
        self,
        paths: AppPaths,
        config: Config,
        logger: Any,
        files: FileService,
        ffmpeg: FFmpegService,
        ai: AIService,
        settings: SettingsCapability,
        events: EventBus,
        hooks: HookRelay,
        tool_registry: ToolRegistry,
        skill_catalog: SkillCatalog,
        service_registry: LayeredRegistry,
    ) -> None:
        self.paths = paths
        self.config = config
        self.logger = logger
        self.files = files
        self.ffmpeg = ffmpeg
        self.ai = ai
        self.settings = settings
        self.events = events
        self.hooks = hooks
        self.tool_registry = tool_registry
        self.skill_catalog = skill_catalog
        self.service_registry = service_registry
        self._plugin_rows: dict[str, dict] = {}
        self._host_rows: dict[str, dict] = {}
        self._user_rows: dict[str, dict] = {}

    # ---------- 装配 ----------
    @classmethod
    def create(
        cls,
        root: Path | str | None = None,
        overrides: dict[str, Any] | None = None,
        console: bool = True,
        approver: Callable[..., bool] | None = None,
    ) -> "AppContext":
        """按"配置 → 日志 → 能力 → 注册表"的顺序装配内核。"""
        paths = AppPaths(root).ensure()
        config = Config(paths.config_file)
        if overrides:
            for key, value in overrides.items():
                config.set(key, value, save=False)
            config.save()

        logger = setup_logging(
            paths, level=str(config.get("app.log_level", "INFO")), console=console
        )
        files = FileService(logger)
        ffmpeg = FFmpegService(
            ffmpeg_path=config.get("ffmpeg.ffmpeg_path", "ffmpeg"),
            ffprobe_path=config.get("ffmpeg.ffprobe_path", "ffprobe"),
            logger=logger,
        )
        ai = build_ai_service(config, logger)
        settings = SettingsCapability(config, paths.secrets_file, logger)
        events = EventBus(logger)

        hooks = HookRelay(logger)
        hooks.add_specs(KernelHooks)

        tool_registry = ToolRegistry(logger=logger, hooks=hooks, approver=approver)
        skill_catalog = SkillCatalog(logger=logger)
        skill_catalog.register_provider(FileSystemSkillProvider([paths.user_skills_dir], logger))

        service_registry = LayeredRegistry("服务", logger)

        context = cls(
            paths, config, logger, files, ffmpeg, ai, settings, events, hooks,
            tool_registry, skill_catalog, service_registry,
        )
        context.reload_plugin_config()
        logger.info(
            "内核启动：%s (API %s / 清单规范 %s)，数据目录 %s",
            CORE_VERSION, CORE_API_VERSION, API_SPEC_VERSION, paths.root,
        )
        return context

    # ---------- 插件配置（三层） ----------
    def config_search_paths(self) -> list[Path]:
        """宿主层 + 用户层的插件配置文件路径。"""
        paths: list[Path] = []
        builtin = builtin_config_dir()
        if builtin is not None:
            paths.append(builtin / "plugins.json")
        paths.append(self.paths.user_plugins_config)
        return paths

    def reload_plugin_config(self) -> dict[str, dict]:
        """重新读取三层配置中的宿主层与用户层并合并。

        注意：宿主层目录可能不存在（例如打包后），此时**不能**退化成
        "把用户层当成宿主层"，否则用户层会被自己覆盖，语义就错了。
        """
        builtin = builtin_config_dir()
        host_path = (builtin / "plugins.json") if builtin is not None else None
        user_path = self.paths.user_plugins_config

        host_rows = load_rows(host_path) if host_path is not None and host_path.is_file() else {}
        user_rows = load_rows(user_path)
        self._host_rows = host_rows
        self._user_rows = user_rows
        self._plugin_rows = merge_rows(host_rows, user_rows)
        return self._plugin_rows

    @property
    def plugin_rows(self) -> dict[str, dict]:
        return self._plugin_rows

    def effective_config_for(self, manifest: PluginManifest) -> dict[str, Any]:
        """算出一个插件的最终有效配置（默认层 / 宿主层 / 用户层）。"""
        value, _ = effective_config(manifest, self._host_rows, self._user_rows)
        return value

    def config_source_for(self, manifest: PluginManifest) -> str:
        """最终配置来自哪一层：default / host / user。"""
        _, source = effective_config(manifest, self._host_rows, self._user_rows)
        return source

    def is_enabled(self, manifest: PluginManifest) -> bool:
        row = self._plugin_rows.get(manifest.id, {})
        enabled = row.get("enabled")
        return bool(enabled) if isinstance(enabled, bool) else True

    # ---------- 插件相关 ----------
    def for_plugin(
        self,
        manifest: PluginManifest,
        scope: Scope | None = None,
    ) -> PluginContext:
        plugin_scope = scope or Scope(f"plugin:{manifest.id}", logger=self.logger)
        return PluginContext(
            self,
            manifest,
            plugin_scope,
            self.effective_config_for(manifest),
            plugin_logger(manifest.id),
        )

    def plugin_search_paths(self) -> list[Path]:
        """内置插件目录 + 用户插件目录。

        用户目录是将来插件商城的落地位置：
        把插件文件夹放进去即安装，删掉即卸载。
        """
        paths: list[Path] = []
        builtin = builtin_plugins_dir()
        if builtin is not None:
            paths.append(builtin)
        paths.append(self.paths.user_plugins_dir)
        return paths

    def create_plugin_manager(self) -> Any:
        # 延迟导入，避免 context ←→ runtime.manager 的循环依赖
        from .runtime.manager import PluginManager

        return PluginManager(self, self.plugin_search_paths(), self.logger)

    def close(self) -> None:
        """释放内核持有的资源（当前主要是日志文件句柄）。"""
        shutdown_logging()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"AppContext(root={self.paths.root})"
