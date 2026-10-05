"""AI 能力门面（Core Capability）。

## 插件唯一能用的 AI 入口

业务插件**不允许**直接调模型、直接发 HTTP、直接 import 某个模型的 SDK。
它们只能通过 `ctx.ai`（本对象）说话：

```python
result = ctx.ai.text_generate("写一句自我介绍")
vision = ctx.ai.vision_analyze(["D:/pic.png"])
vectors = ctx.ai.embedding(["第一段", "第二段"])
```

这样做的实际收益（不是口号）：

1. **换模型不改插件** —— Provider 是装配细节，配置里换个名字就行；
2. 超时、密钥、重试策略、错误形态都在一处，不必每个插件重写；
3. 出问题时能说清"是谁、用什么模型、生成了什么"。

## 装配：显式，不做注册表

`build_ai_service()` 只认两个名字 —— `ollama` 与 `openai_compatible`。
加第三个厂商要改这一处装配代码，但**不改任何插件**。

## 未配置 / 不可用时：明确报错，不静默失败

`build_ai_service()` 在"一个 Provider 都没装配起来"时，会把**具体原因**
记在服务上；此后调用任一能力都会抛出带 **provider 名、原因、检查建议** 的
`AIUnavailable`，而不是返回空值让调用方猜。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .providers import OllamaProvider, OpenAICompatibleProvider
from .request import EmbeddingRequest, ImageEmbeddingRequest, TextRequest, VisionRequest
from .types import (
    AICapability,
    AIError,
    AIUnavailable,
    AIUnsupported,
    EmbeddingResult,
    SpeechResult,
    TextResult,
    VisionAnswer,
    VisionResult,
    VoiceProfile,
)
from .request import (  # noqa: F401  —— 门面要用到
    EmbeddingRequest,
    ImageEmbeddingRequest,
    SpeechRequest,
    TextRequest,
    VisionRequest,
    VoiceCloneRequest,
)

Disposer = Callable[[], Any]

#: 内核实现的 Provider 名字。加厂商要改这里（以及下面 build_ai_service 的分支）。
KNOWN_PROVIDERS = ("ollama", "openai_compatible")


class AIService:
    """AI 能力门面。一个进程一份，由 `AppContext` 装配。"""

    def __init__(
        self,
        logger: Any = None,
        configuration_error: str = "",
        models_dir: Path | str | None = None,
    ) -> None:
        self._providers: dict[str, Any] = {}
        self._default: str | None = None
        self._log = logger
        #: "为什么一个 Provider 都没装配起来"，供报错时使用
        self._configuration_error = configuration_error
        #: **Core 级共享模型目录**。Provider 不该自己拼路径，插件更不该。
        self._models_dir = Path(models_dir) if models_dir else None

    # ---------------- 装配 ----------------
    def register(self, provider: Any, *, default: bool = False) -> Disposer:
        """注册一个 Provider。返回 disposer（可挂到作用域上）。

        注册时会把**共享模型目录**交给 Provider（`bind_models`）——
        这就是"模型路径由 Core / 注册机制解析"的落点：
        **插件不需要知道模型在哪，甚至不需要知道有这个模型。**
        """
        if not provider.name:
            raise AIError("Provider 必须有 name")
        if self._models_dir is not None and hasattr(provider, "bind_models"):
            provider.bind_models(self._models_dir)
        self._providers[provider.name] = provider
        if default or self._default is None:
            self._default = provider.name
        return lambda: self._providers.pop(provider.name, None)

    @property
    def models_dir(self) -> Path:
        """共享模型目录。**插件不该直接用它拼路径**，交给 Provider 的 `bind_models`。"""
        return self._models_dir or Path("models")

    def model_dir(self, model_id: str) -> Path:
        """解析某个模型在共享目录下的位置。

        路径拼接是 Core 的职责 —— 换一处存放位置只改这里，不用改任何 Provider 或插件。
        """
        return self.models_dir / str(model_id)

    @property
    def default_provider(self) -> str:
        return self._default or ""

    def providers(self) -> list[str]:
        return sorted(self._providers)

    def provider(self, name: str | None = None, *, capability: AICapability | None = None) -> Any:
        """取一个 Provider。指定 `capability` 时会校验它确实支持该能力。"""
        if not self._providers:
            raise AIUnavailable(
                self._configuration_error
                or "没有可用的 AI Provider。检查建议：在 config.json 里设置 ai.provider。"
            )

        if name is not None:
            found = self._providers.get(name)
            if found is None:
                raise AIUnavailable(
                    f"没有名为 {name!r} 的 Provider；已装配: {self.providers()}"
                )
            if capability is not None and not found.supports(capability):
                raise AIUnsupported(
                    f"Provider {name!r} 不支持 {capability}；"
                    f"支持 {capability} 的有: {self._providers_for(capability)}"
                )
            return found

        if capability is None:
            if self._default and self._default in self._providers:
                return self._providers[self._default]
            raise AIUnavailable(f"没有可用的 AI Provider；已装配: {self.providers()}")

        candidates = self._providers_for(capability)
        if not candidates:
            raise AIUnsupported(
                f"没有 Provider 支持 {capability}；已装配: {self.providers()}"
            )
        if self._default in candidates:
            return self._providers[self._default]
        return self._providers[candidates[0]]

    def _providers_for(self, capability: AICapability) -> list[str]:
        return sorted(name for name, p in self._providers.items() if p.supports(capability))

    # ---------------- 诊断 ----------------
    def capabilities(self, name: str | None = None) -> list[str]:
        provider = self.provider(name)
        return sorted(str(c) for c in provider.capabilities)

    def supports(self, capability: AICapability | str, provider: str | None = None) -> bool:
        want = capability if isinstance(capability, AICapability) else AICapability(str(capability))
        try:
            return self.provider(provider).supports(want)
        except AIError:
            return False

    def status(self, *, probe: bool = False) -> dict[str, dict[str, Any]]:
        """每个 Provider 的能力与状态。

        **默认不联网**（`probe=False`）：只报告"装配了谁、各自支持什么、配置齐不齐"。
        需要真实可用性时显式传 `probe=True`（`doctor` 就是这么做的）。

        早先的版本默认做活性探测，一次状态查询要 4 秒 —— 状态查询会被放在热路径上
        （插件列表、界面刷新），这个代价不能接受。
        """
        report: dict[str, dict[str, Any]] = {}
        for name, provider in sorted(self._providers.items()):
            try:
                report[name] = provider.describe(probe=probe)
            except Exception as exc:  # noqa: BLE001 - 诊断不该因为一个 Provider 崩掉
                report[name] = {
                    "name": name, "available": False, "configured": False,
                    "capabilities": [], "models": [], "error": str(exc),
                }
            report[name]["default"] = name == self._default
        return report

    def available(self, capability: AICapability | None = None) -> bool:
        """**会真的连一下服务**。明确问"现在能不能用"时才调用。"""
        try:
            return bool(self.provider(capability=capability).available())
        except AIError:
            return False

    # ---------------- 能力入口 ----------------
    def text_generate(self, prompt: str, *, system: str | None = None) -> TextResult:
        """文本生成。输入：提示词 + 可选 system。"""
        chosen = self.provider(capability=AICapability.TEXT)
        return chosen.text_generate(TextRequest(prompt=prompt, system=system))

    def vision_analyze(
        self, images: list[str] | str, *, question: str | None = None
    ) -> VisionResult | VisionAnswer:
        """视觉理解。输入：**本地图片路径**（单个或一组）。

        只接受本地路径 —— 不接受 base64、不接受 URL。传错形态会明确报错。

        两种模式，返回两种类型：

        - 不传 `question` → `VisionResult`（`description` + `labels`），
          行为与 TASK-007 完全一致；
        - 传 `question` → `VisionAnswer`（`answer` + `labels`），
          用调用方的问题去问这张图。
        """
        paths = [images] if isinstance(images, str) else list(images)
        chosen = self.provider(capability=AICapability.VISION)
        return chosen.vision_analyze(
            VisionRequest(images=[str(p) for p in paths], question=question)
        )

    def embedding(self, texts: list[str] | str) -> EmbeddingResult:
        """文本向量化。输入：一段或多段文本。

        **返回值不可跨 Provider / 跨模型混用** —— 详见 `types.py` 开头那一段。
        """
        items = [texts] if isinstance(texts, str) else list(texts)
        chosen = self.provider(capability=AICapability.EMBEDDING)
        return chosen.embedding(EmbeddingRequest(texts=[str(t) for t in items]))

    def embed_images(self, images: list[str] | str) -> EmbeddingResult:
        """图片向量化。输入：**本地图片路径**（单个或一组）。

        只接受本地路径 —— 与 `vision_analyze` 同一条规矩，传 base64 / URL 会明确报错。

        与 `embedding()` 是**两种能力**，按 `AICapability.IMAGE_EMBEDDING` 路由 ——
        一个只做文本嵌入的 Provider（如 `nomic-embed-text`）不会被误选，
        而是抛出可读的 `AIUnsupported`。

        **返回的向量与文本向量属于不同向量空间，绝对不可互相比较。**
        """
        items = [images] if isinstance(images, str) else list(images)
        chosen = self.provider(capability=AICapability.IMAGE_EMBEDDING)
        return chosen.embed_images(
            ImageEmbeddingRequest(images=[str(p) for p in items])
        )

    def synthesize(self, text: str, *, output_path: str, voice: str = "") -> SpeechResult:
        """语音合成。输入：文本 + 输出路径 + 可选音色。

        `voice` 留空时用实现方的默认音色。产物写到 `output_path`，
        返回值里有**实测**的时长 / 采样率 / 声道数。

        按 `AICapability.SPEECH` 路由 —— 只会向量化的 Provider 不会被误选，
        而是抛出可读的 `AIUnsupported`。
        """
        chosen = self.provider(capability=AICapability.SPEECH)
        return chosen.synthesize(
            SpeechRequest(text=str(text), output_path=str(output_path), voice=str(voice))
        )

    def clone_voice(self, reference_audio: str, *, name: str = "") -> VoiceProfile:
        """音色登记。输入：**本地参考语音路径**。

        只接受本地路径 —— 与 `vision_analyze` / `embed_images` 同一条规矩。

        **Core 不保存任何声音**：这个方法只确认"这段语音能不能当音色用"，
        返回规格与指纹；声音库由调用方（插件）自己落盘。
        """
        chosen = self.provider(capability=AICapability.SPEECH)
        return chosen.clone_voice(
            VoiceCloneRequest(reference_audio=str(reference_audio), name=str(name))
        )


def build_ai_service(
    config: Any,
    logger: Any = None,
    secrets: Any = None,
    models_dir: Path | str | None = None,
) -> AIService:
    """按配置装配 AI 服务。**显式两个分支，没有注册表。**

    `models_dir` 是**共享模型目录**（`<数据根>/models`）。它由 Core 从路径层传入，
    注册 Provider 时会通过 `bind_models` 交给实现方 ——
    插件与 Provider 都**不自己拼模型路径**。

    配置形状：

    ```json
    {
      "ai": {
        "provider": "ollama",
        "model": "",
        "embedding_model": "",
        "options": {},
        "ollama": { "url": "http://127.0.0.1:11434" },
        "openai_compatible": { "base_url": "", "api_key_secret": "" }
      }
    }
    ```

    任一环节不成立都会得到一个**能说清原因**的服务，而不是静默的空服务。
    """
    provider_name = str(config.get("ai.provider", "") or "").strip()
    model = str(config.get("ai.model", "") or "").strip()
    embedding_model = str(config.get("ai.embedding_model", "") or "").strip()
    options = dict(config.get("ai.options", {}) or {})
    timeout = int(options.get("timeout", 0) or 0)

    if not provider_name:
        return AIService(
            logger=logger,
            models_dir=models_dir,
            configuration_error=(
                "未配置 AI Provider\n"
                "原因：config.json 的 ai.provider 为空\n"
                "检查建议：把 ai.provider 设为 ollama（本地）或 openai_compatible（API）。"
            ),
        )

    if provider_name not in KNOWN_PROVIDERS:
        return AIService(
            logger=logger,
            models_dir=models_dir,
            configuration_error=(
                f"ai.provider 配置有误\n"
                f"原因：ai.provider = {provider_name!r}，内核未实现该 Provider\n"
                f"检查建议：改为 {' 或 '.join(KNOWN_PROVIDERS)} 之一。"
            ),
        )

    service = AIService(logger=logger, models_dir=models_dir)

    if provider_name == "ollama":
        ollama_cfg = dict(config.get("ai.ollama", {}) or {})
        url = str(ollama_cfg.get("url", "") or "http://127.0.0.1:11434")
        service.register(
            OllamaProvider(
                url=url,
                model=model,
                embedding_model=embedding_model,
                timeout=timeout or 180,
                options=options,
                logger=logger,
            ),
            default=True,
        )
        return service

    # openai_compatible
    api_cfg = dict(config.get("ai.openai_compatible", {}) or {})
    base = str(api_cfg.get("base_url", "") or "").strip()
    if not base:
        return AIService(
            logger=logger,
            models_dir=models_dir,
            configuration_error=(
                "provider=openai_compatible 配置不完整\n"
                "原因：未配置 ai.openai_compatible.base_url，不知道要连哪个服务\n"
                "检查建议：填上服务基址（例如 https://api.deepseek.com/v1），"
                "并在 secrets.json 里放好 API Key；或把 ai.provider 改回 ollama。"
            ),
        )

    secret_name = str(api_cfg.get("api_key_secret", "") or "")
    api_key = ""
    if secrets is not None and secret_name:
        api_key = secrets.get_secret(secret_name) or ""

    service.register(
        OpenAICompatibleProvider(
            base_url_value=base,
            model=model,
            embedding_model=embedding_model,
            api_key=api_key,
            timeout=timeout or 120,
            logger=logger,
        ),
        default=True,
    )
    return service
