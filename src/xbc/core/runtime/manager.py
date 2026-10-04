"""插件管理器：发现 → 加载 → 激活 → 停用 → 卸载。

三条设计原则（方案 3.3 / 1.1）：

1. **内核不认识任何具体插件** —— 只通过清单 + 基类契约驱动。
2. **单个插件的失败被限制在它自己范围内**，绝不拖垮宿主或其他插件。
3. **注册即资源** —— 每次激活创建一个作用域，停用时整体释放（见 scope.py）。

与旧版的差异：不再有 `@action` / `invoke`。插件向外提供的是 **Tool**
（走工具注册表），这才是 Agent 唯一能执行的东西（方案 4.2）。
"""

from __future__ import annotations

import importlib.util
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..contract.manifest import (
    MANIFEST_NAME,
    PluginManifest,
    is_api_compatible,
    is_spec_compatible,
    load_manifest,
)
from ..contract.plugin import PluginState, XbcPlugin
from ..errors import (
    PluginApiMismatch,
    PluginLoadError,
    PluginStateError,
    ToolError,
)
from ..logging_setup import get_logger
from ..tools.schema import SchemaError, validate
from .scope import Scope


@dataclass
class PluginRecord:
    """内核记录的单个插件状态。"""

    manifest: PluginManifest
    instance: XbcPlugin | None = None
    state: PluginState = PluginState.DISCOVERED
    error: str = ""
    blocked_reason: str = ""
    enabled: bool = True
    scope: Scope | None = None
    config_source: str = "default"
    tools_registered: list[str] = field(default_factory=list)
    #: 来自哪个搜索路径的下标（越大优先级越高：用户目录在内置目录之后）
    source_index: int = 0
    #: "builtin" 或 "user"，供界面与诊断展示
    source: str = ""

    @property
    def id(self) -> str:
        return self.manifest.id

    @property
    def name(self) -> str:
        return self.manifest.name

    def to_dict(self) -> dict[str, Any]:
        data = {
            **self.manifest.to_dict(),
            "state": self.state.value,
            "enabled": self.enabled,
            "error": self.error,
            "blocked_reason": self.blocked_reason,
            "config_source": self.config_source,
            "source": self.source,
        }
        return data


class PluginManager:
    def __init__(
        self,
        context: Any,
        search_paths: list[Path | str],
        logger: Any = None,
    ) -> None:
        self._ctx = context
        self._search_paths = [Path(p) for p in search_paths if p]
        self._log = logger or get_logger()
        self._records: dict[str, PluginRecord] = {}
        self._modules: dict[str, Any] = {}

        # 内置插件目录：用于给记录打上 builtin / user 标签
        self._builtin_root: Path | None = None
        try:
            from ..paths import builtin_plugins_dir

            builtin = builtin_plugins_dir()
            self._builtin_root = builtin.resolve() if builtin is not None else None
        except Exception:  # noqa: BLE001 - 拿不到不影响发现
            self._builtin_root = None

    def _source_of(self, child: Path) -> str:
        if self._builtin_root is None:
            return "user"
        try:
            return "builtin" if child.resolve().is_relative_to(self._builtin_root) else "user"
        except OSError:  # pragma: no cover - 路径异常时按用户插件处理
            return "user"

    # ---------------- 发现 ----------------
    @property
    def search_paths(self) -> list[Path]:
        return list(self._search_paths)

    def discover(self) -> list[PluginRecord]:
        """扫描搜索路径下的一级子目录并读取清单。

        清单不合法只记录日志、跳过，不抛异常：
        一个坏插件不能让整个工具箱打不开。
        **只读清单，不导入代码** —— 这就是 `DISCOVERED` 状态"零成本"的含义。

        ## 重名裁决（产品化必需）

        `plugin_search_paths()` 的顺序是 `[内置目录, 用户目录]`，**越靠后优先级越高**：

        - 用户安装的插件**覆盖**同 id 的内置插件 —— 这是"用户升级了内置插件"的常见场景；
        - 优先级相同（同一个目录下的两个文件夹声明同一 id）才是**真冲突**，报错并跳过后者；
        - 同一个目录被重复扫描（界面刷新）安静跳过。
        """
        for index, base in enumerate(self._search_paths):
            if not base.is_dir():
                self._log.debug("插件目录不存在，跳过: %s", base)
                continue
            for child in sorted(base.iterdir()):
                # 跳过隐藏目录：安装/升级用的 .staging-* 与 .backup-* 都藏在这里，
                # 扫描时绝不能把它们当成插件（否则会看到半成品）
                if child.name.startswith("."):
                    continue
                if not child.is_dir() or not (child / MANIFEST_NAME).is_file():
                    continue
                try:
                    manifest = load_manifest(child)
                except Exception as exc:  # noqa: BLE001 - 单个插件的问题必须被隔离
                    self._log.error("插件清单不合法，已跳过 %s: %s", child, exc)
                    continue

                existing = self._records.get(manifest.id)
                if existing is not None:
                    if self._is_same_dir(existing, child):
                        self._log.debug("插件已发现，跳过重复扫描: %s", manifest.id)
                    elif index > existing.source_index:
                        # 用户插件覆盖内置插件（升级内置插件的正常路径）
                        self._log.info(
                            "用户插件覆盖内置插件：%s %s → %s（%s）",
                            manifest.id, existing.manifest.version, manifest.version, child,
                        )
                        self._records[manifest.id] = self._make_record(manifest, index, child)
                    elif index == existing.source_index:
                        self._log.error(
                            "插件 id 冲突：%s 与 %s 都声明了 id=%s（同一目录下），已跳过后者",
                            existing.manifest.path, child, manifest.id,
                        )
                    else:
                        self._log.debug(
                            "插件 %s 已被更高优先级的版本覆盖，跳过 %s",
                            manifest.id, child,
                        )
                    continue

                if not is_spec_compatible(manifest.spec_version):
                    self._log.warning(
                        "插件 %s 的清单规范版本 %s 比内核新，未知字段将被忽略",
                        manifest.id, manifest.spec_version,
                    )
                self._records[manifest.id] = self._make_record(manifest, index, child)
                record = self._records[manifest.id]
                self._log.info(
                    "发现插件: %s v%s (%s)%s",
                    manifest.name, manifest.version, manifest.id,
                    "" if record.enabled else " [已禁用]",
                )
        return self.records()

    def _make_record(self, manifest: PluginManifest, index: int, child: Path) -> PluginRecord:
        return PluginRecord(
            manifest=manifest,
            enabled=self._ctx.is_enabled(manifest),
            source_index=index,
            source=self._source_of(child),
        )

    @staticmethod
    def _is_same_dir(record: PluginRecord, child: Path) -> bool:
        existing_path = record.manifest.path
        if existing_path is None:
            return False
        try:
            return existing_path.resolve() == child.resolve()
        except OSError:  # pragma: no cover
            return False

    def reset(self) -> None:
        """停用并清空全部记录，用于重新扫描。"""
        self.deactivate_all()
        for record in list(self._records.values()):
            self.unload(record.id)
        self._records.clear()

    # ---------------- 查询 ----------------
    def records(self) -> list[PluginRecord]:
        return list(self._records.values())

    def get(self, plugin_id: str) -> PluginRecord:
        record = self._records.get(plugin_id)
        if record is None:
            raise PluginStateError(f"未找到插件 {plugin_id!r}；已发现: {sorted(self._records)}")
        return record

    def summary(self) -> list[dict[str, Any]]:
        """给 CLI / 界面用的只读快照（**不导入代码**）。"""
        return [record.to_dict() for record in self.records()]

    # ---------------- 加载 ----------------
    def load(self, plugin_id: str) -> PluginRecord:
        record = self.get(plugin_id)
        if record.state in (PluginState.LOADED, PluginState.ACTIVE, PluginState.INACTIVE):
            return record

        if not is_api_compatible(record.manifest.api_version):
            self._fail(
                record, "加载",
                PluginApiMismatch(
                    f"插件 API 版本 {record.manifest.api_version} 与内核不兼容"
                ),
            )
            return record

        try:
            plugin_class = self._resolve_class(record.manifest)
            instance = plugin_class()
            if not isinstance(instance, XbcPlugin):
                raise PluginLoadError(
                    f"{record.id}: {record.manifest.entry} 指向的对象不是 XbcPlugin 子类"
                )
            config = self._ctx.effective_config_for(record.manifest)
            self._validate_config(record, config)
            record.config_source = self._ctx.config_source_for(record.manifest)

            record.instance = instance
            self._bind_scope(record)
            self._set_state(record, PluginState.LOADED)
            record.error = ""
            self._log.info("插件已加载: %s", record.id)
        except Exception as exc:  # noqa: BLE001 - 隔离
            self._fail(record, "加载", exc)
        return record

    def _validate_config(self, record: PluginRecord, config: dict) -> None:
        """激活前用清单里的 config_schema 校验配置。"""
        schema = record.manifest.config_schema
        if not schema:
            return
        try:
            validate(config, schema)
        except SchemaError as exc:
            raise PluginLoadError(f"{record.id}: 配置不合法 —— {exc}") from exc

    def _bind_scope(self, record: PluginRecord) -> None:
        """为插件创建一个新的作用域，并注入受限上下文。"""
        scope = Scope(f"plugin:{record.id}", logger=self._log)
        record.scope = scope
        plugin_ctx = self._ctx.for_plugin(record.manifest, scope)
        record.instance.bind(record.manifest, plugin_ctx, plugin_ctx.logger)

    def load_all(self) -> list[PluginRecord]:
        for record in self.records():
            if record.state in (PluginState.DISCOVERED, PluginState.FAILED, PluginState.UNLOADED):
                self.load(record.id)
        return self.records()

    # ---------------- 激活 / 停用 ----------------
    def activate(self, plugin_id: str) -> PluginRecord:
        record = self.get(plugin_id)
        if record.state is PluginState.ACTIVE:
            return record

        if not record.enabled:
            record.blocked_reason = "已被用户禁用（enabled=false）"
            self._log.info("插件 %s 已禁用，跳过激活", record.id)
            return record

        if record.instance is None or record.state is PluginState.UNLOADED:
            self.load(plugin_id)
        if record.instance is None:
            return record  # 加载失败，状态已是 FAILED

        missing = self._missing_dependencies(record)
        if missing:
            record.blocked_reason = f"缺少依赖的服务: {missing}"
            self._set_state(record, PluginState.INACTIVE)
            self._log.warning("插件 %s 保持未激活：%s", record.id, record.blocked_reason)
            return record

        try:
            # 配置在**每次激活时**重新读取：用户改了配置，重新启用就该生效。
            config = self._ctx.effective_config_for(record.manifest)
            record.config_source = self._ctx.config_source_for(record.manifest)
            # 让 ctx.config 与 apply 的入参保持一致 —— 否则重新激活后
            # 插件的 ctx.config 还是上一次的旧配置（曾经的一个真实 bug）
            if record.instance.ctx is not None:
                record.instance.ctx.config = config

            record.instance.apply(record.instance.ctx, config)
            registered = self._ctx.hooks.register(record.instance, record.id)
            record.tools_registered = record.instance.ctx.tools.names()
            record.blocked_reason = ""
            self._set_state(record, PluginState.ACTIVE)
            record.error = ""
            self._log.info(
                "插件已激活: %s（注册工具 %s 个，钩子 %s 个）",
                record.id, len(record.tools_registered), registered,
            )
        except Exception as exc:  # noqa: BLE001 - 隔离
            self._fail(record, "激活", exc)
        return record

    def _missing_dependencies(self, record: PluginRecord) -> list[str]:
        missing: list[str] = []
        for name in record.manifest.requires:
            if self._ctx.service_registry.get(name) is None:
                missing.append(name)
        return missing

    def deactivate(self, plugin_id: str) -> PluginRecord:
        """停用：释放作用域（注册即资源）、注销钩子，然后准备一个干净的作用域。"""
        record = self.get(plugin_id)
        if record.state is not PluginState.ACTIVE:
            return record  # 幂等：未激活的插件 deactivate 不做任何事

        try:
            self._ctx.hooks.unregister(record.id)
            self._ctx.events.off_owner(record.id)
            if record.scope is not None:
                record.scope.dispose()
        except Exception as exc:  # noqa: BLE001 - 停用失败不能阻断用户操作
            self._log.error("插件 %s 停用时出错: %s", record.id, exc)
        finally:
            record.tools_registered = []
            self._set_state(record, PluginState.INACTIVE)
            # 为可能的再次激活准备全新作用域（旧作用域已经释放）
            if record.instance is not None:
                self._bind_scope(record)
            self._log.info("插件已停用: %s", record.id)
        return record

    # ---------------- 卸载 ----------------
    def unload(self, plugin_id: str) -> PluginRecord:
        record = self.get(plugin_id)
        if record.state is PluginState.ACTIVE:
            self.deactivate(plugin_id)

        if record.instance is not None:
            try:
                record.instance.on_unload()
            except Exception as exc:  # noqa: BLE001 - 卸载失败不阻断流程
                self._log.error("插件 %s 的 on_unload 失败: %s", record.id, exc)
            record.instance = None

        if record.scope is not None:
            record.scope.dispose()
            record.scope = None
        self._ctx.hooks.unregister(record.id)
        self._drop_module(record.id)
        self._set_state(record, PluginState.UNLOADED)
        self._log.info("插件已卸载: %s", record.id)
        return record

    def activate_all(self) -> list[PluginRecord]:
        for record in self.records():
            self.activate(record.id)
        return self.records()

    def deactivate_all(self) -> list[PluginRecord]:
        for record in reversed(self.records()):
            if record.state is PluginState.ACTIVE:
                self.deactivate(record.id)
        return self.records()

    # ---------------- 启用 / 禁用（持久化） ----------------
    def enable(self, plugin_id: str) -> PluginRecord:
        record = self.get(plugin_id)
        self._write_enabled(record.id, True)
        record.enabled = True
        record.blocked_reason = ""
        return self.activate(plugin_id)

    def disable(self, plugin_id: str) -> PluginRecord:
        record = self.get(plugin_id)
        if record.state is PluginState.ACTIVE:
            self.deactivate(plugin_id)
        self._write_enabled(record.id, False)
        record.enabled = False
        record.blocked_reason = "已被用户禁用（enabled=false）"
        self._log.info("插件已禁用: %s", plugin_id)
        return record

    def _write_enabled(self, plugin_id: str, enabled: bool) -> None:
        from ..config.layers import write_row

        write_row(self._ctx.paths.user_plugins_config, plugin_id, enabled=enabled)
        self._ctx.reload_plugin_config()

    # ---------------- 内部：导入与状态 ----------------
    def _resolve_class(self, manifest: PluginManifest) -> type:
        module = self._import_module(manifest)
        target = getattr(module, manifest.class_name, None)
        if target is None:
            raise PluginLoadError(
                f"{manifest.id}: 模块 {manifest.module} 中找不到 {manifest.class_name}"
            )
        if not isinstance(target, type):
            raise PluginLoadError(f"{manifest.id}: {manifest.class_name} 不是类")
        return target

    def _import_module(self, manifest: PluginManifest) -> Any:
        """按文件路径导入插件模块（一个文件夹 = 一个插件）。"""
        cached = self._modules.get(manifest.id)
        if cached is not None:
            return cached

        plugin_dir = manifest.path
        if plugin_dir is None:
            raise PluginLoadError(f"{manifest.id}: 清单缺少所在目录信息")

        target = plugin_dir / manifest.module
        if target.suffix != ".py":
            target = target.with_suffix(".py")
        if not target.is_file():
            raise PluginLoadError(f"{manifest.id}: 找不到入口文件 {target}")

        module_name = f"xbc_plugin_{manifest.id}"
        spec = importlib.util.spec_from_file_location(module_name, target)
        if spec is None or spec.loader is None:
            raise PluginLoadError(f"{manifest.id}: 无法为 {target} 建立导入规格")

        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module

        directory = str(plugin_dir)
        inserted = directory not in sys.path
        if inserted:
            sys.path.insert(0, directory)
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            sys.modules.pop(module_name, None)
            raise PluginLoadError(f"{manifest.id}: 导入 {target.name} 失败: {exc}") from exc
        finally:
            if inserted:
                try:
                    sys.path.remove(directory)
                except ValueError:
                    pass

        self._modules[manifest.id] = module
        return module

    def _drop_module(self, plugin_id: str) -> None:
        sys.modules.pop(f"xbc_plugin_{plugin_id}", None)
        self._modules.pop(plugin_id, None)

    def _set_state(self, record: PluginRecord, state: PluginState) -> None:
        old = record.state
        record.state = state
        if record.instance is not None:
            record.instance._state = state  # 状态由内核驱动
        if old is not state:
            try:
                self._ctx.hooks.call(
                    "on_plugin_state_changed",
                    plugin_id=record.id,
                    old_state=old.value,
                    new_state=state.value,
                )
            except Exception:  # noqa: BLE001 - 通知失败不影响状态变更
                pass

    def _fail(self, record: PluginRecord, step: str, exc: BaseException) -> None:
        record.error = f"{step}失败: {exc}"
        self._set_state(record, PluginState.FAILED)
        self._log.error(
            "插件 %s %s失败: %s\n%s", record.id, step, exc, traceback.format_exc()
        )

    # ---------------- 便捷入口 ----------------
    def call_tool(self, tool_name: str, arguments: dict | None = None) -> Any:
        """调用工具（转发到工具注册表，保持入口单一）。"""
        result = self._ctx.tool_registry.call(tool_name, arguments or {})
        if not result.ok:
            raise ToolError(f"[{result.code}] {result.message}")
        return result.value

    # 兼容旧命名（方案里用 start/stop 描述过）：语义等同 activate/deactivate
    start = activate
    stop = deactivate
    start_all = activate_all
    stop_all = deactivate_all
