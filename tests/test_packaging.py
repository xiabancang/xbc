"""插件产品化基础测试（TASK-006）。

覆盖：包格式、安装、卸载、升级、版本管理、用户数据隔离，
以及"安装流程不改动核心源码"这条验收标准。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
CORE_DIR = SRC_DIR / "xbc"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from xbc.core.context import AppContext  # noqa: E402
from xbc.core.packaging import (  # noqa: E402
    PackageError,
    PluginInstaller,
    PluginVersion,
    build_package,
    extract_package,
    is_package,
    read_package_manifest,
)
from xbc.core.runtime.manager import PluginManager  # noqa: E402

_PLUGIN_BODY = '''
from xbc.core.contract.plugin import XbcPlugin


class Demo(XbcPlugin):
    def apply(self, ctx, config):
        ctx.tools.register(
            "demo_ping",
            self.ping,
            description="ping",
            input_schema={"type": "object", "properties": {}},
            risk="read",
        )

    def ping(self):
        return {"pong": True, "version": self.manifest.version}
'''


def write_plugin_dir(base: Path, plugin_id: str, version: str = "1.0.0", **extra) -> Path:
    target = base / plugin_id
    target.mkdir(parents=True, exist_ok=True)
    manifest = {
        "id": plugin_id,
        "name": plugin_id,
        "version": version,
        "spec_version": "1.0",
        "api_version": "1.0",
        "entry": "plugin.py:Demo",
        "capabilities": ["files"],
        "tools": [{"name": "demo_ping", "description": "ping", "risk": "read"}],
    }
    manifest.update(extra)
    (target / "plugin.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    (target / "plugin.py").write_text(_PLUGIN_BODY, encoding="utf-8")
    return target


def make_zip(path: Path, entries: dict[str, bytes]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return path


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="xbc-pkg-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.build_dir = self.root / "build"
        self.ctx = AppContext.create(root=self.root, console=False)
        self.addCleanup(self.ctx.close)
        self.installer: PluginInstaller = self.ctx.create_installer()

    def package(self, plugin_id: str, version: str = "1.0.0", **extra) -> Path:
        source = write_plugin_dir(self.build_dir, plugin_id, version, **extra)
        return build_package(source, self.build_dir / f"{plugin_id}-{version}.xbcplugin")

    def manager(self) -> PluginManager:
        """只从用户插件目录发现 —— 确保测的是"装上去的那个"。"""
        manager = PluginManager(self.ctx, [self.ctx.paths.user_plugins_dir], self.ctx.logger)
        manager.discover()
        return manager

    def write_user_data(self, plugin_id: str, text: str = "用户数据") -> Path:
        data_dir = self.ctx.paths.plugin_data_dir(plugin_id)
        data_dir.mkdir(parents=True, exist_ok=True)
        target = data_dir / "note.txt"
        target.write_text(text, encoding="utf-8")
        return target


# ================= 版本 =================


class VersionTests(unittest.TestCase):
    def test_parse_valid(self) -> None:
        version = PluginVersion.parse("1.2.3")
        self.assertEqual((version.major, version.minor, version.patch), (1, 2, 3))
        self.assertEqual(str(version), "1.2.3")

    def test_parse_invalid_raises(self) -> None:
        for bad in ("1.2", "v1.2.3", "1.2.3.4", "", "abc"):
            with self.assertRaises(ValueError, msg=bad):
                PluginVersion.parse(bad)
        self.assertIsNone(PluginVersion.try_parse("1.2"))

    def test_numeric_ordering_not_lexicographic(self) -> None:
        """1.2.10 必须大于 1.2.9 —— 字符串比较会在这里翻车。"""
        self.assertLess(PluginVersion.parse("1.2.9"), PluginVersion.parse("1.2.10"))

    def test_major_minor_patch_precedence(self) -> None:
        self.assertLess(PluginVersion.parse("1.9.9"), PluginVersion.parse("2.0.0"))
        self.assertLess(PluginVersion.parse("1.2.9"), PluginVersion.parse("1.3.0"))

    def test_prerelease_is_less_than_release(self) -> None:
        self.assertLess(PluginVersion.parse("1.0.0-beta"), PluginVersion.parse("1.0.0"))
        self.assertLess(PluginVersion.parse("1.0.0-beta.1"), PluginVersion.parse("1.0.0-beta.2"))
        self.assertLess(PluginVersion.parse("1.0.0-alpha"), PluginVersion.parse("1.0.0-beta"))

    def test_build_metadata_ignored_in_comparison(self) -> None:
        self.assertEqual(PluginVersion.parse("1.0.0+aaa"), PluginVersion.parse("1.0.0+bbb"))

    def test_repr_roundtrip(self) -> None:
        for text in ("1.2.3", "0.1.0-beta.1", "2.0.0+build.5"):
            self.assertEqual(str(PluginVersion.parse(text)), text)


# ================= 包格式与安全 =================


class PackageFormatTests(_Base):
    def test_build_and_read(self) -> None:
        package = self.package("demo", "1.2.3")
        self.assertTrue(is_package(package))
        self.assertEqual(package.suffix, ".xbcplugin")

        manifest = read_package_manifest(package)
        self.assertEqual(manifest.id, "demo")
        self.assertEqual(manifest.version, "1.2.3")

    def test_package_is_a_zip_with_manifest_at_root(self) -> None:
        package = self.package("demo")
        with zipfile.ZipFile(package) as archive:
            names = archive.namelist()
        self.assertIn("plugin.json", names)
        self.assertIn("plugin.py", names)
        self.assertFalse(any(name.startswith("/") for name in names))

    def test_manifest_read_does_not_extract(self) -> None:
        """只读清单必须真的"只读" —— 不落盘。"""
        package = self.package("demo")
        before = set(self.root.rglob("*"))
        read_package_manifest(package)
        self.assertEqual(set(self.root.rglob("*")) - before, set())

    def test_build_rejects_non_semver_version(self) -> None:
        source = write_plugin_dir(self.build_dir, "demo", version="v1")
        with self.assertRaises(PackageError) as ctx:
            build_package(source)
        self.assertIn("版本号", str(ctx.exception))

    def test_build_rejects_non_plugin_dir(self) -> None:
        empty = self.build_dir / "empty"
        empty.mkdir(parents=True)
        with self.assertRaises(PackageError):
            build_package(empty)

    def test_read_rejects_missing_manifest(self) -> None:
        package = make_zip(self.root / "bad.xbcplugin", {"readme.txt": b"hi"})
        with self.assertRaises(PackageError):
            read_package_manifest(package)

    def test_read_rejects_non_zip(self) -> None:
        bogus = self.root / "notazip.xbcplugin"
        bogus.write_bytes(b"definitely not a zip")
        with self.assertRaises(PackageError):
            read_package_manifest(bogus)

    def test_extract_rejects_path_traversal(self) -> None:
        """zip-slip：包内 `../` 越界必须被拒绝。"""
        package = make_zip(
            self.root / "slip.xbcplugin",
            {"plugin.json": b"{}", "../evil.txt": b"pwned"},
        )
        with self.assertRaises(PackageError) as ctx:
            extract_package(package, self.root / "out")
        self.assertIn("越界", str(ctx.exception))
        self.assertFalse((self.root / "evil.txt").exists())

    def test_extract_rejects_absolute_path(self) -> None:
        package = make_zip(
            self.root / "abs.xbcplugin",
            {"plugin.json": b"{}", "/tmp/evil.txt": b"x"},
        )
        with self.assertRaises(PackageError):
            extract_package(package, self.root / "out2")

    def test_extract_rejects_backslash_disguise(self) -> None:
        package = make_zip(
            self.root / "bs.xbcplugin",
            {"plugin.json": b"{}", "..\\evil.txt": b"x"},
        )
        with self.assertRaises(PackageError):
            extract_package(package, self.root / "out3")

    def test_extract_rejects_symlink(self) -> None:
        package = self.root / "link.xbcplugin"
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("plugin.json", b"{}")
            info = zipfile.ZipInfo("evil_link")
            info.external_attr = (0xA1FF & 0xFFFF) << 16  # Unix 符号链接模式
            archive.writestr(info, b"/etc/passwd")
        with self.assertRaises(PackageError) as ctx:
            extract_package(package, self.root / "out4")
        self.assertIn("符号链接", str(ctx.exception))

    def test_extract_requires_manifest(self) -> None:
        package = make_zip(self.root / "nomani.xbcplugin", {"a.txt": b"x"})
        with self.assertRaises(PackageError):
            extract_package(package, self.root / "out5")


# ================= 安装 / 卸载 / 升级 =================


class InstallTests(_Base):
    def test_install_places_plugin_and_records_ledger(self) -> None:
        result = self.installer.install(self.package("demo", "1.0.0"))
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.action, "install")
        self.assertEqual(result.version, "1.0.0")

        installed = self.ctx.paths.user_plugins_dir / "demo"
        self.assertTrue((installed / "plugin.json").is_file())

        status = self.installer.get("demo")
        self.assertIsNotNone(status)
        self.assertEqual(status.version, "1.0.0")
        self.assertTrue(status.installed_at)

    def test_installed_plugin_runs(self) -> None:
        self.installer.install(self.package("demo", "1.0.0"))
        manager = self.manager()
        self.assertIn("demo", [r.id for r in manager.records()])

        manager.activate("demo")
        result = self.ctx.tool_registry.call("demo_ping", {})
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.value["version"], "1.0.0")

    def test_reinstall_without_force_is_rejected(self) -> None:
        self.installer.install(self.package("demo"))
        again = self.installer.install(self.package("demo"))
        self.assertFalse(again.ok)
        self.assertIn("已安装", again.message)

    def test_force_reinstall_replaces(self) -> None:
        self.installer.install(self.package("demo", "1.0.0"))
        forced = self.installer.install(self.package("demo", "1.0.0"), force=True)
        self.assertTrue(forced.ok, forced.message)
        self.assertEqual(forced.action, "upgrade")

    def test_install_rejects_bad_package(self) -> None:
        result = self.installer.install(self.root / "missing.xbcplugin")
        self.assertFalse(result.ok)
        self.assertIn("不是插件包", result.message)

    def test_installed_list_is_empty_initially(self) -> None:
        self.assertEqual(self.installer.installed(), [])

    def test_no_staging_leftovers_after_install(self) -> None:
        self.installer.install(self.package("demo"))
        leftovers = [p.name for p in self.ctx.paths.user_plugins_dir.iterdir() if p.name.startswith(".")]
        self.assertEqual(leftovers, [], "安装后不应残留 .staging-* / .backup-*")


class UpgradeTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.installer.install(self.package("demo", "1.0.0"))

    def test_upgrade_newer_version(self) -> None:
        result = self.installer.upgrade(self.package("demo", "1.1.0"))
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.previous_version, "1.0.0")
        self.assertEqual(result.version, "1.1.0")
        self.assertEqual(self.installer.get("demo").version, "1.1.0")

    def test_upgraded_code_is_actually_loaded(self) -> None:
        """升级后 Runtime 必须加载**新**代码，而不是缓存的旧版本。"""
        self.installer.upgrade(self.package("demo", "2.0.0"))
        manager = self.manager()
        manager.activate("demo")
        result = self.ctx.tool_registry.call("demo_ping", {})
        self.assertEqual(result.value["version"], "2.0.0")

    def test_same_version_rejected(self) -> None:
        result = self.installer.upgrade(self.package("demo", "1.0.0"))
        self.assertFalse(result.ok)
        self.assertIn("版本相同", result.message)

    def test_downgrade_rejected_without_force(self) -> None:
        result = self.installer.upgrade(self.package("demo", "0.9.0"))
        self.assertFalse(result.ok)
        self.assertIn("拒绝降级", result.message)
        self.assertEqual(self.installer.get("demo").version, "1.0.0")

    def test_downgrade_allowed_with_force(self) -> None:
        result = self.installer.upgrade(self.package("demo", "0.9.0"), force=True)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(self.installer.get("demo").version, "0.9.0")

    def test_upgrade_not_installed_is_rejected(self) -> None:
        result = self.installer.upgrade(self.package("other", "1.0.0"))
        self.assertFalse(result.ok)
        self.assertIn("尚未安装", result.message)

    def test_ledger_records_history(self) -> None:
        self.installer.upgrade(self.package("demo", "1.1.0"))
        self.installer.upgrade(self.package("demo", "1.2.0"))

        entry = self.installer.get("demo")
        self.assertEqual(entry.version, "1.2.0")
        versions = [item["version"] for item in entry.history]
        self.assertEqual(versions, ["1.0.0", "1.1.0", "1.2.0"])
        self.assertEqual(entry.to_dict()["upgrade_count"], 2)

    def test_broken_package_does_not_damage_installed_plugin(self) -> None:
        """坏包升级失败后，已安装的版本必须完好无损（原子性）。"""
        broken = make_zip(self.root / "broken.xbcplugin", {"plugin.json": b"{ not json"})
        result = self.installer.upgrade(broken)
        self.assertFalse(result.ok)

        self.assertEqual(self.installer.get("demo").version, "1.0.0")

        manager = self.manager()
        manager.activate("demo")
        self.assertTrue(self.ctx.tool_registry.call("demo_ping", {}).ok)

    def test_package_without_entry_file_is_rejected_before_install(self) -> None:
        """缺少入口文件的包必须在安装前被拦住（否则装上去只会在加载时失败）。"""
        bad = make_zip(
            self.root / "noentry.xbcplugin",
            {
                "plugin.json": json.dumps({
                    "id": "demo", "name": "demo", "version": "3.0.0",
                    "spec_version": "1.0", "api_version": "1.0",
                    "entry": "plugin.py:Demo",
                }).encode("utf-8"),
            },
        )
        result = self.installer.upgrade(bad)
        self.assertFalse(result.ok)
        self.assertIn("入口文件", result.message)
        self.assertEqual(self.installer.get("demo").version, "1.0.0")


# ================= 卸载与数据隔离 =================


class UninstallTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.installer.install(self.package("demo", "1.0.0"))
        self.data_file = self.write_user_data("demo")
        self.ctx.config.plugin_section("demo").set("runs", 7)

    def test_uninstall_removes_plugin_but_keeps_user_data(self) -> None:
        result = self.installer.uninstall("demo")

        self.assertTrue(result.ok, result.message)
        self.assertFalse((self.ctx.paths.user_plugins_dir / "demo").exists())
        self.assertTrue(result.data_preserved)
        self.assertTrue(self.data_file.is_file(), "默认卸载必须保留用户数据")
        self.assertIsNone(self.installer.get("demo"))
        self.assertEqual(self.ctx.config.plugin_section("demo").get("runs"), 7)

    def test_purge_removes_user_data_and_config(self) -> None:
        result = self.installer.uninstall("demo", purge=True)

        self.assertTrue(result.ok, result.message)
        self.assertFalse(result.data_preserved)
        self.assertFalse(self.data_file.exists())
        self.assertFalse(self.ctx.paths.plugin_data_dir("demo").exists())
        self.assertIsNone(self.ctx.config.plugin_section("demo").get("runs"))

    def test_uninstall_removes_cache(self) -> None:
        cache = self.ctx.paths.plugin_cache_dir("demo")
        cache.mkdir(parents=True, exist_ok=True)
        (cache / "tmp.bin").write_bytes(b"x")

        self.installer.uninstall("demo")
        self.assertFalse(cache.exists(), "缓存应随卸载一并清除")

    def test_uninstall_unknown_plugin_is_rejected(self) -> None:
        result = self.installer.uninstall("nope")
        self.assertFalse(result.ok)
        self.assertIn("未安装", result.message)

    def test_discover_after_uninstall_finds_nothing(self) -> None:
        self.installer.uninstall("demo")
        manager = self.manager()
        self.assertEqual([r.id for r in manager.records()], [])


class DataIsolationTests(_Base):
    """TASK-006 第 6 项：用户数据目录分离。"""

    def test_upgrade_preserves_user_data_and_config(self) -> None:
        self.installer.install(self.package("demo", "1.0.0"))
        data_file = self.write_user_data("demo", "重要数据")
        self.ctx.config.plugin_section("demo").set("runs", 42)
        cache = self.ctx.paths.plugin_cache_dir("demo")
        cache.mkdir(parents=True, exist_ok=True)
        (cache / "c.bin").write_bytes(b"c")

        self.installer.upgrade(self.package("demo", "2.0.0"))

        self.assertEqual(data_file.read_text(encoding="utf-8"), "重要数据")
        self.assertEqual(self.ctx.config.plugin_section("demo").get("runs"), 42)
        self.assertTrue(cache.exists(), "升级不该动缓存目录")
        self.assertEqual(self.installer.get("demo").version, "2.0.0")

    def test_data_dirs_are_per_plugin(self) -> None:
        """数据目录按插件隔离：一个插件拿不到另一个的数据路径。"""
        first = self.ctx.paths.plugin_data_dir("alpha")
        second = self.ctx.paths.plugin_data_dir("beta")
        self.assertNotEqual(first, second)
        self.assertEqual(first.name, "alpha")
        self.assertEqual(second.name, "beta")

    def test_install_over_existing_data_keeps_it(self) -> None:
        """先有数据、后安装插件（例如重装），数据不能被覆盖。"""
        data_file = self.write_user_data("demo", "装插件前就有的数据")
        self.installer.install(self.package("demo"))
        self.assertEqual(data_file.read_text(encoding="utf-8"), "装插件前就有的数据")


# ================= 验收：安装流程不改动核心源码 =================


class CoreUntouchedTests(_Base):
    def _core_hashes(self) -> dict[str, str]:
        return {
            str(p.relative_to(REPO_ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in CORE_DIR.rglob("*.py")
        }

    def test_full_flow_does_not_modify_core_sources(self) -> None:
        """安装 → 运行 → 升级 → 卸载 全程不得改动 `src/xbc/` 下任何 .py 文件。

        这是 TASK-006 的验收标准"Runtime 不修改核心代码"的直接检验：
        加/升/删插件完全是数据操作，不需要也不应该碰内核源码。
        """
        before = self._core_hashes()
        self.assertTrue(before, "前置条件：应能找到核心源码文件")

        # 安装
        self.installer.install(self.package("demo", "1.0.0"))
        # 运行
        manager = self.manager()
        manager.activate("demo")
        manager.deactivate("demo")
        # 造数据 + 升级
        self.write_user_data("demo")
        self.installer.upgrade(self.package("demo", "2.0.0"))
        # 再运行一次
        manager2 = self.manager()
        manager2.activate("demo")
        manager2.deactivate("demo")
        # 卸载
        self.installer.uninstall("demo")

        after = self._core_hashes()
        self.assertEqual(before, after, "核心源码在安装流程中被改动了")

    def test_runtime_discovers_plugins_without_core_changes(self) -> None:
        """再加一个全新插件也不需要改内核：只有数据目录发生变化。"""
        before = self._core_hashes()

        self.installer.install(self.package("brand_new", "1.0.0"))
        manager = self.manager()
        self.assertIn("brand_new", [r.id for r in manager.records()])
        manager.activate("brand_new")
        self.assertIn("demo_ping", self.ctx.tool_registry.names())

        self.assertEqual(before, self._core_hashes())


class DiscoveryHygieneTests(_Base):
    def test_discover_skips_staging_and_backup_dirs(self) -> None:
        """`.staging-*` / `.backup-*` 不能被当成插件（否则会看到半成品）。"""
        self.installer.install(self.package("demo"))
        base = self.ctx.paths.user_plugins_dir
        for name in (".staging-demo-abc", ".backup-demo-123"):
            folder = base / name
            shutil.copytree(base / "demo", folder)

        manager = self.manager()
        self.assertEqual([r.id for r in manager.records()], ["demo"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
