"""AI 能力层测试（TASK-007）。

## 怎么测才可信

**不依赖真的 Ollama，也不连任何云端。** 测试里起一个**本地假模型服务**
（标准库 `http.server`），同时实现两套协议：

- Ollama 形状：`GET /api/tags`、`POST /api/generate`、`POST /api/embed`
- OpenAI 兼容形状：`POST /v1/chat/completions`、`POST /v1/embeddings`

这样 Provider 的请求构造、响应解析、错误处理都能被确定性地验证，
而且恰好让"**换 Provider 不换插件**"这条验收可以被真的跑一遍。
"""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from xbc.core.capabilities.ai import (  # noqa: E402
    AICapability,
    AIError,
    AIService,
    AIUnavailable,
    AIUnsupported,
    EmbeddingResult,
    OllamaProvider,
    OpenAICompatibleProvider,
    TextResult,
)
from xbc.core.capabilities.ai.openai_compat import OpenAICompatibleProvider as OAIProvider  # noqa: E402
from xbc.core.capabilities.ai.provider import base_url  # noqa: E402
from xbc.core.config.layers import write_json  # noqa: E402
from xbc.core.context import AppContext  # noqa: E402
from xbc.core.paths import AppPaths  # noqa: E402
from xbc.core.runtime.manager import PluginManager  # noqa: E402

PLUGINS_DIR = REPO_ROOT / "plugins"

#: 1x1 透明 PNG —— 用来验证图片编码，不依赖任何素材文件
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class MockModelServer:
    """本地假模型服务：记录收到的请求，按固定内容回应。"""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.text_reply = "这是假模型的回答"
        self.embedding = [0.1, 0.2, 0.3, 0.4]
        self.fail_with: int | None = None
        self.fail_body: dict | None = None
        self.disable_embed_endpoint = False
        self.broken_json = False

        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "MockModel/1.0"

            def log_message(self, *args) -> None:  # 静音
                return

            def _json(self, payload: dict, status: int = 200) -> None:
                body = (
                    b"{ not json" if owner.broken_json
                    else json.dumps(payload).encode("utf-8")
                )
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/api/tags":
                    self._json({"models": [{"name": "mock-text:latest"}, {"name": "mock-embed:latest"}]})
                else:
                    self._json({"error": "not found"}, status=404)

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", 0) or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    payload = json.loads(raw or b"{}")
                except json.JSONDecodeError:
                    payload = {}
                owner.requests.append(
                    {"path": self.path, "payload": payload, "headers": dict(self.headers)}
                )
                if owner.fail_with is not None:
                    self._json(owner.fail_body or {"error": "boom"}, status=owner.fail_with)
                    return

                if self.path == "/api/generate":
                    self._json({
                        "response": owner.text_reply,
                        "model": payload.get("model"),
                        "eval_count": 7,
                        "prompt_eval_count": 11,
                        "done_reason": "stop",
                    })
                elif self.path == "/api/embed":
                    if owner.disable_embed_endpoint:
                        # 模拟老版本 Ollama：没有这个端点
                        self._json({"error": "404 page not found"}, status=404)
                        return
                    inputs = payload.get("input") or []
                    self._json({"embeddings": [list(owner.embedding) for _ in inputs]})
                elif self.path == "/api/embeddings":
                    self._json({"embedding": list(owner.embedding)})
                elif self.path.endswith("/chat/completions"):
                    self._json({
                        "model": payload.get("model"),
                        "choices": [{
                            "message": {"role": "assistant", "content": owner.text_reply},
                            "finish_reason": "stop",
                        }],
                        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
                    })
                elif self.path.endswith("/embeddings"):
                    items = payload.get("input") or []
                    if isinstance(items, str):
                        items = [items]
                    self._json({
                        "model": payload.get("model"),
                        "data": [
                            {"index": i, "embedding": list(owner.embedding)}
                            for i in range(len(items))
                        ],
                    })
                else:
                    self._json({"error": f"unknown path {self.path}"}, status=404)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_port}"

    def last(self, path_contains: str = "") -> dict:
        for item in reversed(self.requests):
            if path_contains in item["path"]:
                return item
        raise AssertionError(f"没有收到匹配 {path_contains!r} 的请求：{[r['path'] for r in self.requests]}")

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class _ServerBase(unittest.TestCase):
    def setUp(self) -> None:
        self.server = MockModelServer()
        self.addCleanup(self.server.stop)


# ================= 接口契约 =================


class StubProvider:
    """最小可用的假 Provider，用于测路由（不发网络请求）。"""

    def __init__(self, name: str, capabilities: set[AICapability], **kwargs) -> None:
        self.name = name
        self.capabilities = frozenset(capabilities)
        self.kwargs = kwargs

    def available(self) -> bool:
        return self.kwargs.get("available", True)

    def configured(self) -> bool:
        return self.kwargs.get("configured", True)

    def supports(self, capability) -> bool:
        return capability in self.capabilities

    def describe(self, *, probe: bool = True) -> dict:
        return {
            "name": self.name,
            "capabilities": sorted(str(c) for c in self.capabilities),
            "configured": self.configured(),
            "available": bool(self.available()) if probe else None,
            "models": ["stub"] if probe else [],
        }

    def text_generate(self, prompt, **kwargs) -> TextResult:
        return TextResult(text=f"[{self.name}] {prompt}", provider=self.name, model="stub")

    def embedding(self, texts, **kwargs) -> EmbeddingResult:
        return EmbeddingResult(vectors=[[0.0] * 3 for _ in texts], provider=self.name, model="stub")


class InterfaceTests(unittest.TestCase):
    def test_register_and_default(self) -> None:
        service = AIService()
        service.register(StubProvider("first", {AICapability.TEXT}))
        service.register(StubProvider("second", {AICapability.EMBEDDING}))

        self.assertEqual(service.providers(), ["first", "second"])
        self.assertEqual(service.default_provider, "first", "第一个注册的成为默认")

        service.set_default("second")
        self.assertEqual(service.default_provider, "second")

    def test_register_with_default_flag(self) -> None:
        service = AIService()
        service.register(StubProvider("a", {AICapability.TEXT}))
        service.register(StubProvider("b", {AICapability.TEXT}), default=True)
        self.assertEqual(service.default_provider, "b")

    def test_disposer_unregisters(self) -> None:
        service = AIService()
        dispose = service.register(StubProvider("temp", {AICapability.TEXT}))
        self.assertIn("temp", service.providers())
        dispose()
        self.assertNotIn("temp", service.providers())

    def test_unknown_provider_lists_known_ones(self) -> None:
        service = AIService()
        service.register(StubProvider("known", {AICapability.TEXT}))
        with self.assertRaises(AIUnavailable) as ctx:
            service.provider("nope")
        self.assertIn("known", str(ctx.exception))

    def test_capability_routing_skips_provider_without_capability(self) -> None:
        """默认 Provider 不支持某能力时，应自动找支持的，而不是失败。"""
        service = AIService()
        service.register(StubProvider("text_only", {AICapability.TEXT}))
        service.register(StubProvider("embed_only", {AICapability.EMBEDDING}))

        self.assertEqual(service.provider(capability=AICapability.TEXT).name, "text_only")
        self.assertEqual(service.provider(capability=AICapability.EMBEDDING).name, "embed_only")

        result = service.embedding(["x"])
        self.assertEqual(result.provider, "embed_only")

    def test_missing_capability_is_a_clear_error(self) -> None:
        service = AIService()
        service.register(StubProvider("text_only", {AICapability.TEXT}))
        with self.assertRaises(AIUnsupported) as ctx:
            service.vision_analyze("看图", [b"x"])
        message = str(ctx.exception)
        self.assertIn("vision", message)

    def test_explicit_provider_without_capability_is_rejected(self) -> None:
        service = AIService()
        service.register(StubProvider("text_only", {AICapability.TEXT}))
        with self.assertRaises(AIUnsupported):
            service.embedding(["x"], provider="text_only")

    def test_status_does_not_touch_network(self) -> None:
        """状态查询必须便宜：默认不联网（曾经默认探测，一次要 4 秒）。"""

        class ExplodingProvider(StubProvider):
            def available(self) -> bool:
                raise AssertionError("status(probe=False) 不应调用 available()")

            def models(self):
                raise AssertionError("status(probe=False) 不应调用 models()")

        service = AIService()
        service.register(ExplodingProvider("boom", {AICapability.TEXT}))
        report = service.status()
        self.assertIsNone(report["boom"]["available"], "未探测时 available 应为 None")
        self.assertTrue(report["boom"]["configured"])

    def test_status_with_probe_reports_availability(self) -> None:
        service = AIService()
        service.register(StubProvider("ok", {AICapability.TEXT}, available=True, ))
        self.assertTrue(service.status(probe=True)["ok"]["available"])

    def test_text_result_json(self) -> None:
        result = TextResult(text='{"a": 1}')
        self.assertEqual(result.json(), {"a": 1})

        from xbc.core.capabilities.ai import AIResponseError

        with self.assertRaises(AIResponseError):
            TextResult(text="不是 JSON").json()

    def test_embedding_result_shape(self) -> None:
        result = EmbeddingResult(vectors=[[1.0, 2.0], [3.0, 4.0]], provider="p", model="m")
        self.assertEqual(result.count, 2)
        self.assertEqual(result.dimensions, 2)
        self.assertEqual(result.to_dict()["dimensions"], 2)


class BaseUrlTests(unittest.TestCase):
    def test_strips_ollama_legacy_endpoint(self) -> None:
        self.assertEqual(base_url("http://localhost:11434/api/generate"), "http://localhost:11434")
        self.assertEqual(base_url("http://localhost:11434/api/embed"), "http://localhost:11434")

    def test_keeps_openai_v1_suffix(self) -> None:
        """`/v1` 是 OpenAI 兼容服务的基址惯例，剥掉会让请求 404。"""
        self.assertEqual(base_url("https://api.example.com/v1"), "https://api.example.com/v1")
        self.assertEqual(base_url("https://api.example.com/v1/"), "https://api.example.com/v1")

    def test_strips_full_endpoint(self) -> None:
        self.assertEqual(
            base_url("https://api.example.com/v1/chat/completions"), "https://api.example.com/v1"
        )


# ================= Ollama Provider =================


class OllamaProviderTests(_ServerBase):
    def provider(self, **kwargs) -> OllamaProvider:
        defaults = dict(
            url=self.server.url,
            model="mock-text",
            embedding_model="mock-embed",
            timeout=10,
            options={"num_ctx": 4096},
        )
        defaults.update(kwargs)
        return OllamaProvider(**defaults)

    def test_available_when_server_up(self) -> None:
        self.assertTrue(self.provider().available())

    def test_unavailable_when_server_down(self) -> None:
        dead = OllamaProvider(url="http://127.0.0.1:1", timeout=2)
        self.assertFalse(dead.available())

    def test_models(self) -> None:
        self.assertEqual(self.provider().models(), ["mock-text:latest", "mock-embed:latest"])

    def test_text_generate_payload_and_result(self) -> None:
        result = self.provider().text_generate("你好", system="你是助手", max_tokens=64)

        sent = self.server.last("/api/generate")
        self.assertEqual(sent["payload"]["model"], "mock-text")
        self.assertEqual(sent["payload"]["prompt"], "你好")
        self.assertEqual(sent["payload"]["system"], "你是助手")
        self.assertFalse(sent["payload"]["stream"], "必须 stream=false，否则拿到的是流")
        self.assertEqual(sent["payload"]["options"]["num_predict"], 64)
        self.assertEqual(sent["payload"]["options"]["num_ctx"], 4096)

        self.assertEqual(result.text, self.server.text_reply)
        self.assertEqual(result.provider, "ollama")
        self.assertEqual(result.model, "mock-text")
        self.assertEqual(result.usage["completion_tokens"], 7)

    def test_json_mode_sets_format(self) -> None:
        self.provider().text_generate("给我 JSON", json_mode=True)
        self.assertEqual(self.server.last("/api/generate")["payload"]["format"], "json")

    def test_temperature_override(self) -> None:
        self.provider().text_generate("x", temperature=0.9)
        self.assertEqual(self.server.last("/api/generate")["payload"]["options"]["temperature"], 0.9)

    def test_vision_sends_base64_images(self) -> None:
        image = self.server.url  # 占位，下面换成真文件
        fixture = Path(tempfile.mkdtemp(prefix="xbc-img-"))
        self.addCleanup(shutil.rmtree, fixture, ignore_errors=True)
        target = fixture / "pic.png"
        target.write_bytes(PNG_1PX)

        self.provider().vision_analyze("描述这张图", [target])

        sent = self.server.last("/api/generate")
        images = sent["payload"]["images"]
        self.assertEqual(len(images), 1)
        self.assertEqual(base64.b64decode(images[0]), PNG_1PX, "图片必须原样 base64 编码")
        del image

    def test_vision_accepts_raw_bytes(self) -> None:
        self.provider().vision_analyze("看图", [PNG_1PX])
        self.assertEqual(len(self.server.last("/api/generate")["payload"]["images"]), 1)

    def test_vision_without_images_is_rejected(self) -> None:
        from xbc.core.capabilities.ai import AIResponseError

        with self.assertRaises(AIResponseError):
            self.provider().vision_analyze("看图", [])

    def test_embedding_batch(self) -> None:
        result = self.provider().embedding(["第一段", "第二段"])

        sent = self.server.last("/api/embed")
        self.assertEqual(sent["payload"]["model"], "mock-embed")
        self.assertEqual(sent["payload"]["input"], ["第一段", "第二段"])

        self.assertEqual(result.count, 2)
        self.assertEqual(result.dimensions, 4)
        self.assertEqual(result.model, "mock-embed")

    def test_missing_embedding_model_suggests_pull(self) -> None:
        """模型不存在时要告诉用户怎么办，而不是只抛一个 HTTP 404。"""
        from xbc.core.capabilities.ai import AIResponseError

        self.server.fail_with = 404
        self.server.fail_body = {"error": 'model "mock-embed" not found, try pulling it first'}
        with self.assertRaises(AIResponseError) as ctx:
            self.provider().embedding(["一段"])
        self.assertIn("ollama pull mock-embed", str(ctx.exception))

    def test_legacy_embedding_endpoint_fallback(self) -> None:
        """老版本 Ollama 没有 /api/embed 时，应回退到 /api/embeddings。"""
        self.server.disable_embed_endpoint = True
        result = self.provider().embedding(["一段", "二段"])

        self.assertEqual(result.count, 2)
        self.assertEqual(result.dimensions, 4)
        self.assertTrue(
            any(item["path"] == "/api/embeddings" for item in self.server.requests),
            [item["path"] for item in self.server.requests],
        )

    def test_embedding_empty_input_makes_no_request(self) -> None:
        result = self.provider().embedding([])
        self.assertEqual(result.vectors, [])
        self.assertEqual(self.server.requests, [])

    def test_http_error_becomes_readable_error(self) -> None:
        from xbc.core.capabilities.ai import AIResponseError

        self.server.fail_with = 500
        with self.assertRaises(AIResponseError) as ctx:
            self.provider().text_generate("x")
        self.assertIn("500", str(ctx.exception))

    def test_dead_server_raises_unavailable(self) -> None:
        dead = OllamaProvider(url="http://127.0.0.1:1", model="m", timeout=2)
        with self.assertRaises(AIUnavailable):
            dead.text_generate("x")

    def test_missing_model_is_a_clear_error(self) -> None:
        provider = OllamaProvider(url=self.server.url, model="", timeout=5)
        with self.assertRaises(AIUnavailable) as ctx:
            provider.text_generate("x")
        self.assertIn("模型名", str(ctx.exception))


# ================= OpenAI 兼容 Provider =================


class OpenAIProviderTests(_ServerBase):
    def provider(self, **kwargs) -> OpenAICompatibleProvider:
        defaults = dict(
            base_url_value=f"{self.server.url}/v1",
            model="mock-text",
            embedding_model="mock-embed",
            api_key="sk-test-123",
            timeout=10,
        )
        defaults.update(kwargs)
        return OpenAICompatibleProvider(**defaults)

    def test_configured_requires_key(self) -> None:
        self.assertTrue(self.provider().configured())
        self.assertFalse(self.provider(api_key="").configured())
        self.assertFalse(self.provider(base_url_value="").configured())

    def test_chat_completions_payload_and_headers(self) -> None:
        result = self.provider().text_generate("你好", system="你是助手", max_tokens=64)

        sent = self.server.last("/chat/completions")
        self.assertEqual(sent["path"], "/v1/chat/completions", "基址的 /v1 不能被剥掉")
        self.assertEqual(sent["headers"].get("Authorization"), "Bearer sk-test-123")
        payload = sent["payload"]
        self.assertEqual(payload["model"], "mock-text")
        self.assertEqual(payload["messages"][0], {"role": "system", "content": "你是助手"})
        self.assertEqual(payload["messages"][1], {"role": "user", "content": "你好"})
        self.assertEqual(payload["max_tokens"], 64)

        self.assertEqual(result.text, self.server.text_reply)
        self.assertEqual(result.provider, "openai_compatible")
        self.assertEqual(result.usage["prompt_tokens"], 11)

    def test_json_mode_sets_response_format(self) -> None:
        self.provider().text_generate("给 JSON", json_mode=True)
        sent = self.server.last("/chat/completions")
        self.assertEqual(sent["payload"]["response_format"], {"type": "json_object"})

    def test_vision_uses_data_url(self) -> None:
        self.provider().vision_analyze("看图", [PNG_1PX])

        content = self.server.last("/chat/completions")["payload"]["messages"][-1]["content"]
        self.assertEqual(content[0]["type"], "text")
        self.assertEqual(content[1]["type"], "image_url")
        url = content[1]["image_url"]["url"]
        self.assertTrue(url.startswith("data:image/"), url[:40])
        self.assertEqual(base64.b64decode(url.split(",", 1)[1]), PNG_1PX)

    def test_embedding(self) -> None:
        result = self.provider().embedding(["一段", "二段"])

        sent = self.server.last("/embeddings")
        self.assertEqual(sent["path"], "/v1/embeddings")
        self.assertEqual(sent["payload"]["input"], ["一段", "二段"])
        self.assertEqual(result.count, 2)
        self.assertEqual(result.dimensions, 4)

    def test_missing_key_raises_before_request(self) -> None:
        provider = self.provider(api_key="")
        with self.assertRaises(AIUnavailable) as ctx:
            provider.text_generate("x")
        self.assertIn("API Key", str(ctx.exception))
        self.assertEqual(self.server.requests, [], "缺密钥时不应发出请求")

    def test_malformed_response_is_reported(self) -> None:
        from xbc.core.capabilities.ai import AIResponseError

        self.server.broken_json = True
        with self.assertRaises(AIResponseError):
            self.provider().text_generate("x")


# ================= 插件端到端（验收） =================


class AIPluginTests(unittest.TestCase):
    """AI 测试插件的端到端验收。"""

    def setUp(self) -> None:
        self.server = MockModelServer()
        self.addCleanup(self.server.stop)
        self.root = Path(tempfile.mkdtemp(prefix="xbc-ai-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def make_context(self, provider_name: str, *, with_key: bool = True) -> AppContext:
        overrides = {
            "ai.provider": provider_name,
            "ai.ollama.url": self.server.url,
            "ai.ollama.model": "mock-text",
            "ai.ollama.embedding_model": "mock-embed",
            "ai.ollama.timeout": 10,
            "ai.openai_compatible.base_url": f"{self.server.url}/v1",
            "ai.openai_compatible.model": "mock-text",
            "ai.openai_compatible.embedding_model": "mock-embed",
            "ai.openai_compatible.api_key_secret": "mock_api_key",
            "ai.openai_compatible.timeout": 10,
        }
        if with_key:
            # 密钥必须在装配前就写好：AI 服务是 create() 里建的
            paths = AppPaths(self.root).ensure()
            write_json(paths.secrets_file, {"mock_api_key": "sk-mock-abcdef"})

        ctx = AppContext.create(root=self.root, overrides=overrides, console=False)
        self.addCleanup(ctx.close)

        manager = PluginManager(ctx, [PLUGINS_DIR], ctx.logger)
        manager.discover()
        manager.activate("ai_probe")
        self.manager = manager
        return ctx

    def call(self, ctx: AppContext, tool: str, **arguments):
        return ctx.tool_registry.call(tool, arguments)

    # ---------- 插件本身 ----------
    def test_plugin_discovers_and_registers_four_tools(self) -> None:
        ctx = self.make_context("ollama")
        names = set(ctx.tool_registry.names())
        self.assertTrue({"ai_status", "ai_text", "ai_vision", "ai_embed"}.issubset(names))

    def test_ai_status_is_cheap_by_default(self) -> None:
        ctx = self.make_context("ollama")
        result = self.call(ctx, "ai_status")
        self.assertTrue(result.ok, result.message)
        value = result.value
        self.assertIn("ollama", value["providers"])
        self.assertEqual(value["default_provider"], "ollama")
        self.assertTrue(value["configured"])
        self.assertFalse(value["probed"])
        self.assertIsNone(value["available"])
        self.assertEqual(self.server.requests, [], "默认状态查询不应联网")

    def test_ai_status_with_probe_reports_availability(self) -> None:
        ctx = self.make_context("ollama")
        result = self.call(ctx, "ai_status", probe=True)
        self.assertTrue(result.ok, result.message)
        self.assertTrue(result.value["available"])
        self.assertTrue(result.value["probed"])

    def test_ai_text_via_capability(self) -> None:
        ctx = self.make_context("ollama")
        result = self.call(ctx, "ai_text", prompt="你好")
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.value["text"], self.server.text_reply)
        self.assertEqual(result.value["provider"], "ollama")
        self.assertEqual(result.value["model"], "mock-text")

    def test_ai_vision_via_capability(self) -> None:
        ctx = self.make_context("ollama")
        fixture = self.root / "pic.png"
        fixture.write_bytes(PNG_1PX)

        result = self.call(ctx, "ai_vision", prompt="描述这张图", images=[str(fixture)])
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.value["text"], self.server.text_reply)
        self.assertEqual(len(self.server.last("/api/generate")["payload"]["images"]), 1)

    def test_ai_embed_via_capability(self) -> None:
        ctx = self.make_context("ollama")
        result = self.call(ctx, "ai_embed", texts=["一段", "二段"])
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.value["count"], 2)
        self.assertEqual(result.value["dimensions"], 4)

    def test_missing_image_is_a_clear_error(self) -> None:
        ctx = self.make_context("ollama")
        result = self.call(ctx, "ai_vision", prompt="看图", images=[str(self.root / "nope.png")])
        self.assertFalse(result.ok)
        self.assertIn("不存在", result.message)

    # ---------- 验收核心：换 Provider 不改插件 ----------
    def _plugin_source_hash(self) -> str:
        return hashlib.sha256((PLUGINS_DIR / "ai_probe" / "plugin.py").read_bytes()).hexdigest()

    def test_swapping_provider_requires_no_plugin_change(self) -> None:
        """把 ai.provider 从 ollama 换成 openai_compatible，插件代码一个字节都不用改。"""
        before_hash = self._plugin_source_hash()

        # 第一次：Ollama Provider
        ollama_ctx = self.make_context("ollama")
        first = self.call(ollama_ctx, "ai_text", prompt="你好")
        self.assertTrue(first.ok, first.message)
        self.assertEqual(first.value["provider"], "ollama")
        self.assertEqual(self.server.last("/api/generate")["path"], "/api/generate")

        # 第二次：OpenAI 兼容 Provider（同样的插件、同样的调用）
        openai_ctx = self.make_context("openai_compatible")
        second = self.call(openai_ctx, "ai_text", prompt="你好")
        self.assertTrue(second.ok, second.message)
        self.assertEqual(second.value["provider"], "openai_compatible")
        self.assertEqual(self.server.last("/chat/completions")["path"], "/v1/chat/completions")

        # 两个 Provider 都给出了回答，插件源码没变
        self.assertEqual(first.value["text"], self.server.text_reply)
        self.assertEqual(second.value["text"], self.server.text_reply)
        self.assertEqual(self._plugin_source_hash(), before_hash, "插件源码被改动了")

    def test_swapping_provider_keeps_vision_and_embedding_working(self) -> None:
        ctx = self.make_context("openai_compatible")
        fixture = self.root / "pic.png"
        fixture.write_bytes(PNG_1PX)

        vision = self.call(ctx, "ai_vision", prompt="看图", images=[str(fixture)])
        self.assertTrue(vision.ok, vision.message)
        self.assertEqual(vision.value["provider"], "openai_compatible")

        embed = self.call(ctx, "ai_embed", texts=["一段"])
        self.assertTrue(embed.ok, embed.message)
        self.assertEqual(embed.value["dimensions"], 4)

    def test_vision_tool_rejects_raw_bytes(self) -> None:
        """工具声明的 schema 是字符串路径 —— 原始字节应被拒绝（能力层支持，但工具没暴露）。"""
        ctx = self.make_context("ollama")
        result = self.call(ctx, "ai_vision", prompt="看图", images=[PNG_1PX])
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "invalid_arguments")

    def test_explicit_provider_argument_overrides_default(self) -> None:
        """插件还能按调用指定 Provider —— 让上层做 A/B 对比。"""
        ctx = self.make_context("ollama")
        result = self.call(ctx, "ai_text", prompt="你好", provider="openai_compatible")
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.value["provider"], "openai_compatible")
        self.assertEqual(self.server.last("/chat/completions")["path"], "/v1/chat/completions")

    def test_plugin_config_reaches_the_capability_call(self) -> None:
        """插件配置能影响行为：prompt_prefix 应出现在发给模型的提示词里。

        配置在**激活时**读取，所以改配置后要重新激活 —— 这与 video_analyzer
        的阈值测试是同一套语义。
        """
        ctx = self.make_context("ollama")
        from xbc.core.config.layers import write_row

        write_row(ctx.paths.user_plugins_config, "ai_probe", config={"prompt_prefix": "[前缀] "})
        ctx.reload_plugin_config()
        self.manager.deactivate("ai_probe")
        self.manager.activate("ai_probe")

        result = ctx.tool_registry.call("ai_text", {"prompt": "正文"})
        self.assertTrue(result.ok, result.message)
        self.assertEqual(self.server.last("/api/generate")["payload"]["prompt"], "[前缀] 正文")


# ================= 约束：插件不得直接调模型 =================


class NoDirectModelAccessTests(unittest.TestCase):
    """TASK-007 的硬要求：业务插件不得直接调用模型。

    这条如果只是写在文档里就没人守得住，所以让它变成一条会失败的测试：
    扫描所有插件的源码，出现模型 SDK / HTTP 客户端就报错。
    """

    #: 直接访问模型的常见方式
    FORBIDDEN_MODULES = {
        "ollama", "openai", "anthropic", "litellm", "transformers", "torch",
        "requests", "httpx", "aiohttp", "http.client", "urllib.request",
        "websockets", "grpc",
    }

    def plugin_sources(self) -> list[Path]:
        return sorted(PLUGINS_DIR.glob("*/plugin.py"))

    def test_plugin_sources_exist(self) -> None:
        self.assertTrue(self.plugin_sources(), "前置条件：应能找到插件源码")

    def test_plugins_do_not_import_model_or_http_clients(self) -> None:
        import ast

        offenders: list[str] = []
        for source in self.plugin_sources():
            tree = ast.parse(source.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                for name in names:
                    if name in self.FORBIDDEN_MODULES:
                        offenders.append(f"{source.parent.name}: import {name}")

        self.assertEqual(
            offenders, [],
            "插件不得直接调用模型或发 HTTP，应通过 ctx.ai：\n" + "\n".join(offenders),
        )

    def test_core_ai_layer_is_the_only_place_with_model_endpoints(self) -> None:
        """模型端点字符串只应出现在 AI 能力层里，不该散落到插件或内核其他位置。"""
        offenders: list[str] = []
        for path in list(PLUGINS_DIR.glob("*/*.py")) + [
            p for p in (SRC_DIR / "xbc").rglob("*.py")
            if "capabilities" not in p.parts or "ai" not in p.parts
        ]:
            text = path.read_text(encoding="utf-8")
            if "/api/generate" in text or "/chat/completions" in text:
                offenders.append(str(path.relative_to(REPO_ROOT)))

        self.assertEqual(
            offenders, [],
            "模型端点只应出现在 src/xbc/core/capabilities/ai/ 下：\n" + "\n".join(offenders),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
