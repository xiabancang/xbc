"""video_analyzer 插件的测试（TASK-005 验收）。

对应验收标准：

1. 插件独立安装 —— 复制到用户插件目录后照样能被发现并运行
2. Runtime 发现插件 —— `discover()` 能发现它
3. Tool 可调用 —— 4 个工具都能调用成功
4. 输出结果正确 —— 用**带 2 个硬切的合成视频**断言精确的时长与切点
5. 禁用插件无残留 —— 工具/技能/事件订阅全部释放
6. 全量测试通过 —— 包含在 `unittest discover` 里

测试视频是运行时用 FFmpeg 合成的（不往仓库里塞二进制），没有 FFmpeg 时整体跳过。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from xbc.core.context import AppContext  # noqa: E402
from xbc.core.contract.plugin import PluginState  # noqa: E402
from xbc.core.runtime.manager import PluginManager  # noqa: E402

BUILTIN_PLUGINS = REPO_ROOT / "plugins"
PLUGIN_DIR = BUILTIN_PLUGINS / "video_analyzer"

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
FFMPEG_OK = bool(FFMPEG and FFPROBE)

#: 合成视频：3 段画面完全不同的素材拼接，各 2 秒 → 总长 6 秒、2 个硬切
SEGMENT_SECONDS = 2
SEGMENT_COUNT = 3
EXPECTED_DURATION = float(SEGMENT_SECONDS * SEGMENT_COUNT)
EXPECTED_CUTS = [float(SEGMENT_SECONDS * i) for i in range(1, SEGMENT_COUNT)]


def make_test_video(target: Path) -> bool:
    """用 lavfi 合成一段带硬切的视频。成功返回 True。"""
    command = [
        str(FFMPEG), "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"testsrc=duration={SEGMENT_SECONDS}:size=320x240:rate=10",
        "-f", "lavfi", "-i", f"smptebars=duration={SEGMENT_SECONDS}:size=320x240:rate=10",
        "-f", "lavfi", "-i", f"testsrc2=duration={SEGMENT_SECONDS}:size=320x240:rate=10",
        "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0",
        "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "veryfast",
        str(target),
    ]
    try:
        completed = subprocess.run(command, capture_output=True, timeout=180)
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0 and target.is_file()


@unittest.skipUnless(FFMPEG_OK, "需要 FFmpeg / ffprobe")
class VideoAnalyzerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture_dir = Path(tempfile.mkdtemp(prefix="xbc-va-fixture-"))
        cls.video = cls.fixture_dir / "test_cuts.mp4"
        cls.video_ready = make_test_video(cls.video)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.fixture_dir, ignore_errors=True)

    def setUp(self) -> None:
        if not self.video_ready:
            self.skipTest("无法合成测试视频（FFmpeg 不可用或编码器缺失）")
        self.root = Path(tempfile.mkdtemp(prefix="xbc-va-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.ctx = AppContext.create(root=self.root, console=False)
        self.addCleanup(self.ctx.close)
        self.manager = PluginManager(self.ctx, [BUILTIN_PLUGINS], self.ctx.logger)
        self.manager.discover()
        self.manager.activate("video_analyzer")

    def call(self, tool: str, **arguments):
        return self.ctx.tool_registry.call(tool, arguments)

    # ---------------- 2. Runtime 发现插件 ----------------
    def test_runtime_discovers_plugin(self) -> None:
        ids = [r.id for r in self.manager.records()]
        self.assertIn("video_analyzer", ids)
        record = self.manager.get("video_analyzer")
        self.assertEqual(record.state, PluginState.ACTIVE)
        self.assertEqual(record.manifest.name, "视频分析器")

    def test_discover_does_not_import_plugin_code(self) -> None:
        """沿用 A1 的零成本清单约定：未激活时不导入代码。"""
        for name in [n for n in sys.modules if n.startswith("xbc_plugin_")]:
            del sys.modules[name]
        manager = PluginManager(self.ctx, [BUILTIN_PLUGINS], self.ctx.logger)
        manager.discover()
        record = manager.get("video_analyzer")
        self.assertIsNone(record.instance)
        self.assertNotIn("xbc_plugin_video_analyzer", sys.modules)

    # ---------------- 3. Tool 可调用 ----------------
    def test_four_tools_registered_with_read_risk(self) -> None:
        expected = {"video_probe", "video_split_shots", "video_extract_keyframes", "video_analyze"}
        self.assertTrue(expected.issubset(set(self.ctx.tool_registry.names())))
        for name in expected:
            spec = self.ctx.tool_registry.get(name)
            self.assertEqual(spec.risk, "read", f"{name} 的风险等级应为 read")
            self.assertEqual(spec.plugin_id, "video_analyzer")

    def test_plugin_declares_expected_capabilities(self) -> None:
        """TASK-008 起，本插件通过 `ctx.ai` 做画面理解，因此必须声明 ai 能力。"""
        manifest = self.manager.get("video_analyzer").manifest
        self.assertEqual(set(manifest.capabilities), {"files", "ffmpeg", "settings", "ai"})

    def test_ai_capability_is_actually_reachable(self) -> None:
        """声明了 ai 就必须真的能拿到 —— 否则会在调用时才炸。"""
        record = self.manager.get("video_analyzer")
        self.assertIn("ai", tuple(record.instance.ctx.capabilities))
        self.assertIsNotNone(record.instance.ctx.ai)

    # ---------------- 4. 输出结果正确 ----------------
    def test_video_probe_returns_correct_media_info(self) -> None:
        result = self.call("video_probe", path=str(self.video))
        self.assertTrue(result.ok, result.message)
        value = result.value

        self.assertAlmostEqual(value["duration_seconds"], EXPECTED_DURATION, places=2)
        self.assertTrue(value["has_video"])
        self.assertFalse(value["has_audio"])
        self.assertEqual(value["video"]["width"], 320)
        self.assertEqual(value["video"]["height"], 240)
        self.assertEqual(value["video"]["codec"], "h264")
        self.assertAlmostEqual(value["video"]["fps"], 10.0, places=2)
        self.assertEqual(value["file_name"], "test_cuts.mp4")

    def test_video_split_shots_finds_the_two_cuts(self) -> None:
        result = self.call("video_split_shots", path=str(self.video))
        self.assertTrue(result.ok, result.message)
        value = result.value

        self.assertEqual(value["shot_count"], SEGMENT_COUNT, "应切出 3 个镜头")
        self.assertEqual(value["raw_cut_count"], len(EXPECTED_CUTS))
        self.assertAlmostEqual(value["duration_seconds"], EXPECTED_DURATION, places=2)

        shots = value["shots"]
        self.assertEqual([s["index"] for s in shots], [0, 1, 2])
        self.assertAlmostEqual(shots[0]["start"], 0.0, places=2)
        for shot, expected_cut in zip(shots[1:], EXPECTED_CUTS):
            self.assertAlmostEqual(shot["start"], expected_cut, delta=0.35,
                                   msg=f"切点应接近 {expected_cut}")
        # 镜头首尾相接、覆盖全片
        self.assertAlmostEqual(shots[-1]["end"], EXPECTED_DURATION, places=2)
        for previous, current in zip(shots, shots[1:]):
            self.assertAlmostEqual(previous["end"], current["start"], places=3)

    def test_video_extract_keyframes_writes_real_files(self) -> None:
        result = self.call("video_extract_keyframes", path=str(self.video), frames_per_shot=2)
        self.assertTrue(result.ok, result.message)
        value = result.value

        self.assertEqual(value["shot_count"], SEGMENT_COUNT)
        self.assertEqual(value["frame_count"], SEGMENT_COUNT * 2)

        for frame in value["frames"]:
            path = Path(frame["file"])
            self.assertTrue(path.is_file(), f"关键帧文件不存在：{path}")
            self.assertGreater(path.stat().st_size, 0)
            self.assertEqual(path.read_bytes()[:2], b"\xff\xd8", "应是 JPEG")

        # 帧时间必须落在对应镜头区间内
        shots = self.call("video_split_shots", path=str(self.video)).value["shots"]
        for frame in value["frames"]:
            shot = shots[frame["shot_index"]]
            self.assertGreaterEqual(frame["time"], shot["start"])
            self.assertLessEqual(frame["time"], shot["end"])

    def test_keyframes_stay_inside_plugin_data_dir(self) -> None:
        """插件只能写自己的数据目录 —— 这是能力边界，不是约定。"""
        result = self.call("video_extract_keyframes", path=str(self.video), frames_per_shot=1)
        self.assertTrue(result.ok, result.message)
        data_dir = self.ctx.paths.plugin_data_dir("video_analyzer").resolve()
        for frame in result.value["frames"]:
            self.assertTrue(
                Path(frame["file"]).resolve().is_relative_to(data_dir),
                f"关键帧写到了插件数据目录之外：{frame['file']}",
            )

    def test_video_analyze_returns_complete_structure(self) -> None:
        result = self.call("video_analyze", path=str(self.video), frames_per_shot=1)
        self.assertTrue(result.ok, result.message)
        value = result.value

        for key in ("media", "shots", "keyframes", "summary"):
            self.assertIn(key, value)
        self.assertEqual(value["summary"]["shot_count"], SEGMENT_COUNT)
        self.assertEqual(value["summary"]["frame_count"], SEGMENT_COUNT)
        self.assertEqual(value["summary"]["resolution"], "320x240")
        self.assertAlmostEqual(value["summary"]["duration_seconds"], EXPECTED_DURATION, places=2)
        self.assertFalse(value["summary"]["has_audio"])

    def test_threshold_from_config_changes_result(self) -> None:
        """配置能真正影响业务结果：阈值调到 0.95 → 检测不到切换 → 只剩 1 个镜头。"""
        from xbc.core.config.layers import write_row

        write_row(self.ctx.paths.user_plugins_config, "video_analyzer", config={"scene_threshold": 0.95})
        self.ctx.reload_plugin_config()
        self.manager.deactivate("video_analyzer")
        self.manager.activate("video_analyzer")

        record = self.manager.get("video_analyzer")
        self.assertEqual(record.config_source, "user")

        result = self.call("video_split_shots", path=str(self.video))
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.value["shot_count"], 1)
        self.assertEqual(result.value["raw_cut_count"], 0)

    # ---------------- 输入校验 ----------------
    def test_missing_file_is_rejected_readably(self) -> None:
        result = self.call("video_probe", path=str(self.fixture_dir / "nope.mp4"))
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "execution_failed")
        self.assertIn("文件不存在", result.message)

    def test_non_video_extension_is_rejected(self) -> None:
        bogus = self.fixture_dir / "note.txt"
        bogus.write_text("not a video", encoding="utf-8")
        result = self.call("video_probe", path=str(bogus))
        self.assertFalse(result.ok)
        self.assertIn("扩展名", result.message)

    def test_missing_required_argument_is_protocol_error(self) -> None:
        result = self.ctx.tool_registry.call("video_split_shots", {})
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "invalid_arguments")

    # ---------------- 5. 禁用无残留 ----------------
    def declared_tool_count(self) -> int:
        """清单声明的工具数 —— 不硬编码，否则插件加一个工具这条测试就假失败。"""
        return len(self.manager.get("video_analyzer").manifest.tools)

    def test_disable_leaves_no_residue(self) -> None:
        expected = self.declared_tool_count()
        self.assertGreater(expected, 0)
        self.assertEqual(len(self.ctx.tool_registry), expected)
        record = self.manager.get("video_analyzer")
        self.assertGreater(record.scope.effect_count, 0)

        self.manager.disable("video_analyzer")

        self.assertEqual(len(self.ctx.tool_registry), 0, "停用后工具必须全部消失")
        self.assertEqual(len(self.ctx.skill_catalog), 0)
        self.assertEqual(len(self.ctx.events), 0)
        self.assertEqual(record.scope.effect_count, 0, "作用域必须释放干净")
        self.assertFalse(record.enabled)

    def test_reactivation_after_disable(self) -> None:
        before = len(self.ctx.tool_registry)
        self.manager.disable("video_analyzer")
        self.assertEqual(len(self.ctx.tool_registry), 0)
        self.manager.enable("video_analyzer")
        self.assertEqual(len(self.ctx.tool_registry), before)
        self.assertTrue(self.call("video_probe", path=str(self.video)).ok)

    # ---------------- 1. 独立安装 ----------------
    def test_plugin_has_no_third_party_dependencies(self) -> None:
        """独立安装的前提：只依赖标准库、内核契约、以及插件自己的模块。

        插件目录会被插入 `sys.path`，所以同目录的兄弟模块（`xbc_va_*`）是**插件自身代码**，
        不是第三方依赖。命名统一带 `xbc_` 前缀就是为此（避免与其他插件撞名）。
        """
        source = (PLUGIN_DIR / "plugin.py").read_text(encoding="utf-8")
        imported = set(re.findall(r"^(?:from|import)\s+([A-Za-z_][\w.]*)", source, re.MULTILINE))
        self.assertTrue(imported, "前置条件：应能从源码解析出 import")

        local_modules = {
            path.stem for path in PLUGIN_DIR.glob("*.py")
        }
        allowed = set(sys.stdlib_module_names) | {"xbc", "__future__"} | local_modules
        for name in imported:
            top = name.split(".")[0]
            self.assertIn(top, allowed, f"插件引入了非标准库依赖：{name}")

    def test_local_modules_are_prefixed_to_avoid_collisions(self) -> None:
        """插件共享同一个 sys.modules —— 兄弟模块必须带唯一前缀。"""
        for path in PLUGIN_DIR.glob("*.py"):
            if path.stem == "plugin":
                continue
            self.assertTrue(
                path.stem.startswith("xbc_va_"),
                f"{path.name} 应以 xbc_va_ 前缀命名，避免和其他插件撞名",
            )

    def test_standalone_install_into_user_plugin_dir(self) -> None:
        """把插件文件夹复制进用户插件目录 → Runtime 能发现 → Tool 能调用。"""
        user_root = Path(tempfile.mkdtemp(prefix="xbc-va-user-"))
        self.addCleanup(shutil.rmtree, user_root, ignore_errors=True)
        ctx = AppContext.create(root=user_root, console=False)
        self.addCleanup(ctx.close)

        target = ctx.paths.user_plugins_dir / "video_analyzer"
        shutil.copytree(PLUGIN_DIR, target)

        # 只从**用户插件目录**发现，不依赖内置目录
        manager = PluginManager(ctx, [ctx.paths.user_plugins_dir], ctx.logger)
        manager.discover()
        self.assertIn("video_analyzer", [r.id for r in manager.records()])

        record = manager.activate("video_analyzer")
        self.assertEqual(record.state, PluginState.ACTIVE)

        result = ctx.tool_registry.call("video_probe", {"path": str(self.video)})
        self.assertTrue(result.ok, result.message)
        self.assertAlmostEqual(result.value["duration_seconds"], EXPECTED_DURATION, places=2)

    def test_plugin_manifest_declares_all_four_tools(self) -> None:
        """清单声明的工具与实际注册的工具必须一致（零成本展示才不会骗人）。"""
        declared = {t.name for t in self.manager.get("video_analyzer").manifest.tools}
        registered = set(self.ctx.tool_registry.names())
        self.assertEqual(declared, registered)


if __name__ == "__main__":
    unittest.main(verbosity=2)
