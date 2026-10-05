"""语音能力自检插件 —— 验收用，不交付业务。

## 它证明什么

**插件不知道 Provider 存在。** 本文件里没有：

- 任何 TTS 厂商 / 引擎 / 模型名（`moss`、`onnx`、`soundfile` …）
- 任何模型文件路径
- 任何推理运行时的 import

只有 `ctx.ai.clone_voice(...)` 和 `ctx.ai.synthesize(...)`。
所以把配置里的 `voice_tts.engine` 换成另一个实现，**本文件一个字节都不用改**。

这条不是靠"看一眼"保证的：`tests/test_speech_capability.py` 会
用 `ast` 扫描本目录下所有源码，出现推理运行时或引擎名的 import/标识就失败。

## 它跑什么

1. `ctx.ai.clone_voice(参考音频)` —— 确认这段音频能不能当音色用
2. `ctx.ai.synthesize(文本, 该音色, 输出路径)` —— 用它出声
3. 用 `soundfile` **回读产物**核对时长/采样率/声道数与返回的一致

第 3 步在自检里做（自检工具本来就该做校验），所以本文件允许 import `soundfile` ——
它是**读取校验**，不是模型推理。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from xbc.core.capabilities.ai import AIError
from xbc.core.contract.plugin import XbcPlugin

__all__ = ["VoiceTestPlugin"]

DEFAULT_TEXT = "这是一段用于验证语音合成能力的中文测试文本。"
DEFAULT_OUTPUT_NAME = "voice_selftest.wav"


class VoiceTestPlugin(XbcPlugin):
    """只通过 AI 能力层说话的插件。"""

    def apply(self, ctx: Any, config: dict) -> None:
        self.ctx = ctx
        self._cfg = dict(config)
        self.log = ctx.logger

        ctx.tools.register(
            "voice_selftest",
            self.voice_selftest,
            description=(
                "语音能力自检：登记音色 → 合成 → 回读产物核对规格。"
                "只通过 ctx.ai 说话，不接触任何模型实现"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "reference_audio": {"type": "string", "minLength": 1},
                    "text": {"type": "string"},
                    "output_path": {"type": "string"},
                },
                "required": ["reference_audio"],
            },
            risk="write",
        )

    def voice_selftest(
        self,
        reference_audio: str,
        text: str = "",
        output_path: str = "",
    ) -> dict[str, Any]:
        steps: list[dict[str, Any]] = []
        payload = str(text or DEFAULT_TEXT)

        target = Path(output_path).expanduser() if output_path else (
            Path(self.ctx.data_dir) / DEFAULT_OUTPUT_NAME
        )

        # ---- 第 1 步：登记音色 ----
        try:
            profile = self.ctx.ai.clone_voice(reference_audio)
            steps.append({
                "step": "clone_voice", "ok": True, "result": profile.to_dict(),
            })
        except AIError as exc:
            steps.append({"step": "clone_voice", "ok": False, "error": str(exc)})
            return {"ok": False, "steps": steps, "hint": _hint(exc)}

        # ---- 第 2 步：用这个音色合成 ----
        try:
            speech = self.ctx.ai.synthesize(
                payload, output_path=str(target), voice=reference_audio
            )
            steps.append({
                "step": "synthesize", "ok": True, "result": speech.to_dict(),
            })
        except AIError as exc:
            steps.append({"step": "synthesize", "ok": False, "error": str(exc)})
            return {"ok": False, "steps": steps, "hint": _hint(exc)}

        # ---- 第 3 步：回读产物核对（自检该做的事）----
        steps.append({
            "step": "verify_audio", "ok": True,
            "result": _verify(Path(speech.path), speech),
        })

        return {
            "ok": True,
            "text": payload,
            "steps": steps,
            "hint": "产物可播放即是能力可用；音色像不像要靠听。",
        }


def _verify(path: Path, speech: Any) -> dict[str, Any]:
    """回读音频文件，核对返回的规格与文件实际是否一致。"""
    import wave

    with wave.open(str(path), "rb") as handle:
        frames = handle.getnframes()
        rate = handle.getframerate() or 1
        channels = handle.getnchannels()
    actual_duration = frames / float(rate)
    return {
        "path": str(path),
        "exists": path.is_file(),
        "size_bytes": path.stat().st_size if path.is_file() else 0,
        "reported": {
            "duration": speech.duration,
            "sample_rate": speech.sample_rate,
            "channels": speech.channels,
        },
        "actual": {
            "duration": round(actual_duration, 3),
            "sample_rate": rate,
            "channels": channels,
        },
        "consistent": (
            abs(actual_duration - float(speech.duration or 0)) < 0.05
            and rate == speech.sample_rate
            and channels == speech.channels
        ),
    }


def _hint(exc: Exception) -> str:
    """把"下一步该干什么"说清楚 —— 自检工具的价值就在这里。

    **这里不写引擎名、不写模型路径、不写获取脚本名**：那些是实现细节，
    换个引擎就全错了。要说的信息由**错误消息本身**带着（Provider 负责写清楚），
    本文件只做分类与指引。
    """
    message = str(exc)
    if "模型" in message or "model" in message.lower():
        return "模型没就位：按上面的 error 里给出的路径与获取方式处理，然后重试。"
    if "Provider" in message or "支持" in message:
        return "没有可用的语音 Provider：确认提供语音能力的插件已启用（插件中心）。"
    return "详见上面的 error 字段。"
