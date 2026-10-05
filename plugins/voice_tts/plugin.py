"""语音合成 / 声音克隆插件。

## 它提供什么

1. **一个 Core AI Provider** —— 把本地的 TTS 引擎注册成 `speech` 能力。
   业务代码**永远不直接调它**，只走 `ctx.ai.synthesize(...)` / `ctx.ai.clone_voice(...)`。
2. **声音库** —— "名称 ↔ 参考音频"的档案，落在**插件自己的数据目录**里，
   Core 对此一无所知（Core 不保存任何声音）。

## 引擎按配置选，加引擎不用改本文件

`voice_tts.engine` 决定加载 `providers/<引擎名>.py`：

```json
{ "plugins": { "voice_tts": { "config": { "engine": "moss_tts" } } } }
```

加载约定只有一条：那个模块要暴露 `create_provider()` 工厂。
所以**新增一个引擎 = 加一个文件 + 改一行配置**，本文件一个字节都不用动。

## 为什么用显式加载器而不是 `import providers.moss_tts`

插件加载器会把插件目录**临时**放进 `sys.path`，导入完**立刻移除**（`manager._import_module`）。
于是 `providers` 会以**顶层包名**留在 `sys.modules` 里 —— 而 "providers" 是个很通用的名字，
别的插件若也有同名目录，就会静默拿到**我们这个**模块。

所以这里按文件路径加载，并给模块起一个带插件前缀的唯一名（`xbc_voice_tts_provider_*`）。
路径仍然是任务书要求的 `providers/moss_tts.py`。

## 职责边界（TASK-010 §7 细化）

| 文件 | 身份 | 允许 import |
|---|---|---|
| `providers/*.py` | **模型实现** | 本地推理运行时（onnxruntime / soundfile / soxr …） |
| `plugin.py`（本文件） | **模型使用者** | 只能通过 `ctx.ai.*` |

本文件里没有任何推理运行时的 import —— 它只认识 `ctx.ai`。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from xbc.core.capabilities.ai import AIError
from xbc.core.contract.plugin import XbcPlugin

#: 声音库文件名（存在插件自己的 `ctx.data_dir` 下）
VOICES_FILE = "voices.json"
#: 声音库结构版本 —— 以后要改结构时靠它做迁移
VOICES_VERSION = 1
#: 默认引擎
DEFAULT_ENGINE = "moss_tts"
#: 每个 owner 默认能加几个声音（**可配置，不写死**）
DEFAULT_MAX_VOICES_PER_OWNER = 3


class VoiceTtsPlugin(XbcPlugin):
    """语音合成 + 声音库。"""

    def apply(self, ctx: Any, config: dict) -> None:
        self.ctx = ctx
        self._cfg = dict(config)
        self.log = ctx.logger

        self.engine = str(self._cfg.get("engine") or DEFAULT_ENGINE)
        self.max_voices_per_owner = int(
            self._cfg.get("max_voices_per_owner") or DEFAULT_MAX_VOICES_PER_OWNER
        )
        #: 引擎加载失败的原因（没有就为空串）。**插件不因为引擎起不来就整个不可用** ——
        #: 声音库那些工具不依赖引擎，照样能用；要出声时才给出这句原因。
        self.engine_error = ""

        # 把本地引擎注册成 Core 的一个 AI Provider。
        #
        # **插件只声明"提供一个 speech 能力"，不提任何模型文件**：
        #   · 构造时不传路径 —— 插件不知道模型在哪，也不需要知道
        #   · Core 在 register() 时通过 `bind_models` 注入共享模型目录
        #   · Provider 用自己知道的 MODEL_ID 从那个目录往下解析
        try:
            provider = self._load_engine(self.engine)
            ctx.ai.register(provider)
        except AIError as exc:
            # 引擎起不来（少了推理依赖、配置写错引擎名……）：
            # **把原因记下来并继续**，而不是让整个插件 FAILED ——
            # 否则连"看一眼有哪些声音"都做不到，用户也拿不到原因。
            self.engine_error = str(exc)
            self.log.error("TTS 引擎 %s 起不来：%s", self.engine, exc)

        self._register_tools()

    # ---------------- 引擎加载 ----------------
    def _load_engine(self, engine: str) -> Any:
        """按配置里的引擎名加载 `providers/<engine>.py` 并调它的工厂。"""
        module_path = Path(__file__).resolve().parent / "providers" / f"{engine}.py"
        if not module_path.is_file():
            raise AIError(
                f"voice_tts 配置里的引擎 {engine!r} 没有对应实现：{module_path}\n"
                f"可用的引擎见 {module_path.parent} 目录。"
            )

        # 起一个带插件前缀的唯一模块名 —— 避免和别的插件撞名（见模块说明）
        module_name = f"xbc_voice_tts_provider_{engine}"
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        if spec is None or spec.loader is None:
            raise AIError(f"无法为 {module_path} 建立导入规格")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            sys.modules.pop(module_name, None)
            raise AIError(f"加载 TTS 引擎 {engine!r} 失败：{exc}") from exc

        factory = getattr(module, "create_provider", None)
        if factory is None:
            raise AIError(
                f"{module_path} 没有 create_provider() 工厂 —— "
                f"引擎约定见 plugins/voice_tts/README.md"
            )
        return factory()

    # ---------------- 声音库（存插件自己的数据目录） ----------------
    @property
    def voices_path(self) -> Path:
        return Path(self.ctx.data_dir) / VOICES_FILE

    def _load_voices(self) -> dict[str, Any]:
        path = self.voices_path
        if not path.is_file():
            return {"version": VOICES_VERSION, "owners": {}}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # 声音库坏了不该让整个插件不可用 —— 当成空的，但**说出来**
            self.log.warning("声音库读不出来，按空库处理：%s", path)
            return {"version": VOICES_VERSION, "owners": {}}
        if not isinstance(data, dict):
            return {"version": VOICES_VERSION, "owners": {}}
        data.setdefault("version", VOICES_VERSION)
        data.setdefault("owners", {})
        return data

    def _save_voices(self, data: dict[str, Any]) -> Path:
        path = self.voices_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return path

    @staticmethod
    def _owner_key(owner: str) -> str:
        key = str(owner or "").strip()
        return key or "default"

    def _owner_voices(self, data: dict[str, Any], owner: str) -> list[dict[str, Any]]:
        bucket = data["owners"].setdefault(self._owner_key(owner), {"voices": []})
        bucket.setdefault("voices", [])
        return bucket["voices"]

    # ---------------- 工具注册 ----------------
    def _register_tools(self) -> None:
        ctx = self.ctx
        ctx.tools.register(
            "voice_clone",
            self.voice_clone,
            description=(
                "用一段参考音频登记一个声音（零样本克隆，不训练模型）。"
                "返回该声音的规格与内容指纹"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "reference_audio": {"type": "string", "minLength": 1},
                    "name": {"type": "string"},
                    "owner": {"type": "string"},
                },
                "required": ["reference_audio"],
            },
            risk="write",
        )
        ctx.tools.register(
            "voice_list",
            self.voice_list,
            description="列出某个 owner 已登记的声音，以及还能再加几个",
            input_schema={
                "type": "object",
                "properties": {"owner": {"type": "string"}},
            },
            risk="read",
        )
        ctx.tools.register(
            "voice_remove",
            self.voice_remove,
            description="删掉某个 owner 的一个声音（只删档案，不动参考音频文件）",
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "minLength": 1},
                    "owner": {"type": "string"},
                },
                "required": ["name"],
            },
            risk="write",
        )
        ctx.tools.register(
            "voice_speak",
            self.voice_speak,
            description="用已登记的声音把文本合成成音频文件（走 Core 的 speech 能力）",
            input_schema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "minLength": 1},
                    "output_path": {"type": "string", "minLength": 1},
                    "voice": {"type": "string"},
                    "owner": {"type": "string"},
                },
                "required": ["text", "output_path"],
            },
            risk="write",
        )

    # ---------------- 工具实现 ----------------
    def voice_clone(
        self, reference_audio: str, name: str = "", owner: str = ""
    ) -> dict[str, Any]:
        """登记一个声音。

        **校验走 Core 能力**（`ctx.ai.clone_voice`），本文件不碰任何推理运行时。
        """
        data = self._load_voices()
        voices = self._owner_voices(data, owner)
        if len(voices) >= self.max_voices_per_owner:
            raise AIError(
                f"owner {self._owner_key(owner)!r} 的声音已达上限 "
                f"{self.max_voices_per_owner} 个（可在配置 voice_tts.max_voices_per_owner 调整）。"
                f"现有：{[v['name'] for v in voices]}"
            )

        profile = self.ctx.ai.clone_voice(reference_audio, name=name or "")
        label = str(name or profile.name or Path(reference_audio).stem)

        replaced = False
        for index, existing in enumerate(voices):
            if existing.get("name") == label:
                voices[index] = self._record(profile, label)
                replaced = True
                break
        if not replaced:
            voices.append(self._record(profile, label))

        path = self._save_voices(data)
        return {
            "name": label,
            "owner": self._owner_key(owner),
            "replaced": replaced,
            "total": len(voices),
            "remaining": max(0, self.max_voices_per_owner - len(voices)),
            "voices_path": str(path),
            **profile.to_dict(),
        }

    @staticmethod
    def _record(profile: Any, label: str) -> dict[str, Any]:
        return {
            "name": label,
            "reference_audio": profile.reference_audio,
            "fingerprint": profile.fingerprint,
            "duration": profile.duration,
            "sample_rate": profile.sample_rate,
            "channels": profile.channels,
            "provider": profile.provider,
            "model": profile.model,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }

    def voice_list(self, owner: str = "") -> dict[str, Any]:
        data = self._load_voices()
        voices = self._owner_voices(data, owner)
        return {
            "owner": self._owner_key(owner),
            "count": len(voices),
            "limit": self.max_voices_per_owner,
            "remaining": max(0, self.max_voices_per_owner - len(voices)),
            "voices": [dict(v) for v in voices],
            "voices_path": str(self.voices_path),
            "engine": self.engine,
            # 引擎起不来的原因在这里也报一次 —— 不然用户只能靠"点了没反应"猜
            "engine_error": self.engine_error,
        }

    def voice_remove(self, name: str, owner: str = "") -> dict[str, Any]:
        data = self._load_voices()
        voices = self._owner_voices(data, owner)
        before = len(voices)
        voices[:] = [v for v in voices if v.get("name") != name]
        removed = before - len(voices)
        if removed:
            self._save_voices(data)
        return {
            "owner": self._owner_key(owner),
            "name": name,
            "removed": removed,
            "count": len(voices),
            "remaining": max(0, self.max_voices_per_owner - len(voices)),
        }

    def voice_speak(
        self, text: str, output_path: str, voice: str = "", owner: str = ""
    ) -> dict[str, Any]:
        """用已登记的声音合成。

        `voice` 传名字就用那个声音的参考音频；留空则用该 owner 的第一个声音。
        """
        data = self._load_voices()
        voices = self._owner_voices(data, owner)

        reference = ""
        if voice:
            found = next((v for v in voices if v.get("name") == voice), None)
            if found is None:
                raise AIError(
                    f"owner {self._owner_key(owner)!r} 里没有名为 {voice!r} 的声音；"
                    f"现有：{[v['name'] for v in voices]}"
                )
            reference = str(found.get("reference_audio") or "")
        elif voices:
            reference = str(voices[0].get("reference_audio") or "")

        result = self.ctx.ai.synthesize(text, output_path=output_path, voice=reference)
        return {
            "owner": self._owner_key(owner),
            "voice": voice or (voices[0]["name"] if voices else ""),
            **result.to_dict(),
        }
