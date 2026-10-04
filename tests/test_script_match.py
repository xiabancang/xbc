"""TASK-011 文案 → 镜头匹配的测试。

分两层：
- **纯逻辑**（分段 / 存取 / 编辑）—— 不需要素材库，快
- **端到端**（经插件工具）—— 用进程内假 Provider，不需要真模型
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
PLUGIN_DIR = REPO_ROOT / "plugins" / "video_analyzer"
for path in (str(REPO_ROOT), str(SRC_DIR), str(PLUGIN_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from xbc.core.capabilities.ai import (  # noqa: E402
    AICapability,
    EmbeddingRequest,
    EmbeddingResult,
    ModelProvider,
)
from xbc.core.context import AppContext  # noqa: E402
from xbc.core.runtime.manager import PluginManager  # noqa: E402

from xbc_va_match import (  # noqa: E402
    MatchError,
    MatchStore,
    find_candidate,
    reorder_candidate,
    segment_script,
    select_shot,
)

try:
    from tests.test_video_library import (  # noqa: E402
        EMBED_DIM,
        FakeAIProvider,
        FakeImageProvider,
        make_test_video,
    )
except ImportError:  # pragma: no cover
    from test_video_library import (  # noqa: E402
        EMBED_DIM,
        FakeAIProvider,
        FakeImageProvider,
        make_test_video,
    )

PLUGINS_DIR = REPO_ROOT / "plugins"


# ================= 分段 =================


class SegmentationTests(unittest.TestCase):
    def test_splits_on_sentence_endings(self) -> None:
        script = "夜幕降临。城市亮起灯光！车流开始涌动？"
        self.assertEqual(
            segment_script(script),
            ["夜幕降临。", "城市亮起灯光！", "车流开始涌动？"],
        )

    def test_keeps_the_terminator_with_its_sentence(self) -> None:
        for piece in segment_script("第一句。第二句。"):
            self.assertTrue(piece.endswith("。"), piece)

    def test_newline_is_a_boundary(self) -> None:
        self.assertEqual(segment_script("第一行\n第二行"), ["第一行", "第二行"])

    def test_long_sentence_splits_on_clause_punctuation(self) -> None:
        script = "这是一个很长的句子，它有很多分句，每一个分句都在讲不同的事情。"
        pieces = segment_script(script, max_chars=12, min_chars=0)
        self.assertTrue(len(pieces) > 1, "超长句子必须被切开")
        self.assertTrue(all(len(p) <= 12 for p in pieces), pieces)

    def test_hard_cut_is_the_last_resort(self) -> None:
        # 没有任何标点的长串只能硬切
        pieces = segment_script("啊" * 25, max_chars=10, min_chars=0)
        self.assertEqual([len(p) for p in pieces], [10, 10, 5])

    def test_short_author_sentences_are_kept_as_is(self) -> None:
        """作者写的短句是**节拍**，不是噪声 —— 不能因为短就并掉。"""
        pieces = segment_script("完整的句子在这里。短！", min_chars=6)
        self.assertEqual(len(pieces), 2, pieces)
        self.assertEqual(pieces[1], "短！")

    def test_short_fragments_from_clause_splitting_do_merge(self) -> None:
        """**我们自己**切出来的碎片才该合并。"""
        script = "这是一句很长的话，短，但它后面还有很长的内容要继续讲下去。"
        pieces = segment_script(script, max_chars=10, min_chars=6)
        self.assertNotIn("短，", pieces, "孤立的两字碎片不该单独成段")
        self.assertIn("短", pieces[0])
        # 合并会让片段略微超过上限 —— 上限是**软目标**（真正硬约束是 50 token），
        # 宁可略超也不要留下无语义的碎片
        self.assertLessEqual(max(len(p) for p in pieces), 10 + 6)

    def test_leading_short_fragment_merges_forward(self) -> None:
        pieces = segment_script("短，这是一句很长的话，后面还有内容。", max_chars=10, min_chars=6)
        self.assertEqual(pieces[0], "短，这是一句很长的话，", "首片过短时并入下一片")

    def test_empty_input_yields_no_segments(self) -> None:
        self.assertEqual(segment_script(""), [])
        self.assertEqual(segment_script("   \n\n  "), [])

    def test_carriage_returns_are_normalised(self) -> None:
        self.assertEqual(segment_script("第一行\r\n第二行\r第三行"), ["第一行", "第二行", "第三行"])

    def test_min_chars_zero_keeps_everything(self) -> None:
        self.assertEqual(len(segment_script("短。也短。", min_chars=0)), 2)

    def test_invalid_parameters_are_rejected(self) -> None:
        with self.assertRaises(MatchError):
            segment_script("文案", max_chars=0)
        with self.assertRaises(MatchError):
            segment_script("文案", min_chars=-1)

    def test_default_granularity_is_documented(self) -> None:
        """默认 32 字上限的依据是中文 CLIP 的 context_length=52（留余量）。"""
        from xbc_va_match import DEFAULT_MAX_CHARS, DEFAULT_MIN_CHARS

        self.assertEqual(DEFAULT_MAX_CHARS, 32)
        self.assertEqual(DEFAULT_MIN_CHARS, 6)
        self.assertLess(DEFAULT_MAX_CHARS, 50, "必须留在 50 token 以内")


# ================= 存取 =================


class MatchStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="xbc-match-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.store = MatchStore(self.root)

    def test_round_trip(self) -> None:
        payload = {"name": "demo", "segments": [{"index": 0, "text": "文案"}]}
        path = self.store.save(payload)
        self.assertTrue(path.is_file())
        loaded = self.store.load("demo")
        self.assertEqual(loaded["segments"][0]["text"], "文案")
        self.assertTrue(loaded["created_at"])
        self.assertTrue(loaded["updated_at"])

    def test_lives_under_the_plugin_data_dir(self) -> None:
        self.store.save({"name": "demo", "segments": []})
        self.assertEqual(self.store.dir, self.root / "matches")

    def test_rejects_path_traversal_names(self) -> None:
        for bad in ("../escape", "a/b", "a\\b", "", "x" * 65, "na me"):
            with self.assertRaises(MatchError, msg=bad):
                self.store.validate_name(bad)

    def test_accepts_chinese_names(self) -> None:
        self.assertEqual(self.store.validate_name("宣传片-第一版"), "宣传片-第一版")

    def test_missing_name_is_reported(self) -> None:
        with self.assertRaises(MatchError) as caught:
            self.store.load("不存在")
        self.assertIn("没有这份匹配结果", str(caught.exception))

    def test_broken_json_is_reported(self) -> None:
        self.store.dir.mkdir(parents=True, exist_ok=True)
        (self.store.dir / "broken.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(MatchError) as caught:
            self.store.load("broken")
        self.assertIn("不是合法 JSON", str(caught.exception))

    def test_listing_reports_broken_files_without_crashing(self) -> None:
        self.store.save({"name": "ok", "segments": [{"index": 0}]})
        self.store.dir.joinpath("broken.json").write_text("{not json", encoding="utf-8")
        items = {item["name"]: item for item in self.store.listing()}
        self.assertIn("ok", items)
        self.assertTrue(items["broken"].get("broken"))


# ================= 编辑 =================


def _payload() -> dict:
    return {
        "name": "demo",
        "segments": [
            {
                "index": 0,
                "text": "第一段",
                "selected_shot_id": 1,
                "candidates": [
                    {"shot_id": 1, "score": 0.9},
                    {"shot_id": 2, "score": 0.8},
                    {"shot_id": 3, "score": 0.7},
                ],
            }
        ],
    }


class EditingTests(unittest.TestCase):
    def test_select_an_existing_candidate(self) -> None:
        payload = _payload()
        select_shot(payload, segment_index=0, shot_id=2)
        segment = payload["segments"][0]
        self.assertEqual(segment["selected_shot_id"], 2)
        self.assertEqual(len(segment["candidates"]), 3, "只是改选，不该插新候选")

    def test_select_a_shot_outside_the_candidates_inserts_it(self) -> None:
        payload = _payload()
        shot = {"video_path": "/v.mp4", "shot_index": 5,
                "start_seconds": 1.0, "end_seconds": 2.0,
                "duration_seconds": 1.0, "labels": ["夜景"]}
        select_shot(payload, segment_index=0, shot_id=99, shot=shot)
        segment = payload["segments"][0]
        self.assertEqual(segment["selected_shot_id"], 99)
        self.assertEqual(segment["candidates"][0]["shot_id"], 99)
        self.assertTrue(segment["candidates"][0]["manual"])
        self.assertIsNone(segment["candidates"][0]["score"], "人工指定的候选不给检索分数")

    def test_select_outside_without_shot_info_is_rejected(self) -> None:
        with self.assertRaises(MatchError) as caught:
            select_shot(_payload(), segment_index=0, shot_id=99)
        self.assertIn("无法插入", str(caught.exception))

    def test_reorder_up_and_down(self) -> None:
        payload = _payload()
        reorder_candidate(payload, segment_index=0, shot_id=3, direction="up")
        self.assertEqual([c["shot_id"] for c in payload["segments"][0]["candidates"]],
                         [1, 3, 2])
        reorder_candidate(payload, segment_index=0, shot_id=3, direction="down")
        self.assertEqual([c["shot_id"] for c in payload["segments"][0]["candidates"]],
                         [1, 2, 3])

    def test_reorder_at_the_edge_is_reported(self) -> None:
        with self.assertRaises(MatchError) as caught:
            reorder_candidate(_payload(), segment_index=0, shot_id=1, direction="up")
        self.assertIn("最前", str(caught.exception))
        with self.assertRaises(MatchError):
            reorder_candidate(_payload(), segment_index=0, shot_id=3, direction="down")

    def test_bad_direction_is_rejected(self) -> None:
        with self.assertRaises(MatchError):
            reorder_candidate(_payload(), segment_index=0, shot_id=2, direction="sideways")

    def test_out_of_range_segment_is_reported(self) -> None:
        with self.assertRaises(MatchError) as caught:
            select_shot(_payload(), segment_index=7, shot_id=1)
        self.assertIn("越界", str(caught.exception))

    def test_missing_shot_in_candidates_is_reported(self) -> None:
        with self.assertRaises(MatchError):
            reorder_candidate(_payload(), segment_index=0, shot_id=42, direction="up")

    def test_find_candidate(self) -> None:
        segment = _payload()["segments"][0]
        self.assertEqual(find_candidate(segment, 2), 1)
        self.assertEqual(find_candidate(segment, 99), -1)


# ================= 端到端 =================


class _MatchBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture_dir = Path(tempfile.mkdtemp(prefix="xbc-match-fixture-"))
        cls.video = cls.fixture_dir / "first.mp4"
        cls.video_ready = make_test_video(cls.video)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.fixture_dir, ignore_errors=True)

    def setUp(self) -> None:
        if not self.video_ready:
            self.skipTest("无法合成测试视频（FFmpeg 不可用）")
        self.root = Path(tempfile.mkdtemp(prefix="xbc-match-e2e-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.materials = self.root / "materials"
        self.materials.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.video, self.materials / "first.mp4")

        self.ctx = AppContext.create(root=self.root, console=False)
        self.addCleanup(self.ctx.close)
        self.provider = FakeAIProvider()
        self.image_provider = FakeImageProvider()
        self.ctx.ai.register(self.provider, default=True)
        self.ctx.ai.register(self.image_provider)

        manager = PluginManager(self.ctx, [PLUGINS_DIR], self.ctx.logger)
        manager.discover()
        manager.activate("video_analyzer")
        self.ctx.tool_registry.call("library_scan", {"directory": str(self.materials)})

    def call(self, tool: str, **arguments):
        return self.ctx.tool_registry.call(tool, arguments)


class ScriptMatchEndToEndTests(_MatchBase):
    SCRIPT = "镜头的画面是 shot000 的样子。接着切到 shot001 的画面。最后回到 shot002。"

    def test_returns_top_n_candidates_per_segment(self) -> None:
        value = self.call("script_match", name="demo", script=self.SCRIPT).value
        self.assertEqual(value["segments"], 3, "三句话应切成三段")
        self.assertEqual(value["top_n"], 3)
        for segment in value["results"]:
            self.assertGreaterEqual(len(segment["candidates"]), 3, "每段至少 3 个候选")
            for candidate in segment["candidates"]:
                self.assertIsInstance(candidate["score"], float)
                self.assertTrue(Path(candidate["video_path"]).is_file(), "候选来自真实镜头")
                self.assertLess(candidate["start"], candidate["end"])

    def test_candidates_are_sorted_by_score(self) -> None:
        value = self.call("script_match", name="demo", script=self.SCRIPT).value
        for segment in value["results"]:
            scores = [c["score"] for c in segment["candidates"]]
            self.assertEqual(scores, sorted(scores, reverse=True))

    def test_reuses_the_task_010_retrieval_exactly(self) -> None:
        """**复用证据**：匹配给出的候选与 library_search_semantic 逐字段一致。

        如果匹配自己写了一套打分，两者迟早会分叉 —— 这条测试就是防分叉的。
        """
        matched = self.call("script_match", name="demo", script=self.SCRIPT).value
        for segment in matched["results"]:
            searched = self.call(
                "library_search_semantic",
                query=segment["text"], limit=matched["top_n"], space="auto",
            ).value
            self.assertEqual(
                [(c["shot_id"], c["score"]) for c in segment["candidates"]],
                [(r["shot_id"], r["score"]) for r in searched["results"]],
                f"分段「{segment['text']}」的候选必须与检索工具完全一致",
            )

    def test_result_is_persisted_and_readable(self) -> None:
        self.call("script_match", name="demo", script=self.SCRIPT)
        shown = self.call("match_show", name="demo").value
        self.assertEqual(shown["name"], "demo")
        self.assertEqual(len(shown["segments"]), 3)
        self.assertEqual(shown["script"], self.SCRIPT)
        self.assertTrue(shown["space"]["provider"])

    def test_show_without_name_lists_saved_results(self) -> None:
        self.call("script_match", name="demo", script=self.SCRIPT)
        listing = self.call("match_show").value
        self.assertEqual(listing["count"], 1)
        self.assertEqual(listing["matches"][0]["name"], "demo")
        self.assertEqual(listing["matches"][0]["segments"], 3)

    def test_empty_script_is_rejected(self) -> None:
        result = self.call("script_match", name="demo", script="   ")
        self.assertFalse(result.ok)
        self.assertIn("没有可匹配的段", result.message)

    def test_top_n_is_configurable(self) -> None:
        value = self.call("script_match", name="demo", script=self.SCRIPT, top_n=2).value
        self.assertEqual(value["top_n"], 2)
        self.assertTrue(all(len(s["candidates"]) == 2 for s in value["results"]))

    def test_segmentation_parameters_are_configurable(self) -> None:
        value = self.call(
            "script_match", name="demo", script=self.SCRIPT, max_chars=8, min_chars=0
        ).value
        self.assertGreater(value["segments"], 3, "上限收紧后应切出更多段")


class ManualAdjustmentTests(_MatchBase):
    SCRIPT = "第一段讲的是 shot000。第二段讲的是 shot001。"

    def setUp(self) -> None:
        super().setUp()
        self.call("script_match", name="demo", script=self.SCRIPT)

    def test_select_changes_the_chosen_shot_and_persists(self) -> None:
        before = self.call("match_show", name="demo").value
        segment = before["segments"][0]
        other = segment["candidates"][1]["shot_id"]

        self.call("match_select", name="demo", segment_index=0, shot_id=other)

        after = self.call("match_show", name="demo").value
        self.assertEqual(after["segments"][0]["selected_shot_id"], other)

    def test_manual_shot_outside_candidates_is_inserted_and_persists(self) -> None:
        import sqlite3

        # 用 top_n=1 另开一份，保证库里必有镜头**不在候选里**
        self.call("script_match", name="narrow", script=self.SCRIPT, top_n=1)
        existing = self.call("match_show", name="narrow").value
        used = {c["shot_id"] for s in existing["segments"] for c in s["candidates"]}

        db = self.ctx.paths.plugin_data_dir("video_analyzer") / "library.db"
        connection = sqlite3.connect(str(db))
        all_shots = {row[0] for row in connection.execute("SELECT id FROM shots")}
        connection.close()

        outside = sorted(all_shots - used)
        self.assertTrue(outside, "前置条件：top_n=1 时应有镜头不在候选里")
        target = outside[0]

        self.call("match_select", name="narrow", segment_index=0, shot_id=target)

        segment = self.call("match_show", name="narrow").value["segments"][0]
        self.assertEqual(segment["selected_shot_id"], target)
        self.assertEqual(segment["candidates"][0]["shot_id"], target, "手动指定的插到首位")
        self.assertTrue(segment["candidates"][0]["manual"])
        self.assertIsNone(segment["candidates"][0]["score"], "人工指定的候选不给检索分数")
        self.assertEqual(len(segment["candidates"]), 2, "原候选仍在，只是被挤到后面")

    def test_reorder_changes_order_and_persists(self) -> None:
        before = self.call("match_show", name="demo").value["segments"][0]
        order_before = [c["shot_id"] for c in before["candidates"]]
        moved = order_before[1]

        self.call("match_reorder", name="demo", segment_index=0, shot_id=moved, direction="up")

        after = self.call("match_show", name="demo").value["segments"][0]
        order_after = [c["shot_id"] for c in after["candidates"]]
        self.assertEqual(order_after[0], moved)
        self.assertEqual(sorted(order_after), sorted(order_before), "只换顺序，不增减候选")

    def test_reorder_at_the_edge_is_a_clear_error(self) -> None:
        first = self.call("match_show", name="demo").value["segments"][0]
        result = self.call(
            "match_reorder", name="demo", segment_index=0,
            shot_id=first["candidates"][0]["shot_id"], direction="up",
        )
        self.assertFalse(result.ok)
        self.assertIn("最前", result.message)

    def test_unknown_match_name_is_a_clear_error(self) -> None:
        result = self.call("match_show", name="不存在的名字")
        self.assertFalse(result.ok)
        self.assertIn("没有这份匹配结果", result.message)

    def test_traversal_name_is_rejected(self) -> None:
        result = self.call("script_match", name="../escape", script=self.SCRIPT)
        self.assertFalse(result.ok)
        self.assertIn("只允许", result.message)

    def test_adjustments_survive_a_reload_of_the_plugin_context(self) -> None:
        """存盘之后重新打开上下文再读 —— 调整必须还在。"""
        segment = self.call("match_show", name="demo").value["segments"][0]
        target = segment["candidates"][1]["shot_id"]
        self.call("match_select", name="demo", segment_index=0, shot_id=target)
        self.call("match_reorder", name="demo", segment_index=0, shot_id=target, direction="up")

        self.ctx.close()
        self.ctx = AppContext.create(root=self.root, console=False)
        self.addCleanup(self.ctx.close)
        manager = PluginManager(self.ctx, [PLUGINS_DIR], self.ctx.logger)
        manager.discover()
        manager.activate("video_analyzer")
        shown = self.ctx.tool_registry.call("match_show", {"name": "demo"}).value
        self.assertEqual(shown["segments"][0]["selected_shot_id"], target)
        self.assertEqual(shown["segments"][0]["candidates"][0]["shot_id"], target)


class NoVectorsTests(unittest.TestCase):
    def test_matching_with_an_empty_library_is_a_clear_error(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="xbc-match-empty-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        ctx = AppContext.create(root=root, console=False)
        self.addCleanup(ctx.close)
        ctx.ai.register(FakeAIProvider(), default=True)
        manager = PluginManager(ctx, [PLUGINS_DIR], ctx.logger)
        manager.discover()
        manager.activate("video_analyzer")

        result = ctx.tool_registry.call(
            "script_match", {"name": "demo", "script": "随便一句文案。"}
        )
        self.assertFalse(result.ok)
        self.assertIn("库里还没有向量", result.message)


if __name__ == "__main__":
    unittest.main(verbosity=2)
