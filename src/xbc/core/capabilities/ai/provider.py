"""模型 Provider 抽象。

插件只依赖 `AIService`，**永远不直接依赖 Provider**。Provider 是内核的装配细节：
换一个实现，插件一行都不用改。

Provider 声明自己支持哪些能力（`capabilities`），`AIService` 据此路由 ——
"某个 Provider 不支持 embedding"不会等到调用时才炸，选 Provider 的那一刻就能说清理由。

三类方法接收**请求对象**（`TextRequest` / `VisionRequest` / `EmbeddingRequest`），
返回**结果对象**（`TextResult` / `VisionResult` / `EmbeddingResult`）。

## 视觉能力的输出契约

`vision_analyze` 必须返回 `VisionResult`（`description` + `labels`），**不是字符串**。
这意味着每个视觉 Provider 都要负责把模型的自由文本收敛成这个结构 ——
把提示词工程留在内核这一层，而不是推给每一个插件。
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from .request import (
    EmbeddingRequest,
    ImageEmbeddingRequest,
    SpeechRequest,
    TextRequest,
    VisionRequest,
    VoiceCloneRequest,
)
from .types import (
    AICapability,
    AIError,
    AIResponseError,
    AIUnavailable,
    AIUnsupported,
    EmbeddingResult,
    SpeechResult,
    TextResult,
    VisionResult,
    VoiceProfile,
)


class ModelProvider(ABC):
    """模型 Provider 基类。

    子类必须给出 `name` 与 `capabilities`，并实现 `available()`；
    不支持的能力方法**不必覆盖** —— 基类会抛出可读的 `AIUnsupported`。
    """

    #: Provider 名（配置里的 `ai.provider` 用它选择）
    name: str = ""
    #: 支持的能力集合
    capabilities: frozenset[AICapability] = frozenset()

    # ---------- 必须实现 ----------
    @abstractmethod
    def available(self) -> bool:
        """当前是否真的可用（服务在跑、凭证齐备）。**不得抛异常**。"""

    # ---------- 可选覆盖 ----------
    def bind_models(self, models_dir: Path) -> None:
        """Core 在注册时告诉 Provider：**共享模型目录**在哪。

        这就是"模型路径解析由 Core / 注册机制负责"的落点：

        - **插件不知道任何模型路径**，它只注册 Provider；
        - **Provider 自己知道需要哪个模型**，从 `models_dir` 往下解析；
        - 换存放位置只改 Core 一处。

        默认实现什么都不做 —— 不依赖本地模型的 Provider（如走 HTTP 的 Ollama）
        不需要覆盖它。需要本地模型的实现应该把它存下来，供后续使用。
        """

    def configured(self) -> bool:
        """配置是否齐备。**不看服务在不在**，也**不联网** —— 必须便宜。"""
        return True

    def models(self) -> list[str]:
        """已知模型列表（用于诊断；拿不到就返回空）。"""
        return []

    def text_generate(self, request: TextRequest) -> TextResult:
        raise AIUnsupported(f"Provider {self.name!r} 不支持文本生成（text_generate）")

    def vision_analyze(self, request: VisionRequest) -> Any:
        """视觉理解。

        不传 `request.question` → 返回 `VisionResult`（`description` + `labels`）；
        传了 → 返回 `VisionAnswer`（`answer` + `labels`）。

        实现必须把 `len(request.images)` 传给 `vision_prompt()`，
        否则多张图片仍会收到"这张图片"的单数提示词 —— 接口与提示词就不自洽了。
        """
        raise AIUnsupported(f"Provider {self.name!r} 不支持视觉理解（vision_analyze）")

    def embedding(self, request: EmbeddingRequest) -> EmbeddingResult:
        raise AIUnsupported(f"Provider {self.name!r} 不支持向量化（embedding）")

    def embed_images(self, request: ImageEmbeddingRequest) -> EmbeddingResult:
        """图片向量化。

        与 `embedding()` 是**两种能力**（`AICapability.IMAGE_EMBEDDING` /
        `AICapability.EMBEDDING`）。返回的是同一个 `EmbeddingResult` 结构，
        但**图片向量与文本向量属于不同向量空间，不可互相比较**。

        实现方可以只支持其中一种：不支持时这里的默认实现会抛出可读的 `AIUnsupported`，
        调用方要在选 Provider 的那一刻就能得到理由，而不是等到算出结果才发现不兼容。
        """
        raise AIUnsupported(f"Provider {self.name!r} 不支持图片向量化（embed_images）")

    def synthesize(self, request: SpeechRequest) -> SpeechResult:
        """语音合成：把 `request.text` 用 `request.voice` 的音色念出来，写到 `output_path`。

        返回的 `SpeechResult` 里必须带上**实测**的 `duration` / `sample_rate` /
        `channels`，不能按文本长度估算 —— 调用方要靠它做音画对齐（TASK-014）。
        """
        raise AIUnsupported(f"Provider {self.name!r} 不支持语音合成（synthesize）")

    def clone_voice(self, request: VoiceCloneRequest) -> VoiceProfile:
        """音色登记：确认这段参考语音能不能当音色用，返回它的规格与指纹。

        **不落盘** —— 声音库归调用方管（见 `VoiceProfile` 的说明）。
        """
        raise AIUnsupported(f"Provider {self.name!r} 不支持音色登记（clone_voice）")

    # ---------- 公共 ----------
    def supports(self, capability: AICapability) -> bool:
        return capability in self.capabilities

    def describe(self, *, probe: bool = True) -> dict[str, Any]:
        """给诊断用的自述。

        `probe=False` 时**不联网**，只报告结构信息（配置是否齐备、支持哪些能力）。
        状态查询会被放在热路径上（插件列表、界面刷新），在那里做网络探测会让界面卡几秒。
        """
        data: dict[str, Any] = {
            "name": self.name,
            "capabilities": sorted(str(c) for c in self.capabilities),
            "configured": bool(self.configured()),
            "available": None,  # 未探测
            "models": [],
        }
        if probe:
            data["available"] = bool(self.available())
            data["models"] = self.models()[:20]
        return data

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<ModelProvider {self.name} capabilities={sorted(str(c) for c in self.capabilities)}>"


# ---------------- 视觉结果的解析 ----------------

#: 视觉能力的**内置抽取提示词**。
#:
#: 调用方不能自定义它（第一版不做可配置提示词）—— 目的就是让"视觉理解"这件事
#: 在所有插件之间行为一致：同一张图，谁来问都得到同构的 description + labels。
_VISION_PROMPT = (
    "请分析这张图片，并**只**输出一个 JSON 对象，不要输出任何其他文字或代码块标记。\n"
    '格式：{"description": "对画面的客观描述", "labels": ["标签1", "标签2"]}\n'
    "要求：description 用一到三句话描述画面内容；labels 给出 3 到 8 个简短标签。"
)

#: 多张图片时的描述提示词。
#:
#: 接口收的是列表，提示词就必须能表达"多张" —— 否则调用方读不出
#: "多张图是各自分析还是合成一次判断"。这里把语义定死：**同一场景的不同视角或时刻，综合成一次判断**。
_VISION_PROMPT_MULTI = (
    "请综合分析这 __COUNT__ 张图片（同一场景的不同视角或时刻），"
    "并**只**输出一个 JSON 对象，不要输出任何其他文字或代码块标记。\n"
    '格式：{"description": "对画面的整体描述", "labels": ["标签1", "标签2"]}\n'
    "要求：description 用一到三句话描述这些图片**共同**呈现的内容；"
    "labels 给出 3 到 8 个覆盖它们共性的简短标签。"
)

#: 定向提问模式的提示词前缀。问题原文接在后面。
#:
#: 用拼接而不是 `str.format()`：调用方的问题里可能出现花括号，
#: 而提示词里本来就有 JSON 的花括号，两者混在一起必然出错。
_VISION_QUESTION_PROMPT_HEAD = (
    "请根据这张图片回答问题，并**只**输出一个 JSON 对象，不要输出任何其他文字或代码块标记。\n"
    '格式：{"answer": "对问题的回答", "labels": ["标签1", "标签2"]}\n'
    "要求：answer 直接回答下面的问题；labels 给出 3 到 8 个与画面相关的简短标签。\n"
    "问题："
)

#: 多张图片时的定向提问前缀。语义同上：综合成一次判断。
_VISION_QUESTION_PROMPT_MULTI_HEAD = (
    "请根据这 __COUNT__ 张图片（同一场景的不同视角或时刻）回答问题，"
    "并**只**输出一个 JSON 对象，不要输出任何其他文字或代码块标记。\n"
    '格式：{"answer": "对问题的回答", "labels": ["标签1", "标签2"]}\n'
    "要求：answer 综合这些图片直接回答问题；"
    "labels 给出 3 到 8 个与画面相关的简短标签。\n"
    "问题："
)


def vision_prompt(question: str | None = None, *, image_count: int = 1) -> str:
    """按模式与**图片张数**给出内置提示词。

    - 不给 `question`：描述模式，要求输出 `description` + `labels`
    - 给了 `question`：定向提问模式，要求输出 `answer` + `labels`
    - `image_count > 1`：换用"综合分析这几张图"的措辞，与 `images` 列表自洽

    单张的措辞与之前逐字相同 —— 这次只补上"多张"这一种情况的表达。
    """
    count = max(1, int(image_count))
    multi = count > 1
    if question:
        head = _VISION_QUESTION_PROMPT_MULTI_HEAD if multi else _VISION_QUESTION_PROMPT_HEAD
    else:
        head = _VISION_PROMPT_MULTI if multi else _VISION_PROMPT

    text = head.replace("__COUNT__", str(count))
    return f"{text}{question}" if question else text


def parse_vision_payload(text: str, *, field: str = "description") -> tuple[str, list[str]]:
    """把模型输出解析成 `(正文, labels)`。

    `field` 指定正文取 JSON 里的哪个键：描述模式用 `description`（默认），
    定向提问模式用 `answer`。默认值保证 TASK-007 的调用行为完全不变。

    **容错优先**：模型没给出合法 JSON 时，把整段文本当作正文，labels 置空 ——
    而不是抛错让整条链路失败。视觉是"给人的信息"，降级比失败更有用。
    """
    raw = (text or "").strip()
    if not raw:
        return "", []

    candidate = raw
    # 容忍 ```json ... ``` 包裹
    if candidate.startswith("```"):
        candidate = candidate.strip("`")
        if candidate.lower().startswith("json"):
            candidate = candidate[4:]
        candidate = candidate.strip()
    # 容忍前后有解释性文字：截取第一个 { 到最后一个 }
    start, end = candidate.find("{"), candidate.rfind("}")
    if start != -1 and end > start:
        candidate = candidate[start : end + 1]

    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        return raw, []

    if not isinstance(data, dict):
        return raw, []

    body = data.get(field)
    if not isinstance(body, str) or not body.strip():
        body = raw

    labels: list[str] = []
    raw_labels = data.get("labels")
    if isinstance(raw_labels, list):
        for item in raw_labels:
            text_item = str(item).strip()
            if text_item and text_item not in labels:
                labels.append(text_item)

    return body.strip(), labels


# ---------------- 共用工具 ----------------


def encode_image(source: Any) -> str:
    """把**本地图片文件**读成 base64。

    第一版只支持本地路径：传原始字节、http(s) URL 或 `data:` URL 都会明确报错，
    而不是悄悄接受一种"看起来也行"的形态。
    """
    if isinstance(source, (bytes, bytearray)):
        raise AIError("vision_analyze 只接受本地图片路径，不接受原始字节")

    text = str(source or "").strip()
    lowered = text.lower()
    if lowered.startswith(("http://", "https://")):
        raise AIError(f"vision_analyze 只接受本地图片路径，不接受网络 URL：{text[:80]}")
    if lowered.startswith("data:"):
        raise AIError("vision_analyze 只接受本地图片路径，不接受 base64 data URL")

    file = Path(text)
    if not file.is_file():
        raise AIError(f"图片文件不存在：{file}")
    return base64.b64encode(file.read_bytes()).decode("ascii")


def image_data_url(source: Any) -> str:
    """把本地图片转成 data URL（OpenAI 兼容接口用这个格式）。"""
    suffix = Path(str(source)).suffix.lower() or ".jpeg"
    mime = {".png": "image/png", ".webp": "image/webp", ".gif": "image/gif"}.get(suffix, "image/jpeg")
    return f"data:{mime};base64,{encode_image(source)}"


def base_url(url: str) -> str:
    """把可能带端点的 URL 规范化成基址。

    兼容老配置：更早版本的 `ai.ollama.url` 形如 `http://localhost:11434/api/generate`。

    **不要剥掉 `/v1`** —— OpenAI 兼容服务的基址惯例就是带 `/v1` 的
    （`https://api.openai.com/v1`）。早先的实现把它一起剥了，
    结果请求打到 `/chat/completions` 上直接 404。
    """
    text = str(url or "").strip().rstrip("/")
    if not text:
        return ""
    if "/api/" in text:
        text = text.split("/api/", 1)[0].rstrip("/")
    for suffix in ("/chat/completions", "/embeddings"):
        if text.endswith(suffix):
            text = text[: -len(suffix)].rstrip("/")
    return text


def _decode(response: Any) -> dict[str, Any]:
    raw = response.read().decode("utf-8", errors="replace")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AIResponseError(f"模型服务返回的不是 JSON：{raw[:200]!r}") from exc
    if not isinstance(data, dict):
        raise AIResponseError(f"模型服务返回的 JSON 不是对象：{raw[:200]!r}")
    return data


#: 本地地址绝不走系统代理
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0", "[::1]"}
_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_SYSTEM_OPENER = urllib.request.build_opener()


def opener_for(url: str) -> Any:
    """按地址挑选 opener：**本地服务绕过系统代理**。

    Windows 上装了代理（Clash / 公司网关）时，`getproxies()` 会返回系统代理，
    而它未必把 `127.0.0.1` 放进 no_proxy —— 结果"连本地模型"的请求会绕到代理上，
    轻则变慢、重则直接失败。对一个**本地优先**的产品这是致命的，
    所以这里对回环地址显式绕过。
    """
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return _DIRECT_OPENER if host in _LOOPBACK_HOSTS else _SYSTEM_OPENER


def request_json(
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 120,
) -> dict[str, Any]:
    """统一的 HTTP 调用：超时、HTTP 错误、非 JSON 响应全部翻译成可读的 AI 错误。"""
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with opener_for(url).open(request, timeout=timeout) as response:
            return _decode(response)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:  # noqa: BLE001 - 读错误体失败不影响报错
            pass
        raise AIResponseError(f"模型服务返回 HTTP {exc.code}：{detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise AIUnavailable(f"连不上模型服务 {url}：{exc.reason}") from exc
    except TimeoutError as exc:
        raise AIUnavailable(f"调用模型服务超时（{timeout}s）：{url}") from exc
    except OSError as exc:
        raise AIUnavailable(f"调用模型服务失败（{url}）：{exc}") from exc


def get_json(url: str, *, timeout: int = 5) -> dict[str, Any]:
    return request_json(url, timeout=timeout)


def probe_available(url: str, timeout: int = 2) -> bool:
    """探测服务是否在跑。**任何失败都返回 False，不抛异常**（可用性探测不该炸）。

    注意：在部分机器上，连一个**没人监听**的端口不会立刻被拒绝，而是挂到超时
    （本机实测 2.0s）。所以调用方不要把它放进每次请求的必经路径 ——
    真正的失败由 `request_json` 直接给出，更准也更快。
    """
    try:
        opener_for(url).open(urllib.request.Request(url), timeout=timeout).close()
        return True
    except Exception:  # noqa: BLE001 - 探测失败就是不可用
        return False


__all__ = [
    "ModelProvider",
    "base_url",
    "encode_image",
    "get_json",
    "image_data_url",
    "opener_for",
    "parse_vision_payload",
    "probe_available",
    "request_json",
    "vision_prompt",
]
