"""模型 Provider 抽象（TASK-007 第 2 项）。

插件只依赖 `AIService`（见 service.py），**永远不直接依赖 Provider**。
Provider 是内核的装配细节：换一个实现，插件一行都不用改。

Provider 需要声明自己支持哪些能力（`capabilities`），
`AIService` 据此做路由 —— 这样"某个 Provider 不支持 embedding"不会等到调用时才炸，
选 Provider 的那一刻就能给出清楚的理由。

这里同时提供两个共用工具：

- `encode_image()`：把图片路径或字节转成 base64（视觉能力的共同前置）
- `post_json()` / `get_json()`：统一的 HTTP 调用与错误翻译，
  让每个 Provider 不必各写一遍超时、错误码、非 JSON 响应的处理
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from .types import (
    AIUnavailable,
    AIUnsupported,
    AIResponseError,
    AICapability,
    EmbeddingResult,
    TextResult,
)


class AIProvider(ABC):
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
    def configured(self) -> bool:
        """配置是否齐备。**不看服务在不在**，也**不联网** —— 必须便宜。"""
        return True

    def models(self) -> list[str]:
        """已知模型列表（用于诊断；拿不到就返回空）。"""
        return []

    def default_model(self, capability: AICapability) -> str:
        """该能力默认用哪个模型。"""
        return ""

    def text_generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        model: str | None = None,
        json_mode: bool = False,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> TextResult:
        raise AIUnsupported(f"Provider {self.name!r} 不支持文本生成（text_generate）")

    def vision_analyze(
        self,
        prompt: str,
        images: list[Any],
        *,
        system: str | None = None,
        model: str | None = None,
        json_mode: bool = False,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> TextResult:
        raise AIUnsupported(f"Provider {self.name!r} 不支持视觉理解（vision_analyze）")

    def embedding(self, texts: list[str], *, model: str | None = None) -> EmbeddingResult:
        raise AIUnsupported(f"Provider {self.name!r} 不支持向量化（embedding）")

    # ---------- 公共 ----------
    def supports(self, capability: AICapability) -> bool:
        return capability in self.capabilities

    def describe(self, *, probe: bool = True) -> dict[str, Any]:
        """给诊断用的自述。

        `probe=False` 时**不联网**，只报告结构信息（配置是否齐备、支持哪些能力）。
        这个区分很重要：状态查询会被放在热路径上（插件列表、界面刷新），
        在那里做网络探测会让整个界面卡住几秒。
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
        return f"<AIProvider {self.name} capabilities={sorted(str(c) for c in self.capabilities)}>"


# ---------------- 共用工具 ----------------


def encode_image(source: Any) -> str:
    """把图片（路径 / bytes）转成 base64 字符串；WebP/JPEG/PNG 都按原始字节处理。"""
    if isinstance(source, (bytes, bytearray)):
        return base64.b64encode(bytes(source)).decode("ascii")
    path = Path(str(source))
    if not path.is_file():
        raise AIResponseError(f"图片文件不存在：{path}")
    return base64.b64encode(path.read_bytes()).decode("ascii")


def image_data_url(source: Any) -> str:
    """把图片转成 data URL（OpenAI 兼容接口用这个格式）。"""
    suffix = ".jpeg"
    if not isinstance(source, (bytes, bytearray)):
        suffix = Path(str(source)).suffix.lower() or ".jpeg"
    mime = {".png": "image/png", ".webp": "image/webp", ".gif": "image/gif"}.get(suffix, "image/jpeg")
    return f"data:{mime};base64,{encode_image(source)}"


def base_url(url: str) -> str:
    """把可能带端点的 URL 规范化成基址。

    兼容老配置：V1 之前的 `ai.ollama.url` 形如 `http://localhost:11434/api/generate`。

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
        with urllib.request.urlopen(request, timeout=timeout) as response:
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
    """探测服务是否在跑。**任何失败都返回 False，不抛异常**（可用性探测不该炸）。"""
    try:
        urllib.request.urlopen(urllib.request.Request(url), timeout=timeout).close()
        return True
    except Exception:  # noqa: BLE001 - 探测失败就是不可用
        return False


__all__ = [
    "AIProvider",
    "base_url",
    "encode_image",
    "get_json",
    "image_data_url",
    "probe_available",
    "request_json",
]
