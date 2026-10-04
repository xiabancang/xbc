"""AI 能力层测试（TASK-007）。

## 怎么测才可信

**不依赖真的 Ollama，也不连任何外部服务。** 测试里起一个**本地 mock 服务**
（标准库 `http.server`），同时实现两套协议形状：

- Ollama 形状：`GET /api/tags`、`POST /api/generate`、`POST /api/embed`
- OpenAI 兼容形状：`POST /v1/chat/completions`、`POST /v1/embeddings`

这样请求构造、响应解析、错误处理都能被确定性地验证，
而且恰好让「换 Provider 不换插件」这条验收可以被真的跑一遍。

## 验收标准对应

| # | 验收标准 | 对应测试 |
|---|---|---|
| 1 | 测试插件可调用三个能力 | `AITestPluginTests.test_selftest_runs_all_three_capabilities` |
| 2 | 切换 Provider 不改插件代码 | `test_swapping_provider_keeps_every_plugin_file_identical`（**全文件哈希**） |
| 3 | 不可用时错误明确 | `ProviderErrorTests`（provider 名 + 原因 + 检查建议） |
| 4 | AI Capability 属于 Core | `CorePlacementTests` |
| 5 | 插件无直接模型调用 | `NoDirectModelAccessTests`（字面 grep + ast 双证据） |
| 6 | Core 无业务耦合 | `NoBusinessCouplingTests`（词表 + 词边界） |
| 7 | 跨 Provider 约束写进文档与注释 | `EmbeddingConstraintTests` |
| 8 | 全部测试通过 | 整套套件 |
"""

from __future__ import annotations

import ast
import base64
import hashlib
import json
import re
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
    EmbeddingRequest,
    EmbeddingResult,
    ModelProvider,
    OllamaProvider,
    OpenAICompatibleProvider,
    TextRequest,
    TextResult,
    VisionAnswer,
    VisionRequest,
    VisionResult,
    build_ai_service,
    parse_vision_payload,
    vision_prompt,
)
from xbc.core.capabilities.ai.provider import base_url  # noqa: E402
from xbc.core.config.layers import write_json  # noqa: E402
from xbc.core.context import AppContext  # noqa: E402
from xbc.core.paths import AppPaths  # noqa: E402
from xbc.core.runtime.manager import PluginManager  # noqa: E402

PLUGINS_DIR = REPO_ROOT / "plugins"
TEST_PLUGIN_DIR = PLUGINS_DIR / "ai_test_plugin"
AI_CAPABILITY_DIR = SRC_DIR / "xbc" / "core" / "capabilities" / "ai"

#: 1x1 透明 PNG —— 用来验证图片读取，不依赖任何素材文件
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

VISION_JSON = '{"description": "画面里有一个黑色方块与彩色条纹", "labels": ["方块", "条纹", "测试图"]}'
#: 定向提问模式的模型回复
VISION_ANSWER = '{"answer": "画面里有一个黑色方块与彩色条纹", "labels": ["方块", "条纹"]}'

#: 定向提问提示词里的分隔标记，mock 用它判断本次是哪一种模式
QUESTION_MARKER = "问题："


def prompt_text(payload: dict) -> str:
    """从两类协议的请求体里取出提示词原文。"""
    if "prompt" in payload:
        return str(payload["prompt"])
    messages = payload.get("messages") or []
    if not messages:
        return ""
    content = messages[-1].get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(part.get("text", "")) for part in content if isinstance(part, dict)
        )
    return ""

#: 验收第 5 条：插件里不允许出现的直接引用
FORBIDDEN_IN_PLUGINS = [
    "openai", "ollama", "anthropic", "litellm",
    "requests", "httpx", "aiohttp", "urllib", "http.client",
]

#: 验收第 6 条：Core 的 AI 层里不允许出现的业务词。
#: 用正则而不是纯字符串，因为「客户端」(client) 里含「客户」(customer) —— 那不是业务词。
BUSINESS_PATTERNS = [
    r"视频", r"音频", r"字幕", r"剪辑", r"镜头", r"关键帧",
    r"财务", r"会计", r"发票",
    r"知识库", r"文档库",
    r"文案", r"营销", r"订单", r"商品", r"支付", r"商城",
    r"客户(?!端)",
]


class MockModelServer:
    """本地 mock 服务：记录收到的请求，按固定内容回应。"""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.text_reply = "这是假模型的回答"
        self.vision_reply = VISION_JSON
        self.answer_reply = VISION_ANSWER
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
                    self._json({"models": [{"name": "mock-text:latest"}]})
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
                    wants_json = payload.get("format") == "json"
                    if not wants_json:
                        reply = owner.text_reply
                    elif QUESTION_MARKER in prompt_text(payload):
                        reply = owner.answer_reply
                    else:
                        reply = owner.vision_reply
                    self._json({
                        "response": reply,
                        "model": payload.get("model"),
                        "eval_count": 7,
                        "prompt_eval_count": 11,
                    })
                elif self.path == "/api/embed":
                    if owner.disable_embed_endpoint:
                        self._json({"error": "404 page not found"}, status=404)
                        return
                    inputs = payload.get("input") or []
                    self._json({"embeddings": [list(owner.embedding) for _ in inputs]})
                elif self.path == "/api/embeddings":
                    self._json({"embedding": list(owner.embedding)})
                elif self.path.endswith("/chat/completions"):
                    wants_json = "response_format" in payload
                    if not wants_json:
                        reply = owner.text_reply
                    elif QUESTION_MARKER in prompt_text(payload):
                        reply = owner.answer_reply
                    else:
                        reply = owner.vision_reply
                    self._json({
                        "model": payload.get("model"),
                        "choices": [{
                            "message": {"role": "assistant", "content": reply},
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
        # poll_interval 默认 0.5s，会让每个用例的 shutdown() 白等半秒
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_port}"

    def last(self, path_contains: str = "") -> dict:
        for item in reversed(self.requests):
            if path_contains in item["path"]:
                return item
        raise AssertionError(
            f"没有收到匹配 {path_contains!r} 的请求：{[r['path'] for r in self.requests]}"
        )

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class _ServerBase(unittest.TestCase):
    def setUp(self) -> None:
        self.server = MockModelServer()
        self.addCleanup(self.server.stop)

    def png(self, name: str = "pic.png") -> Path:
        folder = Path(tempfile.mkdtemp(prefix="xbc-img-"))
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        target = folder / name
        target.write_bytes(PNG_1PX)
        return target


# ================= 接口契约 =================


class StubProvider(ModelProvider):
    """最小可用的假 Provider，用于测路由（不发网络请求）。"""

    def __init__(self, name: str, capabilities: set[AICapability], **kwargs) -> None:
        self.name = name
        self.capabilities = frozenset(capabilities)
        self.kwargs = kwargs
        self.seen: list = []

    def available(self) -> bool:
        return self.kwargs.get("available", True)

    def configured(self) -> bool:
        return self.kwargs.get("configured", True)

    def text_generate(self, request: TextRequest) -> TextResult:
        self.seen.append(request)
        return TextResult(text=f"[{self.name}] {request.prompt}", provider=self.name, model="stub")

    def vision_analyze(self, request: VisionRequest) -> VisionResult | VisionAnswer:
        self.seen.append(request)
        if request.question:
            return VisionAnswer(
                question=request.question,
                answer=f"[{self.name}] 回答：{request.question}",
                labels=["stub"],
                provider=self.name,
                model="stub",
            )
        return VisionResult(
            description=f"[{self.name}] {len(request.images)} 张图",
            labels=["stub"],
            provider=self.name,
            model="stub",
        )

    def embedding(self, request: EmbeddingRequest) -> EmbeddingResult:
        self.seen.append(request)
        return EmbeddingResult(
            vectors=[[0.0] * 3 for _ in request.texts], provider=self.name, model="stub"
        )


class InterfaceTests(unittest.TestCase):
    def test_register_and_default(self) -> None:
        service = AIService()
        service.register(StubProvider("first", {AICapability.TEXT}))
        service.register(StubProvider("second", {AICapability.EMBEDDING}))
        self.assertEqual(service.providers(), ["first", "second"])
        self.assertEqual(service.default_provider, "first", "第一个注册的成为默认")

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

    def test_no_provider_surfaces_configuration_error(self) -> None:
        """一个 Provider 都没有时，报错要说清为什么 —— 不能给个空服务让调用方猜。"""
        service = AIService(configuration_error="未配置 AI Provider（测试用消息）")
        with self.assertRaises(AIUnavailable) as ctx:
            service.text_generate("你好")
        self.assertIn("未配置 AI Provider", str(ctx.exception))

    def test_unknown_provider_name_lists_known_ones(self) -> None:
        service = AIService()
        service.register(StubProvider("known", {AICapability.TEXT}))
        with self.assertRaises(AIUnavailable) as ctx:
            service.provider("nope")
        self.assertIn("known", str(ctx.exception))

    def test_capability_routing_skips_provider_without_capability(self) -> None:
        service = AIService()
        service.register(StubProvider("text_only", {AICapability.TEXT}))
        service.register(StubProvider("embed_only", {AICapability.EMBEDDING}))
        self.assertEqual(service.provider(capability=AICapability.TEXT).name, "text_only")
        self.assertEqual(service.embedding(["x"]).provider, "embed_only")

    def test_missing_capability_is_a_clear_error(self) -> None:
        service = AIService()
        service.register(StubProvider("text_only", {AICapability.TEXT}))
        with self.assertRaises(AIUnsupported) as ctx:
            service.vision_analyze(["x.png"])
        self.assertIn("vision", str(ctx.exception))

    def test_service_builds_request_objects(self) -> None:
        service = AIService()
        stub = StubProvider("s", {AICapability.TEXT, AICapability.VISION, AICapability.EMBEDDING})
        service.register(stub)

        service.text_generate("你好", system="你是助手")
        request = stub.seen[-1]
        self.assertIsInstance(request, TextRequest)
        self.assertEqual(request.prompt, "你好")
        self.assertEqual(request.system, "你是助手")

        service.vision_analyze(["a.png", "b.png"])
        self.assertIsInstance(stub.seen[-1], VisionRequest)
        self.assertEqual(stub.seen[-1].images, ["a.png", "b.png"])

        service.embedding("单段文本")
        self.assertIsInstance(stub.seen[-1], EmbeddingRequest)
        self.assertEqual(stub.seen[-1].texts, ["单段文本"], "单个字符串应被包装成列表")

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
        service.register(StubProvider("ok", {AICapability.TEXT}, available=True))
        self.assertTrue(service.status(probe=True)["ok"]["available"])

    def test_text_result_json(self) -> None:
        self.assertEqual(TextResult(text='{"a": 1}').json(), {"a": 1})
        from xbc.core.capabilities.ai import AIResponseError

        with self.assertRaises(AIResponseError):
            TextResult(text="不是 JSON").json()

    def test_vision_result_shape(self) -> None:
        """验收要点：视觉输出必须是结构化的，不能只是一个字符串。"""
        result = VisionResult(
            description="一个人站在窗前", labels=["人", "窗"], provider="p", model="m"
        )
        data = result.to_dict()
        self.assertEqual(data["description"], "一个人站在窗前")
        self.assertEqual(data["labels"], ["人", "窗"])
        self.assertIn("raw", data)

    def test_embedding_result_dim(self) -> None:
        result = EmbeddingResult(vectors=[[1.0, 2.0], [3.0, 4.0]], provider="p", model="m")
        self.assertEqual(result.dim, 2)
        self.assertEqual(result.count, 2)
        self.assertEqual(result.to_dict()["dim"], 2)
        self.assertEqual(EmbeddingResult(vectors=[]).dim, 0, "空结果维度为 0，不是异常")


class VisionQuestionModeTests(unittest.TestCase):
    """TASK-008：`vision_analyze` 的定向提问模式。

    两条硬要求：

    1. **向后兼容** —— 不传 `question` 时行为与 TASK-007 完全一致；
    2. **两种模式的返回结构明确区分** —— 不用同一个字段承载两种语义。
    """

    def stub(self) -> tuple[AIService, StubProvider]:
        service = AIService()
        provider = StubProvider("s", {AICapability.VISION})
        service.register(provider)
        return service, provider

    # ---------- 向后兼容 ----------
    def test_without_question_returns_description_result(self) -> None:
        service, _ = self.stub()
        result = service.vision_analyze(["a.png"])
        self.assertIsInstance(result, VisionResult)
        self.assertNotIsInstance(result, VisionAnswer)
        self.assertTrue(result.description)
        self.assertEqual(result.labels, ["stub"])

    def test_question_defaults_to_none(self) -> None:
        service, provider = self.stub()
        service.vision_analyze(["a.png"])
        self.assertIsNone(provider.seen[-1].question, "不传 question 时应为 None")

    # ---------- 定向提问模式 ----------
    def test_with_question_returns_answer_result(self) -> None:
        service, provider = self.stub()
        result = service.vision_analyze(["a.png"], question="画面里有几个人？")

        self.assertIsInstance(result, VisionAnswer)
        self.assertNotIsInstance(result, VisionResult)
        self.assertEqual(result.question, "画面里有几个人？")
        self.assertIn("画面里有几个人？", result.answer, "answer 应与问题相关")
        self.assertEqual(result.labels, ["stub"])
        self.assertEqual(provider.seen[-1].question, "画面里有几个人？")

    # ---------- 两种结构必须区分 ----------
    def test_two_modes_use_different_field_names(self) -> None:
        service, _ = self.stub()
        described = service.vision_analyze(["a.png"]).to_dict()
        answered = service.vision_analyze(["a.png"], question="这是什么？").to_dict()

        self.assertIn("description", described)
        self.assertNotIn("answer", described, "描述模式不该出现 answer 字段")

        self.assertIn("answer", answered)
        self.assertNotIn("description", answered, "提问模式不该出现 description 字段")
        self.assertIn("question", answered, "提问模式应回显问题，便于归因")

        # 共有字段（labels / provider / model / usage）两边一致
        self.assertEqual(set(described) & set(answered), {"labels", "provider", "model", "raw", "usage"})

    def test_two_modes_are_different_types(self) -> None:
        self.assertIsNot(VisionResult, VisionAnswer)
        service, _ = self.stub()
        self.assertNotIsInstance(service.vision_analyze(["a.png"]), VisionAnswer)
        self.assertNotIsInstance(
            service.vision_analyze(["a.png"], question="q"), VisionResult
        )

    # ---------- 提示词 ----------
    def test_prompt_differs_between_modes(self) -> None:
        describe_prompt = vision_prompt()
        question_prompt = vision_prompt("画面里有几个人？")

        self.assertNotEqual(describe_prompt, question_prompt)
        self.assertIn('"description"', describe_prompt)
        self.assertIn('"answer"', question_prompt)
        self.assertIn("画面里有几个人？", question_prompt)
        self.assertNotIn("画面里有几个人？", describe_prompt)

    def test_question_with_braces_does_not_break_prompt(self) -> None:
        """问题里出现花括号不能让提示词构造炸掉（用拼接而不是 str.format）。"""
        prompt = vision_prompt("描述这个 {奇怪的} 画面 {0}")
        self.assertIn("{奇怪的}", prompt)
        self.assertIn("{0}", prompt)

    # ---------- 解析 ----------
    def test_parse_answer_field(self) -> None:
        body, labels = parse_vision_payload(
            '{"answer": "有两个人", "labels": ["人", "室内"]}', field="answer"
        )
        self.assertEqual(body, "有两个人")
        self.assertEqual(labels, ["人", "室内"])

    def test_parse_answer_falls_back_to_raw(self) -> None:
        body, labels = parse_vision_payload("模型没给 JSON", field="answer")
        self.assertEqual(body, "模型没给 JSON")
        self.assertEqual(labels, [])

    def test_parse_default_field_is_unchanged(self) -> None:
        """默认仍是 description —— TASK-007 的调用方行为不变。"""
        body, _ = parse_vision_payload('{"description": "一只猫", "labels": ["猫"]}')
        self.assertEqual(body, "一只猫")

    def test_request_to_dict_carries_question(self) -> None:
        request = VisionRequest(images=["a.png"], question="这是什么？")
        data = request.to_dict()
        self.assertEqual(data["question"], "这是什么？")
        self.assertEqual(data["images"], ["a.png"])


class VisionPayloadTests(unittest.TestCase):
    """模型返回的自由文本 → (description, labels) 的容错解析。"""

    def test_plain_json(self) -> None:
        description, labels = parse_vision_payload(
            '{"description": "一只猫", "labels": ["猫", "动物"]}'
        )
        self.assertEqual(description, "一只猫")
        self.assertEqual(labels, ["猫", "动物"])

    def test_json_in_code_fence(self) -> None:
        text = '```json\n{"description": "一只猫", "labels": ["猫"]}\n```'
        self.assertEqual(parse_vision_payload(text), ("一只猫", ["猫"]))

    def test_json_with_surrounding_prose(self) -> None:
        text = '好的，分析结果如下：{"description": "一只猫", "labels": ["猫"]} 以上。'
        self.assertEqual(parse_vision_payload(text), ("一只猫", ["猫"]))

    def test_labels_are_deduplicated(self) -> None:
        text = '{"description": "x", "labels": ["猫", "猫", " 狗 "]}'
        self.assertEqual(parse_vision_payload(text)[1], ["猫", "狗"])

    def test_not_json_falls_back_to_raw_text(self) -> None:
        """模型没给 JSON 时降级为"整段当描述"，而不是让整条链路失败。"""
        description, labels = parse_vision_payload("这看起来像一张风景照。")
        self.assertEqual(description, "这看起来像一张风景照。")
        self.assertEqual(labels, [])

    def test_missing_description_falls_back_to_raw(self) -> None:
        description, labels = parse_vision_payload('{"labels": ["猫"]}')
        self.assertIn("labels", description)
        self.assertEqual(labels, ["猫"])

    def test_empty_input(self) -> None:
        self.assertEqual(parse_vision_payload(""), ("", []))
        self.assertEqual(parse_vision_payload("   "), ("", []))


class EmbeddingConstraintTests(unittest.TestCase):
    """验收第 7 条：跨 Provider 约束写进文档与代码注释。"""

    def test_constraint_documented_in_types_module(self) -> None:
        text = (AI_CAPABILITY_DIR / "types.py").read_text(encoding="utf-8")
        self.assertIn("不允许跨 Provider", text)
        self.assertIn("混用", text)

    def test_constraint_stated_on_result_class(self) -> None:
        """不只是模块文档 —— 结果类自己的注释里也要说，因为看代码的人可能只看类。"""
        text = (AI_CAPABILITY_DIR / "types.py").read_text(encoding="utf-8")
        class_doc = VisionResult.__doc__ or ""
        self.assertTrue(class_doc)
        embedding_doc = EmbeddingResult.__doc__ or ""
        self.assertIn("跨 Provider", embedding_doc)

    def test_constraint_is_stated_in_public_readme(self) -> None:
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("混用", readme)

    def test_result_carries_provider_and_model_for_detection(self) -> None:
        """约束要能被**执行**：结果里必须带上判断空间所需的字段。"""
        result = EmbeddingResult(vectors=[[0.1]], provider="ollama", model="nomic-embed-text")
        data = result.to_dict()
        for key in ("provider", "model", "dim"):
            self.assertIn(key, data)


class BaseUrlTests(unittest.TestCase):
    def test_strips_legacy_endpoint(self) -> None:
        self.assertEqual(base_url("http://127.0.0.1:11434/api/generate"), "http://127.0.0.1:11434")
        self.assertEqual(base_url("http://127.0.0.1:11434/api/embed"), "http://127.0.0.1:11434")

    def test_keeps_v1_suffix(self) -> None:
        """`/v1` 是兼容服务的基址惯例，剥掉会让请求 404。"""
        self.assertEqual(base_url("https://api.example.com/v1"), "https://api.example.com/v1")
        self.assertEqual(
            base_url("https://api.example.com/v1/chat/completions"), "https://api.example.com/v1"
        )


class ProxyBypassTests(unittest.TestCase):
    """本地模型服务不能绕到系统代理上。"""

    def test_loopback_bypasses_proxy(self) -> None:
        from xbc.core.capabilities.ai.provider import _DIRECT_OPENER, opener_for

        for url in (
            "http://127.0.0.1:11434/api/tags",
            "http://localhost:11434/api/tags",
            "http://[::1]:11434/api/tags",
        ):
            self.assertIs(opener_for(url), _DIRECT_OPENER, url)

    def test_remote_uses_system_proxy(self) -> None:
        from xbc.core.capabilities.ai.provider import _SYSTEM_OPENER, opener_for

        self.assertIs(opener_for("https://api.example.com/v1/chat/completions"), _SYSTEM_OPENER)


# ================= Ollama Provider =================


class OllamaProviderTests(_ServerBase):
    def provider(self, **kwargs) -> OllamaProvider:
        defaults = dict(
            url=self.server.url,
            model="mock-text",
            embedding_model="mock-embed",
            timeout=10,
            options={"num_ctx": 4096, "temperature": 0.1},
        )
        defaults.update(kwargs)
        return OllamaProvider(**defaults)

    def test_available_when_server_up(self) -> None:
        self.assertTrue(self.provider().available())

    def test_models(self) -> None:
        self.assertEqual(self.provider().models(), ["mock-text:latest"])

    def test_text_generate_payload_and_result(self) -> None:
        result = self.provider().text_generate(TextRequest(prompt="你好", system="你是助手"))

        sent = self.server.last("/api/generate")["payload"]
        self.assertEqual(sent["model"], "mock-text")
        self.assertEqual(sent["prompt"], "你好")
        self.assertEqual(sent["system"], "你是助手")
        self.assertFalse(sent["stream"], "必须 stream=false，否则拿到的是流")
        self.assertEqual(sent["options"]["num_ctx"], 4096, "options 应来自配置")
        self.assertEqual(sent["options"]["temperature"], 0.1)

        self.assertEqual(result.text, self.server.text_reply)
        self.assertEqual(result.provider, "ollama")
        self.assertEqual(result.usage["completion_tokens"], 7)

    def test_vision_returns_structured_result(self) -> None:
        image = self.png()
        result = self.provider().vision_analyze(VisionRequest(images=[str(image)]))

        self.assertIsInstance(result, VisionResult)
        self.assertEqual(result.description, "画面里有一个黑色方块与彩色条纹")
        self.assertEqual(result.labels, ["方块", "条纹", "测试图"])
        self.assertEqual(result.raw, VISION_JSON)

        sent = self.server.last("/api/generate")["payload"]
        self.assertEqual(sent["format"], "json", "视觉必须要求 JSON 输出，否则拿不到 labels")
        self.assertEqual(base64.b64decode(sent["images"][0]), PNG_1PX)

    def test_vision_question_mode_returns_answer(self) -> None:
        image = self.png()
        result = self.provider().vision_analyze(
            VisionRequest(images=[str(image)], question="画面里有什么颜色？")
        )

        self.assertIsInstance(result, VisionAnswer)
        self.assertEqual(result.question, "画面里有什么颜色？")
        self.assertEqual(result.answer, "画面里有一个黑色方块与彩色条纹")
        self.assertEqual(result.labels, ["方块", "条纹"])

        sent = self.server.last("/api/generate")["payload"]
        self.assertIn("画面里有什么颜色？", sent["prompt"], "问题必须出现在发给模型的提示词里")
        self.assertEqual(sent["format"], "json")

    def test_vision_rejects_network_url(self) -> None:
        with self.assertRaises(AIError) as ctx:
            self.provider().vision_analyze(VisionRequest(images=["https://example.com/a.png"]))
        self.assertIn("只接受本地图片路径", str(ctx.exception))

    def test_vision_rejects_data_url(self) -> None:
        payload = "data:image/png;base64," + base64.b64encode(PNG_1PX).decode()
        with self.assertRaises(AIError) as ctx:
            self.provider().vision_analyze(VisionRequest(images=[payload]))
        self.assertIn("data URL", str(ctx.exception))

    def test_vision_rejects_raw_bytes(self) -> None:
        with self.assertRaises(AIError) as ctx:
            self.provider().vision_analyze(VisionRequest(images=[PNG_1PX]))
        self.assertIn("原始字节", str(ctx.exception))

    def test_vision_without_images(self) -> None:
        from xbc.core.capabilities.ai import AIResponseError

        with self.assertRaises(AIResponseError):
            self.provider().vision_analyze(VisionRequest(images=[]))

    def test_embedding_returns_vectors_and_dim(self) -> None:
        result = self.provider().embedding(EmbeddingRequest(texts=["一段", "二段"]))

        sent = self.server.last("/api/embed")["payload"]
        self.assertEqual(sent["model"], "mock-embed")
        self.assertEqual(sent["input"], ["一段", "二段"])
        self.assertEqual(result.count, 2)
        self.assertEqual(result.dim, 4)
        self.assertEqual(result.model, "mock-embed")

    def test_missing_embedding_model_suggests_pull(self) -> None:
        from xbc.core.capabilities.ai import AIResponseError

        self.server.fail_with = 404
        self.server.fail_body = {"error": 'model "mock-embed" not found, try pulling it first'}
        with self.assertRaises(AIResponseError) as ctx:
            self.provider().embedding(EmbeddingRequest(texts=["一段"]))
        self.assertIn("ollama pull mock-embed", str(ctx.exception))

    def test_legacy_embedding_endpoint_fallback(self) -> None:
        self.server.disable_embed_endpoint = True
        result = self.provider().embedding(EmbeddingRequest(texts=["一段", "二段"]))
        self.assertEqual(result.count, 2)
        self.assertTrue(any(r["path"] == "/api/embeddings" for r in self.server.requests))

    def test_embedding_empty_input_makes_no_request(self) -> None:
        result = self.provider().embedding(EmbeddingRequest(texts=[]))
        self.assertEqual(result.vectors, [])
        self.assertEqual(result.dim, 0)
        self.assertEqual(self.server.requests, [])

    def test_http_error_becomes_readable_error(self) -> None:
        from xbc.core.capabilities.ai import AIResponseError

        self.server.fail_with = 500
        with self.assertRaises(AIResponseError) as ctx:
            self.provider().text_generate(TextRequest(prompt="x"))
        self.assertIn("500", str(ctx.exception))

    def test_generate_does_not_pre_probe(self) -> None:
        """生成前不做多余的可用性探测（白多一个往返，且关闭端口会挂满超时）。"""
        provider = self.provider()
        probes: list[int] = []
        original = provider.available

        def spy() -> bool:
            probes.append(1)
            return original()

        provider.available = spy  # type: ignore[method-assign]
        provider.text_generate(TextRequest(prompt="x"))
        self.assertEqual(probes, [], "text_generate 不应调用 available()")


# ================= 错误处理（验收第 3 条） =================


class ProviderErrorTests(unittest.TestCase):
    """Provider 不可用 / 未配置时，错误必须包含 **provider 名 + 原因 + 检查建议**。"""

    DEAD_URL = "http://127.0.0.1:1"
    DEAD_TIMEOUT = 1

    def dead(self, **kwargs) -> OllamaProvider:
        defaults = dict(
            url=self.DEAD_URL, model="m", embedding_model="e", timeout=self.DEAD_TIMEOUT
        )
        defaults.update(kwargs)
        return OllamaProvider(**defaults)

    def assert_three_parts(self, message: str, provider: str) -> None:
        self.assertIn(f"provider={provider}", message, "错误里必须点名 provider")
        self.assertIn("原因：", message, "错误里必须说清原因")
        self.assertIn("检查建议：", message, "错误里必须给出可执行的检查建议")

    def test_connection_failure_has_provider_reason_and_advice(self) -> None:
        with self.assertRaises(AIUnavailable) as ctx:
            self.dead().text_generate(TextRequest(prompt="hi"))
        message = str(ctx.exception)
        self.assert_three_parts(message, "ollama")
        self.assertIn(self.DEAD_URL, message, "错误里要说清连的是哪个地址")

    def test_vision_and_embedding_also_report_unavailable(self) -> None:
        with self.assertRaises(AIUnavailable) as ctx:
            self.dead().vision_analyze(VisionRequest(images=[str(self._png())]))
        self.assert_three_parts(str(ctx.exception), "ollama")

        with self.assertRaises(AIUnavailable) as ctx:
            self.dead().embedding(EmbeddingRequest(texts=["x"]))
        self.assert_three_parts(str(ctx.exception), "ollama")

    def test_missing_model_names_the_config_key(self) -> None:
        provider = self.dead(model="")
        with self.assertRaises(AIUnavailable) as ctx:
            provider.text_generate(TextRequest(prompt="x"))
        message = str(ctx.exception)
        self.assert_three_parts(message, "ollama")
        self.assertIn("ai.model", message, "要指出该改哪个配置项")

    def test_missing_embedding_model_points_at_its_own_key(self) -> None:
        provider = self.dead(embedding_model="")
        with self.assertRaises(AIUnavailable) as ctx:
            provider.embedding(EmbeddingRequest(texts=["x"]))
        self.assertIn("ai.embedding_model", str(ctx.exception))

    def test_embedding_advice_does_not_suggest_a_chat_model(self) -> None:
        """检查建议里的示例必须与配置项匹配。

        曾经的真实缺陷：`ai.embedding_model` 的建议里给的是 `"model": "qwen2.5vl:3b"`
        —— 照做会把对话/视觉模型设成向量模型，然后继续失败。
        """
        provider = self.dead(embedding_model="")
        with self.assertRaises(AIUnavailable) as ctx:
            provider.embedding(EmbeddingRequest(texts=["x"]))
        message = str(ctx.exception)
        self.assertIn("embedding_model", message)
        self.assertNotIn('"model":', message, "向量模型的建议里不该出现对话模型的示例")

    def test_text_advice_names_the_text_key(self) -> None:
        provider = self.dead(model="")
        with self.assertRaises(AIUnavailable) as ctx:
            provider.text_generate(TextRequest(prompt="x"))
        message = str(ctx.exception)
        self.assertIn("ai.model", message)
        self.assertNotIn("embedding_model", message, "文本模型的建议里不该混入向量模型的键")

    def test_available_returns_false_instead_of_raising(self) -> None:
        """可用性探测绝不抛异常 —— 否则诊断会因为服务没起就崩掉。"""
        self.assertFalse(self.dead().available())

    def test_error_hierarchy(self) -> None:
        from xbc.core.errors import XbcError

        self.assertTrue(issubclass(AIUnavailable, AIError))
        self.assertTrue(issubclass(AIError, XbcError))

    def _png(self) -> Path:
        folder = Path(tempfile.mkdtemp(prefix="xbc-img-"))
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        target = folder / "pic.png"
        target.write_bytes(PNG_1PX)
        return target


class ConfigurationErrorTests(unittest.TestCase):
    """配置不成立时必须明确报错，不静默失败。"""

    def config(self, **ai_settings) -> dict:
        base = {"ai.provider": "ollama", "ai.model": "m"}
        base.update(ai_settings)

        class _Config(dict):
            def get(self, key, default=None):
                return dict.get(self, key, default)

        return _Config(base)

    def test_missing_provider_is_reported(self) -> None:
        service = build_ai_service(self.config(**{"ai.provider": ""}))
        with self.assertRaises(AIUnavailable) as ctx:
            service.text_generate("x")
        message = str(ctx.exception)
        self.assertIn("ai.provider", message)
        self.assertIn("检查建议：", message)

    def test_unknown_provider_is_reported(self) -> None:
        service = build_ai_service(self.config(**{"ai.provider": "not_a_provider"}))
        with self.assertRaises(AIUnavailable) as ctx:
            service.text_generate("x")
        message = str(ctx.exception)
        self.assertIn("not_a_provider", message)
        self.assertIn("ollama", message, "应列出内核实现了哪些")

    def test_openai_compatible_without_base_url_is_reported(self) -> None:
        service = build_ai_service(self.config(**{"ai.provider": "openai_compatible"}))
        with self.assertRaises(AIUnavailable) as ctx:
            service.text_generate("x")
        message = str(ctx.exception)
        self.assertIn("openai_compatible", message)
        self.assertIn("base_url", message)
        self.assertIn("检查建议：", message)

    def test_ollama_preconfigured_by_default(self) -> None:
        """只写 ai.provider 与 ai.model 就应该能装配起来。"""
        service = build_ai_service(self.config())
        self.assertEqual(service.providers(), ["ollama"])
        self.assertEqual(service.default_provider, "ollama")


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

    def test_configured_requires_key_and_base(self) -> None:
        self.assertTrue(self.provider().configured())
        self.assertFalse(self.provider(api_key="").configured())
        self.assertFalse(self.provider(base_url_value="").configured())

    def test_chat_payload_and_headers(self) -> None:
        result = self.provider().text_generate(TextRequest(prompt="你好", system="你是助手"))

        sent = self.server.last("/chat/completions")
        self.assertEqual(sent["path"], "/v1/chat/completions", "基址的 /v1 不能被剥掉")
        self.assertEqual(sent["headers"].get("Authorization"), "Bearer sk-test-123")
        self.assertEqual(sent["payload"]["messages"][0], {"role": "system", "content": "你是助手"})
        self.assertEqual(sent["payload"]["messages"][1], {"role": "user", "content": "你好"})
        self.assertEqual(result.text, self.server.text_reply)

    def test_vision_uses_data_url_and_returns_structure(self) -> None:
        image = self.png()
        result = self.provider().vision_analyze(VisionRequest(images=[str(image)]))

        self.assertEqual(result.description, "画面里有一个黑色方块与彩色条纹")
        self.assertEqual(result.labels, ["方块", "条纹", "测试图"])

        sent = self.server.last("/chat/completions")["payload"]
        content = sent["messages"][-1]["content"]
        self.assertEqual(content[0]["type"], "text")
        self.assertEqual(content[1]["type"], "image_url")
        url = content[1]["image_url"]["url"]
        self.assertTrue(url.startswith("data:image/"), url[:40])
        self.assertEqual(base64.b64decode(url.split(",", 1)[1]), PNG_1PX)

    def test_vision_question_mode_returns_answer(self) -> None:
        image = self.png()
        result = self.provider().vision_analyze(
            VisionRequest(images=[str(image)], question="画面里有什么颜色？")
        )

        self.assertIsInstance(result, VisionAnswer)
        self.assertEqual(result.answer, "画面里有一个黑色方块与彩色条纹")
        self.assertEqual(result.labels, ["方块", "条纹"])

        sent = self.server.last("/chat/completions")["payload"]
        text = " ".join(
            part.get("text", "") for part in sent["messages"][-1]["content"]
            if isinstance(part, dict)
        )
        self.assertIn("画面里有什么颜色？", text)

    def test_embedding(self) -> None:
        result = self.provider().embedding(EmbeddingRequest(texts=["一段", "二段"]))
        sent = self.server.last("/embeddings")
        self.assertEqual(sent["path"], "/v1/embeddings")
        self.assertEqual(result.count, 2)
        self.assertEqual(result.dim, 4)

    def test_missing_key_raises_before_request(self) -> None:
        with self.assertRaises(AIUnavailable) as ctx:
            self.provider(api_key="").text_generate(TextRequest(prompt="x"))
        message = str(ctx.exception)
        self.assertIn("provider=openai_compatible", message)
        self.assertIn("原因：", message)
        self.assertIn("检查建议：", message)
        self.assertEqual(self.server.requests, [], "缺密钥时不应发出请求")

    def test_malformed_response_is_reported(self) -> None:
        from xbc.core.capabilities.ai import AIResponseError

        self.server.broken_json = True
        with self.assertRaises(AIResponseError):
            self.provider().text_generate(TextRequest(prompt="x"))


# ================= 验收 1/2：测试插件端到端 =================


class AITestPluginTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = MockModelServer()
        self.addCleanup(self.server.stop)
        self.root = Path(tempfile.mkdtemp(prefix="xbc-ai-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def make_context(self, provider: str = "ollama", **extra) -> AppContext:
        overrides = {
            "ai.provider": provider,
            "ai.model": "mock-text",
            "ai.embedding_model": "mock-embed",
            "ai.options": {"timeout": 10},
            "ai.ollama.url": self.server.url,
            "ai.openai_compatible.base_url": f"{self.server.url}/v1",
            "ai.openai_compatible.api_key_secret": "mock_api_key",
        }
        overrides.update(extra)
        paths = AppPaths(self.root).ensure()
        write_json(paths.secrets_file, {"mock_api_key": "sk-mock-abcdef"})

        ctx = AppContext.create(root=self.root, overrides=overrides, console=False)
        self.addCleanup(ctx.close)
        manager = PluginManager(ctx, [PLUGINS_DIR], ctx.logger)
        manager.discover()
        manager.activate("ai_test_plugin")
        self.manager = manager
        return ctx

    def selftest(self, ctx: AppContext, **arguments):
        return ctx.tool_registry.call("ai_selftest", arguments)

    # ---------- 插件形态 ----------
    def test_plugin_registers_exactly_one_tool(self) -> None:
        ctx = self.make_context()
        self.assertEqual(
            [n for n in ctx.tool_registry.names() if n.startswith("ai_")], ["ai_selftest"]
        )

    # ---------- 验收 1 ----------
    def test_selftest_runs_all_three_capabilities(self) -> None:
        ctx = self.make_context()
        image = Path(self.root) / "pic.png"
        image.write_bytes(PNG_1PX)

        result = self.selftest(ctx, prompt="你好", images=[str(image)])
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.value["provider"], "ollama")
        self.assertTrue(result.value["ok"], "三步都应成功")

        steps = {step["capability"]: step for step in result.value["steps"]}
        self.assertEqual(set(steps), {"text_generate", "vision_analyze", "embedding"})

        self.assertEqual(steps["text_generate"]["result"]["text"], self.server.text_reply)
        self.assertEqual(
            steps["vision_analyze"]["result"]["description"], "画面里有一个黑色方块与彩色条纹"
        )
        self.assertEqual(steps["vision_analyze"]["result"]["labels"], ["方块", "条纹", "测试图"])
        self.assertEqual(steps["embedding"]["result"]["dim"], 4)
        self.assertEqual(steps["embedding"]["result"]["count"], 2)

    def test_vision_step_is_skipped_without_images(self) -> None:
        ctx = self.make_context()
        result = self.selftest(ctx, prompt="你好")
        steps = {step["capability"]: step for step in result.value["steps"]}
        self.assertTrue(steps["vision_analyze"]["skipped"])
        self.assertFalse(steps["vision_analyze"]["ok"])
        self.assertFalse(result.value["ok"])
        self.assertTrue(steps["text_generate"]["ok"], "跳过视觉不该影响其他步骤")

    def test_plugin_reports_capability_failures_explicitly(self) -> None:
        """模型服务不可用时，自检报告要把原因摆出来，而不是静默返回空。"""
        ctx = self.make_context(**{"ai.ollama.url": "http://127.0.0.1:1", "ai.options": {"timeout": 1}})
        result = self.selftest(ctx, prompt="你好")
        self.assertTrue(result.ok, "工具本身调用成功 —— 失败在能力层，被记录进报告")
        self.assertFalse(result.value["ok"])

        steps = {step["capability"]: step for step in result.value["steps"]}
        self.assertFalse(steps["text_generate"]["ok"])
        message = steps["text_generate"]["error"]
        self.assertIn("provider=ollama", message)
        self.assertIn("检查建议：", message)

    def test_successful_steps_are_printed(self) -> None:
        """任务书第 5 节要求"打印结果" —— 成功的步骤也必须打印。

        曾经的真实缺陷：只打印失败与汇总，三步全成功时控制台上看不到任何结果。
        """
        ctx = self.make_context()
        image = Path(self.root) / "pic.png"
        image.write_bytes(PNG_1PX)

        with self.assertLogs("xbc.plugin.ai_test_plugin", level="INFO") as captured:
            self.selftest(ctx, prompt="你好", images=[str(image)])

        joined = "\n".join(record.getMessage() for record in captured.records)
        for capability in ("text_generate", "vision_analyze", "embedding"):
            self.assertIn(f"{capability} 成功", joined, f"{capability} 的结果没有被打印")
        self.assertIn("dim=4", joined, "向量维度应出现在打印的结果里")
        self.assertIn("labels=", joined, "标签应出现在打印的结果里")

    # ---------- 验收 2：切换 Provider 不改插件（全文件哈希）----------
    def plugin_files_hash(self) -> dict[str, str]:
        return {
            str(path.relative_to(TEST_PLUGIN_DIR)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(TEST_PLUGIN_DIR.rglob("*"))
            if path.is_file() and "__pycache__" not in path.parts
        }

    def test_swapping_provider_keeps_every_plugin_file_identical(self) -> None:
        before = self.plugin_files_hash()
        self.assertTrue(before, "前置条件：插件目录下应能找到文件")
        self.assertIn("plugin.json", before)
        self.assertIn("plugin.py", before)

        image = Path(self.root) / "pic.png"
        image.write_bytes(PNG_1PX)

        # 第一次：本地 Provider
        ctx_a = self.make_context("ollama")
        run_a = self.selftest(ctx_a, prompt="同一句提示词", images=[str(image)])
        self.assertTrue(run_a.value["ok"], run_a.value)
        self.assertEqual(run_a.value["provider"], "ollama")
        self.assertEqual(self.server.last("/api/generate")["path"], "/api/generate")

        # 第二次：另一个 Provider，同一份插件、同一个工具、同一组参数
        ctx_b = self.make_context("openai_compatible")
        run_b = self.selftest(ctx_b, prompt="同一句提示词", images=[str(image)])
        self.assertTrue(run_b.value["ok"], run_b.value)
        self.assertEqual(run_b.value["provider"], "openai_compatible")
        self.assertEqual(self.server.last("/chat/completions")["path"], "/v1/chat/completions")

        # 两次跑的是同一个能力契约，插件目录逐文件哈希一致
        after = self.plugin_files_hash()
        self.assertEqual(before.keys(), after.keys())
        for name in before:
            self.assertEqual(before[name], after[name], f"{name} 被改动了")

    def test_same_contract_across_providers(self) -> None:
        """两个 Provider 返回的**结构**必须一致 —— 否则插件就得为它们写分支。"""
        image = Path(self.root) / "pic.png"
        image.write_bytes(PNG_1PX)

        results = []
        for provider in ("ollama", "openai_compatible"):
            ctx = self.make_context(provider)
            outcome = self.selftest(ctx, prompt="同一句提示词", images=[str(image)]).value
            steps = {s["capability"]: s["result"] for s in outcome["steps"]}
            results.append((steps["text_generate"].keys(), steps["vision_analyze"].keys(),
                            steps["embedding"].keys()))

        self.assertEqual(results[0][0], results[1][0], "TextResult 字段不一致")
        self.assertEqual(results[0][1], results[1][1], "VisionResult 字段不一致")
        self.assertEqual(results[0][2], results[1][2], "EmbeddingResult 字段不一致")


# ================= TASK-008：AI Capability 的第一次真实消费 =================


try:  # 两种调用方式（`tests.` 包 与 直接 discover）都要能导入
    from tests.test_video_analyzer import EXPECTED_DURATION, make_test_video
except ImportError:  # pragma: no cover
    from test_video_analyzer import EXPECTED_DURATION, make_test_video


class VideoAnalyzerConsumesAICapabilityTests(unittest.TestCase):
    """video_analyzer 通过 `ctx.ai` 完成「关键帧 → AI 理解 → 标签」。

    用本地 mock 服务替代真模型，验证的是**链路与数据流**：
    标签确实来自 AI 返回，插件侧没有任何硬编码映射。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture_dir = Path(tempfile.mkdtemp(prefix="xbc-va-ai-"))
        cls.video = cls.fixture_dir / "scenes.mp4"
        cls.video_ready = make_test_video(cls.video)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.fixture_dir, ignore_errors=True)

    def setUp(self) -> None:
        if not self.video_ready:
            self.skipTest("无法合成测试视频（FFmpeg 不可用或编码器缺失）")
        self.server = MockModelServer()
        self.addCleanup(self.server.stop)
        self.root = Path(tempfile.mkdtemp(prefix="xbc-va-ai-home-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def make_context(self) -> AppContext:
        paths = AppPaths(self.root).ensure()
        write_json(paths.secrets_file, {"mock_api_key": "sk-mock-abcdef"})
        ctx = AppContext.create(
            root=self.root,
            console=False,
            overrides={
                "ai.provider": "openai_compatible",
                "ai.model": "mock-text",
                "ai.embedding_model": "mock-embed",
                "ai.options": {"timeout": 30},
                "ai.openai_compatible.base_url": f"{self.server.url}/v1",
                "ai.openai_compatible.api_key_secret": "mock_api_key",
            },
        )
        self.addCleanup(ctx.close)
        manager = PluginManager(ctx, [PLUGINS_DIR], ctx.logger)
        manager.discover()
        manager.activate("video_analyzer")
        self.manager = manager
        return ctx

    def annotate(self, ctx: AppContext, **arguments):
        return ctx.tool_registry.call("video_annotate", {"path": str(self.video), **arguments})

    # ---------- 链路 ----------
    def test_annotate_returns_ai_labels_per_shot(self) -> None:
        ctx = self.make_context()
        result = self.annotate(ctx)

        self.assertTrue(result.ok, result.message)
        value = result.value
        self.assertEqual(value["shot_count"], 3, "测试视频应切出 3 个镜头")
        self.assertEqual(value["frames_analyzed"], 3, "默认每镜头 1 帧")
        self.assertEqual(value["errors"], [])
        self.assertEqual(value["ai"]["provider"], "openai_compatible")

        for shot in value["shots"]:
            self.assertEqual(shot["frames_analyzed"], 1)
            self.assertTrue(shot["answers"], "每个镜头都应有 AI 的回答")
            self.assertEqual(shot["labels"], ["方块", "条纹"], "标签应来自 AI 返回")

        self.assertEqual(value["label_vocabulary"], ["方块", "条纹"])

    def test_labels_come_from_the_model_not_a_hardcoded_map(self) -> None:
        """把模型回复换掉，工具输出的标签必须跟着变。"""
        ctx = self.make_context()
        self.server.answer_reply = '{"answer": "一片森林", "labels": ["森林", "树木", "户外"]}'

        value = self.annotate(ctx).value
        self.assertEqual(value["label_vocabulary"], ["森林", "树木", "户外"])
        for shot in value["shots"]:
            self.assertEqual(shot["labels"], ["森林", "树木", "户外"])
        self.assertIn("一片森林", value["shots"][0]["answers"])

    def test_annotate_uses_question_mode(self) -> None:
        """必须走定向提问模式：提示词里要带上插件定义的问题。"""
        ctx = self.make_context()
        value = self.annotate(ctx).value

        self.assertTrue(value["question"], "工具应声明它问了什么")
        sent = self.server.last("/chat/completions")["payload"]
        text = prompt_text(sent)
        self.assertIn(QUESTION_MARKER, text, "应走提问模式的提示词")
        self.assertIn(value["question"], text, "插件定义的问题必须原样发给模型")

    def test_frames_per_shot_is_respected(self) -> None:
        ctx = self.make_context()
        value = self.annotate(ctx, frames_per_shot=2).value
        self.assertEqual(value["frames_analyzed"], 6, "3 镜头 × 2 帧")

    # ---------- 失败处理 ----------
    def test_fails_clearly_when_ai_produces_nothing(self) -> None:
        """一帧都没成功时必须明确失败 —— 返回一堆空标签比失败更糟。

        否则调用方会以为"AI 说这个视频没有内容"。
        """
        ctx = self.make_context()
        self.server.fail_with = 500
        result = self.annotate(ctx)

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "execution_failed")
        self.assertIn("AI 理解未产出任何结果", result.message)
        self.assertIn("500", result.message)

    def test_ai_layer_not_configured_fails_with_actionable_message(self) -> None:
        ctx = AppContext.create(root=self.root, console=False, overrides={"ai.model": ""})
        self.addCleanup(ctx.close)
        manager = PluginManager(ctx, [PLUGINS_DIR], ctx.logger)
        manager.discover()
        manager.activate("video_analyzer")

        result = ctx.tool_registry.call("video_annotate", {"path": str(self.video)})
        self.assertFalse(result.ok)
        message = result.message
        self.assertIn("provider=ollama", message)
        self.assertIn("检查建议：", message)


# ================= 验收 4：AI 能力属于 Core =================


class CorePlacementTests(unittest.TestCase):
    REQUIRED = ("service.py", "provider.py", "request.py", "types.py")

    def test_capability_lives_in_core(self) -> None:
        self.assertTrue(AI_CAPABILITY_DIR.is_dir(), f"不在内核目录下：{AI_CAPABILITY_DIR}")
        for name in self.REQUIRED:
            self.assertTrue((AI_CAPABILITY_DIR / name).is_file(), f"缺少 {name}")

    def test_providers_are_a_subpackage(self) -> None:
        providers = AI_CAPABILITY_DIR / "providers"
        self.assertTrue((providers / "ollama.py").is_file())
        self.assertTrue((providers / "openai_compatible.py").is_file())

    def test_no_registry_module(self) -> None:
        """原则 6：不做为未来预留的扩展点 —— 不保留 Provider 注册表。"""
        self.assertFalse((AI_CAPABILITY_DIR / "registry.py").exists())

    def test_core_ai_layer_has_no_third_party_imports(self) -> None:
        allowed = set(sys.stdlib_module_names) | {"xbc", "__future__"}
        offenders: list[str] = []
        for path in AI_CAPABILITY_DIR.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    names = [node.module or ""]
                for name in names:
                    top = name.split(".")[0]
                    if top and top not in allowed:
                        offenders.append(f"{path.name}: import {name}")
        self.assertEqual(offenders, [], "AI 能力层引入了第三方依赖：\n" + "\n".join(offenders))

    def test_capability_is_exposed_on_context(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="xbc-ai-core-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        ctx = AppContext.create(root=root, console=False)
        self.addCleanup(ctx.close)
        self.assertIsInstance(ctx.ai, AIService)


# ================= 验收 5：插件无直接模型调用 =================


class CaseInsensitiveScanTests(unittest.TestCase):
    """TASK-008 验收 3：大小写不敏感的 grep。

    TASK-007 的检查是**大小写敏感**的，于是 `不接 Ollama` 这种大写写法能躲过去 ——
    本任务在做真实接入时发现了这个漏洞（`video_analyzer` 的文档字符串里就有），
    这里把它补上。
    """

    #: 验收标准点名的两个插件
    AI_LAYER_PLUGINS = ("ai_test_plugin", "video_analyzer")

    def sources(self, plugin: str) -> list[Path]:
        return sorted((PLUGINS_DIR / plugin).rglob("*.py"))

    def scan(self, paths: list[Path]) -> list[str]:
        offenders: list[str] = []
        for path in paths:
            lowered = path.read_text(encoding="utf-8").lower()
            for token in FORBIDDEN_IN_PLUGINS:
                if token.lower() in lowered:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}: 含 {token!r}")
        return offenders

    def test_ai_layer_plugins_are_clean_case_insensitively(self) -> None:
        paths: list[Path] = []
        for plugin in self.AI_LAYER_PLUGINS:
            found = self.sources(plugin)
            self.assertTrue(found, f"前置条件：{plugin} 应有源码")
            paths.extend(found)

        self.assertEqual(
            self.scan(paths), [],
            "大小写不敏感的扫描命中了禁止引用：\n" + "\n".join(self.scan(paths)),
        )

    def test_global_scan_known_exception_is_documented(self) -> None:
        """把"全局扫描并不干净"这个事实固化下来，免得以后误以为它是干净的。

        `plugins/knowledge_base` 的文档里出现了被禁词，但**它不是本任务的产物**，
        本任务不改它（范围外只报告）。若哪天它被修好了，这条测试仍然通过。
        """
        offenders = self.scan(sorted(PLUGINS_DIR.rglob("*.py")))
        unexpected = [item for item in offenders if "knowledge_base" not in item]
        self.assertEqual(
            unexpected, [],
            "除已知的 knowledge_base 之外出现了新的命中：\n" + "\n".join(unexpected),
        )


class NoDirectModelAccessTests(unittest.TestCase):
    def plugin_sources(self) -> list[Path]:
        return sorted(PLUGINS_DIR.rglob("*.py"))

    def test_plugin_sources_exist(self) -> None:
        self.assertTrue(self.plugin_sources(), "前置条件：应能找到插件源码")

    def test_literal_grep_finds_no_forbidden_tokens(self) -> None:
        """字面 grep —— 这是验收标准第 5 条要求的证据形式。

        注意：这条能通过的前提是**连注释和文档字符串里都不出现**这些字面量。
        解释规则时不能把规则里禁的词原样抄进去，否则证据自相矛盾。
        """
        offenders: list[str] = []
        for path in self.plugin_sources():
            text = path.read_text(encoding="utf-8")
            for token in FORBIDDEN_IN_PLUGINS:
                if token in text:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}: 含 {token!r}")
        self.assertEqual(
            offenders, [],
            "插件里出现了禁止的直接引用：\n" + "\n".join(offenders),
        )

    def test_ast_scan_finds_no_forbidden_imports(self) -> None:
        """字面 grep 之外再看一遍真实 import —— 两者证据互补。"""
        offenders: list[str] = []
        for path in self.plugin_sources():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    names = [node.module or ""]
                for name in names:
                    top = name.split(".")[0]
                    if top in FORBIDDEN_IN_PLUGINS:
                        offenders.append(f"{path.parent.name}: import {name}")
        self.assertEqual(offenders, [], "插件直接 import 了模型 SDK / 网络库：\n" + "\n".join(offenders))

    def test_plugins_only_reach_ai_through_core(self) -> None:
        """允许的 AI 入口只有一个：`xbc.core.capabilities.ai`（通常经 `ctx.ai`）。"""
        offenders: list[str] = []
        for path in self.plugin_sources():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    if node.module.startswith("xbc.") and not node.module.startswith(
                        "xbc.core.capabilities.ai"
                    ) and not node.module.startswith("xbc.core.contract"):
                        offenders.append(f"{path.parent.name}: from {node.module}")
        self.assertEqual(offenders, [], "插件从内核的非契约模块取东西：\n" + "\n".join(offenders))

    def test_model_endpoints_only_in_ai_capability(self) -> None:
        offenders: list[str] = []
        for path in list(PLUGINS_DIR.rglob("*.py")) + list((SRC_DIR / "xbc").rglob("*.py")):
            if AI_CAPABILITY_DIR in path.parents:
                continue
            text = path.read_text(encoding="utf-8")
            if "/api/generate" in text or "/chat/completions" in text:
                offenders.append(str(path.relative_to(REPO_ROOT)))
        self.assertEqual(
            offenders, [], "模型端点只应出现在 capabilities/ai/ 下：\n" + "\n".join(offenders)
        )


# ================= 验收 6：Core 无业务耦合 =================


class NoBusinessCouplingTests(unittest.TestCase):
    """AI 能力层里不允许出现业务语义。

    用正则而不是纯字符串：`客户端`(client) 里含 `客户`(customer)，
    朴素子串匹配会在这里误报。
    """

    def ai_sources(self) -> list[Path]:
        return sorted(AI_CAPABILITY_DIR.rglob("*.py"))

    def test_word_list_is_not_empty(self) -> None:
        self.assertTrue(BUSINESS_PATTERNS, "前置条件：业务词表不能为空")

    def test_no_business_words_in_ai_capability(self) -> None:
        patterns = [(word, re.compile(word)) for word in BUSINESS_PATTERNS]
        offenders: list[str] = []
        for path in self.ai_sources():
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                for word, regex in patterns:
                    if regex.search(line):
                        offenders.append(
                            f"{path.relative_to(REPO_ROOT)}:{number} 命中 /{word}/ → {line.strip()[:70]}"
                        )
        self.assertEqual(
            offenders, [],
            "AI 能力层里出现了业务词：\n" + "\n".join(offenders),
        )

    def test_word_boundary_rule_is_actually_needed(self) -> None:
        """说明为什么用正则：`客户端` 不该被判成业务词 `客户`。"""
        self.assertIsNotNone(re.search(r"客户(?!端)", "客户信息"))
        self.assertIsNone(re.search(r"客户(?!端)", "HTTP 客户端"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
