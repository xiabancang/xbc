# TASK-010 交付报告 —— 检索质量修复

| 项 | 内容 |
|---|---|
| 任务 | TASK-010 检索质量修复（存储缺陷 + 中文 CLIP 图片嵌入 + 双路融合） |
| 状态 | **完成**（按裁决：扩 Core 支持图片嵌入 / 不做融合 / 294MB 可接受） |
| 环境 | Windows · Python 3.13.15 · Ollama 0.35.1 · onnxruntime 1.30.0 |
| 测试 | **373 项通过**（系统 Python + venv 双环境） |
| Core 改动 | **5 个文件，+79 −10 行，全在 `capabilities/ai/` 内**；AI 层之外零改动 |
| **检索质量** | **top-1 2/10 → 10/10**（目标 ≥6/10） |

---

## 0. 结论摘要

| 验收标准 | 结果 |
|---|---|
| 1 存储缺陷已修（可审计 / 可追溯 / 可复现） | ✅ |
| 2 中文 CLIP 图片嵌入已接入（rn50 / 许可证 / 部署 / 维度） | ✅ |
| 3 双路融合 + 融合前后对比 | ✅ 对比已给；**实测表明不该融合**，故不实现（如实报告） |
| 4 提升到 ≥6/10 | ✅ **10/10** |
| 5 插件无直接模型调用 | ✅ 项目定义的检查 0 命中（见 §6 的诚实说明） |
| 6 Core 无业务耦合 | ✅ 0 命中 |
| 7 全部测试通过（双环境） | ✅ 373 × 2 |
| 8 报告含逐条证据 / 许可证登记 / 对比数据 / 部署说明 | ✅ |

---

## 1. 第一部分：存储缺陷修复（已完成）

### 1.1 缺陷回顾

TASK-009 的向量由 `answer + labels` 算出，**但库里只存了 `labels`** ——
真正被嵌入的 `answer` 从未落库，检索行为无法审计；长回答还稀释了标签区分度。

### 1.2 修复

`vectors` 表新增三列：`embed_input`（**真正被嵌入的输入**）、`kind`、`created_at`。

**为什么叫 `embed_input` 而不是 `embed_text`**：TASK-010 之后同一个表里既有
**文本向量**（`embed_input` = 原文）也有**图片向量**（`embed_input` = 图片路径）。
沿用项目的原则"一个字段不承载两种语义"，名字必须能同时说清两者。

新增工具 **`library_audit`**（第 13 个）作为审计入口。

### 1.3 证据

**（a）审计抽样 —— 原来看不见的那段 `answer` 现在可见**

```
[ollama] shot_centroid  embed_input=这个画面的主要内容是一个复杂的几何图形，
                                  属于抽象艺术场景。 几何图形 抽象艺术 色彩斑斓 …
[chinese_clip] frame    embed_input=C:\…\frames\shot000_f01.jpg
```

两类向量的 `embed_input` 语义一目了然：文本空间存原文，图片空间存路径。

**（b）可复现性**：同一库同一查询跑两次 → 结果字节完全一致 ✅；
同一素材再扫 → 按内容哈希跳过、库不变 ✅；
强制重分析 → 差异可 diff ✅。

**（c）迁移**：v1 → v3 与 v2 → v3 都把数据搬过去，旧向量如实标注 `auditable: false`。

---

## 2. 第二部分：中文 CLIP 图片嵌入

### 2.1 许可证（已登记进 [docs/research/README.md](research/README.md)）

| 项 | 内容 |
|---|---|
| 权重 | `OFA-Sys/chinese-clip-rn50` |
| **许可证** | **Apache-2.0** ✅ 允许商业使用 |
| 核实方式 | `huggingface_hub.HfApi().model_info(...)` 读 `cardData`，非从 README 推断 |
| 代码 | `OFA-Sys/Chinese-CLIP`，**MIT** |

**为什么不用 `vit-base-patch16`**（任务禁止项）：它在 HF 上**未声明许可证**。
代价是 rn50 **不是 transformers 格式**（HF 上只有 `clip_cn_rn50.pt`），需要自己导出 ONNX。

### 2.2 部署方式：ONNX，**运行时不需要 torch**

| 环节 | 依赖 | 在哪跑 |
|---|---|---|
| 导出（一次性） | torch + torchvision + timm + cn_clip + onnx | **开发机** |
| **运行** | **onnxruntime + numpy + pillow** | 用户机 |

导出脚本：[scripts/export_chinese_clip_onnx.py](scripts/export_chinese_clip_onnx.py)
（自带 torch↔ONNX 等价校验，不等价则退出码非 0）。

产物：`vision_model.onnx` 146.1 MB + `text_model.onnx` 147.6 MB + `vocab.txt` 0.1 MB
≈ **294 MB**（已获裁决接受）。

**踩坑记录 —— `cn_clip` 装不上**：它依赖 `lmdb`，而 lmdb 在 Windows 上要从源码编译
（需要 `patch-ng`）。但 lmdb **只用于加载训练数据**，推理用不到 ——
用 `pip install --no-deps cn_clip` 再单独补 `timm/six/regex/ftfy` 即可绕开。脚本注释里写了。

### 2.3 正确性

| 验证 | 结果 |
|---|---|
| ONNX vs torch（14 帧 + 10 查询） | 余弦 **全部 1.00000000**（精确等价） |
| 图像/文本向量维度 | **1024 / 1024**（同一模型、同一空间） |
| **自研 BERT 分词器 vs 官方 `cn_clip`** | **23/23 条逐条等价**（空串/超长/全角/重音/标点/中英混排） |
| 图像预处理 vs 官方 | 逐元素最大差 **0.00000000** |

分词器是**独立实现**（按 BERT 标准算法自己写，不搬代码），再用官方实现做等价性验证。

### 2.4 ⚠️ 预处理必须与官方逐字一致

官方 `image_transform` 是 `Resize((224, 224))` —— **直接拉成正方形**，
不是标准 CLIP 的 shortest-edge + center-crop。

**用错预处理：top-1 从 10/10 掉到 7/10。**

> 对照参考：amon-hen 的 MobileCLIP2 结论**正好相反**（它发现拉伸会损失 0.03 余弦而改用裁切）。
> **不同模型的预处理要求相反，必须按各自官方实现来。**

---

## 3. 第三部分：双路融合 —— 实测表明**不该做**

调研报告基于 `vit-base-patch16` 的 6/10 数据，建议双路融合。
**实测（rn50 + 官方预处理）推翻了它。**

| 路线 | top-1 | 前 3 |
|---|---|---|
| Caption 单路 | 2/10 | 3/10 |
| **中文 CLIP 单路** | **10/10** | **10/10** |
| **RRF 融合（等权，k=60）** | **8/10** | 9/10 |

**融合把 top-1 从 10/10 拉到 8/10。** 根因：图片路已满分、文本路只 2/10，
**等权 RRF 给弱路相同话语权，纯拖后腿**。两路质量差距越大，伤害越明显。

按任务书"如实报告，不要为了看起来成功而调数据"，**结论是不做融合，走单路**。

---

## 4. 第四部分：端到端实测（通过插件自己的工具）

同批 **14 帧 / 10 个中文查询**，真实 Ollama + 真实中文 CLIP：

| 向量空间 | 维度 | top-1 | 前 3 | 平均延迟 |
|---|---|---|---|---|
| **[image] chinese_clip / chinese-clip-rn50** | 1024 | **10/10** | **10/10** | **82 ms/查询** |
| [text] ollama / nomic-embed-text | 768 | 2/10 | 3/10 | 518 ms/查询 |

逐条：

| 查询 | 图片空间 | 文本空间（Caption） |
|---|---|---|
| 星空 | life (1) ✓ | life (1) ✓ |
| 宇宙星云 | life (1) ✓ | life (1) ✓ |
| 夜景 | life (1) ✓ | testsrc2 (13) ✗ |
| 抽象的彩色几何图案 | mandelbrot (1) ✓ | testsrc2 (4) ✗ |
| 分形图案 | mandelbrot (1) ✓ | testsrc2 (6) ✗ |
| 电视信号测试图 | smptebars (1) ✓ | mandelbrot (8) ✗ |
| 彩条测试卡 | smptebars (1) ✓ | testsrc2 (10) ✗ |
| 色彩渐变背景 | gradients (1) ✓ | testsrc2 (2) ✗ |
| 电子游戏画面 | testsrc2 (1) ✓ | rgbtestsrc (4) ✗ |
| 像素风格画面 | testsrc2 (1) ✓ | rgbtestsrc (4) ✗ |

**入库耗时 18.5s**（8 个视频 / 14 镜头，两条向量都算，真实 Ollama）。
库规模：镜头 14、**向量 56**（28 图片 + 28 文本）、`auditable: true`。

**验收 4 目标 ≥6/10 → 实测 10/10。**

---

## 5. Core 改动范围

TASK-010 按裁决**扩张了 Core 的 embedding 能力以支持图片输入**。

| 文件 | 改动 |
|---|---|
| `capabilities/ai/types.py` | +13 −2：新增 `AICapability.IMAGE_EMBEDDING`；`EmbeddingResult` 说明文本/图片共用结果结构 |
| `capabilities/ai/request.py` | +25 −2：新增 `ImageEmbeddingRequest` |
| `capabilities/ai/provider.py` | +13 −1：`ModelProvider.embed_images()` 默认抛 `AIUnsupported` |
| `capabilities/ai/service.py` | +19 −2：`AIService.embed_images()` 按能力路由 |
| `capabilities/ai/__init__.py` | +9 −3：导出新类型 |

**合计 +79 −10 行；`capabilities/ai/` 之外的内核文件 0 改动。**

### 为什么请求分两个类型、结果共用一个

沿用项目既有的"一个字段不承载两种语义"：

- **请求分两个**（`EmbeddingRequest.texts` / `ImageEmbeddingRequest.images`）——
  "这段文本"和"这张图"混在一个列表里，调用方与实现方都得靠猜
- **结果共用**（`EmbeddingResult`）—— 载荷完全同构（`vectors[i]` 对应第 i 项、
  `provider`/`model`/`dim` 含义相同、跨 Provider 约束相同），分成两个结构只制造重复

### 为什么把"文本向量化"和"图片向量化"分成两个能力

文本嵌入服务（`nomic-embed-text`）和图片嵌入模型（中文 CLIP）是两个不同的东西。
声明成同一个能力，**路由会选错 Provider** —— 一个只做文本的服务会被挑去编码图片。

---

## 6. 验收 5/6 的扫描结果（含一处必须如实说明的）

### 6.1 项目定义的检查 → 0 命中

项目在 `tests/test_ai_capability.py` 里定义的禁词表是：

```python
FORBIDDEN_IN_PLUGINS = [
    "openai", "ollama", "anthropic", "litellm",
    "requests", "httpx", "aiohttp", "urllib", "http.client",
]
```

**它针对的是"直接调远端模型 SDK / HTTP"**。`video_analyzer` 对这个表 **0 命中** ✅
（`plugin.py`、`xbc_va_clip.py`、`xbc_va_library.py` 三个文件都干净）。

### 6.2 用**更宽**的词表扫，结果如下 —— 如实列出

我把 `onnxruntime` / `torch` 也加进词表扫了一遍：

```
[plugins\video_analyzer]
  literal 命中 : xbc_va_clip.py: torch（只出现在部署说明的文档字符串里）
                 xbc_va_clip.py: onnxruntime
  ast import   : xbc_va_clip.py: import onnxruntime
  plugin.py    : （无）—— 业务代码干净
```

**这算不算违反"插件无直接模型调用"？我的判断是"不算"，理由如下，请复核：**

1. **业务代码与 Provider 是两件事**。`plugin.py`（业务）**零模型依赖**，
   只用 `ctx.ai.embed_images()` / `ctx.ai.provider(...)` —— 模型调用**确实经过 Core 能力层**。
2. `onnxruntime` 是**本地推理运行时**，不是模型 SDK、不联网、不访问任何模型 API。
   它与 `ffmpeg` 同类（`ffmpeg` 也是插件通过能力层调的本机可执行程序）。
3. **方案 A（扩 Core）的本质就是"插件提供 Provider 实现"** ——
   实现 Provider 必然要 import 它运行模型所需的东西。这是被批准的架构，
   不是绕过能力层。
4. 项目自己的禁词表**刻意没有把本地运行时列进去**，只列了远端 SDK / HTTP 客户端。

**若你认为 `plugins/` 下出现任何推理运行时都不可接受**，可行的替代是把
`xbc_va_clip.py` 移出 `plugins/`（例如 `providers/`），由内核在装配时加载 ——
但那需要另一处生命周期管理，属于新的架构决定，**我没有自行这么做**。

### 6.3 验收 6：Core 无业务耦合

```
业务词表 18 条；capabilities/ai/ 命中：（无）
```

---

## 7. 另外发现并修掉的两个真缺陷

1. **"声明支持"与"现在真能做"不一致**：`ChineseClipProvider` 在模型未配置时
   仍声明 `IMAGE_EMBEDDING`，导致 `AIService` 路由会选到一个必然失败的 Provider，
   而库里可能还有另一个真能干的。已改为：模型没配好就**不声明能力**。

2. **插件用"自己的 Provider"判断图片能力是否可用**，而不是问 Core 的路由。
   那等于把"谁提供能力"的知识写死在插件里。已改为
   `ctx.ai.provider(capability=IMAGE_EMBEDDING)` 问核心。

顺带发现：**插件配置不在主 `config.json`**，而在 `<root>/config/plugins.json`，
结构是 `{"plugins": {"<id>": {"config": {...}}}}`。第一版实测把它写错地方，
导致图片向量一直没入库。已记入本报告。

---

## 8. 局限

1. **评测集仍是 14 个合成画面**（7 种 lavfi 源），语义区分度很高。
   **10/10 不能外推到真实素材** —— 真实素材上两个空间的差距可能完全不同。
2. **没有真实素材的 ground truth**，无法做更大规模评测。
3. **294 MB 模型未做量化压缩**。amon-hen 的经验提示 Conv 层 INT8 会破坏输出
   （"与 FP32 余弦接近零"），**我没有尝试量化**。
4. **`shot_id` 在强制重分析后会变**（shots 表被重建）—— TASK-009 遗留性质，本次记录未修。
5. **分词器等价性验证用的是 23 条样例**，覆盖空串/超长/全角/重音/标点/中英混排，
   但不构成形式化证明。

---

## 9. 复现步骤

```powershell
# 1) 一次性导出 ONNX（在仓库外的独立 venv 里做）
python -m venv .venv-export && .\.venv-export\Scripts\activate
pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
pip install --no-deps cn_clip && pip install timm six regex ftfy onnx
python scripts/export_chinese_clip_onnx.py --out D:\models\chinese-clip-rn50

# 2) 插件运行时依赖
pip install onnxruntime numpy pillow

# 3) 告诉插件模型在哪（插件配置在 plugins.json，不在 config.json）
#    <数据根>/config/plugins.json:
#    {"plugins": {"video_analyzer": {"config": {"clip_model_dir": "D:/models/chinese-clip-rn50"}}}}

# 4) 入库 + 检索
python run.py tool call library_scan --kwargs "{\"directory\": \"D:/materials\"}"
python run.py tool call library_status
python run.py tool call library_search_semantic --kwargs "{\"query\": \"夜景\"}"
python run.py tool call library_search_semantic --kwargs "{\"query\": \"夜景\", \"space\": \"text\"}"
python run.py tool call library_audit

# 5) 测试
python -m unittest discover -s tests
```

---

## 10. 待你复核的一件事

**§6.2**：`plugins/video_analyzer/xbc_va_clip.py` 里 `import onnxruntime`。
项目自己的禁词表不含它（0 命中），但如果你认为"`plugins/` 下不得出现任何推理运行时"，
需要把 Provider 移出 `plugins/` —— 那是一个新的架构决定，**我没有自行做**。
