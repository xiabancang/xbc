"""FFmpeg 能力：只负责"定位 + 执行 + 元数据探测"，不承载任何业务逻辑。

对比 V18 原型：那里把 FFMPEG = "ffmpeg" 写成全局硬编码常量，
既无法配置、也没有"是否真的装了 ffmpeg"的检测，
用户机器上没装 ffmpeg 时只会拿到一个看不懂的报错。

抽帧、镜头切分、转码参数等属于业务逻辑，留在插件里，不进内核。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from ..errors import ServiceUnavailable


class FFmpegService:
    def __init__(
        self,
        ffmpeg_path: str = "ffmpeg",
        ffprobe_path: str = "ffprobe",
        logger: Any = None,
    ) -> None:
        self._ffmpeg_cfg = str(ffmpeg_path)
        self._ffprobe_cfg = str(ffprobe_path)
        self._log = logger
        self._ffmpeg: str | None = None
        self._ffprobe: str | None = None

    # ---------- 定位 ----------
    @staticmethod
    def _resolve(candidate: str) -> str | None:
        """先按绝对/相对路径判断，再退回到 PATH 查找。"""
        if not candidate:
            return None
        path = Path(candidate)
        if path.is_file():
            return str(path)
        return shutil.which(candidate)

    @property
    def ffmpeg(self) -> str | None:
        if self._ffmpeg is None:
            self._ffmpeg = self._resolve(self._ffmpeg_cfg)
        return self._ffmpeg

    @property
    def ffprobe(self) -> str | None:
        if self._ffprobe is None:
            self._ffprobe = self._resolve(self._ffprobe_cfg)
        return self._ffprobe

    def available(self) -> bool:
        """FFmpeg 是否可用。插件在启动时应先问一次，再决定要不要启用相关功能。"""
        return self.ffmpeg is not None

    def version(self) -> str | None:
        """返回形如 "ffmpeg version 9.0.1 ..." 的首行，不可用则返回 None。"""
        if not self.ffmpeg:
            return None
        try:
            result = self.run(["-version"], timeout=15)
        except (ServiceUnavailable, OSError, subprocess.SubprocessError):
            return None
        first_line = (result.stdout or "").strip().splitlines()
        return first_line[0] if first_line else None

    # ---------- 执行 ----------
    def run(
        self,
        args: list[str],
        timeout: int = 600,
        cwd: Path | str | None = None,
    ) -> subprocess.CompletedProcess:
        """执行 ffmpeg，参数由调用方给出（内核不预设业务参数）。"""
        if not self.ffmpeg:
            raise ServiceUnavailable(
                f"未找到 ffmpeg（当前配置为 {self._ffmpeg_cfg!r}）；"
                "请安装 FFmpeg 并加入 PATH，或在 config.json 的 ffmpeg.ffmpeg_path 指定绝对路径"
            )
        command = [self.ffmpeg, *[str(a) for a in args]]
        if self._log:
            self._log.debug("执行: %s", " ".join(command))
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=str(cwd) if cwd else None,
        )

    # ---------- 元数据 ----------
    def probe_duration(self, video_path: Path | str) -> float | None:
        """读取媒体时长（秒）。命令形式参考 V18 的 ffprobe_duration 实现。

        失败返回 None，而不是返回 0.0 —— 0.0 会被误当成"时长为零"的合法值。
        """
        if not self.ffprobe:
            raise ServiceUnavailable(
                f"未找到 ffprobe（当前配置为 {self._ffprobe_cfg!r}）"
            )
        command = [
            self.ffprobe,
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(video_path),
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
            )
            return float((result.stdout or "").strip())
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            if self._log:
                self._log.debug("读取时长失败 %s: %s", video_path, exc)
            return None
