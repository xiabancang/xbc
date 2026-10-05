"""TASK-013 语音合成 / 声音克隆的测试。

分四层：

| 类 | 覆盖 | 需要什么 |
|---|---|---|
| `SpeechCapabilityTests` | Core 的 `speech` 能力：枚举、请求/结果、路由、默认不支持 | 无（假 Provider） |
| `UpstreamContractGuardTests` | **路线 D 的守卫**：上游私有方法不在时必须响亮报错 | 无 |
| `NonAsciiPathTests` | sentencepiece 非 ASCII 路径的绕行逻辑 | 无 |
| `VoiceLibraryTests` | 插件侧声音库：登记 / 列表 / 上限 / 删除 | 真插件（缺依赖则跳过） |
| `PluginPurityTests` | 业务代码不碰推理运行时；自检插件不认识引擎 | 无（ast 扫描） |
| `MossProviderTests` | 模型缺失时的错误内容、真机合成 | 真模型（缺则跳过） |
"""

from __future__ import annotations

import ast
import json
import shutil
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
PLUGINS_DIR = REPO_ROOT / "plugins"
for path in (str(REPO_ROOT), str(SRC_DIR), str(PLUGINS_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from xbc.core.capabilities.ai import (  # noqa: E402
    AICapability,
    AIError,
    AIService,
    AIUnsupported,
    ModelProvider,
    SpeechRequest,
    SpeechResult,
    VoiceCloneRequest,
    VoiceProfile,
)

VOICE_TTS_DIR = PLUGINS_DIR / "voice_tts"
VOICE_TEST_DIR = PLUGINS_DIR / "voice_test_plugin"
MOSS_PROVIDER = VOICE_TTS_DIR / "providers" / "moss_tts.py"

#: 推理运行时 / 引擎名 —— 业务代码里不许出现
RUNTIME_TOKENS = (
    "onnxruntime", "soundfile", "soxr", "sentencepiece",
    "moss_tts", "moss_tts_nano", "onnx_tts_runtime", "ort_cpu_runtime",
    "torch", "torchaudio",
)


def _runtime_imports(path: Path) -> list[str]:
    """ast 扫描：这个文件 import 了哪些推理运行时。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        for name in names:
            root = name.split(".")[0]
            if root in RUNTIME_TOKENS:
                found.append(f"{path.name}: import {name}")
    return found


# ================= Core 能力 =================


class _FakeSpeechProvider(ModelProvider):
    name = "fake_speech"
    capabilities = frozenset({AICapability.SPEECH})

    def __init__(self) -> None:
        self.seen: dict = {}

    def available(self) -> bool:
        return True

    def synthesize(self, request: SpeechRequest) -> SpeechResult:
        self.seen["speech"] = request
        return SpeechResult(
            path=request.output_path, duration=1.25, sample_rate=48000,
            channels=2, voice=request.voice, provider=self.name, model="fake",
        )

    def clone_voice(self, request: VoiceCloneRequest) -> VoiceProfile:
        self.seen["clone"] = request
        return VoiceProfile(
            name=request.name or "x", reference_audio=request.reference_audio,
            fingerprint="deadbeef", duration=3.5, sample_rate=44100,
            channels=1, provider=self.name, model="fake",
        )


class SpeechCapabilityTests(unittest.TestCase):
    def test_speech_is_a_capability(self) -> None:
        self.assertEqual(AICapability.SPEECH.value, "speech")
        self.assertEqual(str(AICapability.SPEECH), "speech")
        self.assertEqual(AICapability("speech"), AICapability.SPEECH)

    def test_base_provider_refuses_speech_by_default(self) -> None:
        """没实现的能力要抛可读错误，而不是静默给空结果。"""
        class Bare(ModelProvider):
            name = "bare"
            capabilities = frozenset({AICapability.SPEECH})

            def available(self) -> bool:
                return True

        bare = Bare()
        with self.assertRaises(AIUnsupported):
            bare.synthesize(SpeechRequest(text="x", output_path="y"))
        with self.assertRaises(AIUnsupported):
            bare.clone_voice(VoiceCloneRequest(reference_audio="r"))

    def test_service_routes_speech_by_capability(self) -> None:
        service = AIService()
        provider = _FakeSpeechProvider()
        service.register(provider, default=True)

        result = service.synthesize("你好", output_path="out.wav", voice="张三")
        self.assertEqual(result.path, "out.wav")
        self.assertEqual(result.sample_rate, 48000)
        self.assertEqual(provider.seen["speech"].text, "你好")
        self.assertEqual(provider.seen["speech"].voice, "张三")

        profile = service.clone_voice("ref.wav", name="张三")
        self.assertEqual(profile.fingerprint, "deadbeef")
        self.assertEqual(provider.seen["clone"].reference_audio, "ref.wav")

    def test_a_non_speech_provider_is_not_selected(self) -> None:
        """只有 embedding 能力的 Provider 不该被语音请求选中。"""

        class OnlyEmbedding(ModelProvider):
            name = "only_embedding"
            capabilities = frozenset({AICapability.EMBEDDING})

            def available(self) -> bool:
                return True

        service = AIService()
        service.register(OnlyEmbedding(), default=True)
        with self.assertRaises(AIUnsupported):
            service.synthesize("x", output_path="y")

    def test_results_carry_measured_audio_facts(self) -> None:
        speech = SpeechResult(path="a.wav", duration=2.5, sample_rate=48000,
                              channels=2, voice="v")
        payload = speech.to_dict()
        for key in ("path", "duration", "sample_rate", "channels", "voice"):
            self.assertIn(key, payload)

        profile = VoiceProfile(name="n", reference_audio="r", fingerprint="f",
                               duration=3.0, sample_rate=44100, channels=1)
        payload = profile.to_dict()
        for key in ("name", "reference_audio", "fingerprint", "duration"):
            self.assertIn(key, payload)

    def test_speech_provider_is_listed_in_describe(self) -> None:
        provider = _FakeSpeechProvider()
        described = provider.describe(probe=False)
        self.assertIn("speech", described["capabilities"])


# ================= 路线 D 的守卫 =================


def _has_numpy() -> bool:
    try:
        import numpy  # noqa: F401
    except ImportError:
        return False
    return True


HAS_NUMPY = _has_numpy()


@unittest.skipUnless(MOSS_PROVIDER.is_file(), "MOSS Provider 文件不在")
@unittest.skipUnless(HAS_NUMPY, "缺 numpy（Provider 模块顶层 import 它）")
class UpstreamContractGuardTests(unittest.TestCase):
    """**TASK-013 决定 D 的守卫要求**：上游私有方法不存在 → 明确报错，不静默。"""

    @classmethod
    def setUpClass(cls) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "xbc_test_moss_provider", MOSS_PROVIDER)
        cls.module = importlib.util.module_from_spec(spec)
        sys.modules["xbc_test_moss_provider"] = cls.module
        try:
            spec.loader.exec_module(cls.module)
        except AIError:
            # 上游真变了 —— 那正是守卫在响，留给下面那条测试断言
            cls.module = None

    def setUp(self) -> None:
        if self.module is None:
            self.skipTest("Provider 模块导入时守卫已响（见 test_module_import_guard）")

    def test_declares_which_private_api_and_which_upstream_version(self) -> None:
        """要求 1：**记录依赖了哪个私有方法、上游版本号**。"""
        module = self.module
        self.assertEqual(module.UPSTREAM_PRIVATE_METHOD, "_load_reference_audio")
        self.assertEqual(module.UPSTREAM_CLASS, "OnnxTtsRuntime")
        self.assertEqual(module.UPSTREAM_MODULE, "onnx_tts_runtime")
        self.assertIn("OpenMOSS/MOSS-TTS-Nano", module.UPSTREAM_REPO)
        # 上游版本号：commit + pyproject 版本，两个都要有
        self.assertRegex(module.UPSTREAM_COMMIT, r"^[0-9a-f]{40}$")
        self.assertTrue(module.UPSTREAM_VERSION)
        self.assertRegex(module.UPSTREAM_METHOD_FILE_SHA256_16, r"^[0-9a-f]{16}$")

    def test_guard_passes_when_the_method_exists(self) -> None:
        class HasIt:
            def _load_reference_audio(self, path):  # noqa: D401
                return path

        self.module.assert_upstream_contract(HasIt)  # 不抛

    def test_guard_raises_when_the_method_is_gone(self) -> None:
        """要求 2：上游改了方法名 → **明确报错，不静默**。"""

        class Renamed:
            def _load_reference_audio_v2(self, path):
                return path

        with self.assertRaises(self.module.UpstreamContractError) as caught:
            self.module.assert_upstream_contract(Renamed)

        message = str(caught.exception)
        # 报错要能指明：哪个方法、哪个上游、以及回退路径
        self.assertIn("_load_reference_audio", message)
        self.assertIn("路线 D", message)
        self.assertIn("docs/research/README.md", message)
        # 回退路径必须写出来
        self.assertIn("路线 C", message)
        self.assertIn("路线 A", message)

    def test_guard_error_is_an_ai_error(self) -> None:
        """守卫错误要能被能力层的统一错误处理接住。"""
        self.assertTrue(issubclass(self.module.UpstreamContractError, AIError))

    def test_torch_stubs_are_gone_after_import(self) -> None:
        """空壳只在导入那一刻存在 —— 之后进程里不该有假 torch。"""
        import importlib

        module = self.module
        # 触发一次真实的导入路径（若上游没装则跳过）
        try:
            module._import_upstream()
        except AIError as exc:
            self.skipTest(f"上游不可用：{exc}")

        for name in ("torch", "torchaudio"):
            found = sys.modules.get(name)
            if found is not None:
                self.assertIsNot(
                    found, getattr(module, "_STUB_MARKER", object()),
                    f"导入之后 {name} 仍然是空壳",
                )
        # 空壳标志：真 torch 会有 __file__，空壳没有
        import importlib.util as u

        spec = u.find_spec("torch") if importlib.util else None
        if sys.modules.get("torch") is not None:
            self.assertTrue(
                getattr(sys.modules["torch"], "__file__", None) is not None
                or sys.modules["torch"].__name__ == "torch",
            )

    def test_stub_functions_raise_if_ever_called(self) -> None:
        """空壳里那两个函数**故意会抛** —— 上游多出 torch 用法时要当场炸。"""
        with self.assertRaises(AssertionError):
            self.module._stub_never_called()


# ================= 非 ASCII 路径绕行 =================


@unittest.skipUnless(MOSS_PROVIDER.is_file(), "MOSS Provider 文件不在")
@unittest.skipUnless(HAS_NUMPY, "缺 numpy（Provider 模块顶层 import 它）")
class NonAsciiPathTests(unittest.TestCase):
    """sentencepiece 打不开含中文的路径 —— 绕行逻辑本身要可测、可还原。"""

    @classmethod
    def setUpClass(cls) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "xbc_test_moss_provider2", MOSS_PROVIDER)
        cls.module = importlib.util.module_from_spec(spec)
        sys.modules["xbc_test_moss_provider2"] = cls.module
        spec.loader.exec_module(cls.module)

    def test_ascii_path_is_returned_untouched(self) -> None:
        path = Path(r"C:\moss\models\tokenizer.model")
        self.assertEqual(self.module._stage_non_ascii_model_file(path), path)

    def test_non_ascii_path_gets_staged_to_an_ascii_copy(self) -> None:
        with tempfile.TemporaryDirectory(prefix="xbc-cjk-") as tmp:
            source_dir = Path(tmp) / "夏半仓工具箱"
            source_dir.mkdir(parents=True, exist_ok=True)
            source = source_dir / "tokenizer.model"
            source.write_bytes(b"fake-tokenizer-bytes")

            staged = self.module._stage_non_ascii_model_file(source)
            self.assertNotEqual(staged, source)
            self.assertTrue(str(staged).isascii(), f"暂存路径仍是非 ASCII：{staged}")
            self.assertTrue(staged.is_file())
            self.assertEqual(staged.read_bytes(), source.read_bytes())

            # 第二次调用复用同一份，不重复复制
            again = self.module._stage_non_ascii_model_file(source)
            self.assertEqual(again, staged)

    def test_staging_root_is_ascii_and_writable(self) -> None:
        root = self.module._ascii_staging_root()
        self.assertTrue(str(root).isascii())
        self.assertTrue(root.is_dir())

    def test_sentencepiece_patcher_restores_the_original(self) -> None:
        """改道只在上下文内生效，退出立刻还原 —— 不留全局副作用。"""
        try:
            import sentencepiece as spm
        except ImportError:
            self.skipTest("没装 sentencepiece")

        original = spm.SentencePieceProcessor
        with self.module._sentencepiece_ascii_paths():
            self.assertIsNot(spm.SentencePieceProcessor, original)
        self.assertIs(spm.SentencePieceProcessor, original)


# ================= 插件：声音库 =================


def _has_voice_deps() -> bool:
    return HAS_NUMPY and _has_soundfile()


def _has_soundfile() -> bool:
    try:
        import soundfile  # noqa: F401
    except ImportError:
        return False
    return True


HAS_VOICE_DEPS = _has_voice_deps()


@unittest.skipUnless(HAS_VOICE_DEPS, "缺 numpy / soundfile")
class VoiceLibraryTests(unittest.TestCase):
    def setUp(self) -> None:
        from xbc.core.context import AppContext
        from xbc.core.runtime.manager import PluginManager

        self.root = Path(tempfile.mkdtemp(prefix="xbc-voice-lib-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.ctx = AppContext.create(root=self.root, console=False,
                                     approver=lambda *_: True)
        self.addCleanup(self.ctx.close)
        self.manager = PluginManager(self.ctx, [PLUGINS_DIR], self.ctx.logger)
        self.manager.discover()
        self.manager.activate("voice_tts")
        record = self.manager.get("voice_tts")
        self.assertEqual(record.state.value, "active", record.error)

        # 造一段**真的能读**的音频当参考（1.5 秒 48k 单声道静音）
        import numpy as np
        import soundfile as sf

        self.reference = self.root / "reference.wav"
        sf.write(str(self.reference), np.zeros(72000, dtype="float32"), 48000)

    def call(self, tool: str, **arguments):
        return self.ctx.tool_registry.call(tool, arguments)

    def test_clone_registers_and_lists(self) -> None:
        result = self.call("voice_clone", owner="甲公司", name="老板",
                           reference_audio=str(self.reference))
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.value["total"], 1)
        self.assertEqual(result.value["remaining"], 2)

        listed = self.call("voice_list", owner="甲公司")
        self.assertTrue(listed.ok)
        self.assertEqual(listed.value["count"], 1)
        self.assertEqual(listed.value["voices"][0]["name"], "老板")

    def test_voice_record_lives_in_plugin_data_dir_not_core(self) -> None:
        self.call("voice_clone", owner="甲公司", name="老板",
                  reference_audio=str(self.reference))
        path = Path(self.call("voice_list", owner="甲公司").value["voices_path"])
        self.assertTrue(path.is_file())
        # 落在**插件自己的**数据目录下（Core 不知道"声音库"这回事）
        self.assertEqual(path.parent.name, "voice_tts")
        self.assertIn("data", path.parts)

    def test_limit_per_owner_is_configurable_and_not_hardcoded(self) -> None:
        """默认 3 个；**上限来自配置**，不是写死的常量。"""
        plugin = self.manager.get("voice_tts").instance
        self.assertEqual(plugin.max_voices_per_owner, 3)

        for index in range(3):
            result = self.call("voice_clone", owner="甲公司",
                               name=f"声音{index}", reference_audio=str(self.reference))
            self.assertTrue(result.ok, result.message)

        over = self.call("voice_clone", owner="甲公司", name="第四个",
                         reference_audio=str(self.reference))
        self.assertFalse(over.ok)
        self.assertIn("上限", over.message)

    def test_owners_are_counted_separately(self) -> None:
        """每公司 3 个 —— 甲公司满了不影响乙公司。"""
        for index in range(3):
            self.call("voice_clone", owner="甲公司", name=f"A{index}",
                      reference_audio=str(self.reference))
        result = self.call("voice_clone", owner="乙公司", name="B0",
                           reference_audio=str(self.reference))
        self.assertTrue(result.ok, result.message)
        self.assertEqual(self.call("voice_list", owner="乙公司").value["count"], 1)
        self.assertEqual(self.call("voice_list", owner="甲公司").value["count"], 3)

    def test_same_name_replaces_instead_of_counting_against_the_limit(self) -> None:
        self.call("voice_clone", owner="甲公司", name="老板",
                  reference_audio=str(self.reference))
        again = self.call("voice_clone", owner="甲公司", name="老板",
                          reference_audio=str(self.reference))
        self.assertTrue(again.value["replaced"])
        self.assertEqual(again.value["total"], 1, "同名应覆盖，不该占两个名额")

    def test_remove_frees_a_slot(self) -> None:
        self.call("voice_clone", owner="甲公司", name="老板",
                  reference_audio=str(self.reference))
        removed = self.call("voice_remove", owner="甲公司", name="老板")
        self.assertEqual(removed.value["removed"], 1)
        self.assertEqual(removed.value["remaining"], 3)

    def test_missing_reference_is_refused(self) -> None:
        result = self.call("voice_clone", owner="甲公司", name="没有的",
                           reference_audio=str(self.root / "nope.wav"))
        self.assertFalse(result.ok)
        self.assertIn("不存在", result.message)

    def test_too_short_reference_is_refused(self) -> None:
        import numpy as np
        import soundfile as sf

        short = self.root / "short.wav"
        sf.write(str(short), np.zeros(4800, dtype="float32"), 48000)  # 0.1 秒
        result = self.call("voice_clone", owner="甲公司", name="太短",
                           reference_audio=str(short))
        self.assertFalse(result.ok)
        self.assertIn("太短", result.message)

    def test_speaking_an_unknown_voice_is_refused(self) -> None:
        result = self.call("voice_speak", owner="甲公司", voice="不存在的",
                           text="你好", output_path=str(self.root / "o.wav"))
        self.assertFalse(result.ok)
        self.assertIn("没有名为", result.message)


# ================= 插件纯度（ast 扫描） =================


class PluginPurityTests(unittest.TestCase):
    """验收 4/5：业务代码不碰推理运行时；自检插件不认识任何引擎。"""

    def test_voice_test_plugin_has_no_runtime_imports(self) -> None:
        offenders: list[str] = []
        for path in sorted(VOICE_TEST_DIR.rglob("*.py")):
            offenders.extend(_runtime_imports(path))
        self.assertEqual(offenders, [], "自检插件里出现了推理运行时：\n" + "\n".join(offenders))

    def test_voice_test_plugin_never_names_an_engine(self) -> None:
        """连**字符串**里都不许出现引擎名 —— 换个引擎它一个字节都不该改。"""
        for path in sorted(VOICE_TEST_DIR.rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
            # 只看真实的字符串字面量，注释与文档字符串除外会误伤 —— 所以扫文档字符串
            docstrings = set()
            for node in ast.walk(tree):
                if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
                    body = getattr(node, "body", [])
                    if body and isinstance(body[0], ast.Expr) and isinstance(
                            body[0].value, ast.Constant):
                        docstrings.add(body[0].value.value)
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if node.value in docstrings:
                        continue
                    for token in ("moss", "onnx", "soxr", "soundfile", "torch"):
                        self.assertNotIn(
                            token, node.value.lower(),
                            f"{path.name} 的字符串里出现了引擎名 {token!r}：{node.value[:60]!r}",
                        )

    def test_voice_tts_business_file_is_clean(self) -> None:
        """`plugin.py` 是**模型使用者**，不许 import 推理运行时。"""
        offenders = _runtime_imports(VOICE_TTS_DIR / "plugin.py")
        self.assertEqual(offenders, [], "\n".join(offenders))

    def test_providers_directory_is_where_the_runtime_lives(self) -> None:
        """反过来：Provider 文件**应当**是唯一 import 推理运行时的地方。"""
        offenders = _runtime_imports(MOSS_PROVIDER)
        self.assertTrue(offenders, "moss_tts.py 里没有推理运行时 import？结构可能变了")

    def test_all_plugin_sources_stay_clean_of_remote_sdks(self) -> None:
        """全 plugins/ 的远端 SDK / HTTP 扫描（与 test_ai_capability 的口径一致）。"""
        forbidden = ("openai", "anthropic", "litellm", "requests", "httpx", "aiohttp")
        offenders: list[str] = []
        for path in sorted(PLUGINS_DIR.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    if name.split(".")[0] in forbidden:
                        offenders.append(f"{path.relative_to(REPO_ROOT)}: import {name}")
        self.assertEqual(offenders, [], "\n".join(offenders))

    def test_core_speech_has_no_business_vocabulary(self) -> None:
        """验收 5：Core 里不许出现业务词。

        **一条已知的、与业务无关的既有命中**：`provider.py` 讲系统代理时写了
        "Clash / 公司网关" —— 那里的"公司"是**网络代理**含义，不是夏半仓的业务域。
        这条命中**早于** TASK-013，本次不改它（不在范围内），但要**明确记下来**，
        不能靠放宽词表来"通过"。
        """
        business = ("老板", "公司", "配音", "年会", "客服")
        known_unrelated = {"provider.py: 公司"}  # 见上面的说明
        offenders: list[str] = []
        for name in ("types.py", "request.py", "provider.py", "service.py"):
            path = SRC_DIR / "xbc" / "core" / "capabilities" / "ai" / name
            text = path.read_text(encoding="utf-8")
            for word in business:
                if word in text:
                    offenders.append(f"{name}: {word}")
        self.assertEqual(
            [item for item in offenders if item not in known_unrelated], [],
            "Core 的 AI 能力层出现了业务词：\n" + "\n".join(offenders),
        )


# ================= MOSS Provider（缺依赖/缺模型则跳过）=================


def _moss_ready() -> bool:
    if not HAS_VOICE_DEPS:
        return False
    try:
        import onnxruntime  # noqa: F401
        import sentencepiece  # noqa: F401
        import soxr  # noqa: F401
    except ImportError:
        return False
    return True


@unittest.skipUnless(_moss_ready(), "缺 onnxruntime / sentencepiece / soxr")
class MossProviderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "xbc_test_moss_provider3", MOSS_PROVIDER)
        cls.module = importlib.util.module_from_spec(spec)
        sys.modules["xbc_test_moss_provider3"] = cls.module
        spec.loader.exec_module(cls.module)

    def make_provider(self, models_dir: Path):
        provider = self.module.MossSpeechProvider()
        provider.bind_models(models_dir)
        return provider

    def test_model_dir_is_resolved_under_the_shared_models_dir(self) -> None:
        """验收 6：模型放 `AppPaths.models_dir/moss-tts-nano/`，路径由 Core 注入。"""
        with tempfile.TemporaryDirectory(prefix="xbc-models-") as tmp:
            provider = self.make_provider(Path(tmp))
            self.assertEqual(provider.model_dir, Path(tmp) / "moss-tts-nano")
            self.assertTrue(str(provider.tts_dir).endswith("MOSS-TTS-Nano-100M-ONNX"))
            self.assertTrue(str(provider.codec_dir).endswith("MOSS-Audio-Tokenizer-Nano-ONNX"))

    def test_missing_model_error_is_actionable(self) -> None:
        """验收 7：错误要含**路径 + 缺失文件 + 获取方式**。"""
        with tempfile.TemporaryDirectory(prefix="xbc-models-") as tmp:
            provider = self.make_provider(Path(tmp))
            self.assertFalse(provider.configured())
            self.assertFalse(provider.available())

            missing = provider.missing_model_files()
            self.assertEqual(len(missing), 4)

            with self.assertRaises(AIError) as caught:
                provider.synthesize(
                    SpeechRequest(text="x", output_path=str(Path(tmp) / "o.wav")))
            message = str(caught.exception)
            self.assertIn(str(provider.model_dir), message, "错误里要有模型目录")
            for item in missing:
                self.assertIn(item, message, f"错误里要说清缺 {item}")
            self.assertIn("export_moss_tts_onnx.py", message, "错误里要给出获取方式")
            self.assertIn("huggingface.co", message)

    def test_describe_reports_upstream_contract(self) -> None:
        with tempfile.TemporaryDirectory(prefix="xbc-models-") as tmp:
            provider = self.make_provider(Path(tmp))
            described = provider.describe(probe=False)
            self.assertEqual(described["upstream"]["private_api"],
                             "OnnxTtsRuntime._load_reference_audio")
            self.assertEqual(described["upstream"]["commit"],
                             self.module.UPSTREAM_COMMIT)
            self.assertFalse(described["model_ready"])
            self.assertEqual(len(described["missing_files"]), 4)

    def test_real_model_synthesizes_and_reports_measured_facts(self) -> None:
        """真机：能出声、返回的规格与文件实际一致（模型不在则跳过）。"""
        root = Path.home() / "AppData" / "Local" / "夏半仓工具箱"
        models_dir = root / "models"
        if not (models_dir / "moss-tts-nano").is_dir():
            self.skipTest("本机没放 moss-tts-nano 模型")
        reference = (REPO_ROOT / "reports" / "task-013-samples"
                     / "A1_原声参考_未知文本.wav")
        if not reference.is_file():
            self.skipTest("没有可用的参考音频")

        provider = self.make_provider(models_dir)
        if not provider.configured():
            self.skipTest("模型文件不齐")

        profile = provider.clone_voice(
            VoiceCloneRequest(reference_audio=str(reference), name="测试"))
        self.assertRegex(profile.fingerprint, r"^[0-9a-f]{16}$")
        self.assertGreater(profile.duration, 0)

        with tempfile.TemporaryDirectory(prefix="xbc-tts-out-") as tmp:
            target = Path(tmp) / "out.wav"
            speech = provider.synthesize(SpeechRequest(
                text="这是一句用于验证的中文。", output_path=str(target),
                voice=str(reference)))
            self.assertTrue(target.is_file())
            with wave.open(str(target), "rb") as handle:
                actual_rate = handle.getframerate()
                actual_channels = handle.getnchannels()
                actual_duration = handle.getnframes() / float(actual_rate)
            self.assertEqual(speech.sample_rate, actual_rate)
            self.assertEqual(speech.channels, actual_channels)
            self.assertAlmostEqual(speech.duration, actual_duration, places=1)
            self.assertGreater(actual_duration, 0.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
