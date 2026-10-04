# TASK-008 交付报告 —— AI Capability 的第一次真实消费

| 项目 | 内容 |
|---|---|
| 任务 | TASK-008 vision 定向提问 + video_analyzer 接入 |
| 环境 | Windows · Python 3.13.15 · Ollama 0.35.1（`qwen2.5vl:3b` / `nomic-embed-text`） |
| 结论 | **7 项验收标准全部通过**；全量 **305** 项测试通过（系统 Python + venv 双环境） |
| 核心产出 | 本报告第 8 节《接口可用性反馈》 |

---

## 1. 实现了什么（对照任务书逐条）

| 任务书 | 实现 | 位置 |
|---|---|---|
| 1. `vision_analyze` 新增 `question`（唯一允许的 Core 改动） | 新增 `VisionAnswer` 类型 + `vision_prompt()` 按模式出提示词 + `parse_vision_payload(field=...)` | `capabilities/ai/` |
| 2. video_analyzer 接入 AI Capability | 新增工具 `video_annotate`：「镜头切分 → 抽取关键帧 → 逐帧 `ctx.ai.vision_analyze(question=...)` → 按镜头汇总标签」 | `plugins/video_analyzer/` |
| 3. 报告接口设计反馈 | 第 8 节 | 本报告 |

### 1.1 Core 侧的改动（严格限定在 `vision_analyze`）

| 文件 | 改了什么 |
|---|---|
| `types.py` | 新增 `VisionAnswer`；模块文档补充"两种模式 / 两种类型"对照表 |
| `request.py` | `VisionRequest` 新增 `question: str \| None = None` |
| `provider.py` | 新增 `vision_prompt(question)` 与提问模式的提示词；`parse_vision_payload(text, *, field="description")` 增加 `field` 参数（默认值保证原行为不变） |
| `providers/ollama.py` | `vision_analyze` 按 `request.question` 分支，返回 `VisionResult` 或 `VisionAnswer` |
| `providers/openai_compatible.py` | 同上 |
| `service.py` | `AIService.vision_analyze(images, *, question=None)` |
| `ai/__init__.py` | 导出 `VisionAnswer`、`vision_prompt` |

**没有动**：`text_generate`、`embedding`、配置系统、能力装配、任何其他能力、任何其他内核文件。

### 1.2 两种模式的契约

| 模式 | 入参 | 返回类型 | 承载答案的字段 | 提示词 |
|---|---|---|---|---|
| 描述 | 不传 `question` | `VisionResult` | `description` | 内置固定提示词（与 TASK-007 逐字相同） |
| 定向提问 | 传 `question` | `VisionAnswer` | `answer` | 内置前缀 + 调用方问题原文 |

两种模式**用两个类型、两个字段名**，不用同一个字段承载两种语义。

### 1.3 video_analyzer 的抽帧策略（任务书要求说明）

沿用 TASK-005 已实现的策略，未新增：

- **镜头切分**：FFmpeg `select='gt(scene,T)',showinfo` 解析 `pts_time` 得到切点，
  再用 `min_shot_seconds`（默认 0.5s）把过短的镜头并回前一个，首尾自动补齐
- **抽帧**：每个镜头取 `annotate_frames_per_shot` 帧（默认 **1**），
  在第 k 帧的 `(k+0.5)/n` 位置取样 —— **刻意避开转场瞬间**
- **送 AI 的帧数**：默认每镜头 1 帧，上限 4；单次调用总上限 `MAX_ANNOTATED_FRAMES = 24`。
  默认取 1 是因为 AI 调用远比抽帧慢，先保证"跑得完"

---

## 2. 验收标准逐条证据

### 验收 1 —— `vision_analyze` 两种模式

**（a）不带 `question`：行为与 TASK-007 一致**

```
输入图片: frame.png (84400 字节)
返回类型   : VisionResult
description: 这是一张色彩斑斓的图像，展示了一个复杂的几何形状，周围环绕着鲜艳的色彩。
labels     : ['几何形状', '色彩斑斓', '抽象艺术', '复杂图案', '几何图形', '抽象几何', '色彩几何', '艺术几何']
字段       : ['description', 'labels', 'model', 'provider', 'raw', 'usage']
```

`VisionResult` 的字段与 TASK-007 完全相同；**TASK-007 的 80 项测试全部通过、未做任何修改**
（`tests/test_ai_capability.py` 只新增测试，没有改动原测试）。

**（b）带 `question`：返回 `{answer, labels}`，answer 与问题相关**

```
问题       : 这个画面里能看出什么颜色？
返回类型   : VisionAnswer
answer     : 红色、绿色、黄色、蓝色
labels     : ['颜色', '色彩', '图像', '抽象', '几何', '艺术', '视觉', '视觉效果']
字段       : ['answer', 'labels', 'model', 'provider', 'question', 'raw', 'usage']
```

回答直接对应"什么颜色"，与问题相关。

**（c）两种结构明确区分**

```
描述模式有 description / 无 answer : True / True
提问模式有 answer / 无 description : True / True
共有字段: ['labels', 'model', 'provider', 'raw', 'usage']
```

### 验收 2 —— video_analyzer 真实跑通

**输入视频**（ffmpeg 合成）：

```
distinct_scenes.mp4   6.0s   320x240   437807 字节
三段画面语义完全不同：mandelbrot 分形图案 / smptebars 电视彩条 / life 细胞自动机
场景切点: 2.0s、4.0s（另有一个 4.2s 的伪切点被 min_shot_seconds 合并）
```

> 说明：本机没有实拍素材，视频由 ffmpeg 的 lavfi 源合成 —— 它是**真实的 MP4 文件**
> （真实编码、真实解码、真实抽帧），但不是摄像机拍摄的画面。
> 下面"逐镜头标签互不相同"正是"标签来自 AI 对各自画面的理解"的证据。

**输出**（真实 Ollama）：

```
tool ok=True
question         : 这个画面的主要内容是什么？属于什么场景或拍摄类型？
shot_count       : 3
frames_analyzed  : 3
ai               : {'provider': 'ollama', 'model': 'qwen2.5vl:3b'}
errors           : []

镜头 0  [0.0s → 2.0s]   回答: 这个画面的主要内容是一个复杂的几何图形，属于抽象艺术场景。
                        标签: ['几何图形','抽象艺术','复杂图案','色彩斑斓','视觉艺术','抽象几何','色彩几何','视觉效果']
镜头 1  [2.0s → 4.0s]   回答: 测试画面
                        标签: ['测试画面','彩色条纹','电视测试','色彩校准','电视屏幕','测试图像','色彩校正']
镜头 2  [4.0s → 6.0s]   回答: 这个画面的主要内容是星空，属于夜空场景或拍摄类型。
                        标签: ['星空','夜空','拍摄','场景','宇宙','天体','自然','天文']

标签集合互不相同: 3 / 3 组
```

- 耗时：首次 **33.0s**（模型冷加载），模型常驻后 **2.1s**
- **标签逐镜头不同**（3/3 组互不相同），说明标签来自 AI 对各自画面的理解，不是硬编码
- 镜头 2 的 `life` 细胞自动机（黑底绿点）被理解为"星空 / 夜空" —— 这是**模型的解读**，
  本报告不声称它正确；记录在此是为了说明输出确实来自模型而非映射表

### 验收 3 —— 插件无直接模型调用

扫描范围严格按验收标准：`ai_test_plugin` 与 `video_analyzer`。

```
扫描: ai_test_plugin, video_analyzer   共 2 个文件
词表: openai, ollama, anthropic, litellm, requests, httpx, aiohttp, urllib, http.client
大小写不敏感 grep 命中: （无）
ast import 命中       : （无）
```

**这里发现并修复了 TASK-007 验收检查的一个漏洞**：

TASK-007 的字面 grep 是**大小写敏感**的，于是 `video_analyzer` 文档字符串里
「不接 **Ollama**」（大写 O）能躲过检查。本任务在做真实接入时发现，处理如下：

1. 改掉 `video_analyzer` 的措辞（**在本任务范围内**，验收 3 点名了这个插件）
2. 新增大小写不敏感的扫描测试，范围按验收标准限定在这两个插件
3. **范围外只报告**：全量 `plugins/` 大小写不敏感扫描会命中
   `plugins/knowledge_base/plugin.py`（含 `ollama`，但 `ast` 扫描显示它**没有真实 import**）。
   该插件不是本任务的产物，**我没有修改它**。

### 验收 4 —— Core 无业务耦合

```
业务词表（18 条，含词边界）:
  视频 音频 字幕 剪辑 镜头 关键帧 财务 会计 发票 知识库 文档库 文案 营销 订单 商品 支付 商城 客户(?!端)
命中: （无）

本次新增的业务相关标识符:
  video_analyzer 里存在 FRAME_QUESTION : True
  capabilities/ai/ 里出现 FRAME_QUESTION: False
```

"问什么问题"是业务语义，**留在插件里**（`plugins/video_analyzer/plugin.py` 的 `FRAME_QUESTION` 常量），
内核只负责"把问题转达给模型并解析回答"。

### 验收 5 —— Core 改动范围锁定

用两级哈希证明（脚本 `xbc_scope_hash.py`，改动前先记录基准）：

```
=== 非 vision 代码单元（不允许变化）===
  受检单元: 17
  发生变化: （无）

=== capabilities/ai/ 之外的内核文件（不允许变化）===
  受检文件: 37
  内容变化: （无）
  新增文件: （无）
  删除文件: （无）
```

17 个受检单元（按 AST 取源码片段逐一哈希）：

| 文件 | 单元 |
|---|---|
| `types.py` | `TextResult`、`EmbeddingResult` |
| `request.py` | `TextRequest`、`EmbeddingRequest` |
| `service.py` | `AIService.text_generate`、`AIService.embedding` |
| `providers/ollama.py` | `text_generate`、`embedding`、`_generate`、`_post`、`_generation_options`、`_legacy_embed` |
| `providers/openai_compatible.py` | `text_generate`、`embedding`、`_chat`、`_post`、`_messages` |

37 个文件 = `src/xbc/` 下 `capabilities/ai/` **之外**的全部内核源码，
逐一字节哈希一致。

**一处刻意的取舍**：`VisionAnswer` 只从 `xbc.core.capabilities.ai` 导出，
**没有**加进 `capabilities/__init__.py` 的 re-export 列表 ——
那样会让 `capabilities/ai/` 之外也出现改动，破坏"改动范围可测量"这条。
代价是 re-export 列表里 `VisionResult` 有、`VisionAnswer` 没有；记在此处供裁决。

### 验收 6 —— 全部测试通过

```
系统 Python    Ran 305 tests in 17.446s | OK  exit=0
venv           Ran 305 tests in 17.420s | OK  exit=0
```

283 → **305**（`test_ai_capability.py` 80 → 99；`test_video_analyzer.py` 18 → 20）。

### 验收 7 —— 报告含接口可用性反馈

见第 8 节，并明确回答了"是否需要返工"。

---

## 3. 真实视频的输入与输出记录

见验收 2。补充两点：

- **抽帧产物**：写在插件自己的数据目录 `<data>/video_analyzer/frames/distinct_scenes_<hash>/`，
  帧文件为 `shot000_f01.jpg` 形式；内核保证与其他插件、与用户文件隔离
- **未使用 mock**：验收 2 的记录来自本机 Ollama（`qwen2.5vl:3b`），
  mock 只用于自动化测试（`VideoAnalyzerConsumesAICapabilityTests`）

---

## 4. vision_analyze 两种模式的调用记录

见验收 1。两种模式在同一张图上各调一次，字段集合已列出。

补充一条自动化证据：`VisionQuestionModeTests` 里有 13 条测试锁定
"不传 `question` 返回 `VisionResult` / 传了返回 `VisionAnswer` / 两种字段集合不相交 /
提问模式不出现 `description` / 描述模式不出现 `answer`"。

---

## 5. 插件无直接调用证据（grep + ast）

见验收 3。两条证据互补：字面 grep 覆盖文档与注释（这次正是它抓到了问题），
`ast` 覆盖真实 import（防止别名或字符串拼接绕过）。

---

## 6. Core 改动范围证据（哈希对比）

见验收 5。

---

## 7. 过程中发现的问题与处理

| # | 问题 | 性质 | 处理 |
|---|---|---|---|
| 1 | TASK-007 的字面 grep 是**大小写敏感**的，`不接 Ollama` 躲过检查 | TASK-007 验收检查不完整 | 改掉该措辞；新增大小写不敏感测试。**这是本任务真实接入时才暴露出来的** |
| 2 | `plugins/knowledge_base/plugin.py` 含被禁词（无真实 import） | **范围外** | 只报告，未修改（工作规则第 6 条） |
| 3 | `test_video_analyzer.py` 硬编码"4 个工具"与能力集合，插件加工具后 3 条测试假失败 | 测试脆弱 | 改为从清单推导（`declared_tool_count()`），并新增"声明了 ai 就必须真能拿到"的测试 |
| 4 | 我在编辑 `tests/test_ai_capability.py` 时**两次**误删换行造成 `IndentationError` | 工作方式问题 | 已修复；记在此处，避免重复 |
| 5 | 临时数据目录未配置 `ai.model` 时 `video_annotate` 报"3 帧全部失败" | **设计正确** | 保留：一帧都没成功必须明确失败，返回一堆空标签比失败更糟。已有测试锁定 |

---

## 8. 接口可用性反馈（本任务核心产出）

以下结论全部来自这一次真实消费（video_analyzer 用 `video_analyze` 的提问模式处理 6 秒 / 3 镜头的视频），
不是推演。

### 8.1 `vision_analyze` 的返回结构够不够用？

**够用。** 真实消费方只用了 4 个字段就完成了整条链路：

| 字段 | 用途 |
|---|---|
| `answer` | 每个镜头的画面结论（原样进报告） |
| `labels` | 汇总成镜头标签集合与全片词表 |
| `provider` / `model` | 标注"这批标签是哪个模型给的" |

`VisionAnswer.question` 的回显让输出自带归因（这份结论是针对什么问题得出的），有用。

### 8.2 有没有"想拿的信息接口没给"？

**有两条，都是具体的。**

**（1）没有"同一镜头的多帧合并问一次"的表达方式。**

- video_analyzer 的自然单位是**镜头**，每镜头抽 1–4 帧
- 接口只能**逐帧**问：4 帧 = 4 次调用 = 4 份成本，而模型**看不到同镜头的其他帧**
- 插件只能在事后按字符串合并标签（去重 + 计数），无法让模型看到"这是同一个场景的几帧"

实测代价：`annotate_frames_per_shot` 从 1 调到 2，AI 调用与耗时直接翻倍
（3 镜头：1 帧时 3 次调用 / 2 帧时 6 次调用）。

**（2）没有 token 用量的汇总。**

逐帧调用会产生 N 份独立的 `usage`（每份是 `{prompt_tokens, completion_tokens}`）。
接口不提供累计值，调用方若要统计成本得自己加。3 帧时无所谓，
`MAX_ANNOTATED_FRAMES = 24` 时就是 24 份小字典。

### 8.3 有没有"接口给的字段用不上"？

**有四个，都是事实记录。**

| 字段 | 真实使用情况 |
|---|---|
| `VisionAnswer.question` | 同一支视频的每一帧问同一个问题，结果里回显 3 次（帧数更多就是 N 次）。插件在汇总层**只保留了一份** |
| `raw`（`VisionResult` / `VisionAnswer`） | 保留模型原始输出是为了排障 —— 正常路径上**一次都没用到** |
| `usage` | 逐帧调用下没人会去累加，本次完全未读 |
| `images: list[str]` | 唯一真实消费方**每次都只传 1 个路径**。列表形态在真实场景里没有被用到 |

**需要说明的是：这四条都不构成"该删"的理由。**
`raw` 是排障资产、`usage` 在单次调用场景有用、`question` 回显让单条结果自解释。
但它们是"接口给了、消费者不用"的事实，按任务书要求如实列出。

**另外一条不算"用不上"但要指出**：`labels` 是**自由文本，不保证可枚举**。
实测 6 秒视频 3 个镜头共产出 **21 个标签**，其中镜头 0 的 8 个里
`几何图形 / 抽象几何 / 色彩几何` 是近义近邻，插件只能按字符串精确去重。
接口文档里**没有声明这一点**，调用方容易误以为 `labels` 可以直接当枚举用于筛选。
这条是文档缺口，不是结构缺陷。

### 8.4 如果重做 TASK-007，我会怎么改这三个接口？

**先说结论：`vision_analyze` 的扩展方式不需要改；但有三处我会在重做时调整。**

#### 改动 1（确定要改）：提示词的单复数与 `images: list[str]` 不自洽

现状：

| 位置 | 内容 |
|---|---|
| 接口签名 | `vision_analyze(images: list[str] \| str, ...)` —— 收**列表** |
| 内置提示词 | `"请分析这张**图片**，并**只**输出一个 JSON 对象…"` —— 说**单数** |
| 提问模式提示词 | `"请根据**这张图片**回答问题…"` —— 同样说单数 |

这是**真实的语义不自洽**：接口允许传多张，提示词却按单张措辞。
真实消费方因此每次只敢传 1 张 —— 传多张的语义是"多张图各自分析"还是"多张图合成一次判断"，
从接口和提示词里都读不出来。

**重做方案**：把 `images` 的语义在提示词里写清楚。二选一：

- 若语义是"同一次观察的多个视角" → 提示词改成「请综合这几张图片…」，
  这样 4 帧可以一次调用完成，直接解决 8.2(1) 的成本问题
- 若语义是"多张图各自分析" → 接口改成单张 `image: str`，把"多张"这件事交给调用方循环，
  避免给出一个语义模糊的列表

**我倾向第一种**：它与真实消费方的需求（同一个镜头的几帧应该一起看）一致。

#### 改动 2（确定要改）：`labels` 的语义没有声明

现状：`labels: list[str]` 没有任何"自由文本、不保证可枚举、不做归一"的说明。

**重做方案**：在 `VisionResult.labels` / `VisionAnswer.labels` 的注释里加一句，
并明确"归一化与去重属于调用方的业务，能力层不做"。这和我已经在 `embedding` 上做的
（跨 Provider 约束写进模块文档 + 类注释）是同一种做法 —— 保持内部一致。

#### 改动 3（待定，取决于下一个消费方）：`answer` 是纯文本，没有结构化选项

本次消费方只需要自由文本标签，**没遇到问题**。
但下一个消费方如果要的是"图里有没有人"（布尔）或"室内 / 室外"（枚举），
就只能去解析 `answer` 的自然语言。

**我不建议现在就改**（任务书禁止"为将来预留"）。
记录在此作为**返工触发条件**：当出现第一个需要结构化判断的消费方时再动。

#### 明确不需要改的

- **两种模式的分离方式**（两个类型、两个字段名）：真实用下来清晰，没有歧义
- **`provider` / `model` 随结果返回**：video_analyzer 的输出里用到了，用于标注标签出处
- **提问模式走 `json_mode`**：模型稳定返回可解析的 JSON，`parse_vision_payload` 的容错兜底一次都没触发
- **只接受本地图片路径**：本次抽帧产物就是本地文件，限制没有造成摩擦

### 8.5 是否需要对 TASK-007 接口做返工？

**不需要返工。** 依据：

1. **扩展方式成立**：TASK-007 的 80 项测试**一条未改、全部通过**；
   17 个非 vision 代码单元与 37 个 AI 层外文件哈希零变化 —— 向后兼容是可验证的，不是承诺。
2. **真实消费方零摩擦跑通**：video_analyzer 在**没有改动 Core 任何其他部分**的前提下
   完成了「关键帧 → AI 理解 → 标签」整条链路，包括错误处理与降级。
3. **发现的问题都属于"下次顺手修"，不属于"用不了"**：
   8.4 的改动 1、2 是文档与措辞层面，改动 3 是待触发项。三项都不会阻塞当前使用。

**但有两处应当在下次动 AI 层时修掉**：提示词单复数不自洽（8.4 改动 1）、
`labels` 语义未声明（8.4 改动 2）。
按任务书要求，我**没有私自改 Core 接口**，只记录在此等下一个任务裁决。

---

## 9. 我没做 / 做不了的部分

| # | 项 | 原因 |
|---|---|---|
| 1 | 没有实拍视频素材 | 本机没有摄像机素材，用 ffmpeg lavfi 合成真实 MP4。**标签逐镜头不同**是"来自 AI"的证据 |
| 2 | 未修 `plugins/knowledge_base` 的被禁词命中 | 范围外，工作规则第 6 条：只报告 |
| 3 | 未把 `VisionAnswer` 加进 `capabilities/__init__.py` 的 re-export | 会让改动范围超出 `capabilities/ai/`，破坏验收 5 的可测量性。已在验收 5 记录取舍 |
| 4 | 未做"多帧一次调用" | 属于 8.4 改动 1，需要改 TASK-007 的提示词语义，超出本任务授权的"只加一个可选参数" |
| 5 | 未做结构化 `answer`（布尔 / 枚举） | 任务书禁止"为将来预留"；且本次消费方不需要 |

---

## 10. 复现步骤

```powershell
cd F:\Downloads\XBC

# 1) 全量测试（双环境）
python -m unittest discover -s tests
.\.venv\Scripts\python.exe -m unittest discover -s tests

# 2) 只跑 AI 能力层 / video_analyzer
python -m unittest tests.test_ai_capability -v
python -m unittest tests.test_video_analyzer -v

# 3) 真实链路（需 Ollama 与已配置的 ai.model）
python run.py tool call video_annotate --kwargs "{\"path\": \"D:/clip.mp4\"}"

# 4) Core 改动范围复核
python xbc_scope_hash.py compare <改动前记录的基准>.json
```

---

## 11. 结论

TASK-008 的 7 项验收标准全部通过，证据可复现。

**接口可用性结论**：TASK-007 建立的 AI Capability 接口在第一次真实消费中**站住了** ——
唯一需要的扩展（定向提问）按向后兼容方式落地，Core 其余部分一个字节未动，
真实消费方零摩擦完成了「关键帧 → AI 理解 → 标签」链路。

**发现 3 处可改进项**（详见第 8 节）：提示词单复数与 `images` 列表不自洽、
`labels` 语义未声明、`answer` 无结构化选项。前两项建议下次动 AI 层时修，
第三项作为返工触发条件记录。

**未做的事**：修改除 `vision_analyze` 以外的 Core 能力、新增 Core 能力、
Agent / 工作流 / 自动决策、云端账号 / 支付 / 商城、UI 美化、视频之外的其他业务插件、
给 `vision_analyze` 加其他预留参数。
