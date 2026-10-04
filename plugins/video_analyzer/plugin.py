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
from pathlib import Path
from typing import Any

from xbc.core.capabilities.ai import AIError
from xbc.core.contract.plugin import XbcPlugin

#: 从 ffmpeg showinfo 输出里取时间戳（V18 同款正则）
_SCENE_TIME_RE = re.compile(r"pts_time:([0-9]+(?:\.[0-9]+)?)")

#: 送给 AI 的定向提问。**由插件定义** —— 这是业务语义，不该出现在内核里。
FRAME_QUESTION = "这个画面的主要内容是什么？属于什么场景或拍摄类型？"

#: 一次标注最多分析多少帧（安全上限，避免把长视频送去跑几十次 AI）
MAX_ANNOTATED_FRAMES = 24

VIDEO_SUFFIXES = {
    ".mp4", ".mov", ".mkv", ".avi", ".webm", ".flv",
    ".m4v", ".ts", ".wmv", ".mpg", ".mpeg", ".rmvb",
}

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

        if not ctx.ffmpeg.available():
            self.log.warning(
                "未检测到 FFmpeg：video_analyzer 的工具会在调用时明确报错，"
                "请安装 FFmpeg 或配置 ffmpeg.ffmpeg_path"
            )
        self.log.info("apply：已注册 5 个视频工具，配置 = %s", self._cfg)

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
