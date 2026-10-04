"""内核与插件机制的测试。

刻意不依赖 PySide6 —— 内核能脱离界面被测试，是"内核与界面解耦"最直接的收益。

运行方式：
    python -m unittest discover -s tests -v
或
    python -m pytest tests -q
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from xbc.core.context import AppContext  # noqa: E402
from xbc.core.errors import (  # noqa: E402
    CapabilityDenied,
    PluginManifestError,
    PluginStateError,
)
from xbc.core.plugins.base import PluginState  # noqa: E402
from xbc.core.plugins.manager import PluginManager  # noqa: E402
from xbc.core.plugins.manifest import parse_manifest  # noqa: E402
from xbc.core.services.files import FileService  # noqa: E402


def _temp_dir(test: unittest.TestCase) -> Path:
    path = Path(tempfile.mkdtemp(prefix="xbc-test-"))
    test.addCleanup(shutil.rmtree, path, ignore_errors=True)
    return path


class ManifestTests(unittest.TestCase):
    """清单是内核与插件的合同，必须严格校验。"""

    def setUp(self) -> None:
        self.dir = _temp_dir(self)

    def base(self, **overrides) -> dict:
        data = {
            "id": "demo",
            "name": "Demo",
            "version": "0.1.0",
            "api_version": "1.0",
            "entry": "plugin.py:DemoPlugin",
        }
        data.update(overrides)
        return data

    def test_valid_manifest_parsed(self) -> None:
        manifest = parse_manifest(self.base(), self.dir)
        self.assertEqual(manifest.id, "demo")
        self.assertEqual(manifest.class_name, "DemoPlugin")

    def test_missing_required_field_rejected(self) -> None:
        data = self.base()
        del data["entry"]
        with self.assertRaises(PluginManifestError):
            parse_manifest(data, self.dir)

    def test_illegal_id_rejected(self) -> None:
        # 路径穿越必须在清单阶段就被挡住
        with self.assertRaises(PluginManifestError):
            parse_manifest(self.base(id="../evil"), self.dir)

    def test_unknown_capability_rejected(self) -> None:
        with self.assertRaises(PluginManifestError):
            parse_manifest(self.base(capabilities=["teleport"]), self.dir)

    def test_entry_format_enforced(self) -> None:
        with self.assertRaises(PluginManifestError):
            parse_manifest(self.base(entry="DemoPlugin"), self.dir)


class CapabilityTests(unittest.TestCase):
    """最小权限：没声明的能力就是拿不到。"""

    def setUp(self) -> None:
        self.root = _temp_dir(self)
        self.ctx = AppContext.create(root=self.root, console=False)
        # addCleanup 后进先出：close 会先于 _temp_dir 注册的目录删除执行，
        # 否则日志文件句柄未释放，Windows 上目录删不掉（会留下垃圾临时目录）
        self.addCleanup(self.ctx.close)

    def test_declared_capability_injected(self) -> None:
        manifest = parse_manifest(
            {
                "id": "demo",
                "name": "Demo",
                "version": "0.1.0",
                "api_version": "1.0",
                "entry": "plugin.py:DemoPlugin",
                "capabilities": ["files"],
            },
            self.root,
        )
        plugin_ctx = self.ctx.for_plugin(manifest)
        self.assertIsInstance(plugin_ctx.files, FileService)

    def test_undeclared_capability_denied(self) -> None:
        manifest = parse_manifest(
            {
                "id": "demo",
                "name": "Demo",
                "version": "0.1.0",
                "api_version": "1.0",
                "entry": "plugin.py:DemoPlugin",
                "capabilities": ["files"],
            },
            self.root,
        )
        plugin_ctx = self.ctx.for_plugin(manifest)
        with self.assertRaises(CapabilityDenied):
            _ = plugin_ctx.ai
        with self.assertRaises(CapabilityDenied):
            _ = plugin_ctx.ffmpeg

    def test_plugin_data_dir_is_isolated(self) -> None:
        manifest = parse_manifest(
            {
                "id": "demo",
                "name": "Demo",
                "version": "0.1.0",
                "api_version": "1.0",
                "entry": "plugin.py:DemoPlugin",
            },
            self.root,
        )
        plugin_ctx = self.ctx.for_plugin(manifest)
        self.assertEqual(plugin_ctx.data_dir, self.ctx.paths.data_dir / "demo")
        self.assertEqual(plugin_ctx.settings.all(), {})


class LifecycleTests(unittest.TestCase):
    """完整插件机制：发现 → 加载 → 启动 → 调用 → 停止 → 卸载。"""

    def setUp(self) -> None:
        self.root = _temp_dir(self)
        self.ctx = AppContext.create(root=self.root, console=False)
        self.addCleanup(self.ctx.close)
        self.manager = self.ctx.create_plugin_manager()
        self.manager.discover()

    def test_builtin_plugin_discovered(self) -> None:
        self.assertIn("hello_xbc", [record.id for record in self.manager.records()])

    def test_full_lifecycle_and_action(self) -> None:
        record = self.manager.start("hello_xbc")
        self.assertEqual(record.state, PluginState.STARTED)

        result = self.manager.invoke("hello_xbc", "hello", name="测试")
        self.assertEqual(result["runs"], 1)

        written = Path(result["file"])
        self.assertTrue(written.is_file())
        self.assertIn("测试", written.read_text(encoding="utf-8"))

        # 插件配置已持久化
        self.assertEqual(self.ctx.config.get("plugins.hello_xbc.runs"), 1)

        self.assertEqual(self.manager.stop("hello_xbc").state, PluginState.STOPPED)
        self.assertEqual(self.manager.unload("hello_xbc").state, PluginState.UNLOADED)

    def test_second_run_increments_counter(self) -> None:
        self.manager.start("hello_xbc")
        self.manager.invoke("hello_xbc", "hello")
        second = self.manager.invoke("hello_xbc", "hello")
        self.assertEqual(second["runs"], 2)

    def test_invoke_before_start_rejected(self) -> None:
        with self.assertRaises(PluginStateError):
            self.manager.invoke("hello_xbc", "hello")

    def test_unknown_action_rejected(self) -> None:
        self.manager.start("hello_xbc")
        with self.assertRaises(PluginStateError):
            self.manager.invoke("hello_xbc", "no_such_action")

    def test_plugin_failure_is_isolated(self) -> None:
        self.manager.start_all()
        with self.assertRaises(RuntimeError):
            self.manager.invoke("hello_xbc", "fail_on_purpose")

        record = self.manager.get("hello_xbc")
        # 失败被记录下来，但插件与宿主都还活着
        self.assertIn("fail_on_purpose", record.error)
        self.assertEqual(record.state, PluginState.STARTED)
        self.assertTrue(self.manager.invoke("hello_xbc", "probe")["core_version"])

    def test_stop_is_idempotent(self) -> None:
        self.manager.start("hello_xbc")
        self.manager.stop("hello_xbc")
        self.assertEqual(self.manager.stop("hello_xbc").state, PluginState.STOPPED)

    def test_broken_manifest_does_not_break_discovery(self) -> None:
        broken = self.root / "plugins" / "broken_plugin"
        broken.mkdir(parents=True, exist_ok=True)
        (broken / "plugin.json").write_text("{ 这不是合法 JSON", encoding="utf-8")

        manager = PluginManager(self.ctx, [self.root / "plugins"], self.ctx.logger)
        manager.discover()
        # 坏插件被跳过，而不是抛异常
        self.assertEqual(manager.records(), [])

    def test_missing_plugin_id_raises(self) -> None:
        with self.assertRaises(PluginStateError):
            self.manager.get("not_installed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
