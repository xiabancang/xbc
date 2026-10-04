# AI Capability Layer V1 —— 测试报告与验收结果

| 项目 | 内容 |
|---|---|
| 任务 | TASK-007 夏半仓 AI Capability Layer V1 |
| 版本 | `CORE_VERSION = 0.1.0`，`API_SPEC_VERSION = 1.0` |
| 环境 | Windows · Python 3.13.15 · Ollama 0.35.1（本机 `qwen2.5vl:3b` / `nomic-embed-text`） |
| 结论 | **8 项验收标准全部通过**；全量 **280** 项测试通过（系统 Python + venv 双环境） |

---

## 1. 实现了什么（对照任务书逐条）

### 设计原则（硬约束）

| # | 原则 | 落实方式 | 证据 |
|---|---|---|---|
| 1 | AI 能力属于 Core Capability | 位于 `src/xbc/core/capabilities/ai/`，通过 `ctx.ai` 暴露 | 验收 4 |
| 2 | Plugin 禁止直接调用模型 SDK / HTTP 客户端 | 字面 grep + `ast` 双重扫描测试 | 验收 5 |
| 3 | Provider 可替换，切换不改插件代码 | 配置驱动装配；插件**全文件哈希**两次运行一致 | 验收 2 |
| 4 | 本地模型优先（Ollama 为第一实现） | `ai.provider` 默认 `ollama` | 验收 1 |
| 5 | 不绑定单一厂商 | 请求/响应结构中立；`openai_compatible` 是通道而非单一厂商 | §5 |
| 6 | Core 保持最小化，不做"为未来预留"的扩展点 | **删除了 Provider 注册表**；移除按调用的可选参数与旧版兼容方法 | §4 |
| 7 | 不引入业务语义 | 18 条业务词表 + 词边界规则扫描 | 验收 6 |

### 实现范围

| # | 任务书要求 | 实现 |
|---|---|---|
| 1 | AI Capability 接口 | `AIService`（对插件暴露的统一入口）、`ModelProvider`（抽象基类）、三个能力各自的 Request / Response 结构 |
| 2 | Provider 机制 | `providers/ollama.py`（第一实现，真实可用）、`providers/openai_compatible.py`（第二实现，验证可替换性） |
| 3 | 第一批能力（仅三个） | `text_generate` / `vision_analyze` / `embedding` |
| 4 | 配置系统 | `ai.provider` / `ai.model` / `ai.options` |
| 5 | 测试插件 | `plugins/ai_test_plugin`（单工具 `ai_selftest`，依次调用三个能力） |

### 三个能力的契约

| 能力 | 输入 | 输出 |
|---|---|---|
| `text_generate` | `prompt: str`，可选 `system: str` | `text: str`，`usage: {prompt_tokens, completion_tokens}` |
| `vision_analyze` | 本地图片路径（**不接受 base64 / URL**） | `description: str` + `labels: list[str]` |
| `embedding` | `texts: list[str]` | `vectors: list[list[float]]` + `dim: int` |

### 交付物

```
src/xbc/core/capabilities/ai/
├─ __init__.py                 统一导出
├─ types.py                    能力枚举 / 结果类型 / 错误分类 / 跨 Provider 约束
├─ request.py                  TextRequest / VisionRequest / EmbeddingRequest
├─ provider.py                 ModelProvider 抽象 + 视觉结果解析 + HTTP / 图片 / 代理工具
├─ providers/
│  ├─ __init__.py
│  ├─ ollama.py                本地模型
│  └─ openai_compatible.py     兼容 OpenAI 形状的通道
└─ service.py                  AIService 门面 + 显式装配

plugins/ai_test_plugin/        验收插件（plugin.json + plugin.py）
tests/test_ai_capability.py    77 项测试
docs/ai-capability-v1-test-report.md   本报告
```

---

## 2. 验收标准逐条证据

| # | 验收标准 | 结果 |
|---|---|---|
| 1 | 测试插件可调用三个 AI 能力，Ollama 环境真实跑通 | ✅ |
| 2 | 切换 Provider 不改插件代码（两次运行后全文件哈希一致） | ✅ |
| 3 | Ollama 不可用时错误明确（provider 名 + 原因 + 排查建议） | ✅ |
| 4 | AI Capability 属于 Core | ✅ |
| 5 | 插件无直接模型调用（grep 证据） | ✅ |
| 6 | Core 无业务耦合（grep 不到业务词） | ✅ |
| 7 | embedding 跨 Provider 约束已写入文档与代码注释 | ✅ |
| 8 | 全部测试通过（系统 Python + venv） | ✅ |

全部证据由脚本 `验收取证` 一次性产出，可复现（见 §7）。

### 验收 1 —— 三个能力的真实调用记录（本机 Ollama，非 mock）

```
tool ok = True   插件报告 provider = ollama   三步全成功 = True

[text_generate]  provider=ollama  model=qwen2.5vl:3b
  text  : 我是来自阿里云的超大规模语言模型，我叫通义千问。
  usage : {'prompt_tokens': 24, 'completion_tokens': 18}

[vision_analyze] provider=ollama  model=qwen2.5vl:3b
  description : 这张图片是一个电视测试图，显示了彩色条纹和一个黑色的方块。
  labels      : ['电视测试图', '彩色条纹', '黑色方块', '测试图像', '电视屏幕', ...]
  raw         : {"description": "这张图片是一个电视测试图，...", "labels": [...]}

[embedding]      provider=ollama  model=nomic-embed-text
  count = 2   dim = 768
  vectors_preview = [[0.02539, 0.05311, -0.16308, -0.06945],
                     [0.02661, 0.02742, -0.15002, -0.04054]]
```

输入图片是 ffmpeg `testsrc` 生成的 320×240 测试图；`vision_analyze` 的描述与标签
与实际画面相符。`text_generate` 的输出内容由模型自行生成，本报告不对其真实性作评价。

### 验收 2 —— 切换 Provider 的全文件哈希对比

两次运行使用**同一份插件、同一个工具、同一组参数**，只改配置里的 `ai.provider`：

| 次数 | `ai.provider` | 插件报告 provider | 实际请求路径 | 三步全成功 |
|---|---|---|---|---|
| ① | `ollama` | `ollama` | `/api/generate` | True |
| ② | `openai_compatible` | `openai_compatible` | `/v1/chat/completions` | True |

`plugins/ai_test_plugin/` 下**所有文件**的 SHA-256：

```
运行前：                          运行后：
144ef6aeb55b8fa5…  plugin.json    144ef6aeb55b8fa5…  plugin.json
82b4e838cb23b1ae…  plugin.py      82b4e838cb23b1ae…  plugin.py

文件集合一致: True
逐文件哈希一致: True
结论: ✅ 切换 Provider 未改动插件任何文件
```

### 验收 3 —— 错误处理演示

**（a）Provider 不可用**（`ai.ollama.url` 指向无人监听的端口）：

```
provider=ollama 不可用
原因：连不上模型服务 http://127.0.0.1:1/api/generate：timed out
检查建议：1) 确认 Ollama 已启动（`ollama serve`）；
          2) 确认 http://127.0.0.1:1 可访问；
          3) 或把 config.json 的 ai.provider 改为其他 Provider。

含 provider 名 : True
含原因        : True
含检查建议    : True
```

**（b）模型未配置**（`ai.model` 为空）：

```
provider=ollama 缺少模型配置
原因：ai.model 未设置，无法确定要调用哪个模型
检查建议：在 config.json 里设置 ai.model（例如 "model": "qwen2.5vl:3b"），
          并用 `ollama list` 确认该模型已下载。
```

**（c）Provider 未配置 / 名字写错**（由 `build_ai_service` 给出，不静默失败）：

```
未配置 AI Provider
原因：config.json 的 ai.provider 为空
检查建议：把 ai.provider 设为 ollama（本地）或 openai_compatible（API）。

ai.provider 配置有误
原因：ai.provider = 'not_a_provider'，内核未实现该 Provider
检查建议：改为 ollama 或 openai_compatible 之一。

provider=openai_compatible 配置不完整
原因：未配置 ai.openai_compatible.base_url，不知道要连哪个服务
检查建议：填上服务基址（例如 https://api.deepseek.com/v1），
          并在 secrets.json 里放好 API Key；或把 ai.provider 改回 ollama。
```

三种情况都由测试 `ProviderErrorTests` / `ConfigurationErrorTests` 锁定。

### 验收 4 —— AI Capability 属于 Core

```
路径存在: True → src\xbc\core\capabilities\ai
文件清单: __init__.py / provider.py / providers\__init__.py / providers\ollama.py /
          providers\openai_compatible.py / request.py / service.py / types.py
无 registry.py（原则 6 禁止预留扩展点）: True
```

另有测试断言：该目录下**只依赖标准库**（`ast` 扫描 import，允许集合 = 标准库 + `xbc`），
且 `ctx.ai` 的类型是 `xbc.core.capabilities.ai.service.AIService`。

### 验收 5 —— 插件无直接模型调用

```
扫描插件源码: 9 个文件
词表: openai, ollama, anthropic, litellm, requests, httpx, aiohttp, urllib, http.client
字面 grep 命中: （无）
真实 import 命中: （无）
结论: ✅ 两条证据都干净
```

两种证据互补：字面 grep 是任务书要求的证据形式；`ast` 扫描真实 import 防止
"用别名或字符串拼接绕过"。

> **一个必须先解决的矛盾**：第一版实现里，插件文档字符串为了解释这条规则，
> 原样写出了这些被禁的词 —— 字面 grep 会命中自己的注释。因此本版把文档字符串改写为
> 「不 import 任何模型 SDK 或网络库」，让字面 grep 证据自洽。

### 验收 6 —— Core 无业务耦合

```
业务词表（18 条）:
  视频  音频  字幕  剪辑  镜头  关键帧  财务  会计  发票
  知识库  文档库  文案  营销  订单  商品  支付  商城  客户(?!端)
命中: （无）
结论: ✅ AI 能力层无业务耦合
```

任务书只举了「视频 / 财务 / 知识库」三个词并说"等"，本报告采用上表 18 条扩展词表
（经你确认）。**用正则而非纯字符串**：`客户端`(client) 里含 `客户`(customer)，
朴素子串匹配会误报 —— 有专项测试 `test_word_boundary_rule_is_actually_needed` 说明这一点。

### 验收 7 —— embedding 跨 Provider 约束

约束同时写在**三处**，有测试逐一断言：

| 位置 | 内容 |
|---|---|
| `types.py` 模块文档 | 整段说明：维度可能不同、语义坐标系不同，**只有在 `provider` 与 `model` 都相同时两组向量才可比较** |
| `EmbeddingResult` 类注释 | 再次声明 + 指向模块说明 |
| `README.md` | 「向量的跨 Provider 约束」小节 |

**约束是可执行的，不只是文字**：`EmbeddingResult` 携带 `provider` / `model` / `dim`
三个字段，调用方据此判断"是否同一空间"；`dim` 是**算出来的属性**而不是存下来的字段，
不可能与实际向量长度不符。

### 验收 8 —— 全部测试通过

```
系统 Python    Ran 280 tests in 14.450s | OK  exit=0
venv           Ran 280 tests in 14.547s | OK  exit=0
```

全量测试 278 → **280**（本轮重写 `tests/test_ai_capability.py` 为 **77 项**）。

---

## 3. 过程中发现的问题与处理

### 3.1 报告给你、由你裁决的三个冲突

| # | 冲突 | 你的裁决 | 处理 |
|---|---|---|---|
| 1 | 原则 6「不做任何为未来预留的扩展点」与上一版为"未来增加 DeepSeek/OpenAI/Claude"而建的 `registry.py` 直接冲突 | 删掉 registry，改成显式装配 | 已删除 `registry.py`；`build_ai_service()` 改为两个显式分支。加第三个厂商要改这一处，但**仍然不改任何插件** |
| 2 | 验收 5 要求"grep 不到"，但解释这条规则的文档字符串本身要写出这些词 | — | 改写文档字符串为"不 import 任何模型 SDK 或网络库"，使字面 grep 自洽 |
| 3 | 「业务词」未界定，且 `客户` 会误命中 `客户端` | 用扩展词表 + 词边界 | 采用 18 条词表，`客户` 用 `客户(?!端)` |

### 3.2 按规格做的改动（不改范围）

| 项 | 改前 | 改后 |
|---|---|---|
| 视觉输出 | `TextResult`（纯字符串） | `VisionResult(description, labels, raw)` |
| 视觉输入 | 路径 / bytes | **仅本地路径**；bytes / URL / data URL 明确报错 |
| 向量字段 | `dimensions` | `dim` |
| 配置 | `ai.<provider>.options` | `ai.provider` / `ai.model` / `ai.options`（连接信息留在各 Provider 段） |
| 插件工具 | 4 个独立工具 | 1 个顺序自检工具 `ai_selftest` |

### 3.3 为满足原则 6 而**移除**的东西（需要你知晓）

| 移除项 | 原因 |
|---|---|
| `registry.py`（Provider 注册表） | 你的裁决：原则 6 禁止预留扩展点 |
| `AIService.set_default()` | 无调用方，属备用接口 |
| 按调用的 `provider=` / `model=` / `temperature=` / `max_tokens=` / `json_mode=` 参数 | 规格把输入定为 `prompt`（+可选 `system`）；参数统一由 `ai.options` 提供，避免同一参数两个来源 |
| 旧版兼容方法 `AIService.generate()` | 无调用方，属预留兼容层 |

### 3.4 本轮新修缺陷

| # | 缺陷 | 影响 | 修复 |
|---|---|---|---|
| B6 | `_embedding_step` 里为生成向量预览**重复调用了一次 embedding** | 每次自检白多一次向量化往返 | 改为在同一个结果上生成预览 |

（前几轮修复的 B1–B5 —— `/v1` 被剥、`status()` 默认联网 4 秒、`localhost` 慢 2 秒、
本地请求走系统代理、生成前多余预探测 —— 详见 §6，本轮未回归。）

### 3.5 范围外、只报告不修的问题

| 项 | 说明 |
|---|---|
| `plugins/knowledge_base/` | 该目录**不是本次任务的产物**，在上一轮工作期间出现，目前处于未跟踪状态。已核实：它的 `ast` 扫描干净，不影响验收 5；验收 6 只扫 `capabilities/ai/`，也不受影响。我**没有修改或提交它**。 |
| `qwen2.5vl:3b` 自称"通义千问" | 模型自身的输出，与本能力层无关；不改代码、不做兜底。 |

---

## 4. 我没做 / 做不了的部分

| # | 项 | 原因 |
|---|---|---|
| 1 | `vision_analyze` 不接受调用方自定义提示词 | 规格把输入定为"本地图片路径"，且原则 6 禁止预留扩展点。因此 Core 内置一段固定的抽取提示词（要求模型输出 `{"description", "labels"}`）。**后果**：调用方不能针对具体问题提问（例如"图里有没有人"）。这是刻意的限制，不是遗漏 —— 若需要，请下新任务。 |
| 2 | `openai_compatible` 未在真实云端服务上验证 | 任务书禁止云端账号 / 支付。正确性用**本地 mock 服务**逐字段验证（请求路径、`Authorization` 头、多模态 content、响应解析、缺密钥不发请求），全程不连接任何外部服务。 |
| 3 | 未内置 DeepSeek / OpenAI / Claude 的专属 Provider | 它们都走 `openai_compatible` 通道，只需改 `base_url` 与模型名。按原则 6 不做预留。 |
| 4 | 无重试 / 无限流 / 无并发控制 / 无流式输出 | 规格只要求三个能力的最小契约，这些属未要求的扩展。 |
| 5 | Core 里不写死任何模型名 | 规格要求"未配置时给出明确错误，不静默失败"。因此 `ai.model` 默认为空，未配置时调用报明确错误。**后果**：开箱首次调用 AI 前需要设一次 `ai.model`。 |
| 6 | 未处理 `plugins/knowledge_base` 的归属 | 未获裁决，按"范围外只报告"处理。 |

---

## 5. Provider 可替换性说明（原则 5）

`openai_compatible` 是一条**通道**而不是单一厂商。下表是配置差异，**代码与插件完全不变**：

| 服务 | `ai.openai_compatible.base_url` |
|---|---|
| DeepSeek | `https://api.deepseek.com/v1` |
| OpenAI | `https://api.openai.com/v1` |
| Claude（兼容端点） | 由服务方提供 |
| 自建网关 / 企业内网 | 由运维提供 |

加一个**新形状**（非 OpenAI 兼容）的 Provider：在 `providers/` 加一个模块 +
在 `service.py` 装配分支加一段。**不改任何插件。**

---

## 6. 前几轮修复的缺陷（本轮未回归）

| # | 缺陷 | 影响 |
|---|---|---|
| B1 | `base_url()` 把 OpenAI 的 `/v1` 后缀一并剥掉 | 请求打到 `/chat/completions` 上 404，该通道整条不可用 |
| B2 | `status()` 默认做网络探测 | 一次状态查询 4 秒；状态查询在热路径上（插件列表、界面刷新） |
| B3 | 默认地址用 `localhost` | Windows 先试 IPv6 `::1` 再回退 IPv4，实测每次探测 2.07s vs `127.0.0.1` 的 0.004s |
| B4 | 本地模型请求会走系统代理 | 开发机代理配置会被 `getproxies()` 读到，连回环地址的请求可能绕到代理上 |
| B5 | 每次生成前做可用性预探测 | 白多一个往返；且"连无人监听的端口"在本机不退化，会挂满超时（2s） |

B2–B5 与测试服务关闭轮询优化后，全套测试从 37s 降到 14.5s。

---

## 7. 复现步骤

```powershell
cd F:\Downloads\XBC

# 1) 全量测试（双环境）
python -m unittest discover -s tests
.\.venv\Scripts\python.exe -m unittest discover -s tests

# 2) 只跑 AI 能力层测试
python -m unittest tests.test_ai_capability -v

# 3) 真实本地模型：先准备模型，再设好 ai.model
ollama pull qwen2.5vl:3b
ollama pull nomic-embed-text
# 在 config.json 里写：
#   "ai": { "provider": "ollama", "model": "qwen2.5vl:3b",
#           "embedding_model": "nomic-embed-text" }
python run.py tool call ai_selftest --kwargs "{\"prompt\": \"用一句话介绍你自己\"}"

# 4) 切换 Provider：只改配置，不改任何代码
#    "ai": { "provider": "openai_compatible", "model": "<该服务的模型名>",
#            "openai_compatible": { "base_url": "https://api.deepseek.com/v1",
#                                   "api_key_secret": "deepseek_key" } }
#    并在 config/secrets.json 里放 { "deepseek_key": "sk-..." }
```

> **Windows 提示**：PowerShell 会吃掉 `--kwargs` 里的双引号。
> 请用上面的反引号转义写法，或 `cmd /c "python run.py ... --kwargs \"{...}\""`。

---

## 8. 结论

TASK-007 的 8 项验收标准全部通过，证据可复现。

这一版交付的重点是**把"模型"这件事从插件里彻底拿走，并且不给内核留任何用不上的口子**：

- 插件只认识 `ctx.ai` 的三个方法，不认识任何厂商、模型名或端口 ——
  由**全文件哈希**与**字面 grep + ast 扫描**双重锁定；
- 换 Provider 是配置动作，不是代码动作；
- 内核只有一个 `AIService`、一个抽象基类、两个真实实现，**没有注册表、没有备用接口、
  没有按调用的可选参数**；
- 约束（插件不得直连模型、Core 不得含业务语义）由会失败的测试强制，不靠自觉。

**未做的事**：Agent 系统、自动决策 / 工作流、Prompt 商城、云端账号 / 支付、UI 美化、
任何业务逻辑、任何"为将来预留"的空接口。
