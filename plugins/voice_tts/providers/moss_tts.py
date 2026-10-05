r"""MOSS-TTS-Nano 的语音合成 Provider（**实现路线 D**）。

## 这个文件是干什么的

把 MOSS-TTS-Nano 的官方 ONNX 版接进夏半仓的 `speech` 能力。
它**只做一件事**：让官方 ONNX 版在没有 PyTorch 的机器上跑起来。

## 为什么需要"路线 D"

官方 ONNX 版的 README 写着 *"No PyTorch dependency during inference"*，
**但代码不是这样**：`onnx_tts_runtime.py` 第 12-13 行硬 `import torch` / `import torchaudio`。

实测：在没有 torch 的环境里跑官方入口 →

    File "onnx_tts_runtime.py", line 12, in <module>
      import torch
    ModuleNotFoundError: No module named 'torch'

而 torch 在整条推理链里**只被用来做一件事**：

    def _load_reference_audio(self, reference_audio_path):
        waveform, sample_rate = torchaudio.load(...)
        ...resample / 声道转换...
        return ...numpy()

`torch 安装体积 1183 MB`，为了读一个 wav 装它，代价太大。

## 做法：给它一个空壳，然后只换掉那一件事

1. 在**导入上游模块的那一瞬间**，把 `torch` / `torchaudio` 换成**空壳**
   （只为让那句顶层 import 不报错）；
2. 导入完成后**立刻撤掉空壳** —— 之后谁再 `import torch` 都会得到正常错误，
   而不是静默拿到假模块；
3. 继承上游的运行类，**只覆写 `_load_reference_audio`**（改用 `soundfile` + `soxr`），
   **其余全部走上游代码**。

空壳里那两个函数是**故意会抛异常**的 —— 万一哪天上游多出一处 torch 用法、
我们的覆写没盖住，它会当场炸出来，而不是静默出错。

## ⚠️ 我们依赖了上游的一个**私有**方法

| 项 | 值 |
|---|---|
| 仓库 | `OpenMOSS/MOSS-TTS-Nano` |
| 提交 | `8b7bcc9341b3b4ef3a3a58ba1338a7d85ff133eb`（2026-09-06） |
| `pyproject` 版本 | `0.1.0` |
| 模块 / 类 | `onnx_tts_runtime.OnnxTtsRuntime` |
| **私有方法** | **`_load_reference_audio(self, reference_audio_path)`**（该文件 L445） |
| 该文件 SHA256(前 16) | `f4169a3ef0fdfb5e` |

**上游一改这个方法的签名或名字，路线 D 就失效。**
所以导入时有一道**守卫**（`assert_upstream_contract`）：方法不在就抛
`UpstreamContractError` 并指出回退路径 —— **响亮地失败，不静默**。

决定与失效触发条件记录在 `docs/research/README.md` 的 TASK-013 一节。

## ⚠️ 第二个坑：sentencepiece 打不开含中文的路径

**实测**（本机 Windows）：

| 路径 | sentencepiece |
|---|---|
| `C:\...\moss-tts-nano\models\...\tokenizer.model`（纯 ASCII） | ✅ |
| `C:\...\夏半仓工具箱\models\...\tokenizer.model`（含中文） | ❌ `NOT_FOUND ... Error #2` |

而夏半仓的**默认数据根就是 `夏半仓工具箱`** —— 所以这条一定会踩到。
对照实测：`onnxruntime` **能**读中文路径（含外部 `.data` 权重），所以只有 sentencepiece 要绕。

8.3 短路径在本机不可用（该卷没生成短名），所以做法是**把 tokenizer 复制到 ASCII 暂存目录**，
用 `_sentencepiece_ascii_paths()` 只包住一次 runtime 构造，退出即还原。

## 实测的等价性（这是路线 D 敢用的依据）

| 参考音频 | 格式 | 与上游 torchaudio 路径的等价性 |
|---|---|---|
| 48 kHz（单/双声道） | 不需要重采样 | **逐帧 100% 一致** |
| 44.1 kHz 单声道 | 需要重采样 | 逐帧 83.67% 一致；**波形余弦 0.99999995、相对误差 0.0506%** |

结论：**差异只来自重采样器**。48 kHz 输入下我们与上游**逐字节等价**；
非 48 kHz 时两个重采样器数值上几乎相同（0.05%），离散码的少量翻转是量化边界效应。
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib
import json
import shutil
import sys
import tempfile
import types
import wave
from pathlib import Path
from typing import Any

import numpy as np

from xbc.core.capabilities.ai import (
    AICapability,
    AIError,
    AIUnavailable,
    ModelProvider,
    SpeechRequest,
    SpeechResult,
    VoiceCloneRequest,
    VoiceProfile,
)

__all__ = [
    "MossSpeechProvider",
    "UpstreamContractError",
    "assert_upstream_contract",
    "create_provider",
    "UPSTREAM_PRIVATE_METHOD",
]


def create_provider() -> "MossSpeechProvider":
    """引擎工厂。

    插件按**配置里的引擎名**（`voice_tts.engine`）加载 `providers/<引擎名>.py`，
    然后调这个工厂拿实例 —— 所以**加一个新引擎不用改插件代码，只改配置**。
    """
    return MossSpeechProvider()

# ==================== 上游契约（改这里之前先读 docs/research/README.md） ====================

UPSTREAM_REPO = "OpenMOSS/MOSS-TTS-Nano"
UPSTREAM_COMMIT = "8b7bcc9341b3b4ef3a3a58ba1338a7d85ff133eb"
UPSTREAM_VERSION = "0.1.0"
UPSTREAM_MODULE = "onnx_tts_runtime"
UPSTREAM_CLASS = "OnnxTtsRuntime"
#: **我们覆写的那个私有方法。** 它不在就说明路线 D 失效。
UPSTREAM_PRIVATE_METHOD = "_load_reference_audio"
UPSTREAM_METHOD_FILE_SHA256_16 = "f4169a3ef0fdfb5e"

#: 共享模型目录下的子目录名（Core 把 `<数据根>/models` 通过 bind_models 交给我们）
MODEL_ID = "moss-tts-nano"
TTS_SUBDIR = "MOSS-TTS-Nano-100M-ONNX"
CODEC_SUBDIR = "MOSS-Audio-Tokenizer-Nano-ONNX"
TTS_REPO_URL = "https://huggingface.co/OpenMOSS-Team/MOSS-TTS-Nano-100M-ONNX"
CODEC_REPO_URL = "https://huggingface.co/OpenMOSS-Team/MOSS-Audio-Tokenizer-Nano-ONNX"

#: codec 要求的音频规格 —— 参考音频与输出都是这个。
#: 从模型元数据实测得来（`codec_browser_onnx_meta.json` 的 `codec_config`）。
TARGET_SAMPLE_RATE = 48000
TARGET_CHANNELS = 2

#: 参考音频的合理区间。低于下限克隆不出音色，高于上限纯属浪费。
MIN_REFERENCE_SECONDS = 1.0
MAX_REFERENCE_SECONDS = 60.0

#: 生成参数（默认值，跟随上游 CLI 的口径 —— `infer_onnx.py` 的 argparse 默认）
DEFAULT_MAX_NEW_FRAMES = 375
DEFAULT_VOICE_CLONE_MAX_TEXT_TOKENS = 75
DEFAULT_CPU_THREADS = 4
#: ⚠️ **不要改成 greedy。** 实测：`do_sample=False` 时模型不吐结束符，
#: 一路生成到 `max_new_frames` 上限 —— 26 个字的文本也输出满 30 秒。
#: 上游 CLI 的默认是 `--do-sample 1` + `--sample-mode fixed`，照它来。
#: （这个坑是靠"四段文本输出时长一模一样"发现的，不是靠听。）
DEFAULT_DO_SAMPLE = True
DEFAULT_SAMPLE_MODE = "fixed"


class UpstreamContractError(AIError):
    """上游私有 API 与适配层对不上了 —— 路线 D 失效。

    这是一个**必须响亮**的错误：它意味着"覆写没盖住 torch 的用法"，
    继续跑下去不是报错就是给出错误结果。
    """


def assert_upstream_contract(runtime_class: Any) -> None:
    """守卫：上游那个私有方法必须还在，否则抛 `UpstreamContractError`。

    单独抽成函数是为了**可测** —— 测试可以丢一个假类进来验证守卫真的会响。
    """
    if not hasattr(runtime_class, UPSTREAM_PRIVATE_METHOD):
        raise UpstreamContractError(
            f"上游 {UPSTREAM_REPO} 的 {UPSTREAM_CLASS}.{UPSTREAM_PRIVATE_METHOD} "
            f"不存在了 —— 实现路线 D 失效。\n"
            f"我们依赖它（记录于 {UPSTREAM_COMMIT[:7]} / v{UPSTREAM_VERSION}）"
            f"把 torchaudio 换成 soundfile + soxr。\n"
            f"回退路径（见 docs/research/README.md 的 TASK-013 一节）：\n"
            f"  · 路线 C —— 不装 PyTorch，自己实现参考音频加载与合成编排\n"
            f"  · 路线 A —— 装 PyTorch + torchaudio，直接用上游官方入口"
        )


def _stub_never_called(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError(
        "空壳 torch / torchaudio 被调用了 —— 说明覆写没盖住上游对 torch 的全部用法。"
        "这是 UpstreamContractError 的另一种形态，请按它的回退路径处理。"
    )


def _import_upstream() -> tuple[Any, Any]:
    """装空壳 → 导入上游 → 校验契约 → **撤掉空壳**。返回 (module, runtime_class)。

    撤掉空壳这一点很关键：它把"假模块"的影响窗口压到**只有一次 import**，
    之后进程里再没有假 torch 存在。
    """
    torch_stub = types.ModuleType("torch")
    torch_stub.float32 = "float32"          # type: ignore[attr-defined]
    torch_stub.Tensor = type("Tensor", (), {})  # type: ignore[attr-defined]

    torchaudio_stub = types.ModuleType("torchaudio")
    functional_stub = types.ModuleType("torchaudio.functional")
    torchaudio_stub.load = _stub_never_called          # type: ignore[attr-defined]
    torchaudio_stub.functional = functional_stub       # type: ignore[attr-defined]
    functional_stub.resample = _stub_never_called      # type: ignore[attr-defined]

    stubs = {
        "torch": torch_stub,
        "torchaudio": torchaudio_stub,
        "torchaudio.functional": functional_stub,
    }
    # 记下原值 —— 万一别人已经 import 过真 torch，要原样还回去
    saved = {name: sys.modules.get(name) for name in stubs}
    sys.modules.update(stubs)
    try:
        module = importlib.import_module(UPSTREAM_MODULE)
    except ImportError as exc:  # 依赖没装齐
        raise AIUnavailable(
            f"导入上游模块 {UPSTREAM_MODULE!r} 失败：{exc}\n"
            f"本 Provider 需要：onnxruntime / numpy / sentencepiece / soundfile / soxr。"
        ) from exc
    finally:
        for name, previous in saved.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous

    runtime_class = getattr(module, UPSTREAM_CLASS, None)
    if runtime_class is None:
        raise UpstreamContractError(
            f"上游模块 {UPSTREAM_MODULE!r} 里没有 {UPSTREAM_CLASS} —— "
            f"上游结构变了，路线 D 失效。见 docs/research/README.md。"
        )
    assert_upstream_contract(runtime_class)
    return module, runtime_class


def _load_reference_audio(self: Any, reference_audio_path: str | Path) -> np.ndarray:
    """**被覆写的那一个私有方法**：替代上游的 `torchaudio` 加载 + 重采样。

    与上游原实现逐句对应（`onnx_tts_runtime.py` L445-461）：

    | 上游 | 这里 |
    |---|---|
    | `torchaudio.load(path)` | `soundfile.read(dtype="float32", always_2d=True)` |
    | `.to(torch.float32)` | 读出来就是 float32 |
    | `torchaudio.functional.resample` | `soxr.resample(quality="VHQ")` |
    | `waveform.repeat(n, 1)` | `numpy.repeat(..., axis=1)` |
    | `waveform.mean(dim=0, keepdim=True)` | `numpy.mean(axis=1, keepdims=True)` |
    | `unsqueeze(0)` + `.numpy()` | `[None, ...]` |

    返回形状与上游一致：`(1, channels, frames)`、float32、范围 [-1, 1]。
    """
    import soundfile as sf
    import soxr

    resolved = Path(reference_audio_path).expanduser().resolve()
    data, sample_rate = sf.read(str(resolved), dtype="float32", always_2d=True)
    if data.size == 0:
        raise AIUnavailable(f"参考音频没有可用的采样：{resolved}")

    target_sample_rate = int(self.codec_meta["codec_config"]["sample_rate"])
    target_channels = int(self.codec_meta["codec_config"]["channels"])

    if sample_rate != target_sample_rate:
        # soxr 是 (frames, channels) 布局，所以这里不转置
        data = soxr.resample(data, sample_rate, target_sample_rate, quality="VHQ")

    current_channels = int(data.shape[1])
    if current_channels == target_channels:
        pass
    elif current_channels == 1 and target_channels > 1:
        data = np.repeat(data, target_channels, axis=1)
    elif current_channels > 1 and target_channels == 1:
        data = data.mean(axis=1, keepdims=True)
    else:
        raise AIUnavailable(
            f"参考音频声道数无法转换：{current_channels} -> {target_channels}（{resolved}）"
        )

    waveform = np.ascontiguousarray(data.T, dtype=np.float32)
    return waveform[None, ...]


def _probe_wav(path: str | Path) -> tuple[float, int, int]:
    """读回音频文件的实测规格：(时长秒, 采样率, 声道数)。"""
    with wave.open(str(path), "rb") as handle:
        frames = handle.getnframes()
        rate = handle.getframerate() or 1
        return frames / float(rate), rate, handle.getnchannels()


# ==================== 非 ASCII 路径的绕行（第二个实测坑） ====================
#
# **实测**：`sentencepiece` 在 Windows 上**打不开含非 ASCII 字符的绝对路径** ——
#
#     Processing  C:\...\夏半仓工具箱\models\...\tokenizer.model   ❌ NOT_FOUND Error #2
#     Processing  C:\...\moss-tts-nano\models\...\tokenizer.model  ✅
#
# 而夏半仓的**默认数据根就含中文**（`夏半仓工具箱`），所以这条路径一定会踩到。
# 对照：`onnxruntime` **能**正常读中文路径（含外部 `.data` 权重），实测确认 ——
# 所以只有 sentencepiece 需要绕。
#
# 8.3 短路径在本机不可用（`GetShortPathNameW` 返回原路径，说明该卷没生成短名）。
# 因此做法是：**把 tokenizer 复制到纯 ASCII 的暂存目录**，只包住一次 runtime 构造。

#: 暂存目录名
_TOKENIZER_STAGING_DIR = "xbc-moss-tts-tokenizer"


def _ascii_staging_root() -> Path:
    """挑一个**纯 ASCII 且可写**的暂存根目录。

    不能想当然用 `%TEMP%` —— 用户名若是中文（`C:\\Users\\张三\\...`），
    临时目录也含非 ASCII，绕行就白做了。所以逐个候选试到能写为止。
    """
    candidates = [
        Path(tempfile.gettempdir()),
        Path("C:/ProgramData"),
        Path("C:/Windows/Temp"),
    ]
    errors: list[str] = []
    for candidate in candidates:
        if not str(candidate).isascii():
            errors.append(f"{candidate}（含非 ASCII）")
            continue
        try:
            probe = candidate / _TOKENIZER_STAGING_DIR
            probe.mkdir(parents=True, exist_ok=True)
            return candidate
        except OSError as exc:
            errors.append(f"{candidate}（不可写：{exc}）")
    raise AIError(
        "找不到纯 ASCII 且可写的临时目录，无法绕开 sentencepiece 的非 ASCII 路径限制。\n"
        "试过：" + "；".join(errors)
    )


def _stage_non_ascii_model_file(path: Path) -> Path:
    """路径全 ASCII 就原样返回；否则复制一份到 ASCII 暂存目录再返回副本路径。"""
    if str(path).isascii():
        return path
    root = _ascii_staging_root()
    target_dir = root / _TOKENIZER_STAGING_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / path.name
    try:
        size_matches = (
            target.is_file() and target.stat().st_size == path.stat().st_size
        )
    except OSError:
        size_matches = False
    if not size_matches:
        shutil.copy2(path, target)
    return target


@contextlib.contextmanager
def _sentencepiece_ascii_paths():
    """临时让 sentencepiece 从 ASCII 暂存副本加载。

    与 torch 空壳同一套思路：**只在需要的那一刻改，用完立刻还原** ——
    不留下任何全局副作用，也不改上游代码。

    这里用一个**工厂函数**替换类（而不是继承它）：sentencepiece 的
    `SentencePieceProcessor` 由 C++ 扩展支撑，继承不保证可用，工厂函数则一定可以。
    上游的用法是 `spm.SentencePieceProcessor(model_file=...)`，拿到的是真对象。
    """
    import sentencepiece as spm

    original = spm.SentencePieceProcessor

    def redirected(*args: Any, **kwargs: Any) -> Any:
        model_file = kwargs.get("model_file")
        if model_file is None and args:
            model_file = args[0]
        if model_file:
            staged = _stage_non_ascii_model_file(Path(str(model_file)))
            if str(staged) != str(model_file):
                if "model_file" in kwargs:
                    kwargs["model_file"] = str(staged)
                else:
                    args = (str(staged), *args[1:])
        return original(*args, **kwargs)

    spm.SentencePieceProcessor = redirected  # type: ignore[assignment]
    try:
        yield
    finally:
        spm.SentencePieceProcessor = original  # type: ignore[assignment]


class MossSpeechProvider(ModelProvider):
    """MOSS-TTS-Nano（官方 ONNX 版）的 `speech` 能力实现。

    **本文件是"模型实现"** —— 只有它 import 本地推理运行时。
    插件里的业务代码不许直接用它，必须走 `ctx.ai.synthesize(...)`。
    """

    name = "moss_tts"
    capabilities = frozenset({AICapability.SPEECH})

    def __init__(self) -> None:
        self._models_dir: Path | None = None
        self._model_dir: Path | None = None
        self._runtime: Any = None
        self._runtime_class: Any = None

    # ---------------- 模型路径（Core 通过 bind_models 告诉我们） ----------------
    def bind_models(self, models_dir: Path) -> None:
        """Core 注册时把**共享模型目录**交过来；我们只负责往下解析自己的子目录。

        插件不知道这个路径，Core 也不关心我们怎么组织子目录 —— 边界就在这里。
        """
        self._models_dir = Path(models_dir)
        self._model_dir = self._models_dir / MODEL_ID
        # 换了模型目录就把已建好的 runtime 丢掉，避免指向旧路径
        self._runtime = None

    @property
    def model_dir(self) -> Path:
        base = self._models_dir or Path("models")
        return self._model_dir or (base / MODEL_ID)

    @property
    def tts_dir(self) -> Path:
        return self.model_dir / TTS_SUBDIR

    @property
    def codec_dir(self) -> Path:
        return self.model_dir / CODEC_SUBDIR

    # ---------------- 状态 ----------------
    def missing_model_files(self) -> list[str]:
        """缺哪些文件（相对 `model_dir` 的路径）。

        **只查文件在不在**，不加载、不联网 —— 状态查询会被放在热路径上。
        """
        required = [
            f"{TTS_SUBDIR}/browser_poc_manifest.json",
            f"{TTS_SUBDIR}/tts_browser_onnx_meta.json",
            f"{TTS_SUBDIR}/tokenizer.model",
            f"{CODEC_SUBDIR}/codec_browser_onnx_meta.json",
        ]
        return [item for item in required if not (self.model_dir / item).is_file()]

    def configured(self) -> bool:
        """配置齐备吗 —— 只看"模型文件在不在"，不加载模型。"""
        return not self.missing_model_files()

    def available(self) -> bool:
        """真的能用吗 —— 模型齐 + 上游依赖能导入。**不得抛异常。**"""
        if not self.configured():
            return False
        try:
            self._ensure_upstream()
        except AIError:
            return False
        return True

    def models(self) -> list[str]:
        return [MODEL_ID] if self.configured() else []

    def describe(self, *, probe: bool = True) -> dict[str, Any]:
        data = super().describe(probe=probe)
        missing = self.missing_model_files()
        data.update({
            "model_id": MODEL_ID,
            "model_dir": str(self.model_dir),
            "model_ready": not missing,
            "missing_files": missing,
            "upstream": {
                "repo": UPSTREAM_REPO,
                "commit": UPSTREAM_COMMIT,
                "version": UPSTREAM_VERSION,
                "private_api": f"{UPSTREAM_CLASS}.{UPSTREAM_PRIVATE_METHOD}",
            },
            "obtain": {
                "command": "python scripts/export_moss_tts_onnx.py",
                "tts": TTS_REPO_URL,
                "codec": CODEC_REPO_URL,
            },
        })
        return data

    # ---------------- 懒加载 ----------------
    def _ensure_upstream(self) -> Any:
        if self._runtime_class is None:
            _module, self._runtime_class = _import_upstream()
        return self._runtime_class

    def _runtime_instance(self) -> Any:
        if self._runtime is None:
            runtime_class = self._ensure_upstream()
            try:
                # 用 `_sentencepiece_ascii_paths()` 包住构造：上游在这个 `__init__`
                # 里就会加载 tokenizer，而 sentencepiece 打不开含中文的路径
                # （夏半仓默认数据根就含中文）。上下文退出后立刻还原。
                with _sentencepiece_ascii_paths():
                    self._runtime = runtime_class(
                        model_dir=str(self.model_dir),
                        thread_count=DEFAULT_CPU_THREADS,
                        execution_provider="cpu",
                    )
            except FileNotFoundError as exc:
                raise self._model_missing_error(str(exc)) from exc
            # **覆写那一个私有方法** —— 这是整条路线 D 的落点
            self._runtime._load_reference_audio = types.MethodType(
                _load_reference_audio, self._runtime
            )
        return self._runtime

    def _model_missing_error(self, detail: str = "") -> AIUnavailable:
        missing = self.missing_model_files()
        lines = [
            f"MOSS-TTS-Nano 模型不可用。",
            f"  模型目录：{self.model_dir}",
        ]
        if missing:
            lines.append(f"  缺失文件：{', '.join(missing)}")
        if detail:
            lines.append(f"  上游报错：{detail}")
        lines += [
            f"  获取方式：在仓库根目录运行  python scripts/export_moss_tts_onnx.py",
            f"    · 语音模型：{TTS_REPO_URL}",
            f"    · 音频分词器：{CODEC_REPO_URL}",
            f"  （脚本会把它们放到 {self.model_dir}，共约 728 MB）",
        ]
        return AIUnavailable("\n".join(lines))

    # ---------------- 能力实现 ----------------
    def synthesize(self, request: SpeechRequest) -> SpeechResult:
        if not self.configured():
            raise self._model_missing_error()

        target = Path(request.output_path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)

        voice = str(request.voice or "").strip()
        prompt_audio: str | None = None
        builtin = ""
        if voice:
            candidate = Path(voice).expanduser()
            if candidate.is_file():
                prompt_audio = str(candidate.resolve())
            else:
                # 不是文件路径 —— 当成上游的内置音色名
                builtin = voice

        runtime = self._runtime_instance()
        try:
            result = runtime.synthesize(
                text=str(request.text),
                voice=builtin,
                prompt_audio_path=prompt_audio,
                output_audio_path=str(target),
                sample_mode=DEFAULT_SAMPLE_MODE,
                do_sample=DEFAULT_DO_SAMPLE,
                streaming=True,
                max_new_frames=DEFAULT_MAX_NEW_FRAMES,
                voice_clone_max_text_tokens=DEFAULT_VOICE_CLONE_MAX_TEXT_TOKENS,
                # ⚠️ WeTextProcessing 是上游的**硬依赖**（`No module named 'tn'` 直接崩），
                #    而它依赖的 pynini 在 Windows 上没有 wheel —— 必须关掉。
                #    改用它自带的 robust 归一化，行为等价。
                enable_wetext=False,
                enable_normalize_tts_text=True,
            )
        except UpstreamContractError:
            raise
        except AssertionError as exc:
            # 空壳被调用 = 上游多了一处 torch 用法，覆写没盖住
            raise UpstreamContractError(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - 统一转成可读的 AIError
            raise AIError(f"MOSS-TTS-Nano 合成失败：{exc}") from exc

        path = str(result.get("audio_path") or target)
        duration, sample_rate, channels = _probe_wav(path)
        return SpeechResult(
            path=path,
            duration=round(duration, 3),
            sample_rate=sample_rate,
            channels=channels,
            voice=voice,
            provider=self.name,
            model=MODEL_ID,
        )

    def clone_voice(self, request: VoiceCloneRequest) -> VoiceProfile:
        """确认参考音频能不能当音色用，返回它的规格与**内容指纹**。

        **不落盘** —— 声音库归插件管（Core 不保存任何声音）。
        这里只做"能不能用"的判定：文件在不在、音频读不读得动、时长够不够。
        """
        import soundfile as sf

        reference = Path(request.reference_audio).expanduser()
        if not reference.is_file():
            raise AIUnavailable(f"参考音频不存在：{reference}")

        try:
            info = sf.info(str(reference))
        except Exception as exc:  # noqa: BLE001 - 交给调用方成为可读错误
            raise AIError(f"参考音频读不出来（{reference}）：{exc}") from exc

        duration = float(info.frames) / float(info.samplerate or 1)
        if duration < MIN_REFERENCE_SECONDS:
            raise AIUnavailable(
                f"参考音频太短：{duration:.2f} 秒（至少 {MIN_REFERENCE_SECONDS:.0f} 秒）。"
                f"文件：{reference}"
            )
        if duration > MAX_REFERENCE_SECONDS:
            raise AIUnavailable(
                f"参考音频太长：{duration:.1f} 秒（上限 {MAX_REFERENCE_SECONDS:.0f} 秒）。"
                f"取其中一段更干净的即可。文件：{reference}"
            )
        if info.channels < 1:
            raise AIUnavailable(f"参考音频没有声道：{reference}")

        digest = hashlib.sha256(reference.read_bytes()).hexdigest()[:16]
        return VoiceProfile(
            name=str(request.name or reference.stem),
            reference_audio=str(reference.resolve()),
            fingerprint=digest,
            duration=round(duration, 3),
            sample_rate=int(info.samplerate),
            channels=int(info.channels),
            provider=self.name,
            model=MODEL_ID,
        )
