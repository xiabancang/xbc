"""TASK-013c 向量空间可见性 —— 验收测试。

TASK-013a 查证发现两个缺口：

1. **静默降级**：`_pick_space(auto)` 在库里没有图片空间时会悄悄退回文本空间
   （实测 top-1 2/10，图片空间 10/10），**不告诉用户**
2. **界面完全不显示向量空间** —— `space` / `provider` / `dim` 一个都没渲染
   （工具早就返回了，UI 没给看）→ 用户只能靠分数猜

本文件按验收标准组织：

| 验收 | 用例 |
|---|---|
| 有图片空间时，界面显示 chinese_clip 1024 维 | `LibraryPageSpaceTests` / `MatchPageSpaceTests` |
| 无图片空间时，界面明确提示"降级到文本空间" | 同上 + `PluginSpaceNoteTests` |
| 匹配逻辑不变（auto 优先图片） | `SpacePickerRegressionTests` |
| 向量数据不动（ollama 文本向量保留） | `VectorDataUntouchedTests` |
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
PLUGIN_DIR = REPO_ROOT / "plugins" / "video_analyzer"
for path in (str(REPO_ROOT), str(SRC_DIR), str(PLUGIN_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from xbc.core.context import AppContext  # noqa: E402
from xbc.core.runtime.manager import PluginManager  # noqa: E402

try:
    from tests.test_video_library import (  # noqa: E402
        FakeAIProvider,
        FakeImageProvider,
        make_test_video,
    )
except ImportError:  # pragma: no cover
    from test_video_library import (  # noqa: E402
        FakeAIProvider,
        FakeImageProvider,
        make_test_video,
    )

PLUGINS_DIR = REPO_ROOT / "plugins"

#: 降级提示里必须出现的两个关键词 —— 验收标准写死了这两点
DOWNGRADE_KEYWORD = "当前使用文本空间"
DOWNGRADE_REASON = "质量弱于图片空间"


class _LibraryBase(unittest.TestCase):
    """建一个真实的素材库：可以选**带不带图片 Provider**。"""

    #: 子类改成 False 就得到一个"只有文本空间"的库
    WITH_IMAGE = True

    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture_dir = Path(tempfile.mkdtemp(prefix="xbc-space-fixture-"))
        cls.video = cls.fixture_dir / "first.mp4"
        cls.video_ready = make_test_video(cls.video)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.fixture_dir, ignore_errors=True)

    def setUp(self) -> None:
        if not self.video_ready:
            self.skipTest("无法合成测试视频（FFmpeg 不可用）")
        self.root = Path(tempfile.mkdtemp(prefix="xbc-space-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        materials = self.root / "materials"
        materials.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.video, materials / "first.mp4")

        self.ctx = AppContext.create(root=self.root, console=False)
        self.addCleanup(self.ctx.close)
        self.ctx.ai.register(FakeAIProvider(), default=True)
        if self.WITH_IMAGE:
            self.ctx.ai.register(FakeImageProvider())

        manager = PluginManager(self.ctx, [PLUGINS_DIR], self.ctx.logger)
        manager.discover()
        manager.activate("video_analyzer")
        self.manager = manager
        self.ctx.tool_registry.call("library_scan", {"directory": str(materials)})

    def call(self, tool: str, **arguments):
        return self.ctx.tool_registry.call(tool, arguments)

    def plugin(self):
        return self.manager.get("video_analyzer").instance


# ================= 插件层：降级出声 =================


class PluginSpaceNoteTests(_LibraryBase):
    WITH_IMAGE = True

    def test_image_space_produces_no_note(self) -> None:
        result = self.call("library_search_semantic", query="shot000 的画面")
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.value["embedding"]["modality"], "image")
        self.assertEqual(result.value["note"], "", "用图片空间时不该有多余提示")

    def test_explicit_text_space_is_not_treated_as_a_downgrade(self) -> None:
        """显式指定 `space="text"` 是**主动选择**（对比/复现），不该报警。"""
        result = self.call("library_search_semantic", query="shot000 的画面", space="text")
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.value["embedding"]["modality"], "text")
        self.assertEqual(result.value["note"], "")

    def test_script_match_on_the_image_space_produces_no_note(self) -> None:
        result = self.call("script_match", name="图片空间", script="镜头是 shot000 的样子。")
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.value["space"]["modality"], "image")
        self.assertEqual(result.value["note"], "")


class PluginDowngradeNoteTests(_LibraryBase):
    """**没有图片 Provider** —— 库里的向量只有文本空间，`auto` 会降级。"""

    WITH_IMAGE = False

    def test_library_search_warns_instead_of_staying_silent(self) -> None:
        result = self.call("library_search_semantic", query="shot000 的画面")
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.value["embedding"]["modality"], "text")
        note = result.value["note"]
        self.assertIn(DOWNGRADE_KEYWORD, note, "降级时必须说出'当前使用文本空间'")
        self.assertIn(DOWNGRADE_REASON, note, "还要说清为什么（质量弱于图片空间）")

    def test_script_match_warns_instead_of_staying_silent(self) -> None:
        result = self.call("script_match", name="文本空间", script="镜头是 shot000 的样子。")
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.value["space"]["modality"], "text")
        self.assertIn(DOWNGRADE_KEYWORD, result.value["note"])

    def test_match_show_labels_a_saved_text_space_result(self) -> None:
        """已存结果也要标注它是哪个空间算的 —— 落盘快照里的 space 就是依据。"""
        self.call("script_match", name="文本空间", script="镜头是 shot000 的样子。")
        result = self.call("match_show", name="文本空间")
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.value["space"]["modality"], "text")
        self.assertIn(DOWNGRADE_KEYWORD, result.value["note"])

    def test_explicit_image_request_still_fails_loudly(self) -> None:
        """显式要图片空间但没有 → 该报错就报错（不是降级，是做不到）。"""
        result = self.call("library_search_semantic", query="x", space="image")
        self.assertTrue(result.ok)
        self.assertEqual(result.value["count"], 0)
        note = result.value["note"]
        self.assertIn("image", note)
        self.assertIn("没有", note)
        self.assertNotIn(DOWNGRADE_KEYWORD, note, "做不到 ≠ 降级，不该套降级话术")


# ================= 匹配逻辑不变 =================


class SpacePickerRegressionTests(unittest.TestCase):
    """**要求 3：不动匹配逻辑。** `auto` 优先图片是对的，锁住它。"""

    @staticmethod
    def _spaces():
        return [
            {"provider": "ollama", "model": "nomic-embed-text", "dim": 768,
             "modality": "text"},
            {"provider": "chinese_clip", "model": "chinese-clip-rn50", "dim": 1024,
             "modality": "image"},
        ]

    def test_auto_still_prefers_the_image_space(self) -> None:
        from plugin import VideoAnalyzerPlugin

        chosen = VideoAnalyzerPlugin._pick_space(self._spaces(), "auto")
        self.assertEqual(chosen["provider"], "chinese_clip")
        self.assertEqual(chosen["modality"], "image")

    def test_auto_prefers_image_even_when_text_comes_first(self) -> None:
        from plugin import VideoAnalyzerPlugin

        spaces = list(reversed(self._spaces()))
        chosen = VideoAnalyzerPlugin._pick_space(spaces, "auto")
        self.assertEqual(chosen["modality"], "image")

    def test_auto_falls_back_to_text_when_there_is_no_image_space(self) -> None:
        from plugin import VideoAnalyzerPlugin

        chosen = VideoAnalyzerPlugin._pick_space(self._spaces()[:1], "auto")
        self.assertEqual(chosen["modality"], "text")

    def test_explicit_modality_is_honoured(self) -> None:
        from plugin import VideoAnalyzerPlugin

        self.assertEqual(
            VideoAnalyzerPlugin._pick_space(self._spaces(), "text")["modality"], "text")
        self.assertEqual(
            VideoAnalyzerPlugin._pick_space(self._spaces(), "image")["modality"], "image")

    def test_note_helper_only_fires_on_auto_downgrade(self) -> None:
        from plugin import VideoAnalyzerPlugin

        text_space = {"provider": "ollama", "model": "nomic", "dim": 768,
                      "modality": "text"}
        image_space = {"provider": "chinese_clip", "model": "rn50", "dim": 1024,
                       "modality": "image"}
        self.assertEqual(VideoAnalyzerPlugin._space_note("auto", image_space), "")
        self.assertEqual(VideoAnalyzerPlugin._space_note("text", text_space), "")
        self.assertEqual(VideoAnalyzerPlugin._space_note("image", image_space), "")
        self.assertIn(DOWNGRADE_KEYWORD,
                      VideoAnalyzerPlugin._space_note("auto", text_space))


class VectorDataUntouchedTests(_LibraryBase):
    """**要求 4：不动向量数据。** 检索不会删掉另一个空间的向量。"""

    WITH_IMAGE = True

    def test_both_spaces_survive_a_search(self) -> None:
        before = {(s["provider"], s["model"], s["dim"])
                  for s in self.plugin()._library_spaces()}
        self.call("library_search_semantic", query="shot000")
        self.call("library_search_semantic", query="shot000", space="text")
        after = {(s["provider"], s["model"], s["dim"])
                 for s in self.plugin()._library_spaces()}
        self.assertEqual(before, after, "检索不得改动任何向量空间")

    def test_the_text_space_is_still_there_after_an_image_search(self) -> None:
        self.call("library_search_semantic", query="shot000")
        spaces = self.plugin()._library_spaces()
        modalities = {s["modality"] for s in spaces}
        self.assertEqual(modalities, {"image", "text"},
                         "两种模态的向量都必须还在（不写死 provider 名、不依赖维度不同）")
        self.assertEqual(len(spaces), 2, "两个空间各一条，检索不该增删")


# ================= 界面层：显示空间 =================


def ok(value):
    return SimpleNamespace(ok=True, value=value, message="", code="ok")


class _FakeBridge:
    """按工具名返回**插件真实形状**的返回值（形状由上面的插件测试锁定）。"""

    def __init__(self, mapping):
        self.mapping = mapping

    def available(self, tool):
        return True

    def missing(self, tools):
        return []

    def spec(self, tool):
        return None

    def names(self):
        return list(self.mapping)

    def call(self, tool, **arguments):
        return self.mapping[tool]


class _UiBase(unittest.TestCase):
    def setUp(self) -> None:
        import os

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        try:
            from PySide6.QtWidgets import QApplication
        except ImportError as exc:  # pragma: no cover
            self.skipTest(f"未安装 PySide6: {exc}")
        self.app = QApplication.instance() or QApplication([])

    def make_page(self, page_class, mapping):
        page = page_class(None, _FakeBridge(mapping))
        page.show()
        self.addCleanup(page.close)
        return page


IMAGE_EMBEDDING = {"provider": "chinese_clip", "model": "chinese-clip-rn50",
                   "dim": 1024, "modality": "image"}
TEXT_EMBEDDING = {"provider": "ollama", "model": "nomic-embed-text",
                  "dim": 768, "modality": "text"}


class LibraryPageSpaceTests(_UiBase):
    def _search_value(self, embedding, note=""):
        return {"query": "星空", "mode": "semantic", "count": 0, "results": [],
                "embedding": embedding, "note": note}

    def test_shows_chinese_clip_1024_for_an_image_search(self) -> None:
        from xbc.ui.pages.library import LibraryPage

        page = self.make_page(LibraryPage, {
            "library_status": ok({"videos_total": 0}),
            "library_scan": ok({}),
            "library_search_semantic": ok(self._search_value(IMAGE_EMBEDDING)),
            "library_search_labels": ok(self._search_value(IMAGE_EMBEDDING)),
        })
        page.query_edit.setText("星空")
        page.search()

        text = page.space_label.text()
        self.assertIn("chinese_clip", text)
        self.assertIn("1024 维", text)
        self.assertIn("图片", text)
        self.assertNotIn("⚠", text, "正常走图片空间时不该报警")

    def test_warns_when_the_search_fell_back_to_the_text_space(self) -> None:
        from xbc.ui.pages.library import LibraryPage

        note = "当前使用文本空间（Caption），质量弱于图片空间：装好图片嵌入模型并重新扫描素材后会自动切到图片空间"
        page = self.make_page(LibraryPage, {
            "library_status": ok({"videos_total": 0}),
            "library_scan": ok({}),
            "library_search_semantic": ok(self._search_value(TEXT_EMBEDDING, note)),
            "library_search_labels": ok(self._search_value(TEXT_EMBEDDING, note)),
        })
        page.query_edit.setText("星空")
        page.search()

        text = page.space_label.text()
        self.assertIn("⚠", text, "降级必须看得见")
        self.assertIn(DOWNGRADE_KEYWORD, text)
        self.assertIn(DOWNGRADE_REASON, text)
        self.assertIn("768 维", text)


class MatchPageSpaceTests(_UiBase):
    def _show_value(self, space, note=""):
        return {"name": "演示", "version": 2, "stale_candidates": 0,
                "space": space, "note": note,
                "segments": [{"index": 0, "text": "第一句。",
                              "selected_shot_key": "k", "selected_shot_id": 1,
                              "selected_stale": False, "candidates": []}]}

    def test_shows_the_image_space_when_opening_a_result(self) -> None:
        from xbc.ui.pages.match import MatchPage

        page = self.make_page(MatchPage, {
            "match_show": ok(self._show_value(
                {"provider": "chinese_clip", "model": "chinese-clip-rn50",
                 "dim": 1024, "modality": "image"})),
            "script_match": ok({}), "match_select": ok({}), "match_reorder": ok({}),
        })
        page.render_open(ok(self._show_value(
            {"provider": "chinese_clip", "model": "chinese-clip-rn50",
             "dim": 1024, "modality": "image"})))

        text = page.space_label.text()
        self.assertIn("chinese_clip", text)
        self.assertIn("1024 维", text)
        self.assertIn("图片", text)
        self.assertNotIn("⚠", text)

    def test_warns_when_the_saved_result_used_the_text_space(self) -> None:
        from xbc.ui.pages.match import MatchPage

        page = self.make_page(MatchPage, {
            "match_show": ok({}), "script_match": ok({}),
            "match_select": ok({}), "match_reorder": ok({}),
        })
        note = "这份匹配是用文本空间（Caption）算的，质量弱于图片空间"
        page.render_open(ok(self._show_value(
            {"provider": "ollama", "model": "nomic-embed-text",
             "dim": 768, "modality": "text"}, note)))

        text = page.space_label.text()
        self.assertIn("⚠", text)
        self.assertIn(DOWNGRADE_REASON, text)
        self.assertIn("768 维", text)

    def test_match_button_also_fills_the_space_label(self) -> None:
        """「开始匹配」走 `render_match` → `render_open`，两个入口显示必须一致。"""
        from xbc.ui.pages.match import MatchPage

        value = {
            "name": "演示", "path": "/tmp/x.json", "segments": 1, "top_n": 3,
            "version": 2, "library_shots": 1,
            "space": {"provider": "chinese_clip", "model": "chinese-clip-rn50",
                      "dim": 1024, "modality": "image"},
            "note": "",
            "results": [{"index": 0, "text": "第一句。", "selected_shot_key": "k",
                         "selected_shot_id": 1, "candidates": []}],
        }
        page = self.make_page(MatchPage, {
            "script_match": ok(value),
            "match_show": ok({"count": 1, "matches": [{"name": "演示", "segments": 1}]}),
            "match_select": ok({}), "match_reorder": ok({}),
        })
        page.name_edit.setText("演示")
        page.script_edit.setPlainText("第一句。")
        page.match()

        self.assertIn("chinese_clip", page.space_label.text())
        self.assertIn("1024 维", page.space_label.text())


class SpaceLineHelperTests(unittest.TestCase):
    """`space_line` 是**纯展示** —— 不认识的就如实说，不猜。"""

    def test_renders_all_four_fields(self) -> None:
        from xbc.ui.pages.base import describe_space

        text = describe_space({"provider": "chinese_clip",
                               "model": "chinese-clip-rn50",
                               "dim": 1024, "modality": "image"})
        for piece in ("chinese_clip", "chinese-clip-rn50", "1024 维", "图片"):
            self.assertIn(piece, text)

    def test_unknown_modality_is_shown_verbatim(self) -> None:
        from xbc.ui.pages.base import describe_space

        self.assertIn("audio", describe_space(
            {"provider": "p", "model": "m", "dim": 512, "modality": "audio"}))

    def test_empty_space_says_unknown(self) -> None:
        from xbc.ui.pages.base import describe_space, space_line

        self.assertIn("未知", describe_space({}))
        self.assertIn("未知", describe_space(None))
        self.assertNotIn("⚠", space_line(None, ""))

    def test_note_turns_into_a_warning_prefix(self) -> None:
        from xbc.ui.pages.base import space_line

        self.assertTrue(space_line({"provider": "p", "model": "m", "dim": 1},
                                   "降级了").startswith("⚠"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
