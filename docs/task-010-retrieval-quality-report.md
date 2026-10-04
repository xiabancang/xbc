# TASK-010 交付报告 —— 检索质量修复

| 项 | 内容 |
|---|---|
| 任务 | TASK-010 检索质量修复（存储缺陷 + 中文 CLIP 图片嵌入 + 双路融合） |
| 状态 | **第一部分完成并交付；第二/三部分按任务书要求停下报告，等 Core 裁决** |
| 环境 | Windows · Python 3.13.15 · Ollama 0.35.1 · onnxruntime 1.30.0 |
| 测试 | **360 项通过**（系统 Python + venv 双环境） |
| Core 改动 | **0** —— `src/` 完全未动 |

---

## 0. 交付状态与停下原因

| 部分 | 状态 | 说明 |
|---|---|---|
| 一、修存储缺陷 | ✅ **完成** | 零依赖、可审计、可复现，全部有证据 |
| 二、中文 CLIP 图片嵌入 | ⚠️ **可行性已验证，未集成** | 模型已导出并实测等价；**集成受阻于 Core 接口** |
| 三、双路召回 + RRF 融合 | ⚠️ **离线实测完成** | 实测表明**融合有害**，如实报告建议放弃 |
| 四、检索质量实测 | ✅ **数据已有** | CLIP 单路 **top-1 10/10**（目标 ≥6/10） |

**停下原因**：任务书「执行注意」明确写了 ——

> 如果发现必须改动 Core（例如 **embedding 能力需要支持图片输入**），先停下报告，等裁决。
> **不要自行扩展 Core。**

这是被预先授权的一次停下。**Core 的 `EmbeddingRequest` 只接受文本**，
而验收标准 5 要求"模型调用通过 Core Capability" ——
**这两条同时成立必须改 Core**（详见 §4）。我没有自行扩展，也没有绕过去。

---

## 1. 第一部分：存储缺陷修复（已完成）

### 1.1 缺陷回顾

TASK-009 的向量是这样算的：

```python
text_for_embedding = " ".join([annotated.answer, *annotated.labels]).strip()
embedded = self.ctx.ai.embedding([text_for_embedding])
```

**但库里只存了 `labels`** —— 真正被嵌入的 `answer` 从未落库。后果：
- 检索行为**无法审计**：你能看到标签，看不到决定向量的那段文本
- `answer` 是较长的通用描述，**稀释了标签的区分度**
- 同素材多次入库标签漂移，检索结果**不可复现**（TASK-009 记 3/7，调研实测 2/7）

### 1.2 修复内容

`xbc_va_library.py` schema **v1 → v2**，`vectors` 表补三列：

| 列 | 作用 |
|---|---|
| `embed_text` | **真正被嵌入的原始文本**（可审计） |
| `kind` | `frame`（单帧）/ `shot_centroid`（多帧质心）—— 避免把质心误读成"某段被嵌入的原文" |
| `created_at` | 向量产生时间（可追溯） |

配合既有的 `vectors.provider` / `vectors.model` / `vectors.dim`、
`videos.file_hash` / `videos.analyzed_at`，一条向量可以完整回答
**"它是从哪个字符串、用哪个模型、什么时候、从哪份素材算出来的"**。

新增工具 **`library_audit`**（第 13 个工具）作为审计入口。

### 1.3 证据

**（a）迁移：v1 旧库能升级，且如实承认旧向量不可审计**

```
迁移前 vectors 列: ['id','shot_id','frame_id','provider','model','dim','vector']
迁移前 schema_version: 1
迁移后 vectors 列: [...,'kind','embed_text','created_at']
迁移后 schema_version: 2
新增列: ['created_at', 'embed_text', 'kind']
迁移后统计: {"vectors":28,"vectors_by_kind":{"frame":28},"vectors_with_embed_text":0,"auditable":false}
note: 库里所有向量都没有 embed_text（v1 旧库）—— 已经算过的向量无法反推原文，
      需要重新入库（library_rebuild）才有可审计性。
```

**旧库不会假装自己可审计** —— `auditable: false` + 明确的 `note`。

**（b）重新入库后 100% 可审计**

```
入库耗时 65.8s
库规模: 视频 8  镜头 14  向量 28
按 kind 分: {'frame': 14, 'shot_centroid': 14}
有 embed_text 的向量: 28/28
auditable: True
```

**审计抽样**（这直接坐实了缺陷诊断 —— 原来看不见的那段 `answer` 现在可见）：

```
vector#1 shot=1 kind=frame  dim=768 model=nomic-embed-text
    embed_text(64 字): 这个画面的主要内容是一个复杂的几何图形，属于抽象艺术场景。
                       几何图形 抽象艺术 色彩斑斓 复杂图案 视觉艺术 抽象几何 色彩对比
    provider=ollama  created_at=2026-10-05T04:09:04
    video_hash=7663474396a60e23  video_analyzed_at=2026-10-05T04:09:08
```

**（c）可复现性**

| 检查 | 结果 |
|---|---|
| 同一库、同一查询跑两次 | **结果字节完全一致** ✅ |
| 同一素材再次扫描 | `analyzed=0 skipped=8`，按内容哈希跳过 —— 库不变，结果不漂 ✅ |
| 强制重新分析后 | 14/14 帧的 `embed_text` 都变了（VLM 非确定性），**但差异现在可见、可 diff** ✅ |

**关于"可复现"的准确说法**：VLM 输出本身无法确定化，所以"两次入库标签一致"做不到。
**能做到的是**：① 已入库数据的检索结果是确定性的；② 不强制重分析时库不变；
③ 一旦重分析，**变了什么、为什么变，全部可查**。
这正是"可审计"要解决的问题，也是本部分交付的东西。

**（d）测试**：`tests/test_video_library.py` 44 项（原 32 + 新增 12），全过。

---

## 2. 第二部分：chinese-clip-rn50（可行性已验证，未集成）

### 2.1 许可证（按规则核实并登记）

| 项 | 内容 |
|---|---|
| 权重 | `OFA-Sys/chinese-clip-rn50` |
| **许可证** | **Apache-2.0** ✅ 允许商业使用（HF `cardData['license'] == 'apache-2.0'`） |
| 代码 | `OFA-Sys/Chinese-CLIP`，**MIT** |
| 核实方式 | `huggingface_hub.HfApi().model_info(...)` 读 `cardData`，非从 README 推断 |
| 登记位置 | [docs/research/README.md](research/README.md) §「TASK-010 采用记录」 |

**为什么不用 `vit-base-patch16`**（任务禁止项）：它在 HF 上**未声明许可证**。
**rn50 是唯一许可证干净的候选** —— 代价是它**不是 transformers 格式**：
HF 上只有一个 `clip_cn_rn50.pt`（294 MB，`cn_clip` 原始格式），
`transformers` 无法直接加载（需 `pytorch_model.bin`/`safetensors`）。

### 2.2 部署方式：ONNX，运行时不需要 torch

任务书要求"优先纯 Python / ONNX 部署"，并规定"如果必须引入 torch，先报告等裁决"。

**实测结论：运行时不需要 torch。**

| 环节 | 依赖 | 是否在用户机器上 |
|---|---|---|
| 导出（一次性） | `torch` + `cn_clip` + `torchvision` + `timm` + `onnx` | **否**（开发机做一次） |
| **运行** | **`onnxruntime` + `numpy` + `pillow`** | 是 |

导出做法：官方 `cn_clip` 包加载 `.pt` → `torch.onnx.export` 分别导出视觉塔与文本塔。
分词用 `cn_clip` 自带的**纯 Python** `FullTokenizer`（BERT WordPiece），运行时也无需额外依赖。

**产物**：

| 文件 | 大小 |
|---|---|
| `vision_model.onnx` | 146.1 MB |
| `text_model.onnx` | 147.6 MB |
| `vocab.txt` | 0.1 MB |
| **合计** | **294 MB** |

**这是本方案的主要成本** —— 需要你知晓（对比：amon-hen 的 MobileCLIP2 ONNX 约 107 MB）。

### 2.3 正确性：ONNX 与 torch 精确等价

在同一批 14 帧图像 + 10 条中文查询上逐个比对：

```
图像向量维度 (14, 1024)   文本向量维度 (10, 1024)
14 帧图像余弦: 最小 1.00000000  最大 1.00000000
10 条文本余弦: 最小 1.00000000  最大 1.00000000
判定: ✅ ONNX 与 torch 等价
```

**维度一致性（验收 2 要求）**：图像 **1024** / 文本 **1024**，来自同一模型同一向量空间 ✅

### 2.4 性能与质量（实测）

```
视觉塔  27 ms/帧     （对比：现有 Caption 路线约 830 ms/镜头，含 1 次 VLM 调用）
文本塔  10.5 ms/查询
模型加载 0.8s
```

**同批 14 帧 / 10 个中文查询：top-1 10/10、前 3 10/10。**

### 2.5 ⚠️ 一个必须记录的坑：预处理必须与官方逐字一致

我第一版用了"标准 CLIP"的 `Resize(224) + CenterCrop(224)`，**top-1 只有 7/10**。
改用官方 `image_transform` 后 **10/10**。

差别在于官方是：

```python
Resize((224, 224), interpolation=BICUBIC)   # 直接拉成正方形
```

**不是** shortest-edge resize + center-crop。CN-CLIP 就是这么训练的。

> **对照资料**：amon-hen 的 MobileCLIP2 结论**正好相反** ——
> 它的 docstring 写"直接拉伸会压扁宽高比、使嵌入偏移约 0.03 余弦"，因而改用裁切。
> **不同模型的预处理要求相反，必须按各自官方实现来，不能套用"标准 CLIP 配方"。**

**这条直接解释了调研报告里的一个疑点**：调研报告用 `transformers` 的默认预处理器测
`vit-base-patch16` 得到 6/10 —— 那个数字很可能被预处理拉低了，不是模型真实水平。

---

## 3. 第三部分：双路融合 —— 实测表明**不该做**

调研报告基于 6/10 的 CLIP 数据，结论是"CLIP 8 胜 1 平 1 负，建议双路融合"。
**实测（rn50 + 官方预处理）推翻了这个建议。**

同一批 14 帧、同样 10 个查询：

| 路线 | top-1 | 前 3 |
|---|---|---|
| Caption 单路（nomic-embed-text） | 2/10 | 3/10 |
| **中文 CLIP 单路（rn50 ONNX）** | **10/10** | **10/10** |
| **RRF 融合（等权，k=60）** | **8/10** | 9/10 |

**融合让 top-1 从 10/10 掉到 8/10。**

逐条看，掉的两条：

| 查询 | Caption | CLIP | RRF |
|---|---|---|---|
| 夜景 | 13 | **1** | **5** |
| 彩条测试卡 | 10 | **1** | **3** |

**根因**：CLIP 单路已经满分，而 Caption 只有 2/10。
**等权 RRF 给弱路相同话语权，纯粹在拖强路的后腿。**
两路质量差距越大，等权融合伤害越明显。

**结论（如实报告，不调数据）**：
- ❌ **不做等权 RRF 融合**
- ✅ **走 CLIP 单路**（10/10，超过验收要求的 6/10）
- ⚠️ 若要保留 Caption 作为第二路，必须**加权**，但权重要靠评测数据调 ——
  当前 14 帧的样本量**不足以支撑调参结论**，不应在证据不足时定权重

这也和任务书的提醒一致：

> 如果实测结果与调研报告不一致（例如融合后反而下降），如实报告，
> 不要为了"看起来成功"而调数据。

---

## 4. ⚠️ 停下报告点：Core 裁决请求

### 4.1 问题

验收标准 5 要求：**"模型调用通过 Core Capability"**。

但 Core 的 AI 能力层只有**文本**嵌入接口：

```python
class EmbeddingRequest:      # src/xbc/core/capabilities/ai/request.py
    texts: list[str]         # ← 只有文本
```

**图片嵌入无法经 Core Capability 走通。** 而任务书「执行注意」把
"embedding 能力需要支持图片输入"列为**明确要停下报告的情形**，所以我停在这里。

### 4.2 三个选项

**方案 A：扩展 Core 的 embedding 能力支持图片（推荐）**

- **Core 改动**：`EmbeddingRequest` 增加图片入参（或新增 `embed_images()`），
  `ModelProvider` 加一个可选方法（不支持时抛 `AIUnsupported`），`AIService` 转发
- **插件改动**：注册一个**本地 ONNX 的 `ModelProvider`**（实现 embedding + 图片），
  `onnxruntime` 与模型文件都留在插件侧
- **优点**：满足验收 5 字面要求；**Core 仍零第三方依赖**（重依赖在插件）；
  与 TASK-007 的 Provider 抽象一致（插件确实能 `ctx.ai.register()`）
- **代价**：动 Core（约 3 个文件）+ 需要定"图片输入"的接口形态

**方案 B：插件内直接做本地 ONNX 推理，不扩 Core**

- **零 Core 改动**，插件自包含
- **代价**：验收 5 的"模型调用通过 Core Capability"**不满足**；
  TASK-007 的隔离原则被开一个口子（模型选择不再由 Core 配置驱动）
- 辩护理由：本地 ONNX 推理不是"调模型 SDK"（无网络、无 provider），
  性质更接近"插件用了一个本地工具"；但这个理由**需要你认可**，我不自行认定

**方案 C：放弃图片嵌入，只保留第一部分的修复**

- 零风险、零新依赖
- **但检索质量仍是 2/10**，达不到验收 4 的 6/10 —— 等于任务目标未达成

### 4.3 我的建议

**选 A**。理由：
1. 它是唯一同时满足"验收 5"与"Core 零第三方依赖"的路径
2. 插件确实具备注册 Provider 的能力（`ctx.ai.register()` 已是公开用法，
   `ai_test_plugin` 与测试都在用），不需要新造机制
3. 方案 B 把"模型选择"从 Core 配置里拿出来，会让"换模型/换设备"重新变成插件内部实现细节 ——
   这正是 TASK-007 想避免的

**无论选 A 还是 B，第 3 节的融合都建议不做**（实测有害）。

---

## 5. 验收标准逐条对照

| # | 验收标准 | 状态 | 证据 |
|---|---|---|---|
| 1 | 存储缺陷已修：可查被嵌入原文 / provider+model / 可复现 | ✅ **完成** | §1；28/28 可审计；两次检索字节一致 |
| 2 | 中文 CLIP 已接入：rn50 / 许可证 / 部署方式 / 维度一致 | ⚠️ **部分** | 许可证 ✅、ONNX 部署 ✅、维度 1024=1024 ✅；**集成受阻于 §4** |
| 3 | 双路融合已实现 + 融合前后对比 | ⚠️ **离线完成** | §3 给出三路对比；**实测表明不该做融合** |
| 4 | 提升到 ≥6/10 | ⚠️ **离线达标** | CLIP 单路 **10/10**；**未在插件内实现**（受阻于 §4） |
| 5 | 插件无直接模型调用（grep + ast 0 命中） | ✅ **完成** | 见 §6 |
| 6 | Core 无业务耦合 | ✅ **完成** | 见 §6 |
| 7 | 全部测试通过（双环境） | ✅ **完成** | 360 项 × 2 环境 |
| 8 | 报告含逐条证据 / 许可证登记 / 对比数据 / 部署说明 | ⚠️ 本报告 | 许可证已登记进 `docs/research/README.md` |

---

## 6. 验收 5 / 6 与 Core 改动范围

**验收 5：插件无直接模型调用**（大小写不敏感 grep + ast，扫描 12 个插件文件）

```
[plugins]                literal 命中: ['plugins\\knowledge_base\\plugin.py: ollama']
                         ast import : （无）
[plugins\video_analyzer] literal 命中: （无）      ← 本插件 0 命中
                         ast import : （无）
```

`knowledge_base` 是**已知的、不在版本库里的**范围外产物（文档字符串提到 ollama，无真实 import），
与 TASK-008/009 的结论一致，本次同样**未修改**。

**插件的实际运行时依赖**（ast 提取）：

```
plugin.py          ['__future__','datetime','hashlib','pathlib','re','typing',
                    'xbc.core.capabilities.ai','xbc.core.contract.plugin','xbc_va_library']
xbc_va_library.py  ['__future__','array','contextlib','datetime','math','pathlib','sqlite3','typing']
```

**纯标准库 + `xbc` 契约 + 本地模块** —— 没有 torch / onnxruntime / requests 等任何重依赖。

**验收 6：Core 无业务耦合**

```
业务词表 18 条；capabilities/ai/ 命中: （无）
```

**Core 改动范围：0**

```
git status --short -- src/          → src/ 下无任何改动
git diff --stat -- src/             → 工作区 src/ 与 HEAD 一致
capabilities/ai/ 8 个文件 SHA256 与 TASK-009 提交时一致
```

---

## 7. 实测数据汇总

| 数据 | 数值 | 来源 |
|---|---|---|
| Caption 单路 中文 top-1 | 2/10 | 本机实测 |
| **中文 CLIP 单路 top-1** | **10/10** | 本机实测 |
| **RRF 融合 top-1** | **8/10**（低于单路） | 本机实测 |
| CLIP 视觉塔延迟 | 27 ms/帧 | 本机实测 |
| CLIP 文本塔延迟 | 10.5 ms/查询 | 本机实测 |
| ONNX vs torch 一致性 | 余弦 1.00000000 | 本机实测 |
| 模型产物大小 | 294 MB | 本机实测 |
| Caption 入库成本 | 4.65 s/镜头（冷）/ 0.83 s/镜头（热） | TASK-009 实测 |
| 错误预处理的代价 | top-1 10/10 → **7/10** | 本机实测 |

---

## 8. 局限

1. **评测集仍是 14 个合成画面**（7 种 lavfi 源），语义区分度很高。
   **10/10 不能外推到真实素材** —— 真实素材上三路差距可能完全不同。
2. **没有真实素材的 ground truth**，无法做更大规模的检索评测。
3. **294 MB 的模型产物**未做量化压缩。amon-hen 的经验提示 Conv 层 INT8 量化会破坏输出
   （"与 FP32 余弦接近零"），**我没有尝试量化** —— 若要压体积需要单独验证。
4. **权重要调的话样本量不够**（14 帧），所以第 3 节只给出"不该等权融合"的结论，
   没有给加权方案。
5. **`shot_id` 在强制重分析后会变**（shots 表被重建），这是 TASK-009 遗留的性质；
   本次发现并记录，未修（不在任务范围）。

---

## 9. 待裁决清单

| # | 问题 | 我的建议 |
|---|---|---|
| 1 | 图片嵌入是否扩展 Core 的 embedding 能力？ | **方案 A**（扩 Core，插件注册本地 ONNX Provider） |
| 2 | 若选 A，图片入参的接口形态（`EmbeddingRequest.images` vs 新方法 `embed_images()`）？ | 新增 `embed_images()` 更清晰，不污染文本接口的语义 |
| 3 | 双路融合是否保留？ | **不保留**（实测 8/10 < 10/10） |
| 4 | 294 MB 模型产物是否可接受？ | 需要你的判断 |
