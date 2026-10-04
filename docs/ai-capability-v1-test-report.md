# AI Capability Layer V1 —— 测试报告与验收结果

| 项目 | 内容 |
|---|---|
| 任务 | TASK-007 夏半仓 AI Capability Layer V1 |
| 版本 | `CORE_VERSION = 0.1.0`，`API_SPEC_VERSION = 1.0` |
| 环境 | Windows · Python 3.13.15 · Ollama 0.35.1（本机 `qwen2.5vl:3b` / `nomic-embed-text`） |
| 结论 | **6 项验收标准全部通过**；全量 **278** 项测试通过（系统 Python 与 venv 双环境） |

---

## 1. 目标与原则

**目标**：建立统一 AI Capability，让未来所有插件通过 Core 调用 AI —— 插件不需要知道
模型是什么、跑在哪里、由谁提供。

| # | 设计原则 | 落实方式 |
|---|---|---|
| 1 | AI 能力属于 Core Capability | 实现位于 `src/xbc/core/capabilities/ai/`，通过 `ctx.ai` 暴露 |
| 2 | Plugin 禁止直接调用模型 | 由 `ast` 扫描测试强制（见 §4.5），不是文档约定 |
| 3 | Provider 可替换 | `ModelProvider` 抽象 + 注册表；换 Provider 只改配置 |
| 4 | 本地模型优先 | Ollama 始终注册；`openai_compatible` 未配置则不出现 |
| 5 | 不绑定单一厂商 | 请求/响应结构中立；`openai_compatible` 是一条通道，覆盖 DeepSeek / OpenAI / Claude / 自建网关 |
| 6 | Core 保持最小化 | 只依赖标准库（有测试断言）；无重试、无队列、无 Agent |

---

## 2. 交付物

```
src/xbc/core/capabilities/ai/
├─ __init__.py                 统一导出
├─ types.py                    AICapability / TextResult / EmbeddingResult / 错误分类
├─ request.py                  TextRequest / VisionRequest / EmbeddingRequest
├─ provider.py                 ModelProvider 抽象 + HTTP、图片、代理工具
├─ registry.py                 Provider 注册表（加厂商不必改内核别处）
├─ service.py                  AIService 门面 + build_ai_service 装配
└─ providers/
   ├─ __init__.py              内置 Provider 登记表
   ├─ ollama.py                Ollama：文本 / 视觉 / 向量
   └─ openai_compatible.py     OpenAI 兼容通道（默认不注册）

plugins/ai_test_plugin/        验收插件（plugin.json + plugin.py）
tests/test_ai_capability.py    75 项测试
```

### 三个能力入口

| 能力 | 方法 | 输入 | 输出 |
|---|---|---|---|
| 文本生成 | `ctx.ai.text_generate(prompt, ...)` | 提示词 | `TextResult` |
| 视觉理解 | `ctx.ai.vision_analyze(prompt, images, ...)` | 提示词 + 图片 | `TextResult` |
| 向量化 | `ctx.ai.embedding(texts, ...)` | 一段或多段文本 | `EmbeddingResult` |

结果对象都带 `provider` / `model` / `usage` —— 出问题时能说清**是谁生成的**。

### 配置

```json
{
  "ai": {
    "provider": "ollama",
    "model": "",
    "embedding_model": "",
    "ollama": {
      "url": "http://127.0.0.1:11434",
      "model": "qwen2.5vl:3b",
      "embedding_model": "nomic-embed-text",
      "timeout": 180,
      "options": { "temperature": 0.1, "num_ctx": 8192, "num_predict": 700 }
    },
    "openai_compatible": {
      "base_url": "",
      "model": "",
      "embedding_model": "",
      "api_key_secret": "openai_compatible_api_key",
      "timeout": 120
    }
  }
}
```

**模型名解析顺序**：`ai.model`（全局）→ `ai.<provider>.model`（Provider 专属）→ Provider 内置默认。

全局项优先，因为 `ai.model` 才是用户直接操作的旋钮 —— 否则用户设了 `ai.model: qwen`，
却会被内置的默认模型名悄悄盖掉。

**API Key** 存在 `secrets.json`，配置里只放键名；密钥不进日志、不进错误信息。

---

## 3. 验收结果

### 3.1 逐条对照

| # | 验收标准 | 结果 | 证据 |
|---|---|---|---|
| 1 | 测试插件可以调用 AI | ✅ | 真机三种能力全部成功（见 §3.2） |
| 2 | 切换 Provider 无需修改插件代码 | ✅ | 三种切换方式下插件源码哈希不变（见 §3.3） |
| 3 | Ollama 不可用时有明确错误 | ✅ | 错误含服务地址 + 排查建议 + 替代方案（见 §3.4） |
| 4 | AI Capability 属于 Core | ✅ | 位于 `core/capabilities/ai/`，`ctx.ai` 类型为 `xbc.core.capabilities.ai.service.AIService`，零第三方依赖 |
| 5 | 插件代码不存在直接模型调用 | ✅ | `ast` 扫描 4 个插件，违规导入 0 项 |
| 6 | 全部测试通过 | ✅ | `Ran 278 tests ... OK`，双环境 |

### 3.2 验收 1 —— 真实 Ollama 上的实际输出

不是 mock，是本机模型的实际响应：

```
text_generate   provider=ollama  model=qwen2.5vl:3b
                prompt: 用一句话说明什么是镜头切分。
                output: 镜头切分是指在影视制作中，通过剪辑将多个镜头组合成一个完整的视频片段。

vision_analyze  provider=ollama  model=qwen2.5vl:3b
                prompt: 这张图里有什么？用一句话回答。
                image : ffmpeg testsrc 生成的 320x240 测试图
                output: 这张图里有彩色条纹和一个黑色的方块。   ← 描述正确

embedding       provider=ollama  model=nomic-embed-text
                count=2  dimensions=768
                同文本 → 相同向量；不同文本 → 不同向量
```

### 3.3 验收 2 —— 三种切换方式，插件代码均未改动

| 方式 | 结果 | model |
|---|---|---|
| ① 默认 `ai.provider=ollama` | `provider=ollama` | `qwen2.5vl:3b` |
| ② 进程内注册第三方 Provider（模拟接入 DeepSeek） | `provider=third_party` | `tp-1` |
| ③ 配置 `ai.model`（同时把 `ai.ollama.model` 故意写成不存在的名字） | 调用仍成功 | `qwen2.5vl:3b` ← 证明 `ai.model` 优先 |

```
插件源码哈希: d79f013e1186842e → d79f013e1186842e
结论: ✅ 三种切换下插件代码均未改动
```

方式 ② 的链路完全绕开了 HTTP —— 说明插件依赖的是**能力层契约**，而不是某个具体实现。

### 3.4 验收 3 —— Ollama 不可用时的错误信息

把 `ai.ollama.url` 指向一个没人监听的端口，插件调用返回：

```
ok=False  code=execution_failed
连不上模型服务 http://127.0.0.1:1/api/generate：timed out
本地 Ollama 不可用（http://127.0.0.1:1）。请确认 Ollama 已启动（`ollama serve`），
或把 ai.provider 改成别的 Provider、把 ai.ollama.url 指向正确的地址。
```

错误里包含三件事：**连的是哪个地址**、**怎么修**、**替代方案**。
插件侧不需要写任何兜底逻辑 —— 错误类型是 `AIUnavailable`（`AIError` → `XbcError` 的子类），
可以被统一捕获。

### 3.5 验收 5 —— 约束如何被强制

「插件不得直接调用模型」如果只写在文档里就没人守得住，所以它是**会失败的测试**：

| 测试 | 检查内容 |
|---|---|
| `test_plugins_do_not_import_model_or_http_clients` | `ast` 扫描所有 `plugins/*/plugin.py`，出现 `ollama` / `openai` / `anthropic` / `litellm` / `requests` / `httpx` / `aiohttp` / `http.client` / `urllib.request` 等即失败 |
| `test_test_plugin_mentions_no_model_vendor` | 验收插件的**代码部分**不得出现任何厂商名或模型名 |
| `test_model_endpoints_only_in_ai_capability` | 模型端点字符串（`/api/generate`、`/chat/completions`）只允许出现在 `capabilities/ai/` 下 |
| `test_core_ai_layer_has_no_third_party_imports` | AI 能力层只依赖标准库 |

扫描结果：4 个插件、违规导入 **0** 项。

---

## 4. 测试统计

```
Ran 278 tests in 14.4s
OK
```

| 范围 | 数量 |
|---|---|
| 全量测试 | **278** |
| 其中 `tests/test_ai_capability.py` | **75** |
| 本轮新增 | +23（并重写既有 AI 测试以匹配新接口） |

### 测试类分布

| 测试类 | 覆盖内容 |
|---|---|
| `InterfaceTests` | 注册 / 默认 / 能力路由 / Request 对象构造 / 状态查询不联网 |
| `BaseUrlTests` | URL 规范化（含 `/v1` 不被剥掉的回归） |
| `ProxyBypassTests` | 回环地址绕过系统代理；远端仍走代理 |
| `RegistryTests` | 注册表 / 第三方 Provider 登记 / 工厂返回 None 跳过 / 模型名优先级 |
| `OllamaProviderTests` | 请求构造、响应解析、JSON 模式、图片编码、向量批量、旧接口回退、无预探测 |
| `OllamaUnavailableTests` | 验收 3：连接失败、探测不抛异常、错误类型层级 |
| `OpenAIProviderTests` | `Authorization` 头、多模态 content、`/v1` 路径、缺密钥不发请求 |
| `AITestPluginTests` | 验收 1/2：端到端调用、三种 Provider 切换、配置生效 |
| `CorePlacementTests` | 验收 4：位置、子包结构、零第三方依赖、`ctx.ai` 暴露 |
| `NoDirectModelAccessTests` | 验收 5：四条扫描规则 |

### 测试方式说明

Provider 的正确性用**本地假模型服务**验证（标准库 `http.server`，同时实现
Ollama 与 OpenAI 两套协议形状）：

- 请求构造、响应解析、错误处理都能被**确定性**复现；
- **不依赖真 Ollama、不连任何云端**，因此在任何机器上都能跑；
- 而且它恰好让「换 Provider 不换插件」这条验收可以被真的执行一遍。

真机 Ollama 则用于端到端确认（§3.2），两者互补。

---

## 5. 开发中发现并修复的缺陷

| # | 缺陷 | 影响 | 修复 |
|---|---|---|---|
| B1 | `base_url()` 把 OpenAI 的 `/v1` 后缀一并剥掉 | 请求打到 `/chat/completions` 上 **404**，`openai_compatible` 整条链路不可用 | 只剥 `/api/...` 与完整端点后缀；加回归测试 |
| B2 | `status()` 默认做网络探测 | 一次状态查询 **4 秒**；状态查询在热路径上（插件列表、界面刷新），会让界面卡住 | 默认 `probe=False` 只报结构信息，`probe=True` 才联网（`doctor` 用） |
| B3 | 默认地址用 `localhost` | Windows 上先试 IPv6 `::1`、失败再回退 IPv4，**实测每次探测 2.07s vs 127.0.0.1 的 0.004s** | 默认地址改为 `127.0.0.1` |
| B4 | 本地模型请求会走系统代理 | 开发机装了 Clash 类代理时，连本地 Ollama 的请求可能绕到代理上（本机 `getproxies()` 返回 `127.0.0.1:7897` 且无 `no_proxy`）→ 变慢或失败 | 回环地址显式绕过代理（`opener_for()`）；远端仍走系统代理 |
| B5 | 每次生成前都做一次可用性预探测 | 白多一个往返；且本机"连没人监听的端口"不会立刻被拒、而是挂满超时（2s），代价直接加到每次调用上 | 移除预探测，失败信息由 HTTP 调用直接给出并附排查建议 |

B2/B3 修完后整套测试从 37s 回到 25s；B4/B5 与测试自身的服务关闭轮询优化后进一步降到 **16.6s**。

---

## 6. 已知限制与已声明偏差

| # | 限制 | 说明 |
|---|---|---|
| L1 | `vision_analyze` 传路径时由内核代读文件 | 需要"看图"的插件应同时声明 `files` 能力。这是与 `ffmpeg` 能力接受路径一致的已知取舍 |
| L2 | `openai_compatible` 未在真实云端服务上验证 | 任务禁止云端账号，因此只用**本地假服务**验证协议正确性。接真实服务需用户自行配置并验证 |
| L3 | `available()` 在部分机器上需约 2s | 因为"连没人监听的端口"不退化为拒绝而是挂超时（本机实测 2.0s）。已把它移出热路径，只在显式探测时使用 |
| L4 | 无重试 / 无限流 / 无并发控制 | V1 明确不做；调用失败即返回错误，由调用方决定是否重试 |
| L5 | Provider 之间无版本协商 | 无 `provider_version` 概念；接口以 `CORE_API_VERSION` 为准 |
| L6 | 向量维度不做校验 | 由 Provider 报告 `dimensions`，能力层不假定固定维度（不同模型维度不同） |
| L7 | 未做流式输出 | `text_generate` 固定 `stream: false`；流式属于后续版本 |
| L8 | 未内置 DeepSeek / OpenAI / Claude 专属 Provider | 它们都走 `openai_compatible` 通道；有需要时各自加一个 Provider 模块并登记一行即可，**插件无需改动** |

---

## 7. 复现步骤

```powershell
cd F:\Downloads\XBC

# 1) 全量测试
python -m unittest discover -s tests

# 2) 只跑 AI 能力层测试
python -m unittest tests.test_ai_capability -v

# 3) 真实 Ollama（需先 ollama serve，并准备模型）
ollama pull qwen2.5vl:3b
ollama pull nomic-embed-text
python run.py tool call ai_status --kwargs "{\"probe\": true}"
python run.py tool call ai_text   --kwargs "{\"prompt\": \"用一句话说明什么是镜头切分\"}"
python run.py tool call ai_embed  --kwargs "{\"texts\": [\"镜头切分\", \"关键帧抽取\"]}"

# 4) 切换 Provider（只改配置，不改任何代码）
#    编辑 <数据目录>/config.json：把 ai.provider 改成 openai_compatible，
#    并填好 ai.openai_compatible.base_url 与 secrets.json 里的 API Key
```

> **Windows 提示**：PowerShell 会吃掉 `--kwargs` 里的双引号。
> 请用上面的反引号转义写法，或 `cmd /c "python run.py ... --kwargs \"{...}\""`。

---

## 8. 结论

TASK-007 的 6 项验收标准**全部通过**。

这一版交付的核心不是"能调模型"，而是**把模型这件事从插件里彻底拿走**：

- 插件只认识 `ctx.ai` 的三个方法，不认识任何厂商、模型名或端口；
- 换 Provider 是配置动作，不是代码动作 —— 有测试用源码哈希守着；
- 加一个新厂商是"加一个 Provider 模块 + 登记一行"，内核其他部分与插件都不动；
- 约束（插件不得直连模型）由会失败的测试强制，而不是靠自觉。

**未做的事**（任务明确禁止）：Agent 系统、自动决策、工作流、Prompt 商城、云端账号、
UI 美化、视频 AI 业务。
