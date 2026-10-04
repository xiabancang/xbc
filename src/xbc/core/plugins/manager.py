"""插件管理器：发现 → 加载 → 启动 → 调用 → 停止 → 卸载。

三条设计原则：
1. 内核不认识任何具体插件，只通过清单 + 基类契约驱动。
2. 单个插件的失败被限制在它自己范围内，绝不拖垮宿主或其他插件。
3. 生命周期由状态机约束，非法转换直接拒绝。
"""

from __future__ import annotations

import importlib.util
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import PluginApiMismatch, PluginLoadError, PluginStateError
from ..logging_setup import get_logger
from .base import PluginState, XbcPlugin
from .manifest import MANIFEST_NAME, PluginManifest, is_api_compatible, load_manifest


@dataclass
class PluginRecord:
    """内核记录的单个插件状态。"""

    manifest: PluginManifest
    instance: XbcPlugin | None = None
    state: PluginState = PluginState.DISCOVERED
    error: str = ""

    @property
    def id(self) -> str:
        return self.manifest.id

    @property
    def name(self) -> str:
        return self.manifest.name

    def to_dict(self) -> dict[str, Any]:
        instance_actions = sorted(self.instance.actions()) if self.instance else []
        return {
            **self.manifest.to_dict(),
            "state": self.state.value,
            "error": self.error,
            "actions": instance_actions or list(self.manifest.actions),
        }


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

    # ---------------- 发现 ----------------
    @property
    def search_paths(self) -> list[Path]:
        return list(self._search_paths)

    def discover(self) -> list["PluginRecord"]:
        """扫描搜索路径下的一级子目录并读取清单。

        清单不合法只记录日志、跳过，不抛异常：
        一个坏插件不能让整个工具箱打不开。
        """
        for base in self._search_paths:
            if not base.is_dir():
                self._log.debug("插件目录不存在，跳过: %s", base)
                continue
            for child in sorted(base.iterdir()):
                if not child.is_dir() or not (child / MANIFEST_NAME).is_file():
                    continue
                try:
                    manifest = load_manifest(child)
                except Exception as exc:  # noqa: BLE001 - 单个插件的问题必须被隔离
                    self._log.error("插件清单不合法，已跳过 %s: %s", child, exc)
                    continue
                if manifest.id in self._records:
                    self._log.error("插件 id 重复，已跳过 %s（id=%s）", child, manifest.id)
                    continue
                self._records[manifest.id] = PluginRecord(manifest=manifest)
                self._log.info("发现插件: %s v%s (%s)", manifest.name, manifest.version, manifest.id)
        return self.records()

    def reset(self) -> None:
        """停止并清空全部记录，用于重新扫描。"""
        self.stop_all()
        for record in list(self._records.values()):
            self.unload(record.id)
        self._records.clear()

    # ---------------- 查询 ----------------
    def records(self) -> list[PluginRecord]:
        return list(self._records.values())

    def get(self, plugin_id: str) -> PluginRecord:
        record = self._records.get(plugin_id)
        if record is None:
            raise PluginStateError(
                f"未找到插件 {plugin_id!r}；已发现: {sorted(self._records)}"
            )
        return record

    def summary(self) -> list[dict[str, Any]]:
        """给 CLI / 图形界面用的只读快照。"""
        return [record.to_dict() for record in self.records()]

    # ---------------- 加载 ----------------
    def load(self, plugin_id: str) -> PluginRecord:
        record = self.get(plugin_id)
        if record.state in (PluginState.LOADED, PluginState.STARTED):
            return record

        if not is_api_compatible(record.manifest.api_version):
            self._fail(
                record,
                "加载",
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
            plugin_context = self._ctx.for_plugin(record.manifest)
            instance.bind(record.manifest, plugin_context, plugin_context.logger)
            instance.on_load()
            instance._state = PluginState.LOADED  # 状态由内核驱动，插件不要自己改
            record.instance = instance
            record.state = PluginState.LOADED
            record.error = ""
            self._log.info("插件已加载: %s", record.id)
        except Exception as exc:  # noqa: BLE001 - 隔离
            self._fail(record, "加载", exc)
        return record

    def load_all(self) -> list[PluginRecord]:
        for record in self.records():
            if record.state in (PluginState.DISCOVERED, PluginState.FAILED):
                self.load(record.id)
        return self.records()

    # ---------------- 启动 / 停止 ----------------
    def start(self, plugin_id: str) -> PluginRecord:
        record = self.get(plugin_id)
        if record.state is PluginState.STARTED:
            return record
        if record.instance is None:
            self.load(plugin_id)
        if record.instance is None:
            return record  # 加载失败，状态已是 FAILED

        try:
            record.instance.on_start()
            record.instance._state = PluginState.STARTED
            record.state = PluginState.STARTED
            record.error = ""
            self._log.info("插件已启动: %s", record.id)
        except Exception as exc:  # noqa: BLE001 - 隔离
            self._fail(record, "启动", exc)
        return record

    def stop(self, plugin_id: str) -> PluginRecord:
        record = self.get(plugin_id)
        if record.instance is None or record.state is not PluginState.STARTED:
            return record  # 幂等：未启动的插件 stop 不做任何事
        try:
            record.instance.on_stop()
        except Exception as exc:  # noqa: BLE001 - 隔离
            self._fail(record, "停止", exc)
            return record
        record.instance._state = PluginState.STOPPED
        record.state = PluginState.STOPPED
        self._log.info("插件已停止: %s", record.id)
        return record

    # ---------------- 卸载 ----------------
    def unload(self, plugin_id: str) -> PluginRecord:
        record = self.get(plugin_id)
        if record.state is PluginState.STARTED:
            self.stop(plugin_id)

        if record.instance is not None:
            try:
                record.instance.on_unload()
            except Exception as exc:  # noqa: BLE001 - 卸载失败不阻断流程
                self._fail(record, "卸载", exc)
                self._log.error("插件 %s 卸载钩子失败: %s", record.id, exc)
            record.instance = None

        self._drop_module(record.id)
        record.state = PluginState.UNLOADED
        self._log.info("插件已卸载: %s", record.id)
        return record

    def start_all(self) -> list[PluginRecord]:
        for record in self.records():
            self.start(record.id)
        return self.records()

    def stop_all(self) -> list[PluginRecord]:
        for record in reversed(self.records()):
            self.stop(record.id)
        return self.records()

    # ---------------- 调用动作 ----------------
    def invoke(self, plugin_id: str, action_name: str, **kwargs: Any) -> Any:
        record = self.get(plugin_id)
        if record.instance is None:
            raise PluginStateError(f"插件 {plugin_id} 尚未加载，无法调用动作 {action_name!r}")
        if record.state is not PluginState.STARTED:
            raise PluginStateError(
                f"插件 {plugin_id} 当前状态为 {record.state.value}，"
                "只有 started 状态才能调用动作"
            )
        try:
            return record.instance.invoke(action_name, **kwargs)
        except Exception as exc:  # noqa: BLE001 - 记录后仍向调用方抛出，便于 CLI 报错
            record.error = f"动作 {action_name} 执行失败: {exc}"
            self._log.error(
                "插件 %s 的动作 %s 执行失败: %s\n%s",
                plugin_id,
                action_name,
                exc,
                traceback.format_exc(),
            )
            raise

    # ---------------- 内部：导入与失败处理 ----------------
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
        """按文件路径导入插件模块。

        不使用包入口点，是为了让"一个文件夹 = 一个插件"成立，
        用户把插件文件夹复制进来/删掉就等于安装/卸载（插件商城的落地形式）。
        """
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

        # 临时把插件目录加入 sys.path，使插件可以 import 同目录的辅助模块
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

    def _fail(self, record: PluginRecord, step: str, exc: BaseException) -> None:
        record.state = PluginState.FAILED
        record.error = f"{step}失败: {exc}"
        self._log.error(
            "插件 %s %s失败: %s\n%s", record.id, step, exc, traceback.format_exc()
        )
