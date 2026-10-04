"""AppContext：插件能看到的"整个世界"。

拆成两个对象，是"最小权限"的落点：
- AppContext    内核自己用的完整上下文（路径、配置、全部公共能力）
- PluginContext 交给插件的受限视图，只暴露该插件在清单里声明过的能力

因此插件拿不到 AppPaths、拿不到别的插件的数据目录，
访问未声明的能力会直接抛 CapabilityDenied（而不是悄悄返回 None）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..version import CORE_API_VERSION, CORE_VERSION
from .config import Config, PluginSettings
from .errors import CapabilityDenied
from .logging_setup import plugin_logger, setup_logging, shutdown_logging
from .paths import AppPaths, builtin_plugins_dir
from .services.ai import AIService, build_ai_service
from .services.ffmpeg import FFmpegService
from .services.files import FileService


class PluginContext:
    """插件视角的上下文。"""

    def __init__(
        self,
        app: "AppContext",
        plugin_id: str,
        capabilities: tuple[str, ...] | list[str],
        logger: Any,
    ) -> None:
        self._app = app
        self.plugin_id = plugin_id
        self.capabilities = tuple(capabilities)
        self.logger = logger

    # ---------- 任何人都有的信息 ----------
    @property
    def core_version(self) -> str:
        return CORE_VERSION

    @property
    def api_version(self) -> str:
        return CORE_API_VERSION

    @property
    def data_dir(self) -> Path:
        """插件私有数据目录，内核保证与其他插件隔离。"""
        return self._app.paths.plugin_data_dir(self.plugin_id)

    @property
    def cache_dir(self) -> Path:
        return self._app.paths.plugin_cache_dir(self.plugin_id)

    @property
    def settings(self) -> PluginSettings:
        """插件配置命名空间（持久化在 config.json 的 plugins.<id>）。"""
        return self._app.config.plugin_section(self.plugin_id)

    # ---------- 需要声明才有的能力 ----------
    def _service(self, name: str, service: Any) -> Any:
        if name not in self.capabilities:
            raise CapabilityDenied(
                f"插件 {self.plugin_id} 未声明能力 {name!r}；"
                f"请在 plugin.json 的 capabilities 中加入 {name!r}"
                f"（当前已声明: {list(self.capabilities)}）"
            )
        return service

    @property
    def files(self) -> FileService:
        return self._service("files", self._app.files)

    @property
    def ffmpeg(self) -> FFmpegService:
        return self._service("ffmpeg", self._app.ffmpeg)

    @property
    def ai(self) -> AIService:
        return self._service("ai", self._app.ai)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"PluginContext(plugin_id={self.plugin_id!r}, capabilities={list(self.capabilities)})"


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
    ) -> None:
        self.paths = paths
        self.config = config
        self.logger = logger
        self.files = files
        self.ffmpeg = ffmpeg
        self.ai = ai

    # ---------- 装配 ----------
    @classmethod
    def create(
        cls,
        root: Path | str | None = None,
        overrides: dict[str, Any] | None = None,
        console: bool = True,
    ) -> "AppContext":
        """按"配置 → 日志 → 能力"的顺序装配内核。

        overrides 用于测试或命令行临时覆盖配置（如 {"ai.provider": "ollama"}）。
        """
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

        logger.info(
            "内核启动：%s (API %s)，数据目录 %s", CORE_VERSION, CORE_API_VERSION, paths.root
        )
        return cls(paths, config, logger, files, ffmpeg, ai)

    # ---------- 插件相关 ----------
    def for_plugin(self, manifest: Any) -> PluginContext:
        return PluginContext(
            self,
            manifest.id,
            manifest.capabilities,
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
        # 延迟导入，避免 context ←→ plugins 的循环依赖
        from .plugins.manager import PluginManager

        return PluginManager(self, self.plugin_search_paths(), self.logger)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"AppContext(root={self.paths.root})"

    def close(self) -> None:
        """释放内核持有的资源（当前主要是日志文件句柄）。

        不调用也不会崩，但日志文件会一直被占用，其所在目录在 Windows 上无法删除。
        """
        shutdown_logging()
