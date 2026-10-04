"""插件安装 / 卸载 / 升级，以及安装台账。

TASK-006 第 2–6 项都在这里。

## 用户数据目录分离（第 6 项）

卸载时的行为差异就是"数据隔离"的落点：

| 目录 | 内容 | 卸载（默认） | 卸载（`purge=True`） |
|---|---|---|---|
| `plugins/<id>/` | 插件代码 | **删除** | 删除 |
| `cache/<id>/` | 可重建的缓存 | **删除** | 删除 |
| `data/<id>/` | **用户数据** | **保留** | 删除 |
| `config/plugins.json` 中该插件的行 | 用户配置 | **保留** | 删除 |

升级时**只替换 `plugins/<id>/`**，其余目录一律不碰 —— 这样升级不会丢用户数据，
也不需要插件作者写迁移代码（数据格式的迁移由插件自己按需处理）。

## 原子性

安装/升级都是"先解到临时目录 → 校验 → 原子替换"，失败自动回滚。
临时目录以 `.` 开头，`discover()` 会跳过它们，所以扫描时不会看到半成品。
"""

from __future__ import annotations

import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config.layers import read_json, write_json
from ..errors import XbcError
from .archive import (
    MANIFEST_NAME,
    PackageError,
    atomic_replace_directory,
    extract_package,
    is_package,
    read_package_manifest,
)
from .version import PluginVersion

LEDGER_NAME = "installed.json"
LEDGER_VERSION = 1


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class InstallError(XbcError):
    """安装/升级/卸载失败。"""


@dataclass
class InstalledPlugin:
    """一个已安装插件的台账记录。"""

    plugin_id: str
    version: str
    path: Path
    installed_at: str = ""
    updated_at: str = ""
    source: str = ""
    history: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "plugin_id": self.plugin_id,
            "version": self.version,
            "path": str(self.path),
            "installed_at": self.installed_at,
            "updated_at": self.updated_at,
            "source": self.source,
            "upgrade_count": max(0, len(self.history) - 1),
        }


@dataclass
class InstallResult:
    """安装类操作的结果。**永远返回结果对象，不抛异常**（便于 CLI/界面直接展示）。"""

    ok: bool
    action: str  # install / upgrade / uninstall
    plugin_id: str = ""
    version: str = ""
    previous_version: str = ""
    path: str = ""
    data_dir: str = ""
    data_preserved: bool = True
    message: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "ok": self.ok,
            "action": self.action,
            "plugin_id": self.plugin_id,
        }
        if self.ok:
            data.update(
                {
                    "version": self.version,
                    "previous_version": self.previous_version,
                    "path": self.path,
                    "data_dir": self.data_dir,
                    "data_preserved": self.data_preserved,
                }
            )
        if self.message:
            data["message"] = self.message
        if self.warnings:
            data["warnings"] = self.warnings
        return data


class PluginInstaller:
    """把插件包落到用户插件目录，并维护安装台账。"""

    def __init__(
        self,
        paths: Any,
        logger: Any = None,
        config: Any = None,
        refresh_config: Any = None,
    ) -> None:
        self.paths = paths
        self._log = logger
        #: 内核配置对象（可选）。有它时，"彻底卸载"通过它清理，
        #: 保证内存副本与磁盘一致；没有时退化为直接改文件。
        self._config = config
        #: 清理完配置后通知宿主重新合并三层配置（可选）
        self._refresh_config = refresh_config

    # ---------------- 台账 ----------------
    @property
    def ledger_path(self) -> Path:
        return self.paths.installed_ledger

    def _read_ledger(self) -> dict[str, dict]:
        data = read_json(self.ledger_path)
        if not isinstance(data, dict):
            return {}
        plugins = data.get("plugins")
        return {k: v for k, v in plugins.items() if isinstance(v, dict)} if isinstance(plugins, dict) else {}

    def _write_ledger(self, plugins: dict[str, dict]) -> None:
        write_json(self.ledger_path, {"version": LEDGER_VERSION, "plugins": plugins})

    # ---------------- 查询 ----------------
    def installed(self) -> list[InstalledPlugin]:
        """已安装插件列表。

        **以磁盘上的 `plugin.json` 为准**，台账只补安装元数据 ——
        这样即使台账丢失或被手工改动，也不会报出错误的版本。
        """
        ledger = self._read_ledger()
        result: list[InstalledPlugin] = []
        base = self.paths.user_plugins_dir
        if not base.is_dir():
            return result

        for child in sorted(base.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            if not (child / MANIFEST_NAME).is_file():
                continue
            try:
                manifest = read_package_manifest_dir(child)
            except Exception:  # noqa: BLE001 - 坏插件不影响列出其他插件
                continue
            entry = ledger.get(manifest.id, {})
            result.append(
                InstalledPlugin(
                    plugin_id=manifest.id,
                    version=manifest.version,
                    path=child,
                    installed_at=str(entry.get("installed_at", "")),
                    updated_at=str(entry.get("updated_at", "")),
                    source=str(entry.get("source", "")),
                    history=list(entry.get("history", []) or []),
                )
            )
        return result

    def get(self, plugin_id: str) -> InstalledPlugin | None:
        return next((p for p in self.installed() if p.plugin_id == plugin_id), None)

    def status(self, plugin_id: str) -> dict[str, Any]:
        """单个插件的安装状态（供 CLI / 界面展示）。"""
        plugin = self.get(plugin_id)
        return plugin.to_dict() if plugin else {"plugin_id": plugin_id, "installed": False}

    # ---------------- 安装 ----------------
    def install(self, package: Path | str, *, force: bool = False) -> InstallResult:
        """安装一个新插件。已安装则拒绝（要走 `upgrade`）。"""
        try:
            manifest, version = self._read_manifest(package)
        except (PackageError, XbcError, ValueError) as exc:
            return InstallResult(ok=False, action="install", message=str(exc))

        existing = self.get(manifest.id)
        if existing is not None and not force:
            return InstallResult(
                ok=False,
                action="install",
                plugin_id=manifest.id,
                previous_version=existing.version,
                message=(
                    f"{manifest.id} 已安装（版本 {existing.version}）；"
                    "升级请用 upgrade，强制覆盖请加 --force"
                ),
            )

        if existing is not None:
            return self.upgrade(package, force=True)

        return self._place(
            package=package,
            manifest=manifest,
            version=version,
            action="install",
            previous_version="",
        )

    # ---------------- 升级 ----------------
    def upgrade(self, package: Path | str, *, force: bool = False) -> InstallResult:
        """升级已安装的插件。默认只接受**更新的版本**。"""
        try:
            manifest, version = self._read_manifest(package)
        except (PackageError, XbcError, ValueError) as exc:
            return InstallResult(ok=False, action="upgrade", message=str(exc))

        existing = self.get(manifest.id)
        if existing is None:
            return InstallResult(
                ok=False,
                action="upgrade",
                plugin_id=manifest.id,
                message=f"{manifest.id} 尚未安装；请先用 install",
            )

        current = PluginVersion.try_parse(existing.version)
        incoming = version
        if current is None:
            if not force:
                return InstallResult(
                    ok=False,
                    action="upgrade",
                    plugin_id=manifest.id,
                    previous_version=existing.version,
                    message=f"已安装版本 {existing.version!r} 不是合法版本号，无法比较；可加 --force 强制覆盖",
                )
        elif not force:
            if incoming < current:
                return InstallResult(
                    ok=False,
                    action="upgrade",
                    plugin_id=manifest.id,
                    version=str(incoming),
                    previous_version=existing.version,
                    message=f"拒绝降级：已安装 {existing.version}，包里是 {incoming}；确实要降级请加 --force",
                )
            if incoming == current:
                return InstallResult(
                    ok=False,
                    action="upgrade",
                    plugin_id=manifest.id,
                    version=str(incoming),
                    previous_version=existing.version,
                    message=f"版本相同（{incoming}），无需升级；要重装请加 --force",
                )

        return self._place(
            package=package,
            manifest=manifest,
            version=version,
            action="upgrade",
            previous_version=existing.version,
        )

    # ---------------- 卸载 ----------------
    def uninstall(self, plugin_id: str, *, purge: bool = False) -> InstallResult:
        """卸载插件。

        默认**保留用户数据与配置**；`purge=True` 才会一并清除。
        """
        existing = self.get(plugin_id)
        data_dir = self.paths.plugin_data_dir(plugin_id)
        cache_dir = self.paths.plugin_cache_dir(plugin_id)

        if existing is None:
            has_data = data_dir.exists()
            if not has_data:
                return InstallResult(
                    ok=False, action="uninstall", plugin_id=plugin_id,
                    message=f"未安装：{plugin_id}",
                )
            # 插件已不在，但有残留数据 → 允许用 purge 清理
            if not purge:
                return InstallResult(
                    ok=False, action="uninstall", plugin_id=plugin_id,
                    data_dir=str(data_dir),
                    message=f"未安装：{plugin_id}（但数据目录仍存在；清理请加 --purge）",
                )
            shutil.rmtree(data_dir, ignore_errors=True)
            return InstallResult(
                ok=True, action="uninstall", plugin_id=plugin_id,
                data_dir=str(data_dir), data_preserved=False,
                message="插件未安装，已清理残留数据",
            )

        plugin_path = existing.path
        try:
            shutil.rmtree(plugin_path)
        except OSError as exc:
            return InstallResult(
                ok=False, action="uninstall", plugin_id=plugin_id,
                message=f"删除插件目录失败：{exc}",
            )

        # 缓存一定删（可重建）
        shutil.rmtree(cache_dir, ignore_errors=True)

        data_preserved = True
        if purge:
            shutil.rmtree(data_dir, ignore_errors=True)
            self._remove_config_row(plugin_id)
            data_preserved = False

        ledger = self._read_ledger()
        ledger.pop(plugin_id, None)
        self._write_ledger(ledger)

        if self._log:
            self._log.info(
                "已卸载插件 %s（版本 %s）%s",
                plugin_id, existing.version, "并清除用户数据" if purge else "；用户数据已保留",
            )

        return InstallResult(
            ok=True,
            action="uninstall",
            plugin_id=plugin_id,
            version=existing.version,
            path=str(plugin_path),
            data_dir=str(data_dir),
            data_preserved=data_preserved,
            message="插件已卸载" + ("，用户数据已清除" if purge else "，用户数据已保留"),
        )

    # ---------------- 打包 ----------------
    def build(self, plugin_dir: Path | str, output: Path | str | None = None) -> Path:
        from .archive import build_package

        return build_package(plugin_dir, output)

    # ---------------- 内部 ----------------
    def _read_manifest(self, package: Path | str) -> tuple[Any, PluginVersion]:
        source = Path(package)
        if not is_package(source):
            raise PackageError(
                f"不是插件包（需要 .xbcplugin 或 .zip）：{source}"
            )
        manifest = read_package_manifest(source)
        version = PluginVersion.try_parse(manifest.version)
        if version is None:
            raise PackageError(
                f"插件 {manifest.id} 的版本号 {manifest.version!r} 不是合法的 主.次.修订 形式，无法做版本管理"
            )
        return manifest, version

    def _place(
        self,
        *,
        package: Path | str,
        manifest: Any,
        version: PluginVersion,
        action: str,
        previous_version: str,
    ) -> InstallResult:
        plugins_dir = self.paths.user_plugins_dir
        plugins_dir.mkdir(parents=True, exist_ok=True)
        target = plugins_dir / manifest.id
        staging = plugins_dir / f".staging-{manifest.id}-{uuid.uuid4().hex[:8]}"
        warnings: list[str] = []

        try:
            extract_package(package, staging)

            # 解包后再校验一次：防止包里 plugin.json 与实际内容不一致
            staged_manifest = read_package_manifest_dir(staging)
            if staged_manifest.id != manifest.id or staged_manifest.version != manifest.version:
                raise PackageError(
                    "包内清单在解包前后不一致，已中止安装"
                    f"（包声明 {manifest.id}@{manifest.version}，实际 {staged_manifest.id}@{staged_manifest.version}）"
                )

            from ..contract.manifest import is_api_compatible

            if not is_api_compatible(staged_manifest.api_version):
                warnings.append(
                    f"插件要求 API {staged_manifest.api_version}，当前内核为不同主版本，加载时会被拒绝"
                )

            backup = atomic_replace_directory(
                staging, target, backup_root=plugins_dir
            )
            if backup is not None:
                shutil.rmtree(backup, ignore_errors=True)
        except Exception as exc:  # noqa: BLE001 - 失败必须回滚并返回可读结果
            shutil.rmtree(staging, ignore_errors=True)
            if self._log:
                self._log.error("插件 %s 失败：%s", manifest.id, exc)
            return InstallResult(
                ok=False,
                action=action,
                plugin_id=manifest.id,
                version=str(version),
                previous_version=previous_version,
                message=f"{action} 失败：{exc}",
            )

        self._record(manifest.id, str(version), str(version), Path(package).name, action)

        data_dir = self.paths.plugin_data_dir(manifest.id)
        if self._log:
            self._log.info("%s 完成：%s@%s → %s", action, manifest.id, version, target)

        return InstallResult(
            ok=True,
            action=action,
            plugin_id=manifest.id,
            version=str(version),
            previous_version=previous_version,
            path=str(target),
            data_dir=str(data_dir),
            data_preserved=True,  # 安装/升级从不碰数据目录
            warnings=warnings,
        )

    def _record(self, plugin_id: str, version: str, source_version: str, source: str, action: str) -> None:
        ledger = self._read_ledger()
        entry = ledger.get(plugin_id, {})
        now = _now()
        history = list(entry.get("history", []) or [])
        history.append({"version": version, "at": now, "action": action})

        ledger[plugin_id] = {
            "version": version,
            "installed_at": entry.get("installed_at") or now,
            "updated_at": now,
            "source": source,
            "history": history[-20:],  # 只留最近 20 条，避免无限增长
        }
        self._write_ledger(ledger)

    def _remove_config_row(self, plugin_id: str) -> None:
        """`purge` 时清掉该插件的**全部**配置痕迹。

        插件配置分散在两处，两处都要清：

        - `config/plugins.json` —— 用户层的三层配置行（enabled / config）
        - `config.json` 的 `plugins.<id>` —— 插件运行期自己维护的设置（如调用次数）

        只清前者是曾经的一个真实缺陷：用户点了"彻底卸载"，插件的运行期设置还留着。
        """
        # 1) 运行期设置（config.json 的 plugins.<id>）
        if self._config is not None:
            self._config.remove_plugin_section(plugin_id)
        else:
            kernel_path = self.paths.config_file
            kernel_data = read_json(kernel_path)
            if isinstance(kernel_data, dict):
                plugins = kernel_data.get("plugins")
                if isinstance(plugins, dict) and plugin_id in plugins:
                    plugins.pop(plugin_id, None)
                    write_json(kernel_path, kernel_data)

        # 2) 用户层的三层配置行
        path = self.paths.user_plugins_config
        data = read_json(path)
        if isinstance(data, dict):
            plugins = data.get("plugins")
            if isinstance(plugins, dict) and plugin_id in plugins:
                plugins.pop(plugin_id, None)
                write_json(path, data)

        # 3) 让宿主内存里的合并结果与磁盘一致
        if self._refresh_config is not None:
            try:
                self._refresh_config()
            except Exception as exc:  # noqa: BLE001 - 刷新失败不该影响卸载结果
                if self._log:
                    self._log.warning("卸载后刷新插件配置失败: %s", exc)


def read_package_manifest_dir(plugin_dir: Path | str):
    """读一个**目录**里的 plugin.json（与包读取区分开，语义更清楚）。"""
    from ..contract.manifest import load_manifest

    return load_manifest(Path(plugin_dir))
