# Harness 工作规则 —— 落盘部分

> **这个文件是什么**：《Harness 工作规则》本身是**会话注入**的（仓库里没有这个文件，
> DSH 安装目录里也没有）。这里只把**需要长期约束代码与架构、且必须可被引用**的那一部分
> 落到磁盘，避免它只活在对话里、下一个人看不到。
>
> 其余规则（任务书边界、报告先于执行、验收必须可复现证据、交付报告四节、
> 范围外只报告、交付前测试全过等）仍以会话注入为准，此处不重复。
>
> 落盘的其他常驻规则见 [TASKS.md](TASKS.md) 开头的《常驻规则：调研结果的使用》。

---

## AI 能力隔离

### 原始规则

> 插件禁止直接调用模型 SDK；模型调用必须经过 Core 的 AI 能力层（`ctx.ai.*`）。

### 细化（TASK-010 架构裁决，2026-10-05）

TASK-010 引入"插件提供本地模型 Provider"之后，"直接调用模型 SDK"这句话出现了歧义：
**实现一个 Provider 必然要 import 它运行模型所需的东西**。据此细化为：

#### 1. 插件**业务代码**禁止

- import 任何**远端模型 SDK**：`openai` / `ollama` / `anthropic` / `litellm` 等
- import 任何 **HTTP 客户端**：`requests` / `httpx` / `aiohttp` / `urllib` / `http.client` 等
- 直接发起**远端模型调用**

判据是"**是否绕过能力层去够远端模型**"，不是"有没有出现某个词"。

#### 2. 插件提供的 **Provider 实现**允许

- import **本地推理运行时**（`onnxruntime` 等）
- **但业务代码不得直接调用该 Provider** —— 必须通过 `ctx.ai.*` 走 Core Capability

也就是说：**Provider 文件是"模型实现"，业务文件是"模型使用者"**，
两者在同一个插件目录里可以共存，但职责不能混。

#### 3. 禁词表

| 类别 | 处理 |
|---|---|
| 远端 SDK / HTTP | **保留在禁词表**，照旧 0 命中 |
| 本地推理运行时 | **不在禁词表里**；改为约束"**仅限 Provider 文件使用**" |

**实测核对（无需改代码）**：项目现有的禁词表**本来就不含本地推理运行时** ——

```python
# tests/test_ai_capability.py
FORBIDDEN_IN_PLUGINS = [
    "openai", "ollama", "anthropic", "litellm",
    "requests", "httpx", "aiohttp", "urllib", "http.client",
]
```

它针对的就是"远端 SDK / HTTP"。所以第 3 条的"从禁词表移除"**在现有表上已经是成立的**，
不需要动任何代码。

### 判定与证据（TASK-010 §7）

| 文件 | 性质 | 对禁词表 | 结论 |
|---|---|---|---|
| `plugins/video_analyzer/plugin.py` | **业务代码** | **0 命中** | ✅ 只用 `ctx.ai.embed_images()` / `ctx.ai.provider()` |
| `plugins/video_analyzer/xbc_va_clip.py` | **Provider 实现** | 不在禁词表内 | ✅ `import onnxruntime` 属于本类允许项 |

端到端证据：中文 CLIP 的图片向量**只经** `ctx.ai.embed_images()` 产出，
文本向量**只经** `ctx.ai.embedding()` / `ctx.ai.provider()` 产出；
业务代码里没有一处 import 推理运行时，也没有一处直接调用 Provider 实例的方法
（除了经 `ctx.ai.provider(...)` 取回对象后调用其能力方法 —— 那正是能力层路由的结果）。

完整判定与理由见
[《TASK-010 交付报告》](task-010-retrieval-quality-report.md) §6。

### 与既有表述的关系

磁盘上原本只有两处相近表述，都在代码注释里（本次**未改动代码**）：

| 位置 | 原文 |
|---|---|
| `src/xbc/core/capabilities/ai/__init__.py` | 插件通过 `ctx.ai` 使用；**业务插件不得直接调用模型** |
| `src/xbc/core/capabilities/ai/provider.py` | 插件只依赖 `AIService`，**永远不直接依赖 Provider** |

两者与本节一致：**"业务插件"是约束对象，"Provider"是允许的例外**。
本节把"业务代码 / Provider 实现"的边界写明，让这条规则可执行而不是靠语义猜测。

### 尚未机械化的部分（待裁决）

"**本地推理运行时仅限 Provider 文件使用**"这条**目前只写在文档里，没有测试守着**。
若要机械 enforce，需要一个新检查（例如：扫描 `plugins/` 下每个 `.py`，
若某文件含本地运行时 import，则要求它同时定义了一个 `ModelProvider` 子类）。

那属于**新增代码**，本次按要求（只动规则文档）**未做**。
