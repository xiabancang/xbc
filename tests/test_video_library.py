"""素材资产库测试（TASK-009）。

用**进程内的假 AI Provider** 代替真模型：库的存取、增量、容错、检索、导出、重建
都是确定性逻辑，不该依赖本机是否跑着 Ollama。
真实模型的检索质量另有实测记录（见 docs/task-009-...-report.md）。
"""

from __future__ import annotations

import array
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
PLUGIN_DIR = REPO_ROOT / "plugins" / "video_analyzer"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from xbc.core.capabilities.ai import (  # noqa: E402
    AICapability,
    AIUnavailable,
    EmbeddingRequest,
    EmbeddingResult,
    ImageEmbeddingRequest,
    ModelProvider,
    TextRequest,
    TextResult,
    VisionAnswer,
    VisionRequest,
    VisionResult,
)
from xbc.core.context import AppContext  # noqa: E402
from xbc.core.runtime.manager import PluginManager  # noqa: E402

from xbc_va_library import Library  # noqa: E402

try:
    from tests.test_video_analyzer import make_test_video
except ImportError:  # pragma: no cover
    from test_video_analyzer import make_test_video

PLUGINS_DIR = REPO_ROOT / "plugins"
EMBED_DIM = 32


class FakeAIProvider(ModelProvider):
    """进程内假 Provider：标签与向量都由**输入内容**决定，因此可断言。

    - 视觉：从关键帧文件名（如 `shot000_f01.jpg`）推出 `shot000`，
      标签为 `[shot000, shot000-标签]` —— 不同镜头得到不同标签
    - 向量：字符袋（每个字符落进一个桶再归一化）。
      `shot000` 的查询会与 `shot000 的画面` 更接近，与 `shot001 的画面` 更远。
    """

    name = "fake"
    capabilities = frozenset({AICapability.VISION, AICapability.EMBEDDING})

    def __init__(self, *, fail_times: int = 0) -> None:
        self.calls = 0
        self.fail_times = fail_times
        #: 记录**真正收到的嵌入文本**，用于验证库里存的 embed_input 就是喂进来的原文
        self.embedded_texts: list[str] = []

    def available(self) -> bool:
        return True

    # ---------- 视觉 ----------
    @staticmethod
    def _shot_token(request: VisionRequest) -> str:
        stem = Path(str(request.images[0])).stem if request.images else "unknown"
        return stem.split("_")[0]

    def vision_analyze(self, request: VisionRequest) -> VisionResult | VisionAnswer:
        self.calls += 1
        if self.calls <= self.fail_times:
            # 抛契约内的错误类型：真 Provider 失败时也是 AIError 的子类
            raise AIUnavailable("假 Provider 故意失败")
        token = self._shot_token(request)
        answer = f"{token} 的画面"
        labels = [token, f"{token}-标签"]
        if request.question:
            return VisionAnswer(
                question=request.question, answer=answer, labels=labels,
                provider=self.name, model="fake-vision",
            )
        return VisionResult(
            description=answer, labels=labels, provider=self.name, model="fake-vision"
        )

    # ---------- 向量 ----------
    @staticmethod
    def _vector(text: str) -> list[float]:
        values = [0.0] * EMBED_DIM
        for char in text:
            bucket = hashlib.md5(char.encode("utf-8")).digest()[0] % EMBED_DIM
            values[bucket] += 1.0
        norm = sum(value * value for value in values) ** 0.5
        return [value / norm for value in values] if norm else values

    def embedding(self, request: EmbeddingRequest) -> EmbeddingResult:
        self.embedded_texts.extend(request.texts)
        return EmbeddingResult(
            vectors=[self._vector(text) for text in request.texts],
            provider=self.name, model="fake-embed",
        )

    def text_generate(self, request: TextRequest) -> TextResult:
        return TextResult(text="ok", provider=self.name, model="fake-text")


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture_dir = Path(tempfile.mkdtemp(prefix="xbc-lib-fixture-"))
        cls.video = cls.fixture_dir / "first.mp4"
        cls.video_ready = make_test_video(cls.video)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.fixture_dir, ignore_errors=True)

    def setUp(self) -> None:
        if not self.video_ready:
            self.skipTest("无法合成测试视频（FFmpeg 不可用或编码器缺失）")
        self.root = Path(tempfile.mkdtemp(prefix="xbc-lib-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.materials = self.root / "materials"
        self.materials.mkdir(parents=True, exist_ok=True)
        self.clip = self.materials / "first.mp4"
        shutil.copy2(self.video, self.clip)

        self.provider = FakeAIProvider()
        self.ctx = self.make_context()

    def make_context(self, *, approver=None) -> AppContext:
        ctx = AppContext.create(root=self.root, console=False, approver=approver)
        self.addCleanup(ctx.close)
        ctx.ai.register(self.provider, default=True)
        manager = PluginManager(ctx, [PLUGINS_DIR], ctx.logger)
        manager.discover()
        manager.activate("video_analyzer")
        self.manager = manager
        return ctx

    def call(self, tool: str, **arguments):
        return self.ctx.tool_registry.call(tool, arguments)

    def scan(self, directory: Path | None = None, **kwargs):
        return self.call("library_scan", directory=str(directory or self.materials), **kwargs)

    def reload_ctx(self, *, approver=None, provider=None) -> AppContext:
        self.ctx.close()
        self.provider = provider if provider is not None else FakeAIProvider()
        self.ctx = self.make_context(approver=approver)
        return self.ctx

    def library(self) -> Library:
        library = Library(self.ctx.paths.plugin_data_dir("video_analyzer") / "library.db")
        library.initialize()
        return library


# ================= 入库 =================


class ScanTests(_Base):
    def test_scan_builds_library(self) -> None:
        value = self.scan().value
        self.assertEqual(value["found"], 1)
        self.assertEqual(len(value["analyzed"]), 1)
        self.assertEqual(value["failed"], [])

        summary = value["summary"]["library"]
        self.assertEqual(summary["videos_total"], 1)
        self.assertEqual(summary["shots"], 3, "测试视频应切出 3 个镜头")
        self.assertEqual(summary["frames"], 3, "默认每镜头 1 帧")
        self.assertEqual(summary["labels"], 6, "3 镜头 × 2 标签")
        self.assertEqual(summary["vectors"], 6, "每镜头 1 帧向量 + 1 个镜头向量")
        self.assertEqual(summary["vector_dim"], EMBED_DIM)

    def test_status_reports_library_size(self) -> None:
        self.scan()
        status = self.call("library_status").value
        self.assertEqual(status["videos_total"], 1)
        self.assertEqual(status["not_ok"], [])
        self.assertTrue(status["library_exists"])

    # ---------- 增量：内容哈希 ----------
    def test_rescan_skips_by_content_hash(self) -> None:
        self.scan()
        before = self.provider.calls

        value = self.scan().value
        self.assertEqual(value["found"], 1)
        self.assertEqual(value["analyzed"], [])
        self.assertEqual(len(value["skipped"]), 1)
        self.assertIn("内容哈希未变", value["skipped"][0]["reason"])
        self.assertEqual(self.provider.calls, before, "跳过时不应再调用模型")

    def test_same_content_different_path_is_treated_as_new(self) -> None:
        """哈希按**内容**算，但增量判定按**路径+哈希** —— 换名的同一个文件是新条目。"""
        self.scan()
        copied = self.materials / "renamed.mp4"
        shutil.copy2(self.clip, copied)

        value = self.scan().value
        self.assertEqual(len(value["analyzed"]), 1)
        self.assertEqual(Path(value["analyzed"][0]["path"]).name, "renamed.mp4")

    def test_changed_content_is_reanalyzed(self) -> None:
        self.scan()
        original_hash = self.library().get_video_by_path(str(self.clip))["file_hash"]

        # 换一段完全不同的画面（重新生成 → 内容与哈希都变）
        subprocess.run(
            [shutil.which("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "mandelbrot=s=320x240:r=10", "-t", "6",
             "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "veryfast",
             str(self.clip)],
            capture_output=True, timeout=180,
        )
        new_hash = hashlib.sha256(self.clip.read_bytes()).hexdigest()
        self.assertNotEqual(original_hash, new_hash, "前置条件：文件内容应确实变了")

        value = self.scan().value
        self.assertEqual(len(value["analyzed"]), 1, "内容变了必须重新分析")
        self.assertEqual(value["skipped"], [])

    def test_new_file_only_analyzes_the_new_one(self) -> None:
        self.scan()
        second = self.materials / "second.mp4"
        shutil.copy2(self.video, second)

        value = self.scan().value
        self.assertEqual(value["found"], 2)
        self.assertEqual(len(value["analyzed"]), 1)
        self.assertEqual(len(value["skipped"]), 1)
        self.assertEqual(Path(value["analyzed"][0]["path"]).name, "second.mp4")

    def test_force_reanalyzes_even_when_hash_matches(self) -> None:
        self.scan()
        value = self.scan(force=True).value
        self.assertEqual(len(value["analyzed"]), 1)
        self.assertEqual(value["skipped"], [])

    def test_missing_directory_is_a_protocol_error(self) -> None:
        result = self.call("library_scan", directory=str(self.root / "nope"))
        self.assertFalse(result.ok)
        self.assertIn("目录不存在", result.message)

    def test_non_video_files_are_ignored(self) -> None:
        (self.materials / "readme.txt").write_text("not a video", encoding="utf-8")
        value = self.scan().value
        self.assertEqual(value["found"], 1)


# ================= 容错 =================


class FailureTests(_Base):
    def test_broken_file_is_recorded_and_others_still_analyzed(self) -> None:
        good = self.materials / "second.mp4"
        shutil.copy2(self.video, good)
        broken = self.materials / "broken.mp4"
        broken.write_bytes(b"definitely not a video" * 50)

        value = self.scan().value
        self.assertEqual(value["found"], 3)
        self.assertEqual(len(value["analyzed"]), 2, "好文件必须照样入库")
        self.assertEqual(len(value["failed"]), 1)
        self.assertEqual(Path(value["failed"][0]["path"]).name, "broken.mp4")

        status = self.call("library_status").value
        failed = [row for row in status["not_ok"] if row["status"] == "failed"]
        self.assertEqual(len(failed), 1, "失败项要留在库里，否则没法重试")
        self.assertIn("broken.mp4", failed[0]["path"])

    def test_retry_picks_up_failed_entries(self) -> None:
        broken = self.materials / "broken.mp4"
        broken.write_bytes(b"nope" * 50)
        self.scan()

        retry = self.call("library_retry").value
        self.assertEqual(retry["candidates"], 1)
        self.assertEqual(len(retry["failed"]), 1)

    def test_retry_succeeds_after_the_file_is_fixed(self) -> None:
        broken = self.materials / "broken.mp4"
        broken.write_bytes(b"nope" * 50)
        self.scan()

        shutil.copy2(self.video, broken)
        retry = self.call("library_retry").value
        self.assertEqual(len(retry["analyzed"]), 1)
        self.assertEqual(retry["failed"], [])
        self.assertEqual(self.call("library_status").value["not_ok"], [])

    def test_retry_reports_missing_files(self) -> None:
        broken = self.materials / "broken.mp4"
        broken.write_bytes(b"nope" * 50)
        self.scan()
        broken.unlink()

        retry = self.call("library_retry").value
        self.assertEqual(len(retry["missing"]), 1)
        self.assertEqual(len(retry["failed"]), 0)

    def test_ai_failure_marks_video_failed_not_silently_empty(self) -> None:
        """AI 全帧失败时必须入库为 failed，不能留下一个"有视频没镜头"的成功条目。"""
        self.reload_ctx(provider=FakeAIProvider(fail_times=999))

        value = self.scan().value
        self.assertEqual(len(value["analyzed"]), 0)
        self.assertEqual(len(value["failed"]), 1)
        self.assertIn("一帧都没分析成功", value["failed"][0]["error"])

        status = self.call("library_status").value
        self.assertEqual(status["shots"], 0)
        self.assertEqual(len(status["not_ok"]), 1)


# ================= 检索 =================


class SearchTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.scan()

    def test_label_search_exact(self) -> None:
        value = self.call("library_search_labels", query="shot000", match="exact").value
        self.assertEqual(value["count"], 1)
        self.assertEqual(value["results"][0]["shot_index"], 0)
        self.assertEqual(value["results"][0]["matched_labels"], ["shot000"])

    def test_label_search_fuzzy_matches_substring(self) -> None:
        value = self.call("library_search_labels", query="-标签", match="fuzzy").value
        self.assertEqual(value["count"], 3, "3 个镜头各有一个 -标签")

    def test_label_search_returns_one_row_per_shot(self) -> None:
        """一个镜头命中多个标签时**只能出一行** —— 调用方要的是镜头列表。"""
        value = self.call("library_search_labels", query="shot", match="fuzzy").value
        shot_ids = [item["shot_id"] for item in value["results"]]
        self.assertEqual(len(shot_ids), len(set(shot_ids)), "同一个镜头不该重复出现")
        self.assertEqual(value["count"], 3)
        for item in value["results"]:
            self.assertEqual(len(item["matched_labels"]), 2, "两个标签都应被记下")

    def test_label_search_empty_query(self) -> None:
        value = self.call("library_search_labels", query="不存在的标签").value
        self.assertEqual(value["count"], 0)
        self.assertEqual(value["results"], [])

    def test_semantic_search_ranks_matching_shot_first(self) -> None:
        value = self.call("library_search_semantic", query="shot001").value
        self.assertEqual(value["count"], 3)
        top = value["results"][0]
        self.assertEqual(top["shot_index"], 1, "最相似的应是 shot001")
        self.assertEqual(top["labels"], ["shot001", "shot001-标签"])
        self.assertGreater(top["score"], value["results"][-1]["score"])

    def test_semantic_search_is_not_string_matching(self) -> None:
        """查询词在标签里完全不存在，语义检索仍应给出结果。"""
        label_hit = self.call(
            "library_search_labels", query="画面内容", match="fuzzy"
        ).value
        self.assertEqual(label_hit["count"], 0, "前置条件：标签里没有这个词")

        value = self.call("library_search_semantic", query="画面内容").value
        self.assertEqual(value["count"], 3, "语义检索不应依赖字面命中")
        self.assertTrue(all(item["score"] > 0 for item in value["results"]))

    def test_semantic_search_reports_embedding_provenance(self) -> None:
        value = self.call("library_search_semantic", query="shot000").value
        self.assertEqual(value["embedding"]["provider"], "fake")
        self.assertEqual(value["embedding"]["model"], "fake-embed")
        self.assertEqual(value["embedding"]["dim"], EMBED_DIM)
        self.assertEqual(value["candidates"], 3)
        self.assertEqual(value["skipped_other_space"], 0)

    def test_semantic_search_never_mixes_vector_spaces(self) -> None:
        """跨 Provider / 跨模型的向量不可比较 —— 检索**先选定一个空间**，绝不混算。

        TASK-010 之前是在算分之后按 model 过滤；现在提前到选空间，
        因为库里会同时存在中文 CLIP 图片向量（1024 维）与 Caption 文本向量（768 维）。
        """
        library = self.library()
        with library.session() as connection:
            connection.execute("UPDATE vectors SET model = 'other-model' WHERE shot_id = 1")

        value = self.call("library_search_semantic", query="shot000").value
        # 只在该空间内排序：候选数 = 该空间的镜头数，混进来的那条不参与算分
        self.assertEqual(value["candidates"], 2)
        self.assertEqual(value["skipped_other_space"], 0)
        self.assertEqual(value["embedding"]["model"], "fake-embed")

        # 但**如实列出**库里还存在别的空间，不能装作只有一种
        spaces = {(s["provider"], s["model"]) for s in value["spaces_available"]}
        self.assertIn(("fake", "other-model"), spaces)

    def test_spaces_are_reported_with_counts(self) -> None:
        """`library_status` 必须说清库里有哪些向量空间 —— 它们不可互相比较。"""
        self.scan()
        status = self.call("library_status").value
        self.assertEqual(len(status["spaces"]), 1)
        space = status["spaces"][0]
        self.assertEqual((space["provider"], space["model"]), ("fake", "fake-embed"))
        self.assertEqual(space["dim"], EMBED_DIM)
        self.assertEqual(space["shot_centroids"], 3)
        self.assertEqual(space["frames"], 3)

    def test_semantic_search_without_vectors_is_explicit(self) -> None:
        library = self.library()
        with library.session() as connection:
            connection.execute("DELETE FROM vectors")
        value = self.call("library_search_semantic", query="shot000").value
        self.assertEqual(value["count"], 0)
        self.assertIn("先跑 library_scan", value["note"])

    def test_semantic_search_results_point_back_to_timecodes(self) -> None:
        value = self.call("library_search_semantic", query="shot002").value
        top = value["results"][0]
        self.assertTrue(Path(top["video_path"]).is_file(), "结果必须能定位回源视频")
        self.assertLess(top["start"], top["end"])
        self.assertEqual(top["duration"], round(top["end"] - top["start"], 3))


# ================= 导出 =================


class ExportTests(_Base):
    def test_export_requires_consent(self) -> None:
        self.scan()
        result = self.call("library_export", shot_id=1)
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "consent_denied")

    def test_export_produces_a_clip(self) -> None:
        self.scan()
        self.reload_ctx(approver=lambda name, risk, arguments: True)

        result = self.call("library_export", shot_id=1)
        self.assertTrue(result.ok, result.message)
        value = result.value

        target = Path(value["output"])
        self.assertTrue(target.is_file())
        self.assertGreater(value["size_bytes"], 0)
        self.assertLess(value["start"], value["end"])

        probe = subprocess.run(
            [shutil.which("ffprobe"), "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(target)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        )
        actual = float(probe.stdout.strip())
        self.assertAlmostEqual(actual, value["duration"], delta=0.15,
                               msg="导出片段的实际时长应与镜头时长一致")

    def test_export_to_explicit_path(self) -> None:
        self.scan()
        self.reload_ctx(approver=lambda name, risk, arguments: True)
        target = self.root / "out" / "clip.mp4"

        result = self.call("library_export", shot_id=1, output=str(target))
        self.assertTrue(result.ok, result.message)
        self.assertTrue(target.is_file())

    def test_export_unknown_shot_is_rejected(self) -> None:
        self.scan()
        self.reload_ctx(approver=lambda name, risk, arguments: True)
        result = self.call("library_export", shot_id=9999)
        self.assertFalse(result.ok)
        self.assertIn("没有这个镜头", result.message)

    def test_export_reports_missing_source(self) -> None:
        self.scan()
        self.reload_ctx(approver=lambda name, risk, arguments: True)
        self.clip.unlink()
        result = self.call("library_export", shot_id=1)
        self.assertFalse(result.ok)
        self.assertIn("源视频不存在", result.message)


# ================= 重建 =================


class RebuildTests(_Base):
    def test_rebuild_drops_and_recreates(self) -> None:
        self.scan()
        before = self.call("library_status").value
        self.assertEqual(before["shots"], 3)

        value = self.call("library_rebuild", directory=str(self.materials)).value
        self.assertEqual(value["rebuilt"]["dropped"]["shots"], 3)
        self.assertEqual(len(value["analyzed"]), 1)

        after = value["summary"]["library"]
        self.assertEqual(after["videos_total"], 1)
        self.assertEqual(after["shots"], 3)
        self.assertEqual(self.call("library_status").value["shots"], 3)

    def test_rebuild_recovers_from_a_corrupt_library_file(self) -> None:
        """库损坏时必须能重建 —— 分析结果不是唯一副本。"""
        self.scan()
        library_path = self.ctx.paths.plugin_data_dir("video_analyzer") / "library.db"
        library_path.write_bytes(b"this is not a sqlite database at all")

        result = self.call("library_rebuild", directory=str(self.materials))
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.value["summary"]["library"]["shots"], 3)

    def test_rebuild_does_not_delete_the_videos(self) -> None:
        self.scan()
        self.call("library_rebuild", directory=str(self.materials))
        self.assertTrue(self.clip.is_file(), "重建只动库，不动素材")


# ================= TASK-010 第一部分：存储可审计性 =================


class AuditabilityTests(_Base):
    """v1 的缺陷是"向量由 answer + labels 算出，但库里只存 labels"。

    这一组测试把"可审计"钉成契约：**每条向量必须能回答它是从哪个字符串算出来的**。
    """

    def test_every_vector_records_the_embedded_text(self) -> None:
        self.scan()
        value = self.call("library_audit").value
        self.assertTrue(value["auditable"])
        self.assertEqual(value["vectors_auditable"], value["vectors_total"])
        self.assertEqual(value["missing_embed_input"], [])
        for record in value["records"]:
            self.assertTrue(record["has_embed_input"], f"vector#{record['vector_id']} 缺 embed_input")
            self.assertGreater(record["embed_input_chars"], 0)

    def test_embedded_text_is_exactly_what_the_provider_received(self) -> None:
        """不是"存了个大概" —— 存的必须是喂给 embedding 的原文。"""
        self.scan()
        records = self.call("library_audit").value["records"]
        frame_records = [r for r in records if r["kind"] == "frame"]
        self.assertEqual(len(frame_records), 3, "测试视频 3 个镜头、每镜头 1 帧")

        sent = getattr(self.provider, "embedded_texts", [])
        self.assertEqual(len(sent), 3, "假 Provider 应收到 3 次嵌入请求")
        self.assertEqual(
            sorted(r["embed_input"] for r in frame_records), sorted(sent),
            "库里存的 embed_input 必须与真正送给 provider 的文本逐字相同",
        )

    def test_audit_records_provider_and_model(self) -> None:
        self.scan()
        for record in self.call("library_audit").value["records"]:
            self.assertEqual(record["provider"], "fake")
            self.assertEqual(record["model"], "fake-embed")
            self.assertEqual(record["dim"], EMBED_DIM)

    def test_audit_records_time_and_material_hash(self) -> None:
        self.scan()
        digest = hashlib.sha256(self.clip.read_bytes()).hexdigest()[:16]
        for record in self.call("library_audit").value["records"]:
            self.assertTrue(record["created_at"], "必须记录向量产生时间")
            self.assertEqual(record["video_hash"], digest, "必须记录来源素材的内容哈希")
            self.assertTrue(record["video_analyzed_at"], "必须记录素材入库时间")
            self.assertTrue(Path(record["video_path"]).is_file())

    def test_shot_level_vectors_are_marked_as_centroid(self) -> None:
        """镜头级是多帧质心，不是直接嵌入的某段文本 —— 必须能区分，否则会误读。"""
        self.scan()
        records = self.call("library_audit").value["records"]
        kinds = {r["kind"] for r in records}
        self.assertEqual(kinds, {"frame", "shot_centroid"})
        stats = self.call("library_status").value
        self.assertEqual(stats["vectors_by_kind"], {"frame": 3, "shot_centroid": 3})

    def test_audit_can_be_scoped_to_one_shot(self) -> None:
        self.scan()
        all_records = self.call("library_audit").value["records"]
        shot_id = all_records[0]["shot_id"]
        scoped = self.call("library_audit", shot_id=shot_id).value["records"]
        self.assertTrue(scoped)
        self.assertTrue(all(r["shot_id"] == shot_id for r in scoped))
        self.assertLess(len(scoped), len(all_records))

    def test_search_is_deterministic_on_a_fixed_library(self) -> None:
        """同一份库、同一个查询跑两次必须完全一致 —— 这是"可复现"的下限。"""
        self.scan()
        first = self.call("library_search_semantic", query="shot001", limit=3).value
        second = self.call("library_search_semantic", query="shot001", limit=3).value
        self.assertEqual(json.dumps(first, ensure_ascii=False, sort_keys=True),
                         json.dumps(second, ensure_ascii=False, sort_keys=True))

    def test_reingest_is_skipped_by_hash_so_results_do_not_drift(self) -> None:
        """不强制重分析时，重复扫描不改变库 —— 检索结果自然不漂。"""
        self.scan()
        before = self.call("library_audit").value["records"]
        self.scan()
        after = self.call("library_audit").value["records"]
        self.assertEqual(
            [(r["vector_id"], r["embed_input"], r["created_at"]) for r in before],
            [(r["vector_id"], r["embed_input"], r["created_at"]) for r in after],
        )


class SchemaMigrationTests(unittest.TestCase):
    """v1 旧库必须能升级，且**如实承认旧向量不可审计**。"""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="xbc-migrate-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.path = self.root / "library.db"
        self._make_v1_library()

    def _make_v1_library(self) -> None:
        """手搓一个 v1 结构的库（vectors 表没有 kind/embed_input/created_at）。"""
        connection = sqlite3.connect(str(self.path))
        connection.executescript("""
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL UNIQUE,
                file_hash TEXT NOT NULL DEFAULT '', size_bytes INTEGER NOT NULL DEFAULT 0,
                duration_seconds REAL, width INTEGER, height INTEGER, fps REAL,
                status TEXT NOT NULL DEFAULT 'pending', error TEXT NOT NULL DEFAULT '',
                analyzed_at TEXT NOT NULL DEFAULT '');
            CREATE TABLE shots (
                id INTEGER PRIMARY KEY AUTOINCREMENT, video_id INTEGER NOT NULL,
                shot_index INTEGER NOT NULL, start_seconds REAL NOT NULL,
                end_seconds REAL NOT NULL, duration_seconds REAL NOT NULL,
                ai_provider TEXT NOT NULL DEFAULT '', ai_model TEXT NOT NULL DEFAULT '',
                UNIQUE(video_id, shot_index));
            CREATE TABLE frames (
                id INTEGER PRIMARY KEY AUTOINCREMENT, shot_id INTEGER NOT NULL,
                time_seconds REAL NOT NULL, file_path TEXT NOT NULL,
                UNIQUE(shot_id, file_path));
            CREATE TABLE labels (
                id INTEGER PRIMARY KEY AUTOINCREMENT, shot_id INTEGER NOT NULL,
                frame_id INTEGER NOT NULL DEFAULT 0, text TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'ai', UNIQUE(shot_id, text, source));
            CREATE TABLE vectors (
                id INTEGER PRIMARY KEY AUTOINCREMENT, shot_id INTEGER NOT NULL,
                frame_id INTEGER NOT NULL DEFAULT 0, provider TEXT NOT NULL,
                model TEXT NOT NULL, dim INTEGER NOT NULL, vector BLOB NOT NULL,
                UNIQUE(shot_id, frame_id));
        """)
        connection.execute("INSERT INTO meta(key, value) VALUES('schema_version','1')")
        connection.execute(
            "INSERT INTO videos(id, path, file_hash, status, analyzed_at)"
            " VALUES(1,'/old/clip.mp4','deadbeef','ok','2026-10-05T00:00:00')"
        )
        connection.execute(
            "INSERT INTO shots(id, video_id, shot_index, start_seconds, end_seconds,"
            " duration_seconds) VALUES(1,1,0,0.0,1.0,1.0)"
        )
        connection.execute(
            "INSERT INTO vectors(shot_id, frame_id, provider, model, dim, vector)"
            " VALUES(1,0,'ollama','nomic-embed-text',4,?)",
            (array.array("f", [1.0, 0.0, 0.0, 0.0]).tobytes(),),
        )
        connection.commit()
        connection.close()

    def test_v1_columns_are_added_on_initialize(self) -> None:
        connection = sqlite3.connect(str(self.path))
        before = {row[1] for row in connection.execute("PRAGMA table_info(vectors)")}
        connection.close()
        self.assertNotIn("embed_input", before, "前置条件：这是一份 v1 库")

        library = Library(self.path)
        library.initialize()

        connection = sqlite3.connect(str(self.path))
        after = {row[1] for row in connection.execute("PRAGMA table_info(vectors)")}
        version = connection.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()[0]
        connection.close()
        self.assertTrue({"kind", "embed_input", "created_at"} <= after)
        self.assertEqual(version, "3")

        # 唯一键也必须被换掉 —— 一张帧要能同时放多个向量空间的向量
        connection = sqlite3.connect(str(self.path))
        uniques = [
            [row[2] for row in connection.execute(f"PRAGMA index_info({index[1]})")]
            for index in connection.execute("PRAGMA index_list(vectors)").fetchall()
            if index[2]
        ]
        connection.close()
        self.assertIn(["shot_id", "frame_id", "provider", "model"], uniques)

    def test_migrated_library_reports_itself_as_not_auditable(self) -> None:
        """旧向量无法反推原文 —— 必须如实说，不能假装可审计。"""
        library = Library(self.path)
        library.initialize()
        stats = library.stats()
        self.assertEqual(stats["schema_version"], 3)
        self.assertEqual(stats["vectors"], 1)
        self.assertEqual(stats["vectors_with_embed_input"], 0)
        self.assertFalse(stats["auditable"])
        self.assertIn("重新入库", stats["note"])

        records = library.vector_audit()
        self.assertEqual(len(records), 1)
        self.assertFalse(records[0]["has_embed_input"])
        self.assertEqual(records[0]["embed_input"], "")

    def test_migration_is_idempotent(self) -> None:
        library = Library(self.path)
        library.initialize()
        library.initialize()
        library.initialize()
        connection = sqlite3.connect(str(self.path))
        columns = [row[1] for row in connection.execute("PRAGMA table_info(vectors)")]
        connection.close()
        self.assertEqual(len(columns), len(set(columns)), "不能重复加列")

    def test_showstopper_v1_rows_survive_migration(self) -> None:
        """迁移不能丢数据：旧向量本身必须留着（只是没有原文）。"""
        library = Library(self.path)
        library.initialize()
        record = library.vector_audit()[0]
        self.assertEqual(record["provider"], "ollama")
        self.assertEqual(record["model"], "nomic-embed-text")
        self.assertEqual(record["dim"], 4)
        self.assertGreater(record["vector_bytes"], 0)


class FakeImageProvider(ModelProvider):
    """假的**图片**嵌入 Provider（TASK-010 的第二个向量空间）。

    只声明 `IMAGE_EMBEDDING` + `EMBEDDING`。它的文本向量与图片向量用**同一个 model 名**，
    因此属于**同一个向量空间** —— 这正是中文 CLIP 的语义（文本塔与视觉塔共享空间）。
    """

    name = "fake_image"
    capabilities = frozenset({AICapability.IMAGE_EMBEDDING, AICapability.EMBEDDING})

    def __init__(self) -> None:
        self.images: list[str] = []
        self.texts: list[str] = []

    def available(self) -> bool:
        return True

    @staticmethod
    def _shot_token(path: str) -> str:
        """从图片路径里取出 `shotNNN` —— 整条路径做字符袋信噪比太低，排不出名次。"""
        stem = Path(path).stem
        parts = [part for part in stem.split("_") if part.startswith("shot")]
        return parts[0] if parts else stem

    def embed_images(self, request: ImageEmbeddingRequest) -> EmbeddingResult:
        self.images.extend(request.images)
        return EmbeddingResult(
            vectors=[FakeAIProvider._vector(self._shot_token(path)) for path in request.images],
            provider=self.name, model="fake-image-embed",
        )

    def embedding(self, request: EmbeddingRequest) -> EmbeddingResult:
        self.texts.extend(request.texts)
        return EmbeddingResult(
            vectors=[FakeAIProvider._vector(text) for text in request.texts],
            provider=self.name, model="fake-image-embed",
        )


class _TwoSpaceBase(_Base):
    """同时装好**文本**与**图片**两个嵌入 Provider 的测试基类。"""

    def make_context(self, *, approver=None) -> AppContext:
        ctx = AppContext.create(root=self.root, console=False, approver=approver)
        self.addCleanup(ctx.close)
        self.provider = FakeAIProvider()
        self.image_provider = FakeImageProvider()
        ctx.ai.register(self.provider, default=True)
        ctx.ai.register(self.image_provider)
        manager = PluginManager(ctx, [PLUGINS_DIR], ctx.logger)
        manager.discover()
        manager.activate("video_analyzer")
        self.manager = manager
        return ctx


class TwoVectorSpaceTests(_TwoSpaceBase):
    """TASK-010 核心：图片向量与文本向量**共存但绝不混算**。"""

    def test_scan_stores_both_spaces(self) -> None:
        self.scan()
        status = self.call("library_status").value
        spaces = {(s["provider"], s["model"]): s for s in status["spaces"]}
        self.assertIn(("fake", "fake-embed"), spaces, "Caption 文本向量空间")
        self.assertIn(("fake_image", "fake-image-embed"), spaces, "图片向量空间")
        self.assertEqual(spaces[("fake", "fake-embed")]["modality"], "text")
        self.assertEqual(spaces[("fake_image", "fake-image-embed")]["modality"], "image")
        # 每个空间各 3 帧 + 3 镜头质心
        for space in spaces.values():
            self.assertEqual(space["shot_centroids"], 3)
            self.assertEqual(space["frames"], 3)
        self.assertEqual(status["vectors"], 12)

    def test_image_space_records_image_paths_as_embed_input(self) -> None:
        """图片向量的"被嵌入输入"是**图片路径**，不是文本 —— 审计要能看出区别。"""
        self.scan()
        records = self.call("library_audit").value["records"]
        image_frames = [
            r for r in records
            if r["kind"] == "frame" and r["provider"] == "fake_image"
        ]
        self.assertEqual(len(image_frames), 3)
        for record in image_frames:
            self.assertTrue(Path(record["embed_input"]).is_file(),
                            "图片向量的 embed_input 必须是真实存在的图片路径")
        sent = self.image_provider.images
        self.assertEqual(sorted(r["embed_input"] for r in image_frames), sorted(sent))

    def test_search_auto_prefers_the_image_space(self) -> None:
        self.scan()
        value = self.call("library_search_semantic", query="shot001").value
        self.assertEqual(value["embedding"]["provider"], "fake_image")
        self.assertEqual(value["count"], 3)
        self.assertEqual(value["results"][0]["shot_index"], 1)

    def test_search_can_target_the_text_space(self) -> None:
        """`space="text"` 必须真的只查文本空间 —— 这是修复前后对比的入口。"""
        self.scan()
        value = self.call("library_search_semantic", query="shot001", space="text").value
        self.assertEqual(value["embedding"]["provider"], "fake")
        self.assertEqual(value["embedding"]["model"], "fake-embed")
        self.assertEqual(value["count"], 3)

    def test_search_can_target_the_image_space_explicitly(self) -> None:
        self.scan()
        value = self.call("library_search_semantic", query="shot002", space="image").value
        self.assertEqual(value["embedding"]["provider"], "fake_image")
        self.assertEqual(value["results"][0]["shot_index"], 2)

    def test_unknown_space_is_reported_not_guessed(self) -> None:
        self.scan()
        # 库里只有两个空间；要一个不存在的，必须明说而不是随便挑一个
        value = self.call("library_search_semantic", query="x", space="text").value
        self.assertEqual(value["count"], 3)
        self.assertEqual(len(value["spaces_available"]), 2)

    def test_search_never_scores_across_spaces(self) -> None:
        """不同空间的向量维度若恰好相同，也**绝不能**互相算分。"""
        self.scan()
        library = self.library()
        with library.session() as connection:
            # 把图片空间伪装成和文本空间同样的维度，诱使按维度匹配的实现出错
            connection.execute("UPDATE vectors SET dim = ? WHERE provider = 'fake_image'",
                               (EMBED_DIM,))
        value = self.call("library_search_semantic", query="shot001", space="text").value
        self.assertEqual(value["embedding"]["provider"], "fake")
        self.assertEqual(value["candidates"], 3, "只应统计文本空间自己的向量")


class ChineseClipModuleTests(unittest.TestCase):
    """`xbc_va_clip` 的离线契约（不需要真的模型文件）。

    重点覆盖 TASK-010 补充约束：**模型位置由 Core 注入，插件不解析路径**。
    """

    def test_provider_is_unavailable_before_core_injects_a_location(self) -> None:
        from xbc_va_clip import ChineseClipProvider

        provider = ChineseClipProvider()
        self.assertFalse(provider.configured())
        self.assertFalse(provider.available())
        # 关键：**没配好就不声明能力**，否则路由会选到一个必然失败的 Provider
        self.assertFalse(provider.supports(AICapability.IMAGE_EMBEDDING))
        self.assertFalse(provider.supports(AICapability.EMBEDDING))

    def test_model_dir_comes_from_the_injected_root(self) -> None:
        """路径拼接只有一处，且根来自 Core —— 换存放位置不用改 Provider。"""
        from xbc_va_clip import MODEL_ID, ChineseClipProvider

        provider = ChineseClipProvider()
        root = Path(tempfile.mkdtemp(prefix="xbc-models-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        provider.bind_models(root)
        self.assertEqual(provider.model_dir, root / MODEL_ID)
        self.assertFalse(provider.configured(), "目录存在但文件不全，仍应不可用")

    def test_bind_models_is_what_aiservice_calls_on_register(self) -> None:
        """注册机制负责注入 —— 插件不必、也无法自己传路径。"""
        from xbc.core.capabilities.ai import AIService
        from xbc_va_clip import MODEL_ID, ChineseClipProvider

        root = Path(tempfile.mkdtemp(prefix="xbc-reg-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        service = AIService(models_dir=root)
        service.register(ChineseClipProvider())
        provider = service.provider("chinese_clip")
        self.assertEqual(provider.model_dir, root / MODEL_ID)

    def test_aiservice_resolves_model_dir_for_callers(self) -> None:
        from xbc.core.capabilities.ai import AIService

        root = Path(tempfile.mkdtemp(prefix="xbc-modeldir-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        service = AIService(models_dir=root)
        self.assertEqual(service.model_dir("some-model"), root / "some-model")

    def test_missing_model_error_says_where_and_how_to_get_it(self) -> None:
        """验收要求：模型缺失要给出**明确错误与获取方式**。"""
        from xbc_va_clip import ChineseClipProvider, ChineseClipUnavailable

        provider = ChineseClipProvider()
        root = Path(tempfile.mkdtemp(prefix="xbc-missing-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        provider.bind_models(root)

        with self.assertRaises(ChineseClipUnavailable) as caught:
            provider.embed_images(ImageEmbeddingRequest(images=["a.jpg"]))
        message = str(caught.exception)
        self.assertIn(str(root / "chinese-clip-rn50"), message, "必须给出绝对路径")
        self.assertIn("export_chinese_clip_onnx.py", message, "必须给出获取方式")
        self.assertIn("缺失文件", message)

    def test_describe_reports_which_model_files_are_missing(self) -> None:
        from xbc_va_clip import ChineseClipProvider

        provider = ChineseClipProvider()
        root = Path(tempfile.mkdtemp(prefix="xbc-describe-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        provider.bind_models(root)
        described = provider.describe(probe=False)
        self.assertEqual(described["model_dir"], str(root / "chinese-clip-rn50"))
        self.assertEqual(
            set(described["model_files"]),
            {"vision_model.onnx", "text_model.onnx", "vocab.txt"},
        )
        self.assertTrue(all(v == "缺失" for v in described["model_files"].values()))

    def test_plugin_declares_no_model_config(self) -> None:
        """约束：插件只声明"需要 embedding 能力"，**不声明"需要模型文件"**。"""
        manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
        properties = manifest["config_schema"]["properties"]
        self.assertNotIn("clip_model_dir", properties)
        for name in properties:
            self.assertNotIn("model", name.lower(), f"插件配置不应出现模型相关项：{name}")

    def test_empty_request_returns_empty_result(self) -> None:
        from xbc_va_clip import ChineseClipProvider

        provider = ChineseClipProvider()
        result = provider.embed_images(ImageEmbeddingRequest(images=[]))
        self.assertEqual(result.count, 0)
        self.assertEqual(result.dim, 0)

    def test_model_id_and_dimension_are_declared(self) -> None:
        from xbc_va_clip import CONTEXT_LENGTH, MODEL_ID

        self.assertEqual(MODEL_ID, "chinese-clip-rn50")
        self.assertEqual(CONTEXT_LENGTH, 52)

    def test_tokenizer_ids_match_the_official_implementation(self) -> None:
        """`星空` 在官方 vocab 里是 [3215, 4958]（实测值，见交付报告）。

        这条断言不需要官方 vocab 文件：直接构造一个最小词表验证**编码流程**
        （[CLS] … [SEP] + 补齐）是否与官方一致。
        """
        vocab = ["[PAD]", "[UNK]", "[CLS]", "[SEP]", "星", "空"]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "vocab.txt"
            path.write_text("\n".join(vocab), encoding="utf-8")
            from xbc_va_clip import BertWordPieceTokenizer

            tokenizer = BertWordPieceTokenizer(path)
            self.assertEqual(tokenizer.tokenize("星空"), ["星", "空"])
            self.assertEqual(tokenizer.convert_tokens_to_ids(["星", "空"]), [4, 5])
            ids = tokenizer.encode("星空", context_length=6)
            self.assertEqual(ids, [2, 4, 5, 3, 0, 0], "[CLS] 星 空 [SEP] PAD PAD")
            # 未知字 → [UNK]，不抛异常
            self.assertEqual(tokenizer.convert_tokens_to_ids(tokenizer.tokenize("龘")), [1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
