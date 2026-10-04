"""视频分析器：FFmpeg 做结构分析，AI 能力层做画面理解。

## 它做什么

- **结构层（纯 FFmpeg）**：媒体信息、按画面变化切分镜头、抽取关键帧
- **理解层（走 `ctx.ai`）**：把关键帧交给 AI 能力层，逐镜头产出标签与画面结论

理解层**只调用 `ctx.ai.vision_analyze`**。本文件不 import 任何模型 SDK 或网络库，
也不知道底下是哪家模型、跑在哪个端口 —— 换 Provider 不需要改这里一个字节。

## 命令形态的来源

镜头切分与抽帧的命令形式参考 V18 原型中**已验证跑通**的实现
（V18 仅作功能参考，全程只读、未修改）：

- 镜头切分：`ffmpeg -i <v> -vf "select='gt(scene,T)',showinfo" -an -f null -`，
  再从 stderr 解析 `pts_time:` —— 对应 V18 的 `detect_scene_boundaries`
- 抽帧：`ffmpeg -ss <t> -i <v> -frames:v 1 -scale W:-2 -q:v 4 <out>` —— 对应 V18 的 `extract_thumbnail`

## 关于风险等级

五个工具都标 `read`。它们确实会写文件，但**只写插件自己的 `ctx.data_dir`**
（内核保证与其他插件、与用户文件隔离），且可随时删除。
按本项目对 `risk` 的定义（对**用户环境**的危险程度），这属于 read。
真正会改用户文件的工具才标 `write` / `destructive`。

## 不做的事（有意为之）

- **不做时间轴回退补齐**：FFmpeg 的 `scene` 检测给出的是近似切点，不追求帧级精度
- **不做转码 / 合成**：那是另一个插件的职责
- **不直接调模型**：理解层一律经 `ctx.ai`，插件侧不持有任何模型知识
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from xbc.core.capabilities.ai import (
    AICapability,
    AIError,
    EmbeddingRequest,
    ImageEmbeddingRequest,
)
from xbc.core.contract.plugin import XbcPlugin

# 模块名带 xbc_va_ 前缀，避免与其他插件撞名（插件共享同一个 sys.modules）
from xbc_va_clip import ChineseClipProvider
from xbc_va_library import Library, LibraryError, cosine  # noqa: E402
from xbc_va_match import (  # noqa: E402
    DEFAULT_MAX_CHARS as MATCH_DEFAULT_MAX_CHARS,
)
from xbc_va_match import (
    DEFAULT_MIN_CHARS as MATCH_DEFAULT_MIN_CHARS,
)
from xbc_va_match import (
    RESULT_VERSION,
    MatchError,
    MatchStore,
    refresh_snapshots,
    reorder_candidate,
    segment_at,
    segment_script,
    select_shot,
)

#: 每段默认给几个候选。3 是验收下限（"每段至少 3 个候选"），
#: 再多会稀释"帮你选好了几个"的体验 —— 人是在**少数几个**里挑，不是翻长列表。
DEFAULT_MATCH_TOP_N = 3

#: 从 ffmpeg showinfo 输出里取时间戳（V18 同款正则）
_SCENE_TIME_RE = re.compile(r"pts_time:([0-9]+(?:\.[0-9]+)?)")

#: 送给 AI 的定向提问。**由插件定义** —— 这是业务语义，不该出现在内核里。
FRAME_QUESTION = "这个画面的主要内容是什么？属于什么场景或拍摄类型？"

#: 一次标注最多分析多少帧（安全上限，避免把长视频送去跑几十次 AI）
MAX_ANNOTATED_FRAMES = 24

#: 素材库文件名与导出目录（都在插件自己的数据目录里）
LIBRARY_DB_NAME = "library.db"
EXPORT_DIR_NAME = "exports"
FRAME_DIR_NAME = "frames"

VIDEO_SUFFIXES = {
    ".mp4", ".mov", ".mkv", ".avi", ".webm", ".flv",
    ".m4v", ".ts", ".wmv", ".mpg", ".mpeg", ".rmvb",
}


def _hash_file(path: Path, chunk: int = 1024 * 1024) -> str:
    """按内容算 SHA-256。

    这里直接 `open()` 而不是走 `ctx.files`：**`ctx.files` 没有二进制读取或哈希能力**
    （只有 read_text / write_text / read_json / copy / size）。
    加一个哈希能力属于"新增 Core 能力"，本任务禁止 —— 所以记进报告的接口缺口，
    在插件内自行实现。
    """
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _timestamp() -> str:
    """入库时间戳（秒精度）。与存储层的 `Library.now()` 同一格式。"""
    return datetime.now().isoformat(timespec="seconds")


def _mean_vector(vectors: list[list[float]], dim: int) -> list[float]:
    """把一个镜头内多帧的向量求平均，作为镜头级向量。

    单帧时就是它自己。多帧时是一个粗糙的质心 —— 足以支撑"找相关镜头"，
    但不做任何归一化或加权（那是检索质量调优，超出本任务范围）。
    """
    usable = [v for v in vectors if len(v) == dim]
    if not usable:
        return []
    return [sum(v[i] for v in usable) / len(usable) for i in range(dim)]

#: 单次调用最多抽取多少帧（安全上限，避免在长视频上跑爆）
MAX_FRAMES_TOTAL = 240


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _parse_fps(value: Any) -> float | None:
    """把 ffprobe 的 "30000/1001" 形式转成浮点。"""
    if not isinstance(value, str) or not value:
        return None
    if "/" in value:
        numerator, _, denominator = value.partition("/")
        num, den = _to_float(numerator), _to_float(denominator)
        if num is None or not den:
            return None
        return round(num / den, 3)
    return _to_float(value)


class VideoAnalyzerPlugin(XbcPlugin):
    """视频分析插件。"""

    def __init__(self) -> None:
        super().__init__()
        self._cfg: dict[str, Any] = {}

    # ---------------- 生命周期 ----------------
    def on_load(self) -> None:
        self.log.info("on_load：视频分析器准备就绪")

    def apply(self, ctx: Any, config: dict) -> None:
        self._cfg = dict(config)

        # 把本地中文 CLIP 注册成 Core 的一个 AI Provider。
        #
        # **插件只声明"需要一个能编码图片的 Provider"，不提任何模型文件**：
        #   · 构造时不传路径 —— 插件不知道模型在哪，也不需要知道
        #   · Core 在 register() 时通过 `bind_models` 注入共享模型目录
        #   · Provider 用自己知道的 MODEL_ID 从那个目录往下解析
        # 业务代码只通过 `ctx.ai.embed_images()` / `ctx.ai.provider(...)` 用它。
        # Provider 是懒加载的：构造时不碰磁盘、不加载模型。
        ctx.ai.register(ChineseClipProvider())

        path_schema = {"type": "string", "minLength": 1}

        ctx.tools.register(
            "video_probe",
            self.video_probe,
            description="读取视频的基本信息：时长、分辨率、帧率、编码、码率、音视频轨",
            input_schema={
                "type": "object",
                "properties": {"path": path_schema},
                "required": ["path"],
            },
            output_schema={
                "type": "object",
                "required": [
                    "file", "duration_seconds", "format_name",
                    "has_video", "has_audio", "video", "audio",
                ],
                "properties": {
                    "file": {"type": "string"},
                    "file_name": {"type": "string"},
                    "size_bytes": {"type": ["integer", "null"]},
                    "duration_seconds": {"type": ["number", "null"]},
                    "format_name": {"type": "string"},
                    "bit_rate": {"type": ["integer", "null"]},
                    "has_video": {"type": "boolean"},
                    "has_audio": {"type": "boolean"},
                    "video": {"type": ["object", "null"]},
                    "audio": {"type": ["object", "null"]},
                },
            },
            risk="read",
        )

        ctx.tools.register(
            "video_split_shots",
            self.video_split_shots,
            description="按画面变化把视频切分成镜头，返回每个镜头的时间区间",
            input_schema={
                "type": "object",
                "properties": {
                    "path": path_schema,
                    "threshold": {"type": "number", "minimum": 0.05, "maximum": 0.95},
                    "min_shot_seconds": {"type": "number", "minimum": 0.1},
                },
                "required": ["path"],
            },
            output_schema={
                "type": "object",
                "required": ["file", "duration_seconds", "shot_count", "shots"],
                "properties": {
                    "file": {"type": "string"},
                    "duration_seconds": {"type": "number"},
                    "scene_threshold": {"type": "number"},
                    "min_shot_seconds": {"type": "number"},
                    "raw_cut_count": {"type": "integer"},
                    "shot_count": {"type": "integer"},
                    "shots": {"type": "array"},
                    "truncated": {"type": "boolean"},
                },
            },
            risk="read",
        )

        ctx.tools.register(
            "video_extract_keyframes",
            self.video_extract_keyframes,
            description="为每个镜头抽取关键帧并保存到插件数据目录，返回帧文件清单",
            input_schema={
                "type": "object",
                "properties": {
                    "path": path_schema,
                    "frames_per_shot": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                "required": ["path"],
            },
            output_schema={
                "type": "object",
                "required": ["file", "shot_count", "frame_count", "output_dir", "frames"],
                "properties": {
                    "file": {"type": "string"},
                    "output_dir": {"type": "string"},
                    "shot_count": {"type": "integer"},
                    "frame_count": {"type": "integer"},
                    "frames": {"type": "array"},
                },
            },
            risk="read",
        )

        ctx.tools.register(
            "video_annotate",
            self.video_annotate,
            description="关键帧 → AI 理解 → 标签：为每个镜头产出 AI 生成的标签与画面结论",
            input_schema={
                "type": "object",
                "properties": {
                    "path": path_schema,
                    "frames_per_shot": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 4,
                        "description": "每个镜头送去做 AI 理解的关键帧数量；留空用配置值",
                    },
                    "threshold": {"type": "number", "minimum": 0.05, "maximum": 0.95},
                },
                "required": ["path"],
            },
            output_schema={
                "type": "object",
                "required": [
                    "file", "question", "shot_count",
                    "frames_analyzed", "label_vocabulary", "shots", "ai",
                ],
                "properties": {
                    "file": {"type": "string"},
                    "question": {"type": "string"},
                    "duration_seconds": {"type": "number"},
                    "shot_count": {"type": "integer"},
                    "frames_analyzed": {"type": "integer"},
                    "label_vocabulary": {"type": "array"},
                    "shots": {"type": "array"},
                    "ai": {"type": "object"},
                    "errors": {"type": "array"},
                },
            },
            risk="read",
        )

        ctx.tools.register(
            "video_analyze",
            self.video_analyze,
            description="一次完成：媒体信息 + 镜头切分 + 关键帧抽取，返回完整结构化结果",
            input_schema={
                "type": "object",
                "properties": {
                    "path": path_schema,
                    "frames_per_shot": {"type": "integer", "minimum": 1, "maximum": 20},
                    "threshold": {"type": "number", "minimum": 0.05, "maximum": 0.95},
                },
                "required": ["path"],
            },
            output_schema={
                "type": "object",
                "required": ["media", "shots", "keyframes", "summary"],
                "properties": {
                    "media": {"type": "object"},
                    "shots": {"type": "object"},
                    "keyframes": {"type": "object"},
                    "summary": {"type": "object"},
                },
            },
            risk="read",
        )

        # ---------------- 素材库工具 ----------------
        # 输出结构较深，这里只声明"是对象" —— 不写会误导的强约束，
        # 但仍让内核校验输出确实是结构化对象。
        object_output = {"type": "object"}

        ctx.tools.register(
            "library_scan", self.library_scan,
            description="扫描目录，把新视频与内容变化的视频分析入库（已分析且内容哈希未变的跳过）",
            input_schema={
                "type": "object",
                "properties": {
                    "directory": {"type": "string", "minLength": 1},
                    "force": {"type": "boolean", "description": "即使哈希未变也重新分析"},
                },
                "required": ["directory"],
            },
            output_schema=object_output, risk="read",
        )
        ctx.tools.register(
            "library_retry", self.library_retry,
            description="重试库里状态为 failed / pending 的视频",
            input_schema={
                "type": "object",
                "properties": {"directory": {"type": "string"}},
            },
            output_schema=object_output, risk="read",
        )
        ctx.tools.register(
            "library_rebuild", self.library_rebuild,
            description="丢弃当前库并从视频文件重新建立（库损坏或丢失时用）",
            input_schema={
                "type": "object",
                "properties": {"directory": {"type": "string", "minLength": 1}},
                "required": ["directory"],
            },
            output_schema=object_output, risk="read",
        )
        ctx.tools.register(
            "library_status", self.library_status,
            description="查看素材库规模：视频数、镜头数、帧数、标签数、失败项",
            input_schema={"type": "object", "properties": {}},
            output_schema=object_output, risk="read",
        )
        ctx.tools.register(
            "library_audit", self.library_audit,
            description="审计库里每条向量的溯源：被嵌入的原始文本、provider/model、维度、产生时间、来源素材哈希",
            input_schema={
                "type": "object",
                "properties": {"shot_id": {"type": "integer", "minimum": 1}},
            },
            output_schema=object_output, risk="read",
        )
        ctx.tools.register(
            "library_search_labels", self.library_search_labels,
            description="按标签检索镜头（match=exact 精确 / fuzzy 模糊子串）",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1},
                    "match": {"type": "string", "enum": ["fuzzy", "exact"]},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
                "required": ["query"],
            },
            output_schema=object_output, risk="read",
        )
        ctx.tools.register(
            "library_search_semantic", self.library_search_semantic,
            description=(
                "语义检索：输入自然语言，返回最相关的镜头。"
                "默认走图片向量空间（中文 CLIP）；space 可显式指定 image / text 用于对比"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                    "space": {"type": "string", "enum": ["auto", "image", "text"]},
                },
                "required": ["query"],
            },
            output_schema=object_output, risk="read",
        )
        ctx.tools.register(
            "library_export", self.library_export,
            description="把选中的镜头导出成可再剪辑的视频片段（H.264/MP4，时间码精确）",
            input_schema={
                "type": "object",
                "properties": {
                    "shot_id": {"type": "integer", "minimum": 1},
                    "output": {"type": "string", "description": "输出路径；留空写到插件数据目录"},
                },
                "required": ["shot_id"],
            },
            output_schema=object_output,
            # 导出会在插件私有目录之外产生一个**用户要用的文件**，
            # 按本项目的 risk 定义这属于 write（需显式授权）。
            risk="write",
        )

        # ---------------- 文案 → 镜头匹配（TASK-011） ----------------
        ctx.tools.register(
            "script_match", self.script_match,
            description=(
                "输入一段文案，自动分段并为每段推荐 Top-N 候选镜头；"
                "结果落盘，可用 match_show / match_select / match_reorder 人工调整"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "minLength": 1, "maxLength": 64},
                    "script": {"type": "string", "minLength": 1},
                    "top_n": {"type": "integer", "minimum": 1, "maximum": 20},
                    "space": {"type": "string", "enum": ["auto", "image", "text"]},
                    "max_chars": {"type": "integer", "minimum": 4, "maximum": 200},
                    "min_chars": {"type": "integer", "minimum": 0, "maximum": 50},
                },
                "required": ["name", "script"],
            },
            output_schema=object_output, risk="read",
        )
        ctx.tools.register(
            "match_show", self.match_show,
            description="查看匹配结果（含每段选中的镜头与候选顺序）；不传 name 则列出已有结果",
            input_schema={
                "type": "object",
                "properties": {"name": {"type": "string"}},
            },
            output_schema=object_output, risk="read",
        )
        ctx.tools.register(
            "match_select", self.match_select,
            description="指定某一段用哪个镜头（替换候选 / 手动指定），改动存盘",
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "minLength": 1},
                    "segment_index": {"type": "integer", "minimum": 0},
                    "shot_id": {"type": "integer", "minimum": 1},
                },
                "required": ["name", "segment_index", "shot_id"],
            },
            output_schema=object_output, risk="read",
        )
        ctx.tools.register(
            "match_reorder", self.match_reorder,
            description="把某一段里的一个候选上移 / 下移一位（调整顺序），改动存盘",
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "minLength": 1},
                    "segment_index": {"type": "integer", "minimum": 0},
                    "shot_id": {"type": "integer", "minimum": 1},
                    "direction": {"type": "string", "enum": ["up", "down"]},
                },
                "required": ["name", "segment_index", "shot_id"],
            },
            output_schema=object_output, risk="read",
        )

        if not ctx.ffmpeg.available():
            self.log.warning(
                "未检测到 FFmpeg：video_analyzer 的工具会在调用时明确报错，"
                "请安装 FFmpeg 或配置 ffmpeg.ffmpeg_path"
            )
        self.log.info("apply：已注册 17 个视频工具 + chinese_clip Provider，配置 = %s", self._cfg)

    # ---------------- 工具实现 ----------------
    def video_probe(self, path: str) -> dict[str, Any]:
        """读取视频基本信息。"""
        return self._probe(self._require_video(path))

    def video_split_shots(
        self,
        path: str,
        threshold: float | None = None,
        min_shot_seconds: float | None = None,
    ) -> dict[str, Any]:
        """按画面变化切分镜头。"""
        source = self._require_video(path)
        duration = self._duration(source)

        scene_threshold = self._number(threshold, "scene_threshold", 0.3)
        min_shot = self._number(min_shot_seconds, "min_shot_seconds", 0.5)
        max_shots = int(self._number(None, "max_shots", 200))

        cuts = self._scene_times(source, scene_threshold)
        shots = self._build_shots(duration, cuts, min_shot, max_shots)

        return {
            "file": str(source),
            "duration_seconds": round(duration, 3),
            "scene_threshold": scene_threshold,
            "min_shot_seconds": min_shot,
            "raw_cut_count": len(cuts),
            "shot_count": len(shots),
            "shots": shots,
            "truncated": len(shots) >= max_shots,
        }

    def video_extract_keyframes(
        self,
        path: str,
        frames_per_shot: int | None = None,
    ) -> dict[str, Any]:
        """为每个镜头抽取关键帧。"""
        source = self._require_video(path)
        duration = self._duration(source)
        per_shot = int(frames_per_shot or self._number(None, "keyframes_per_shot", 3))
        per_shot = max(1, min(20, per_shot))

        cuts = self._scene_times(source, self._number(None, "scene_threshold", 0.3))
        shots = self._build_shots(
            duration, cuts, self._number(None, "min_shot_seconds", 0.5),
            int(self._number(None, "max_shots", 200)),
        )
        return self._extract_frames(source, shots, per_shot)

    def video_annotate(
        self,
        path: str,
        frames_per_shot: int | None = None,
        threshold: float | None = None,
    ) -> dict[str, Any]:
        """关键帧 → AI 理解 → 标签。

        链路：FFmpeg 切分镜头 + 抽取关键帧（都在本插件内完成）
        → 逐帧 `ctx.ai.vision_analyze(..., question=FRAME_QUESTION)`
        → 汇总成每个镜头的答案与标签。

        标签全部来自 AI 返回，插件不做任何硬编码映射。
        """
        source = self._require_video(path)
        duration = self._duration(source)

        per_shot = int(frames_per_shot or self._number(None, "annotate_frames_per_shot", 1))
        per_shot = max(1, min(4, per_shot))

        scene_threshold = self._number(threshold, "scene_threshold", 0.3)
        shots = self._build_shots(
            duration,
            self._scene_times(source, scene_threshold),
            self._number(None, "min_shot_seconds", 0.5),
            int(self._number(None, "max_shots", 200)),
        )
        extracted = self._extract_frames(source, shots, per_shot)
        return self._annotate_frames(source, duration, shots, extracted["frames"])

    def _annotate_frames(
        self,
        source: Path,
        duration: float,
        shots: list[dict[str, Any]],
        frames: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """把抽出来的关键帧逐帧交给 AI，再按镜头汇总。

        逐帧失败**不让整轮失败**：记进 `errors` 继续跑完，这样一段坏帧不会
        让整支视频的分析白做。但**一帧都没成功**时明确抛错 —— 返回一堆空标签
        比失败更糟，调用方会以为"AI 说这个视频没有内容"。
        """
        by_shot: dict[int, list[dict[str, Any]]] = {}
        for frame in frames:
            by_shot.setdefault(frame["shot_index"], []).append(frame)

        annotated: list[dict[str, Any]] = []
        vocabulary: list[str] = []
        errors: list[dict[str, str]] = []
        analyzed = 0
        ai_info: dict[str, Any] = {}

        for shot in shots:
            answers: list[str] = []
            labels: list[str] = []
            counts: dict[str, int] = {}
            shot_errors: list[str] = []

            for frame in by_shot.get(shot["index"], []):
                if analyzed >= MAX_ANNOTATED_FRAMES:
                    shot_errors.append(
                        f"已达本次分析上限（{MAX_ANNOTATED_FRAMES} 帧），跳过 {frame['file']}"
                    )
                    continue
                try:
                    result = self.ctx.ai.vision_analyze(
                        [frame["file"]], question=FRAME_QUESTION
                    )
                except AIError as exc:
                    message = str(exc)
                    errors.append({"frame": frame["file"], "error": message})
                    shot_errors.append(message)
                    continue

                analyzed += 1
                ai_info = {"provider": result.provider, "model": result.model}
                answers.append(result.answer)
                for label in result.labels:
                    counts[label] = counts.get(label, 0) + 1
                    if label not in labels:
                        labels.append(label)
                    if label not in vocabulary:
                        vocabulary.append(label)

            annotated.append(
                {
                    "index": shot["index"],
                    "start": shot["start"],
                    "end": shot["end"],
                    "duration": shot["duration"],
                    "frames_analyzed": len(answers),
                    "answers": answers,
                    "labels": labels,
                    "label_counts": counts,
                    "errors": shot_errors,
                }
            )

        if analyzed == 0 and errors:
            raise AIError(
                f"AI 理解未产出任何结果（{len(errors)} 帧全部失败）。"
                f"首条错误：{errors[0]['error']}"
            )

        return {
            "file": str(source),
            "question": FRAME_QUESTION,
            "duration_seconds": round(duration, 3),
            "shot_count": len(shots),
            "frames_analyzed": analyzed,
            "label_vocabulary": vocabulary,
            "shots": annotated,
            "ai": ai_info,
            "errors": errors,
        }

    def video_analyze(
        self,
        path: str,
        frames_per_shot: int | None = None,
        threshold: float | None = None,
    ) -> dict[str, Any]:
        """一次产出完整结构：媒体信息 + 镜头 + 关键帧。"""
        source = self._require_video(path)
        media = self._probe(source)

        duration = self._duration(source)
        scene_threshold = self._number(threshold, "scene_threshold", 0.3)
        per_shot = int(frames_per_shot or self._number(None, "keyframes_per_shot", 3))
        per_shot = max(1, min(20, per_shot))

        cuts = self._scene_times(source, scene_threshold)
        shots = self._build_shots(
            duration, cuts, self._number(None, "min_shot_seconds", 0.5),
            int(self._number(None, "max_shots", 200)),
        )
        keyframes = self._extract_frames(source, shots, per_shot)

        return {
            "media": media,
            "shots": {
                "scene_threshold": scene_threshold,
                "raw_cut_count": len(cuts),
                "shot_count": len(shots),
                "shots": shots,
                "truncated": len(shots) >= int(self._number(None, "max_shots", 200)),
            },
            "keyframes": {
                "output_dir": keyframes["output_dir"],
                "frame_count": keyframes["frame_count"],
                "frames": keyframes["frames"],
            },
            "summary": {
                "file": str(source),
                "duration_seconds": round(duration, 3),
                "shot_count": len(shots),
                "frame_count": keyframes["frame_count"],
                "has_audio": bool(media.get("has_audio")),
                "resolution": (
                    f"{media['video']['width']}x{media['video']['height']}"
                    if media.get("video") else None
                ),
            },
        }

    # ---------------- 素材库：扫描 / 分析 ----------------
    def _library(self) -> Library:
        library = Library(self.ctx.data_dir / LIBRARY_DB_NAME)
        library.initialize()
        return library

    def _scan_root(self, directory: str) -> Path:
        root = Path(str(directory)).expanduser()
        if not root.is_dir():
            raise ValueError(f"目录不存在或不是目录：{root}")
        return root

    def _video_files(self, root: Path) -> list[Path]:
        return sorted(
            path for path in root.rglob("*")
            if path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES
        )

    @staticmethod
    def _skip_reason(existing: Any, digest: str, force: bool) -> str:
        """判断这个视频能否跳过。**只看内容哈希与状态**，不看路径或修改时间。"""
        if force or existing is None:
            return ""
        if existing["file_hash"] != digest:
            return ""
        if existing["status"] != "ok":
            return ""
        return f"内容哈希未变（{digest[:16]}）且状态为 ok"

    def _image_capability_status(self) -> dict[str, Any]:
        """图片嵌入这条路现在能不能走。

        **问 Core 的路由**，而不是问插件自己注册的那个 Provider ——
        否则"谁提供能力"的知识就被写死在插件里了。
        """
        try:
            provider = self.ctx.ai.provider(capability=AICapability.IMAGE_EMBEDDING)
        except AIError as exc:
            return {
                "available": False,
                "reason": str(exc),
                "hint": (
                    "图片向量这条路停用了；标签检索与文本向量照常工作。"
                    "要启用：python scripts/export_chinese_clip_onnx.py"
                    f' --out "{self.ctx.ai.model_dir("chinese-clip-rn50")}"'
                ),
            }
        described = provider.describe(probe=False)
        return {
            "available": True,
            "provider": provider.name,
            "model_dir": described.get("model_dir", ""),
            "model_files": described.get("model_files", {}),
        }

    def library_status(self) -> dict[str, Any]:
        library = self._library()
        stats = library.stats()
        stats["spaces"] = self._library_spaces()
        stats["image_embedding"] = self._image_capability_status()
        stats["not_ok"] = [
            {"path": row["path"], "status": row["status"], "error": row["error"]}
            for row in library.videos() if row["status"] != "ok"
        ]
        return stats

    def _library_spaces(self) -> list[dict[str, Any]]:
        """库里的向量空间，并标注**模态**（image / text）。

        模态由 **Core 的能力声明**推导 —— 哪个 Provider 声明了
        `AICapability.IMAGE_EMBEDDING`，它产出的向量就是图片向量。
        **插件里不硬编码任何厂商名**，换 Provider 也不用改这里。
        """
        try:
            image_provider = self.ctx.ai.provider(
                capability=AICapability.IMAGE_EMBEDDING
            ).name
        except AIError:
            image_provider = ""
        annotated = []
        for space in self._library().spaces():
            item = dict(space)
            item["modality"] = "image" if space["provider"] == image_provider else "text"
            annotated.append(item)
        return annotated

    def library_audit(self, shot_id: int | None = None) -> dict[str, Any]:
        """审计：每条向量到底是用什么输入、哪个 provider/model、什么时候算出来的。

        TASK-010 第一部分的核心入口 —— v1 只存 `labels`，库里看不到真正被嵌入的文本，
        检索行为无法解释。现在一次调用就能回答"这条向量从哪来"。

        - `kind='frame'`：`embed_input` 就是**被嵌入的那一项原文**
          （文本向量是原文本，图片向量是图片路径）
        - `kind='shot_centroid'`：该向量是**多帧质心**，`embed_input` 是参与平均的各项，
          不是直接嵌入的单个输入
        """
        library = self._library()
        records = library.vector_audit(shot_id)
        missing = [item for item in records if not item["has_embed_input"]]
        stats = library.stats()
        return {
            "library_path": stats["library_path"],
            "schema_version": stats["schema_version"],
            "vectors_total": len(records),
            "vectors_auditable": len(records) - len(missing),
            "auditable": not missing,
            "missing_embed_input": [item["vector_id"] for item in missing],
            "spaces": self._library_spaces(),
            "note": stats["note"] or "所有向量都记录了被嵌入的输入，可审计。",
            "records": records,
        }

    def library_scan(self, directory: str, force: bool = False) -> dict[str, Any]:
        """扫描目录并把视频分析入库。

        - 已分析且**内容哈希未变**的跳过（不按路径、不按修改时间）
        - 内容变了就重新分析（`upsert_video` 会级联清掉旧结果）
        - **单个视频失败不阻塞整体**：记进 `failed` 并把状态置为 failed，可重试
        """
        root = self._scan_root(directory)
        library = self._library()
        candidates = self._video_files(root)

        report: dict[str, Any] = {
            "directory": str(root), "found": len(candidates), "force": bool(force),
            "analyzed": [], "skipped": [], "failed": [],
        }

        for path in candidates:
            digest = _hash_file(path)
            existing = library.get_video_by_path(str(path))
            reason = self._skip_reason(existing, digest, force)
            if reason:
                report["skipped"].append(
                    {"path": str(path), "hash": digest[:16], "reason": reason}
                )
                continue
            try:
                report["analyzed"].append(self._analyze_into_library(library, path, digest))
            except Exception as exc:  # noqa: BLE001 - 一个坏文件不能毁掉整次扫描
                # 失败也要**留下记录**，否则 library_retry 找不到它、永远重试不了。
                # 早期实现在这里只对"已存在的记录"置失败，导致连探信息都失败的坏文件
                # 直接消失在库里 —— 用户看到 failed=1 却找不到是哪一条。
                record = library.get_video_by_path(str(path))
                video_id = (
                    int(record["id"]) if record is not None
                    else library.upsert_video(
                        path=str(path), file_hash=digest,
                        size_bytes=path.stat().st_size, status="failed",
                    )
                )
                library.set_status(video_id, "failed", str(exc))
                self.log.error("分析失败 %s：%s", path, exc)
                report["failed"].append({"path": str(path), "error": str(exc)})

        report["summary"] = {
            "analyzed": len(report["analyzed"]),
            "skipped": len(report["skipped"]),
            "failed": len(report["failed"]),
            "library": library.stats(),
        }
        return report

    def library_retry(self, directory: str | None = None) -> dict[str, Any]:
        """重试未成功的视频（failed 与 pending 都算）。

        `pending` 是"分析中途断了"留下的状态 —— 所以扫描被中断后不需要重新扫全量，
        直接 retry 就能接着跑。
        """
        library = self._library()
        root = Path(str(directory)).expanduser() if directory else None
        pending = [row for row in library.videos() if row["status"] != "ok"]
        if root is not None:
            pending = [
                row for row in pending
                if Path(row["path"]).is_relative_to(root)
            ]

        report: dict[str, Any] = {
            "candidates": len(pending), "analyzed": [], "failed": [], "missing": [],
        }
        for row in pending:
            path = Path(row["path"])
            if not path.is_file():
                library.set_status(int(row["id"]), "failed", "文件不存在，无法重试")
                report["missing"].append(str(path))
                continue
            try:
                report["analyzed"].append(
                    self._analyze_into_library(library, path, _hash_file(path))
                )
            except Exception as exc:  # noqa: BLE001 - 继续重试下一个
                library.set_status(int(row["id"]), "failed", str(exc))
                self.log.error("重试失败 %s：%s", path, exc)
                report["failed"].append({"path": str(path), "error": str(exc)})

        report["summary"] = {
            "analyzed": len(report["analyzed"]),
            "failed": len(report["failed"]),
            "missing": len(report["missing"]),
            "library": library.stats(),
        }
        return report

    def library_rebuild(self, directory: str) -> dict[str, Any]:
        """整库丢弃后重建。

        库不是唯一副本：视频文件还在，分析结果可以全部重算。

        **不经过 `_library()`**：那个方法会先 `initialize()`，而库文件损坏时
        恰恰打不开（`file is not a database`）—— 那样重建就永远救不回来。
        这里的顺序是"先尽力读一下旧统计（失败也无所谓）→ 删文件 → 建新库"。
        """
        root = self._scan_root(directory)
        library = Library(self.ctx.data_dir / LIBRARY_DB_NAME)

        try:
            before = library.stats()
        except Exception as exc:  # noqa: BLE001 - 库已经坏了，读不到统计很正常
            self.log.warning("重建前读不出旧库统计（按已损坏处理）：%s", exc)
            before = {"videos_total": None, "shots": None, "labels": None}

        library.drop()
        library.initialize()
        self.log.info("素材库已丢弃并重建：%s", library.path)

        report = self.library_scan(str(root), force=True)
        report["rebuilt"] = {
            "library_path": str(library.path),
            "dropped": {
                "videos": before["videos_total"],
                "shots": before["shots"],
                "labels": before["labels"],
            },
        }
        return report

    def _analyze_into_library(
        self, library: Library, source: Path, digest: str
    ) -> dict[str, Any]:
        """把一个视频完整分析入库：探信息 → 切镜头 → 抽帧 → AI 理解 → 向量化。

        向量化的是**镜头画面的文字描述**（AI 的 answer + labels），不是图片本身 ——
        `ctx.ai.embedding` 收的是文本。所以语义检索实际匹配的是
        「查询文本 ↔ AI 画面描述文本」。这一点在报告里明确写了。
        """
        media = self._probe(source)
        duration = self._duration(source)
        video_meta = media.get("video") or {}

        video_id = library.upsert_video(
            path=str(source), file_hash=digest,
            size_bytes=int(media.get("size_bytes") or 0),
            duration_seconds=media.get("duration_seconds"),
            width=video_meta.get("width"), height=video_meta.get("height"),
            fps=video_meta.get("fps"), status="pending",
        )
        library.set_status(video_id, "pending")

        per_shot = max(1, min(4, int(self._number(None, "annotate_frames_per_shot", 1))))
        shots = self._build_shots(
            duration,
            self._scene_times(source, self._number(None, "scene_threshold", 0.3)),
            self._number(None, "min_shot_seconds", 0.5),
            int(self._number(None, "max_shots", 200)),
        )
        extracted = self._extract_frames(source, shots, per_shot)

        by_shot: dict[int, list[dict[str, Any]]] = {}
        for frame in extracted["frames"]:
            by_shot.setdefault(frame["shot_index"], []).append(frame)

        payload: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        analyzed = 0
        provider = model = ""
        # 图片向量这条路要不要走，**问 Core 的路由**（有没有任何 Provider 支持图片嵌入），
        # 而不是问插件自己注册的那个 —— 那样会把"谁提供能力"的知识写死在插件里。
        try:
            self.ctx.ai.provider(capability=AICapability.IMAGE_EMBEDDING)
            clip_ready = True
        except AIError:
            clip_ready = False

        for shot in shots:
            frame_records: list[dict[str, Any]] = []
            frame_vectors: list[dict[str, Any]] = []
            clip_vectors: list[dict[str, Any]] = []
            labels: list[str] = []

            for frame in by_shot.get(shot["index"], []):
                if analyzed >= MAX_ANNOTATED_FRAMES:
                    break
                try:
                    annotated = self.ctx.ai.vision_analyze(
                        [frame["file"]], question=FRAME_QUESTION
                    )
                except AIError as exc:
                    errors.append({"frame": frame["file"], "error": str(exc)})
                    continue

                try:
                    # **这个字符串就是被嵌入的东西**，必须落库（TASK-010 第一部分）。
                    # v1 只存了 labels，导致库里看不到真正被嵌入的文本、检索行为无法审计。
                    text_for_embedding = " ".join(
                        [annotated.answer, *annotated.labels]
                    ).strip()
                    embedded = self.ctx.ai.embedding([text_for_embedding])
                except AIError as exc:
                    errors.append({"frame": frame["file"], "error": str(exc)})
                    embedded = None
                    text_for_embedding = ""

                # 中文 CLIP **图片**向量（与上面的文本向量是**两个不同的向量空间**）。
                # 走 Core 的图片嵌入能力 —— 业务代码不碰推理运行时
                clip_embedded = None
                if clip_ready:
                    try:
                        clip_embedded = self.ctx.ai.embed_images([frame["file"]])
                    except AIError as exc:
                        errors.append(
                            {"frame": frame["file"], "error": f"中文 CLIP：{exc}"}
                        )

                provider, model = annotated.provider, annotated.model
                analyzed += 1
                for label in annotated.labels:
                    if label not in labels:
                        labels.append(label)

                frame_records.append({"time": frame["time"], "file": frame["file"]})
                index = len(frame_records) - 1
                if embedded is not None and embedded.vectors:
                    frame_vectors.append({
                        "frame_index": index,
                        "provider": embedded.provider, "model": embedded.model,
                        "dim": embedded.dim, "values": embedded.vectors[0],
                        "embed_input": text_for_embedding,
                        "created_at": _timestamp(),
                    })
                if clip_embedded is not None and clip_embedded.vectors:
                    clip_vectors.append({
                        "frame_index": index,
                        "provider": clip_embedded.provider, "model": clip_embedded.model,
                        "dim": clip_embedded.dim, "values": clip_embedded.vectors[0],
                        # 图片向量的"被嵌入输入"就是图片路径本身
                        "embed_input": frame["file"],
                        "created_at": _timestamp(),
                    })

            def _centroid(items: list[dict[str, Any]]) -> dict[str, Any] | None:
                if not items:
                    return None
                values = _mean_vector([item["values"] for item in items], items[0]["dim"])
                if not values:
                    return None
                return {
                    "provider": items[0]["provider"], "model": items[0]["model"],
                    "dim": items[0]["dim"], "values": values,
                    # 镜头级是**质心**，不是直接嵌入某个输入。记下参与平均的各项，
                    # 配合 kind='shot_centroid' 说明它不代表"被嵌入的原文"。
                    "embed_input": " | ".join(item["embed_input"] for item in items),
                    "created_at": _timestamp(),
                }

            payload.append({
                "index": shot["index"], "start": shot["start"], "end": shot["end"],
                "duration": shot["duration"], "frames": frame_records,
                "labels": labels, "frame_vectors": frame_vectors, "vector": _centroid(frame_vectors),
                "clip_frame_vectors": clip_vectors, "clip_vector": _centroid(clip_vectors),
            })

        if analyzed == 0:
            first = errors[0]["error"] if errors else "没有可用关键帧"
            raise AIError(f"这个视频一帧都没分析成功，未入库：{first}")

        library.replace_analysis(
            video_id, shots=payload, ai_provider=provider, ai_model=model
        )
        library.set_status(video_id, "ok")
        self.log.info("已入库 %s：%d 个镜头 / %d 帧", source.name, len(payload), analyzed)

        return {
            "path": str(source), "hash": digest[:16],
            "duration_seconds": round(duration, 3),
            "shots": len(payload), "frames_analyzed": analyzed,
            "labels": sorted({label for shot in payload for label in shot["labels"]}),
            "ai": {"provider": provider, "model": model},
            "errors": len(errors),
        }

    # ---------------- 素材库：检索 ----------------
    def library_search_labels(
        self, query: str, match: str = "fuzzy", limit: int = 10
    ) -> dict[str, Any]:
        """标签检索。**只是字符串匹配** —— 需要"同义但不同字"时必须用语义检索。"""
        library = self._library()
        mode = match if match in ("exact", "fuzzy") else "fuzzy"
        results = library.search_labels(query, limit=int(limit), match=mode)
        return {
            "query": query, "mode": f"labels/{mode}",
            "count": len(results), "results": results,
        }

    def library_search_semantic(
        self, query: str, limit: int = 10, space: str = "auto"
    ) -> dict[str, Any]:
        """语义检索：把查询文本向量化，与库里的镜头向量比余弦相似度。

        ## 为什么必须**先选向量空间**

        TASK-010 之后库里可能同时有两种向量：**中文 CLIP 图片向量**（1024 维）
        与 **Caption 文本向量**（768 维）。它们来自不同模型、属于不同向量空间，
        **余弦相似度跨空间没有意义** —— 所以这里先按 `(provider, model)` 选定一个空间，
        再用**那个空间自己的 Provider**把查询编码成同空间向量。

        `space`（按**模态**，不按厂商）：
        - `"auto"`（默认）优先图片向量空间 —— 实测 top-1 10/10，
          优于 Caption 文本空间的 2/10
        - `"image"` / `"text"` 显式指定，用于对比与复现

        **不做多路融合**：TASK-010 实测等权 RRF 会把 top-1 从 10/10 拉到 8/10
        （图片路已满分、文本路只 2/10，等权融合纯拖后腿），所以走单路。
        """
        context = self._retrieval_context(space)
        if "error" in context:
            return {
                "query": query, "mode": "semantic", "count": 0, "results": [],
                "spaces": context.get("available", []), "note": context["error"],
            }

        scored = self._score_query(context, query, limit)
        return {
            "query": query, "mode": "semantic", "space": space,
            "count": len(scored["results"]),
            "embedding": scored["embedding"],
            "spaces_available": context["available"],
            "candidates": len(context["stored"]),
            "skipped_other_space": scored["skipped_other_space"],
            "results": scored["results"],
        }

    # ---------------- 检索的唯一实现（检索工具与文案匹配共用） ----------------
    def _retrieval_context(self, space: str) -> dict[str, Any]:
        """检索的**准备阶段**：解析向量空间 → 取镜头向量 → 解析该空间的 Provider。

        **准备阶段只做一次，可以服务多次打分** —— 文案匹配有 N 段文案，
        但向量空间与镜头向量对整批文案是同一份，不该每段重读一次库。

        不可用时返回带 `error` 的字典，由调用方决定怎么呈现（检索返回空结果、
        匹配则整批失败）。
        """
        library = self._library()
        available = self._library_spaces()
        if not available:
            return {"error": "库里还没有向量：先跑 library_scan 入库", "available": []}

        chosen = self._pick_space(available, space)
        if chosen is None:
            return {
                "error": f"库里没有 {space!r} 空间的向量；现有空间：{available}",
                "available": available,
            }

        stored = library.shot_vectors(provider=chosen["provider"], model=chosen["model"])
        if not stored:
            return {
                "error": f"空间 {chosen['provider']}/{chosen['model']} 里没有镜头级向量",
                "available": available,
            }

        try:
            owner = self.ctx.ai.provider(
                chosen["provider"], capability=AICapability.EMBEDDING
            )
        except AIError as exc:
            return {
                "error": f"空间 {chosen['provider']}/{chosen['model']} 没有可用的 Provider：{exc}",
                "available": available,
            }

        return {
            "library": library, "chosen": chosen, "stored": stored,
            "owner": owner, "available": available,
        }

    @staticmethod
    def _score_query(
        context: dict[str, Any], query: str, limit: int
    ) -> dict[str, Any]:
        """检索的**唯一打分实现**：编码查询 → 同空间过滤 → 余弦排序 → 带标签返回。

        **这段代码只有一份。** `library_search_semantic`（检索工具）与
        `script_match`（文案匹配）都调它 —— 匹配不重新实现任何检索逻辑。
        """
        chosen = context["chosen"]
        stored = context["stored"]

        # 查询必须由**该空间自己的 Provider** 编码，才能和库里的向量同空间
        embedded = context["owner"].embedding(EmbeddingRequest(texts=[query]))
        query_vector = embedded.vectors[0]

        usable = [
            item for item in stored
            if item["model"] == embedded.model and item["dim"] == len(query_vector)
        ]
        skipped = len(stored) - len(usable)

        scored = sorted(
            ({"score": round(cosine(query_vector, item["values"]), 4), **item}
             for item in usable),
            key=lambda row: row["score"], reverse=True,
        )[: max(1, int(limit))]
        labels = context["library"].shot_labels([row["shot_id"] for row in scored])

        return {
            "embedding": {
                "provider": embedded.provider, "model": embedded.model,
                "dim": len(query_vector),
            },
            "space": {"provider": chosen["provider"], "model": chosen["model"]},
            "skipped_other_space": skipped,
            "results": [
                {
                    "score": row["score"],
                    # **持久身份**：存下来的引用必须用它，不能用 shot_id
                    # （shot_id 重新分析后就会变，见 xbc_va_library.shot_key）
                    "shot_key": row["shot_key"],
                    "video_path": row["video_path"],
                    "shot_id": row["shot_id"], "shot_index": row["shot_index"],
                    "start": round(row["start"], 3), "end": round(row["end"], 3),
                    "duration": round(row["duration"], 3),
                    "labels": labels.get(row["shot_id"], []),
                }
                for row in scored
            ],
        }

    @staticmethod
    def _pick_space(spaces: list[dict[str, Any]], want: str) -> dict[str, Any] | None:
        """按**模态**挑一个向量空间：`image`（图片）/ `text`（Caption 文本）。"""
        if want == "image":
            return next((s for s in spaces if s["modality"] == "image"), None)
        if want == "text":
            return next((s for s in spaces if s["modality"] == "text"), None)
        # auto：优先图片向量（实测质量更高），没有就退回任意一个
        return (
            next((s for s in spaces if s["modality"] == "image"), None)
            or spaces[0]
        )

    # ---------------- 文案 → 镜头匹配 ----------------
    def _match_store(self) -> MatchStore:
        return MatchStore(self.ctx.data_dir)

    def script_match(
        self,
        name: str,
        script: str,
        top_n: int | None = None,
        space: str = "auto",
        max_chars: int | None = None,
        min_chars: int | None = None,
    ) -> dict[str, Any]:
        """把一段文案切成段，每段推荐 **Top-N 候选镜头**，结果落盘可人工调整。

        ## V1 只推荐，不替人决定

        返回的每段都带 `candidates`（按相似度降序）与 `selected_shot_id`，
        默认选中 top-1 —— **但那是"默认"，不是"决定"**：人可以用
        `match_select` 换成任意镜头、用 `match_reorder` 调顺序。

        ## 复用了什么

        分段与存取在 `xbc_va_match`；**检索一行都没有重写** ——
        每段都调 TASK-010 的 `_score_query`（与 `library_search_semantic` 同一份实现）。
        准备阶段 `_retrieval_context` **只做一次**：向量空间与镜头向量对整批文案是同一份。
        """
        store = self._match_store()
        store.validate_name(name)

        segments = segment_script(
            script,
            max_chars=int(
                max_chars or self._number(None, "match_max_chars", MATCH_DEFAULT_MAX_CHARS)
            ),
            min_chars=int(
                min_chars or self._number(None, "match_min_chars", MATCH_DEFAULT_MIN_CHARS)
            ),
        )
        if not segments:
            raise ValueError("文案为空或全是空白，没有可匹配的段")

        count = int(top_n or self._number(None, "match_top_n", DEFAULT_MATCH_TOP_N))
        count = max(1, min(20, count))

        context = self._retrieval_context(space)
        if "error" in context:
            raise ValueError(f"无法匹配：{context['error']}")

        matched: list[dict[str, Any]] = []
        for index, text in enumerate(segments):
            scored = self._score_query(context, text, count)
            candidates = scored["results"]
            matched.append({
                "index": index,
                "text": text,
                # **身份是 shot_key**：重新分析素材库后 shot_id 会变，shot_key 不会。
                # shot_id 只作为"写入时的快照"，读取时会被重新解析（见 match_show）。
                "selected_shot_key": candidates[0]["shot_key"] if candidates else None,
                "selected_shot_id": candidates[0]["shot_id"] if candidates else None,
                "candidates": candidates,
            })

        chosen = context["chosen"]
        payload: dict[str, Any] = {
            "version": RESULT_VERSION,
            "name": name,
            "script": script,
            "top_n": count,
            "space": {
                "provider": chosen["provider"], "model": chosen["model"],
                "dim": chosen["dim"], "modality": chosen.get("modality", ""),
            },
            "library_shots": len(context["stored"]),
            "segments": matched,
        }
        path = store.save(payload)
        self.log.info("文案匹配完成：%s（%d 段 × %d 候选）", name, len(matched), count)

        return {
            "name": payload["name"],
            "path": str(path),
            "script_chars": len(script),
            "segments": len(matched),
            "top_n": count,
            "space": payload["space"],
            "library_shots": payload["library_shots"],
            "results": matched,
            "hint": (
                "每段默认选中 top-1；用 match_select 换成别的镜头、"
                "match_reorder 调候选顺序，改动会存进同一份 JSON。"
                "身份是 shot_key（重新分析素材库后依然指向同一个镜头）。"
            ),
        }

    def _match_resolver(self) -> Any:
        """给 `refresh_snapshots` 用的解析器：`shot_key` → 当前库里的镜头。"""
        library = self._library()
        keyed = library.shots_by_key  # 绑定一次，逐条解析时不再查属性

        def resolve(key: str, prefer_path: str) -> dict[str, Any] | None:
            matches = keyed(key, prefer_path=prefer_path)
            return matches[0] if matches else None

        return resolve

    def _resolve_current(
        self, payload: dict[str, Any], segment_index: int, shot_id: int
    ) -> tuple[str, dict[str, Any] | None]:
        """把一个**当前**的 `shot_id` 换成 `shot_key`。

        工具签名保持用 `shot_id`（调用方从检索/状态里现取，一定是当前的），
        但落到盘上的身份是 key。候选里的 id 是写盘时的快照，得先刷新再比。
        """
        refresh_snapshots(payload, self._match_resolver())
        segment = segment_at(payload, int(segment_index))

        key = ""
        for candidate in segment.get("candidates", []):
            if candidate.get("shot_id") is None:
                continue
            if int(candidate["shot_id"]) == int(shot_id):
                key = str(candidate.get("shot_key", ""))
                break

        shot = self._library().shot(int(shot_id))
        if shot is None:
            raise ValueError(f"素材库里没有这个镜头：shot_id={shot_id}")
        shot["video_path"] = str(shot.get("video_path", ""))
        if not key:
            key = str(shot.get("shot_key", ""))
        if not key:
            raise ValueError(
                f"镜头 {shot_id} 没有持久标识 —— 这个库是 v3 之前的旧库，"
                "需要重新入库一次（library_scan）"
            )
        return key, shot

    def match_show(self, name: str | None = None) -> dict[str, Any]:
        """查看匹配结果。不传 `name` 就列出已有的。

        **`shot_id` 是现算的**：文件里存的是 `shot_key`，这里用当前库把它解析成
        `shot_id` 再给你 —— 所以哪怕中间重新分析过素材库，看到的 id 也是**对的**。

        解析不到的候选标 `stale: true` 并说明原因，**不会静默指向一个错的镜头**。
        """
        store = self._match_store()
        if not name:
            items = store.listing()
            return {"count": len(items), "matches": items}

        payload = store.load(name)
        refresh_snapshots(payload, self._match_resolver())
        segments = payload.get("segments", [])
        stale = sum(
            1 for segment in segments for candidate in segment.get("candidates", [])
            if candidate.get("stale")
        )
        return {
            "name": payload.get("name", name),
            "path": str(store.path(name)),
            "version": payload.get("version", 1),
            "script": payload.get("script", ""),
            "top_n": payload.get("top_n"),
            "space": payload.get("space", {}),
            "library_shots": payload.get("library_shots"),
            "created_at": payload.get("created_at", ""),
            "updated_at": payload.get("updated_at", ""),
            "stale_candidates": stale,
            "segments": [
                {
                    "index": segment.get("index"),
                    "text": segment.get("text", ""),
                    "selected_shot_key": segment.get("selected_shot_key"),
                    "selected_shot_id": segment.get("selected_shot_id"),
                    "selected_stale": bool(segment.get("selected_stale")),
                    "candidates": segment.get("candidates", []),
                }
                for segment in segments
            ],
        }

    def match_select(
        self, name: str, segment_index: int, shot_id: int
    ) -> dict[str, Any]:
        """指定某一段用哪个镜头（"替换候选 / 手动指定"都走这一步）。

        `shot_id` 用**当前的**库 id（从检索结果或 `library_status` 里现取）。
        存盘时记的是 `shot_key`，所以重新分析后这条选择依然指向同一个镜头。

        - 镜头已在该段候选里 → 直接选中它
        - 不在候选里 → 从素材库取它的信息，插到候选**首位**并选中
        """
        store = self._match_store()
        payload = store.load(name)
        key, shot = self._resolve_current(payload, int(segment_index), int(shot_id))
        segment = select_shot(
            payload, segment_index=int(segment_index), shot_key=key, shot=shot
        )
        path = store.save(payload)
        return {
            "name": payload.get("name", name), "path": str(path),
            "segment": self._segment_view(segment),
        }

    def match_reorder(
        self, name: str, segment_index: int, shot_id: int, direction: str = "up"
    ) -> dict[str, Any]:
        """把某段里的一个候选上移 / 下移一位（调整顺序）。按 `shot_key` 存盘。"""
        store = self._match_store()
        payload = store.load(name)
        key, _shot = self._resolve_current(payload, int(segment_index), int(shot_id))
        segment = reorder_candidate(
            payload, segment_index=int(segment_index),
            shot_key=key, direction=str(direction),
        )
        path = store.save(payload)
        return {
            "name": payload.get("name", name), "path": str(path),
            "segment": self._segment_view(segment),
        }

    @staticmethod
    def _segment_view(segment: dict[str, Any]) -> dict[str, Any]:
        return {
            "index": segment.get("index"),
            "text": segment.get("text", ""),
            "selected_shot_key": segment.get("selected_shot_key"),
            "selected_shot_id": segment.get("selected_shot_id"),
            "candidates": segment.get("candidates", []),
        }

    # ---------------- 素材库：导出 ----------------
    def library_export(self, shot_id: int, output: str | None = None) -> dict[str, Any]:
        """把选中的镜头导出成可再剪辑的片段。

        用**输出侧定位**（`-ss` 放在 `-i` 之后）而不是输入侧快速定位：
        快速定位会从最近的关键帧开始解码，时间码不一定精确；
        本任务的验收标准是"时间码正确"，所以选精确的那条路。
        """
        library = self._library()
        shot = library.shot(int(shot_id))
        if shot is None:
            raise ValueError(f"库里没有这个镜头：shot_id={shot_id}")

        source = Path(shot["video_path"])
        if not source.is_file():
            raise ValueError(f"源视频不存在（库记录已失效，可重新扫描）：{source}")

        start = float(shot["start_seconds"])
        end = float(shot["end_seconds"])
        duration = max(0.05, end - start)

        if output:
            target = Path(str(output)).expanduser()
            self.ctx.files.ensure_dir(target.parent)
        else:
            out_dir = self.ctx.files.ensure_dir(self.ctx.data_dir / EXPORT_DIR_NAME)
            target = out_dir / f"{source.stem}_shot{int(shot['shot_index']):03d}.mp4"

        self.ctx.ffmpeg.run(
            [
                "-y", "-hide_banner", "-nostats",
                "-i", str(source),
                "-ss", f"{start:.3f}", "-t", f"{duration:.3f}",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "160k",
                "-movflags", "+faststart",
                str(target),
            ],
            timeout=600,
        )
        if not target.is_file():
            raise ValueError(f"导出失败，未生成文件：{target}")

        return {
            "shot_id": int(shot_id), "video_path": str(source),
            "shot_index": int(shot["shot_index"]),
            "start": round(start, 3), "end": round(end, 3),
            "duration": round(duration, 3),
            "output": str(target), "size_bytes": self.ctx.files.size(target),
            "labels": list(shot["labels"]),
        }

    # ---------------- 内部：校验与查询 ----------------
    def _require_video(self, path: str) -> Path:
        source = Path(str(path)).expanduser()
        if not source.is_file():
            raise ValueError(f"文件不存在：{source}")
        if source.suffix.lower() not in VIDEO_SUFFIXES:
            raise ValueError(
                f"不是支持的视频扩展名：{source.suffix!r}；支持 {sorted(VIDEO_SUFFIXES)}"
            )
        return source

    def _number(self, override: Any, key: str, fallback: float) -> float:
        if override is not None:
            value = _to_float(override)
            if value is not None:
                return value
        value = _to_float(self._cfg.get(key))
        return value if value is not None else float(fallback)

    def _duration(self, source: Path) -> float:
        duration = self.ctx.ffmpeg.probe_duration(source)
        if duration is None or duration <= 0:
            raise ValueError(f"无法读取视频时长（文件可能损坏或不是有效视频）：{source}")
        return float(duration)

    def _probe(self, source: Path) -> dict[str, Any]:
        raw = self.ctx.ffmpeg.probe_media(source)
        fmt = raw.get("format") or {}
        streams = raw.get("streams") or []
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

        duration = _to_float(fmt.get("duration"))
        if duration is None and isinstance(video, dict):
            duration = _to_float(video.get("duration"))

        return {
            "file": str(source),
            "file_name": source.name,
            "size_bytes": _to_int(fmt.get("size")),
            "duration_seconds": round(duration, 3) if duration is not None else None,
            "format_name": str(fmt.get("format_name") or ""),
            "bit_rate": _to_int(fmt.get("bit_rate")),
            "has_video": video is not None,
            "has_audio": audio is not None,
            "video": None if not isinstance(video, dict) else {
                "codec": str(video.get("codec_name") or ""),
                "width": _to_int(video.get("width")),
                "height": _to_int(video.get("height")),
                "fps": _parse_fps(video.get("avg_frame_rate") or video.get("r_frame_rate")),
                "pix_fmt": str(video.get("pix_fmt") or ""),
                "nb_frames": _to_int(video.get("nb_frames")),
            },
            "audio": None if not isinstance(audio, dict) else {
                "codec": str(audio.get("codec_name") or ""),
                "sample_rate": _to_int(audio.get("sample_rate")),
                "channels": _to_int(audio.get("channels")),
            },
        }

    # ---------------- 内部：镜头切分 ----------------
    def _scene_times(self, source: Path, threshold: float) -> list[float]:
        """用 FFmpeg 的 scene 滤镜找出画面突变的时间点。

        命令形态与 V18 的 detect_scene_boundaries 一致（那里已验证可用）。
        """
        result = self.ctx.ffmpeg.run(
            [
                "-hide_banner", "-nostats",
                "-i", str(source),
                "-vf", f"select='gt(scene,{threshold:.4f})',showinfo",
                "-an",
                "-f", "null", "-",
            ],
            timeout=600,
        )
        text = f"{result.stderr or ''}\n{result.stdout or ''}"

        times: list[float] = []
        for line in text.splitlines():
            if "pts_time:" not in line:
                continue
            match = _SCENE_TIME_RE.search(line)
            if match:
                times.append(float(match.group(1)))

        # 去重：0.25 秒内的多个命中视为同一次切换（与 V18 相同的做法）
        clean: list[float] = []
        for value in sorted(times):
            if not clean or abs(value - clean[-1]) >= 0.25:
                clean.append(value)
        return clean

    def _build_shots(
        self,
        duration: float,
        cuts: list[float],
        min_shot_seconds: float,
        max_shots: int,
    ) -> list[dict[str, Any]]:
        """把切点整理成镜头区间：太短的镜头会被并入前一个，首尾自动补齐。"""
        points: list[float] = [0.0]
        for cut in sorted(cuts):
            if cut - points[-1] < min_shot_seconds:
                continue
            if duration - cut < 0.05:  # 距离结尾太近，不单独成镜头
                continue
            points.append(round(cut, 3))
        points.append(round(duration, 3))

        shots: list[dict[str, Any]] = []
        for index in range(len(points) - 1):
            start, end = points[index], points[index + 1]
            if end - start <= 0:
                continue
            shots.append(
                {
                    "index": index,
                    "start": round(start, 3),
                    "end": round(end, 3),
                    "duration": round(end - start, 3),
                }
            )
            if len(shots) >= max_shots:
                break
        return shots

    # ---------------- 内部：抽帧 ----------------
    def _extract_frames(
        self,
        source: Path,
        shots: list[dict[str, Any]],
        frames_per_shot: int,
    ) -> dict[str, Any]:
        width = int(self._number(None, "frame_width", 480))
        out_dir = self.ctx.data_dir / "frames" / self._output_key(source)
        self.ctx.files.ensure_dir(out_dir)

        frames: list[dict[str, Any]] = []
        skipped = 0
        for shot in shots:
            count = frames_per_shot
            for order in range(count):
                if len(frames) >= MAX_FRAMES_TOTAL:
                    skipped += 1
                    continue
                # 在镜头中间位置取样，避开转场瞬间
                moment = shot["start"] + shot["duration"] * (order + 0.5) / count
                target = out_dir / f"shot{shot['index']:03d}_f{order + 1:02d}.jpg"

                self.ctx.ffmpeg.run(
                    [
                        "-y", "-hide_banner", "-nostats",
                        "-ss", f"{moment:.3f}",
                        "-i", str(source),
                        "-frames:v", "1",
                        "-vf", f"scale={width}:-2",
                        "-q:v", "4",
                        str(target),
                    ],
                    timeout=120,
                )
                if not target.is_file():
                    continue
                frames.append(
                    {
                        "shot_index": shot["index"],
                        "order": order + 1,
                        "time": round(moment, 3),
                        "file": str(target),
                        "width": width,
                        "size_bytes": self.ctx.files.size(target),
                    }
                )

        return {
            "file": str(source),
            "output_dir": str(out_dir),
            "shot_count": len(shots),
            "frame_count": len(frames),
            "frames": frames,
            "skipped": skipped,
        }

    @staticmethod
    def _output_key(source: Path) -> str:
        """用文件名 + 路径短哈希做输出目录名，避免同名视频互相覆盖。"""
        digest = hashlib.sha1(str(source).encode("utf-8")).hexdigest()[:8]
        return f"{source.stem}_{digest}"
