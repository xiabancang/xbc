"""Plugin Runtime V1 的测试套件。

对应《夏半仓 Plugin Runtime 技术方案 V1》第 6.4 节的验收标准 A1–A10。

刻意不依赖 PySide6 —— 运行时能脱离界面被测试，是"内核与界面解耦"的直接收益。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from xbc.core.config.layers import config_defaults, merge_rows, write_row  # noqa: E402
from xbc.core.contract.hookspec import (  # noqa: E402
    HookRelay,
    HookValidationError,
    hookimpl,
    hookspec,
)
from xbc.core.contract.manifest import parse_manifest  # noqa: E402
from xbc.core.contract.plugin import PluginState, XbcPlugin  # noqa: E402
from xbc.core.context import AppContext  # noqa: E402
from xbc.core.errors import (  # noqa: E402
    CapabilityDenied,
    PluginManifestError,
    PluginStateError,
    SkillError,
)
from xbc.core.runtime.manager import PluginManager  # noqa: E402
from xbc.core.runtime.registry import Layer, LayeredRegistry  # noqa: E402
from xbc.core.runtime.scope import Scope, ScopeDisposed  # noqa: E402

BUILTIN_PLUGINS = REPO_ROOT / "plugins"

# ---------------- 临时插件模板 ----------------

_HEALTHY_PLUGIN = '''
from xbc.core.contract.plugin import XbcPlugin


class Demo(XbcPlugin):
    def apply(self, ctx, config):
        self.seen_config = dict(config)
        ctx.tools.register(
            "demo_echo",
            self.echo,
            description="原样返回输入",
            input_schema={
                "type": "object",
                "properties": {"text": {"type": "string", "default": "hi"}},
            },
            risk="read",
        )
        ctx.tools.register(
            "demo_write",
            self.do_write,
            description="写文件（write 风险）",
            input_schema={"type": "object", "properties": {}},
            risk="write",
        )
        ctx.events.on("demo/ping", self.on_ping, owner=ctx.plugin_id)

    def echo(self, text="hi"):
        return {"echo": text, "config": dict(self.ctx.config)}

    def do_write(self):
        target = self.ctx.files.write_text(self.ctx.data_dir / "out.txt", "written")
        return {"file": str(target)}

    def on_ping(self, **_):
        self.ctx.settings.set("pings", int(self.ctx.settings.get("pings", 0) or 0) + 1)
'''

_BROKEN_APPLY_PLUGIN = '''
from xbc.core.contract.plugin import XbcPlugin


class Demo(XbcPlugin):
    def apply(self, ctx, config):
        raise RuntimeError("apply 故意失败")
'''

_BROKEN_IMPORT_PLUGIN = "this is not valid python!!!\n"


def _manifest(plugin_id: str, **overrides) -> dict:
    data = {
        "id": plugin_id,
        "name": plugin_id,
        "version": "0.1.0",
        "spec_version": "1.0",
        "api_version": "1.0",
        "entry": "plugin.py:Demo",
    }
    data.update(overrides)
    return data


def write_plugin(base: Path, plugin_id: str, body: str = _HEALTHY_PLUGIN, **manifest_overrides) -> Path:
    folder = base / plugin_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "plugin.json").write_text(
        json.dumps(_manifest(plugin_id, **manifest_overrides), ensure_ascii=False),
        encoding="utf-8",
    )
    (folder / "plugin.py").write_text(body, encoding="utf-8")
    return folder


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="xbc-rt-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.ctx = AppContext.create(root=self.root, console=False)
        self.addCleanup(self.ctx.close)

    def manager(self, *extra_roots: Path) -> PluginManager:
        roots = [BUILTIN_PLUGINS, *extra_roots]
        manager = PluginManager(self.ctx, roots, self.ctx.logger)
        manager.discover()
        return manager


# ================= A1 / A2 / A4 / A5 / A6：生命周期与隔离 =================


class LifecycleTests(_Base):
    def test_every_builtin_plugin_dir_is_discovered(self) -> None:
        """发现结果应等于内置插件目录的真实内容（不硬编码插件清单）。"""
        expected = {
            folder.name
            for folder in BUILTIN_PLUGINS.iterdir()
            if (folder / "plugin.json").is_file()
        }
        found = {r.id for r in self.manager().records()}
        self.assertEqual(expected, found)

    def test_discover_does_not_import_code(self) -> None:
        """A1：清单里有名称，且**不导入插件代码、不实例化、不建作用域**。"""
        for name in [n for n in sys.modules if n.startswith("xbc_plugin_")]:
            del sys.modules[name]

        manager = self.manager()
        for record in manager.records():
            self.assertTrue(record.manifest.name)
            self.assertIsNone(record.instance, f"{record.id} 不应被实例化")
            self.assertIsNone(record.scope, f"{record.id} 不应建作用域")
            self.assertNotIn(f"xbc_plugin_{record.id}", sys.modules)
        self.assertEqual(len(self.ctx.tool_registry), 0)

        # 但清单里声明的工具在未激活时也已可见（零成本展示）
        toolbox = manager.get("text_toolbox")
        self.assertIn("text_stats", [t.name for t in toolbox.manifest.tools])

    def test_activate_registers_and_deactivate_releases(self) -> None:
        """A5：注册即资源 —— 停用后注册全部清空，且没有残留。

        这里断言的是**关系**（已注册数 == 清单声明数、停用后归零），
        而不是硬编码插件数量 —— 否则每加一个插件这条测试都会假失败。
        """
        manager = self.manager()
        manager.activate_all()

        registered = len(self.ctx.tool_registry)
        declared = sum(
            len(r.manifest.tools)
            for r in manager.records()
            if r.state is PluginState.ACTIVE
        )
        self.assertGreater(registered, 0)
        self.assertEqual(registered, declared, "注册的工具数应与清单声明一致")
        self.assertGreater(len(self.ctx.skill_catalog), 0)

        hello = manager.get("hello_xbc")
        self.assertIsNotNone(hello.scope)
        self.assertGreater(hello.scope.effect_count, 0)

        manager.deactivate_all()
        self.assertEqual(len(self.ctx.tool_registry), 0)
        self.assertEqual(len(self.ctx.skill_catalog), 0)
        for record in manager.records():
            self.assertEqual(record.state, PluginState.INACTIVE)
            self.assertIsNotNone(record.scope)
            self.assertEqual(record.scope.effect_count, 0)

    def test_reactivation_after_deactivate(self) -> None:
        manager = self.manager()
        manager.activate_all()
        before = len(self.ctx.tool_registry)
        manager.deactivate_all()
        manager.activate_all()
        self.assertEqual(len(self.ctx.tool_registry), before)

    def test_state_machine_and_unload(self) -> None:
        manager = self.manager()
        record = manager.get("hello_xbc")
        self.assertEqual(record.state, PluginState.DISCOVERED)
        manager.load("hello_xbc")
        self.assertEqual(record.state, PluginState.LOADED)
        manager.activate("hello_xbc")
        self.assertEqual(record.state, PluginState.ACTIVE)
        manager.deactivate("hello_xbc")
        self.assertEqual(record.state, PluginState.INACTIVE)
        manager.unload("hello_xbc")
        self.assertEqual(record.state, PluginState.UNLOADED)

    def test_deactivate_is_idempotent(self) -> None:
        manager = self.manager()
        manager.activate("hello_xbc")
        manager.deactivate("hello_xbc")
        self.assertEqual(manager.deactivate("hello_xbc").state, PluginState.INACTIVE)

    def test_failed_plugin_is_isolated(self) -> None:
        """A4：一个插件 apply 抛异常 → 它 FAILED，其他插件仍 ACTIVE，宿主正常。"""
        fixture = self.root / "fixtures"
        write_plugin(fixture, "broken_apply", _BROKEN_APPLY_PLUGIN)
        manager = self.manager(fixture)
        manager.activate_all()

        broken = manager.get("broken_apply")
        self.assertEqual(broken.state, PluginState.FAILED)
        self.assertIn("apply 故意失败", broken.error)

        for other in ("hello_xbc", "text_toolbox"):
            self.assertEqual(manager.get(other).state, PluginState.ACTIVE)
        self.assertTrue(self.ctx.tool_registry.get("hello_probe"))

    def test_broken_import_is_isolated(self) -> None:
        fixture = self.root / "fixtures"
        write_plugin(fixture, "broken_import", _BROKEN_IMPORT_PLUGIN)
        manager = self.manager(fixture)
        manager.activate_all()
        self.assertEqual(manager.get("broken_import").state, PluginState.FAILED)
        self.assertEqual(manager.get("text_toolbox").state, PluginState.ACTIVE)

    def test_missing_dependency_stays_inactive_not_failed(self) -> None:
        """A6：依赖缺失 → 保持 INACTIVE（不是报错）。"""
        fixture = self.root / "fixtures"
        write_plugin(fixture, "needs_service", requires=["not_there"])
        manager = self.manager(fixture)
        manager.activate_all()

        record = manager.get("needs_service")
        self.assertEqual(record.state, PluginState.INACTIVE)
        self.assertEqual(record.error, "")
        self.assertIn("not_there", record.blocked_reason)
        self.assertEqual(manager.get("text_toolbox").state, PluginState.ACTIVE)

    def test_unknown_plugin_id_raises(self) -> None:
        with self.assertRaises(PluginStateError):
            self.manager().get("nope")

    def test_broken_manifest_is_skipped(self) -> None:
        fixture = self.root / "fixtures"
        bad = fixture / "bad_manifest"
        bad.mkdir(parents=True)
        (bad / "plugin.json").write_text("{ 这不是合法 JSON", encoding="utf-8")
        manager = self.manager(fixture)
        self.assertNotIn("bad_manifest", [r.id for r in manager.records()])


class EnableDisableTests(_Base):
    def test_disable_persists_across_restart(self) -> None:
        """A2：禁用后重启仍是禁用（用户层配置持久化）。"""
        manager = self.manager()
        manager.disable("text_toolbox")
        self.assertFalse(manager.get("text_toolbox").enabled)

        # 模拟"重启"：同一个 root 上重新装配
        ctx2 = AppContext.create(root=self.root, console=False)
        self.addCleanup(ctx2.close)
        manager2 = PluginManager(ctx2, [BUILTIN_PLUGINS], ctx2.logger)
        manager2.discover()
        self.assertFalse(manager2.get("text_toolbox").enabled)

        manager2.activate_all()
        self.assertEqual(manager2.get("text_toolbox").state, PluginState.DISCOVERED)
        self.assertIn("禁用", manager2.get("text_toolbox").blocked_reason)
        # 另一个插件不受影响
        self.assertEqual(manager2.get("hello_xbc").state, PluginState.ACTIVE)

    def test_enable_restores_activation(self) -> None:
        manager = self.manager()
        manager.disable("text_toolbox")
        manager.enable("text_toolbox")
        self.assertTrue(manager.get("text_toolbox").enabled)
        self.assertEqual(manager.get("text_toolbox").state, PluginState.ACTIVE)


# ================= A3：三层配置 =================


class ConfigLayerTests(_Base):
    def test_default_layer_from_schema(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "a": {"type": "string", "default": "x"},
                "b": {"type": "integer"},
                "c": {"type": "boolean", "default": False},
            },
        }
        self.assertEqual(config_defaults(schema), {"a": "x", "c": False})

    def test_whole_object_replacement_not_deep_merge(self) -> None:
        """整对象替换：高层一旦提供 config，低层被完整取代。"""
        host = {"p": {"config": {"a": 1, "b": 2}}}
        user = {"p": {"config": {"a": 9}}}
        merged = merge_rows(host, user)
        self.assertEqual(merged["p"]["config"], {"a": 9})  # b 不保留

    def test_row_fields_merge_separately_from_config(self) -> None:
        """行字段按字段覆盖：用户行只写 config 时，enabled 仍沿用宿主行。"""
        host = {"p": {"enabled": False, "config": {"a": 1}}}
        user = {"p": {"config": {"a": 2}}}
        merged = merge_rows(host, user)
        self.assertFalse(merged["p"]["enabled"])
        self.assertEqual(merged["p"]["config"], {"a": 2})

    def test_host_layer_applied_and_source_reported(self) -> None:
        """A3：宿主层（config/plugins.json）生效，且能报告来源。"""
        manager = self.manager()
        manager.load("text_toolbox")
        record = manager.get("text_toolbox")
        self.assertEqual(record.config_source, "host")
        self.assertEqual(self.ctx.effective_config_for(record.manifest)["max_lines"], 500)

    def test_user_layer_overrides_host_wholesale(self) -> None:
        """A3：用户层覆盖后，宿主层被整体取代（未写的字段回落到……本层之外没有，即消失）。"""
        write_row(self.ctx.paths.user_plugins_config, "text_toolbox", config={"max_lines": 7})
        self.ctx.reload_plugin_config()

        manager = self.manager()
        manager.load("text_toolbox")
        record = manager.get("text_toolbox")
        self.assertEqual(record.config_source, "user")
        config = self.ctx.effective_config_for(record.manifest)
        self.assertEqual(config, {"max_lines": 7})
        self.assertNotIn("strip_blank_lines", config)  # 整对象替换的直接后果

    def test_plugin_defaults_unchanged_by_layers(self) -> None:
        """内核/插件的默认值本身不被配置层改动。"""
        manifest = self.manager().get("text_toolbox").manifest
        self.assertEqual(config_defaults(manifest.config_schema)["max_lines"], 1000)

    def test_invalid_config_fails_load_with_readable_reason(self) -> None:
        fixture = self.root / "fixtures"
        write_plugin(
            fixture,
            "bad_config",
            config_schema={
                "type": "object",
                "properties": {"n": {"type": "integer"}},
                "required": ["n"],
            },
        )
        manager = self.manager(fixture)
        # 默认层没有 n，宿主层也没有 → 缺少必填字段
        record = manager.load("bad_config")
        self.assertEqual(record.state, PluginState.FAILED)
        self.assertIn("配置不合法", record.error)
        self.assertIn("n", record.error)


# ================= A7 / A8：技能与工具 =================


class SkillTests(_Base):
    def test_catalog_lists_without_body(self) -> None:
        """A7：目录只给名称 + 限长描述，不含正文。"""
        manager = self.manager()
        manager.activate_all()
        text = self.ctx.skill_catalog.catalog_text()
        self.assertIn("hello-workflow", text)
        self.assertIn("text-cleanup", text)
        self.assertNotIn("## 步骤", text)  # 正文没有被塞进目录

    def test_load_returns_body(self) -> None:
        manager = self.manager()
        manager.activate_all()
        content = self.ctx.skill_catalog.load("text-cleanup")
        self.assertIn("## 步骤", content.body)
        self.assertEqual(content.name, "text-cleanup")
        self.assertIsNotNone(content.path)

    def test_load_reports_resource_files(self) -> None:
        """技能可以带参考文件；资源清单**不包含 SKILL.md 本身**。"""
        manager = self.manager()
        manager.activate_all()
        content = self.ctx.skill_catalog.load("text-cleanup")
        self.assertIn("reference.md", content.resources)
        self.assertNotIn("SKILL.md", content.resources)

    def test_unknown_skill_raises(self) -> None:
        manager = self.manager()
        manager.activate_all()
        with self.assertRaises(SkillError):
            self.ctx.skill_catalog.load("nope-nope")

    def test_invocation_policy_matrix(self) -> None:
        from xbc.core.skills import SkillSpec

        catalog = self.ctx.skill_catalog
        catalog.register(
            SkillSpec(name="model-only", description="d", model_invocable=True, user_invocable=False),
            "body",
        )
        self.assertIn("model-only", [s.name for s in catalog.specs(for_model=True)])
        self.assertNotIn("model-only", [s.name for s in catalog.specs(for_model=False)])
        self.assertNotIn("model-only", catalog.catalog_text(for_model=False))

    def test_bad_skill_name_rejected(self) -> None:
        from xbc.core.skills import SkillSpec

        with self.assertRaises(SkillError):
            self.ctx.skill_catalog.register(SkillSpec(name="Bad Name", description="d"), "b")

    def test_skills_released_on_deactivate(self) -> None:
        manager = self.manager()
        manager.activate_all()
        self.assertGreater(len(self.ctx.skill_catalog), 0)
        manager.deactivate_all()
        self.assertEqual(len(self.ctx.skill_catalog), 0)


class ToolTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.manager_instance = self.manager()
        self.manager_instance.activate_all()

    def test_call_success(self) -> None:
        result = self.ctx.tool_registry.call("demo_echo", {"text": "abc"}) if "demo_echo" in self.ctx.tool_registry else self.ctx.tool_registry.call("hello_probe", {})
        self.assertTrue(result.ok)

    def test_invalid_arguments_are_protocol_error(self) -> None:
        """A8：参数不合 schema → invalid_arguments，且信息可读（不是栈）。"""
        result = self.ctx.tool_registry.call("text_stats", {"text": 123})
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "invalid_arguments")
        self.assertIn("$.text", result.message)

    def test_unknown_tool(self) -> None:
        result = self.ctx.tool_registry.call("no_such_tool", {})
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "tool_not_found")

    def test_business_failure_uses_execution_failed(self) -> None:
        """错误二分：业务失败与协议错误用不同的 code。"""
        result = self.ctx.tool_registry.call("hello_fail", {})
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "execution_failed")
        self.assertIn("故意", result.message)

    def test_write_tool_denied_without_consent(self) -> None:
        """内核侧强制风险：write 工具未授权时必须被拒绝。"""
        result = self.ctx.tool_registry.call("text_export", {"text": "a"})
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "consent_denied")

    def test_write_tool_allowed_with_consent(self) -> None:
        ctx = AppContext.create(root=self.root, console=False, approver=lambda *_: True)
        self.addCleanup(ctx.close)
        manager = PluginManager(ctx, [BUILTIN_PLUGINS], ctx.logger)
        manager.discover()
        manager.activate_all()
        result = ctx.tool_registry.call("text_export", {"text": "a\nb\na\n"})
        self.assertTrue(result.ok, result.message)
        self.assertTrue(Path(result.value["file"]).is_file())

    def test_explicit_rejection_is_respected(self) -> None:
        ctx = AppContext.create(root=self.root, console=False, approver=lambda *_: False)
        self.addCleanup(ctx.close)
        manager = PluginManager(ctx, [BUILTIN_PLUGINS], ctx.logger)
        manager.discover()
        manager.activate_all()
        result = ctx.tool_registry.call("text_export", {"text": "a"})
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "consent_denied")
        self.assertIn("拒绝", result.message)

    def test_output_schema_is_enforced(self) -> None:
        from xbc.core.tools import ToolSpec

        registry = self.ctx.tool_registry

        def handler():
            return {"wrong": 1}

        registry.register(
            ToolSpec(
                name="bad_output",
                handler=handler,
                output_schema={"type": "object", "required": ["right"]},
                owner="test",
            )
        )
        result = registry.call("bad_output", {})
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "invalid_output")

    def test_agent_view_shape(self) -> None:
        view = self.ctx.tool_registry.for_agent()
        self.assertTrue(view)
        for item in view:
            self.assertIn("name", item)
            self.assertIn("description", item)
            self.assertIn("inputSchema", item)


# ================= 契约、注册表、作用域、钩子 =================


class ManifestV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="xbc-manifest-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def base(self, **overrides) -> dict:
        return _manifest("demo", **overrides)

    def test_valid_manifest(self) -> None:
        manifest = parse_manifest(self.base(), self.dir)
        self.assertEqual(manifest.id, "demo")
        self.assertEqual(manifest.spec_version, "1.0")

    def test_illegal_id_rejected(self) -> None:
        with self.assertRaises(PluginManifestError):
            parse_manifest(self.base(id="../evil"), self.dir)

    def test_unknown_capability_rejected(self) -> None:
        with self.assertRaises(PluginManifestError):
            parse_manifest(self.base(capabilities=["teleport"]), self.dir)

    def test_unknown_tool_risk_rejected(self) -> None:
        with self.assertRaises(PluginManifestError):
            parse_manifest(self.base(tools=[{"name": "t", "risk": "whatever"}]), self.dir)

    def test_icon_path_traversal_rejected(self) -> None:
        for bad in ("../secret.png", "C:/Windows/x.png", "https://x/y.png"):
            with self.assertRaises(PluginManifestError, msg=bad):
                parse_manifest(self.base(icon=bad), self.dir)

    def test_command_match_type_validated(self) -> None:
        with self.assertRaises(PluginManifestError):
            parse_manifest(self.base(commands=[{"code": "c", "match": {"type": "magic"}}]), self.dir)

    def test_dual_version_fields_kept_separate(self) -> None:
        manifest = parse_manifest(self.base(spec_version="1.0", api_version="1.0"), self.dir)
        self.assertEqual((manifest.spec_version, manifest.api_version), ("1.0", "1.0"))


class LayeredRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = LayeredRegistry("测试")

    def test_higher_layer_wins(self) -> None:
        self.registry.register("a", "kernel", layer=Layer.KERNEL, owner="kernel")
        self.registry.register("a", "plugin", layer=Layer.PLUGIN, owner="p1")
        self.assertEqual(self.registry.get("a"), "plugin")
        self.registry.register("a", "user", layer=Layer.USER, owner="user")
        self.assertEqual(self.registry.get("a"), "user")

    def test_first_come_first_served_within_layer(self) -> None:
        self.registry.register("a", "first", layer=Layer.PLUGIN, owner="p1")
        self.registry.register("a", "second", layer=Layer.PLUGIN, owner="p2")
        self.assertEqual(self.registry.get("a"), "first")

    def test_higher_rank_beats_registration_order(self) -> None:
        self.registry.register("a", "low", layer=Layer.PLUGIN, rank=0, owner="p1")
        self.registry.register("a", "high", layer=Layer.PLUGIN, rank=5, owner="p2")
        self.assertEqual(self.registry.get("a"), "high")

    def test_shadowed_is_visible_not_silent(self) -> None:
        self.registry.register("a", "first", layer=Layer.PLUGIN, owner="p1")
        self.registry.register("a", "second", layer=Layer.PLUGIN, owner="p2")
        shadowed = self.registry.shadowed()
        self.assertEqual(len(shadowed), 1)
        self.assertEqual(shadowed[0].owner, "p2")
        self.assertFalse(shadowed[0].is_effective)

    def test_disposer_removes_registration(self) -> None:
        dispose = self.registry.register("a", 1, owner="p1")
        self.assertIn("a", self.registry)
        dispose()
        self.assertNotIn("a", self.registry)
        dispose()  # 幂等

    def test_shadowed_becomes_effective_after_winner_removed(self) -> None:
        dispose_first = self.registry.register("a", "first", layer=Layer.PLUGIN, owner="p1")
        self.registry.register("a", "second", layer=Layer.PLUGIN, owner="p2")
        dispose_first()
        self.assertEqual(self.registry.get("a"), "second")


class ScopeTests(unittest.TestCase):
    def test_effect_captures_disposer(self) -> None:
        scope = Scope("s")
        calls: list[str] = []
        scope.effect(lambda: (lambda: calls.append("disposed")))
        self.assertEqual(scope.effect_count, 1)
        scope.dispose()
        self.assertEqual(calls, ["disposed"])

    def test_dispose_is_idempotent_and_reverse_order(self) -> None:
        scope = Scope("s")
        order: list[int] = []
        scope.effect(lambda: (lambda: order.append(1)))
        scope.effect(lambda: (lambda: order.append(2)))
        scope.dispose()
        scope.dispose()
        self.assertEqual(order, [2, 1])

    def test_child_scope_disposed_before_parent(self) -> None:
        parent = Scope("p")
        order: list[str] = []
        parent.effect(lambda: (lambda: order.append("parent")))
        child = parent.fork("c")
        child.effect(lambda: (lambda: order.append("child")))
        parent.dispose()
        self.assertEqual(order, ["child", "parent"])

    def test_failing_disposer_does_not_block_others(self) -> None:
        scope = Scope("s")
        done: list[str] = []

        def bad():
            raise RuntimeError("boom")

        scope.effect(lambda: (lambda: done.append("first")))
        scope.effect(lambda: bad)
        scope.dispose()
        self.assertEqual(done, ["first"])

    def test_register_after_dispose_raises(self) -> None:
        scope = Scope("s")
        scope.dispose()
        with self.assertRaises(ScopeDisposed):
            scope.effect(lambda: None)


class HookSpecTests(unittest.TestCase):
    class Specs:
        @hookspec
        def on_thing(self, value: str) -> None:
            """契约。"""

    def _relay(self) -> HookRelay:
        relay = HookRelay()
        relay.add_specs(self.Specs)
        return relay

    def test_valid_impl_registers_and_calls(self) -> None:
        seen: list[str] = []

        class Plugin:
            @hookimpl
            def on_thing(self, value):
                seen.append(value)

        relay = self._relay()
        self.assertEqual(relay.register(Plugin(), "p1"), 1)
        relay.call("on_thing", value="hello")
        self.assertEqual(seen, ["hello"])

    def test_signature_mismatch_is_rejected_at_registration(self) -> None:
        class Plugin:
            @hookimpl
            def on_thing(self, wrong_name):
                pass

        with self.assertRaises(HookValidationError):
            self._relay().register(Plugin(), "p1")

    def test_unknown_hook_rejected_unless_optional(self) -> None:
        class Bad:
            @hookimpl
            def on_nothing(self):
                pass

        with self.assertRaises(HookValidationError):
            self._relay().register(Bad(), "p1")

        class Good:
            @hookimpl(optionalhook=True)
            def on_nothing(self):
                pass

        self._relay().register(Good(), "p2")  # 不抛

    def test_check_pending_reports_optional_orphans(self) -> None:
        """可选钩子被允许运行，但必须能被报出来（版本错配信号）。"""
        relay = HookRelay()

        class Plugin:
            @hookimpl(optionalhook=True)
            def on_future_thing(self):
                pass

        relay.register(Plugin(), "p1")
        self.assertEqual(relay.check_pending(), [("p1", "on_future_thing")])

    def test_check_pending_is_empty_when_specs_exist(self) -> None:
        class Plugin:
            @hookimpl
            def on_thing(self, value):
                pass

        relay = self._relay()
        relay.register(Plugin(), "p1")
        self.assertEqual(relay.check_pending(), [])

    def test_one_impl_failure_does_not_stop_others(self) -> None:
        seen: list[str] = []

        class Bad:
            @hookimpl
            def on_thing(self, value):
                raise RuntimeError("boom")

        class Good:
            @hookimpl
            def on_thing(self, value):
                seen.append(value)

        relay = self._relay()
        relay.register(Bad(), "bad")
        relay.register(Good(), "good")
        relay.call("on_thing", value="x")
        self.assertEqual(seen, ["x"])

    def test_unregister_removes_impls(self) -> None:
        class Plugin:
            @hookimpl
            def on_thing(self, value):
                pass

        relay = self._relay()
        relay.register(Plugin(), "p1")
        self.assertEqual(relay.unregister("p1"), 1)
        self.assertEqual(relay.impls("on_thing"), [])


class CapabilityTests(_Base):
    def test_declared_capability_available(self) -> None:
        manifest = self.manager().get("hello_xbc").manifest
        plugin_ctx = self.ctx.for_plugin(manifest)
        self.assertTrue(plugin_ctx.files)
        self.assertTrue(plugin_ctx.ai)

    def test_undeclared_capability_denied(self) -> None:
        fixture = self.root / "fixtures"
        write_plugin(fixture, "no_caps", capabilities=["files"])
        manifest = self.manager(fixture).get("no_caps").manifest
        plugin_ctx = self.ctx.for_plugin(manifest)
        self.assertTrue(plugin_ctx.files)
        with self.assertRaises(CapabilityDenied):
            _ = plugin_ctx.ai
        with self.assertRaises(CapabilityDenied):
            _ = plugin_ctx.ffmpeg

    def test_plugin_data_dir_is_isolated(self) -> None:
        manifest = self.manager().get("hello_xbc").manifest
        plugin_ctx = self.ctx.for_plugin(manifest)
        self.assertEqual(plugin_ctx.data_dir, self.ctx.paths.data_dir / "hello_xbc")


class EventTests(_Base):
    def test_events_delivered_and_isolated(self) -> None:
        bus = self.ctx.events
        seen: list[str] = []

        def bad(**_):
            raise RuntimeError("boom")

        bus.on("e", bad, owner="a")
        bus.on("e", lambda **_: seen.append("ok"), owner="b")
        self.assertEqual(bus.emit("e"), 1)
        self.assertEqual(seen, ["ok"])

    def test_off_owner_removes_all(self) -> None:
        bus = self.ctx.events
        bus.on("e", lambda **_: None, owner="a")
        bus.on("f", lambda **_: None, owner="a")
        self.assertEqual(bus.off_owner("a"), 2)
        self.assertEqual(len(bus), 0)


class AcceptanceTests(_Base):
    """A10：doctor 无 error；并覆盖"运行时可见性"的几条。"""

    def test_doctor_reports_healthy_runtime(self) -> None:
        from xbc.core.diagnostics import doctor

        manager = self.manager()
        manager.load_all()
        report = doctor(self.ctx, manager)
        total = len(manager.records())
        self.assertTrue(report["runtime"]["ok"], report["runtime"]["problems"])
        self.assertEqual(report["runtime"]["plugin_count"], total)
        self.assertEqual(report["runtime"]["states"].get("loaded"), total)

    def test_doctor_surfaces_failures(self) -> None:
        from xbc.core.diagnostics import doctor

        fixture = self.root / "fixtures"
        write_plugin(fixture, "broken_apply", _BROKEN_APPLY_PLUGIN)
        manager = self.manager(fixture)
        manager.activate_all()
        report = doctor(self.ctx, manager)
        self.assertFalse(report["runtime"]["ok"])
        self.assertEqual(report["runtime"]["failed"][0]["id"], "broken_apply")

    def test_doctor_surfaces_shadowed_registrations(self) -> None:
        from xbc.core.diagnostics import doctor
        from xbc.core.tools import ToolSpec

        manager = self.manager()
        manager.load_all()
        manager.activate_all()
        # 手工制造一次重名，确认 doctor 能报出来（不能静默吞掉）
        existing = self.ctx.tool_registry.get("text_stats")
        self.ctx.tool_registry.register(
            ToolSpec(name="text_stats", handler=existing.handler, description="冲突", owner="intruder")
        )
        report = doctor(self.ctx, manager)
        self.assertFalse(report["runtime"]["ok"])
        self.assertIn("遮蔽", " ".join(report["runtime"]["problems"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
