# TASK-013 前置报告 —— 两道 A 类闸门未过，等你裁决

| 项 | 内容 |
|---|---|
| 类型 | **A 类前置：只查证，未改一行代码** |
| **结论** | **任务书自己定了两道闸门，两道都没过 → 不能开工** |
| 闸门 1 | 「模型选型是 A 类前置：**先确认用户听测结果**，再实现」—— 听测结论你还没给 |
| 闸门 2 | 「如果 MOSS 的 ONNX 版必须依赖 torch → **先报告等裁决**」——**实测确认：确实必须** |
| 本轮产出 | torch 依赖面的实测数据、三条可选路线、Provider 机制可复用性结论、**4 个需要你裁决的问题** |

---

## 0. 为什么停下来

任务书《执行注意》第一条：

> **模型选型是 A 类前置：先确认用户听测结果，再实现**

任务书《实现范围》第 4 条：

> 如果 MOSS 的 ONNX 版必须依赖 torch（调研报告 §3 提到 `onnx_tts_runtime.py` 硬 import torch），**先报告等裁决**

而验收标准 2、3 都是「**用户听测确认**」—— 这两条我**没有任何办法自己满足**。

**所以本轮不改代码。** 下面是我为裁决准备的事实。

---

## 1. 闸门 1：中文听测（需要你听）

样本已在 `F:\Downloads\XBC\reports\task-013b-samples\`：

| 文件 | 内容 |
|---|---|
| `参考音频_共同.wav` | 48 kHz 立体声 5.84 s —— 两段样本**共用的音色来源** |
| `A_MOSS-TTS-Nano.wav` | 48 kHz 立体声 5.12 s，同一句文本 |
| `B_VoxCPM-0.5B.wav` | 16 kHz 单声道 6.48 s，同一句文本 |

**我需要你回答三个问题**：

| # | 问题 | 为什么要问 |
|---|---|---|
| 1 | A（MOSS）的中文**听得懂吗**？像正常说话吗？ | MOSS **零官方中文质量数据**，这是唯一的判据 |
| 2 | A 和 B 哪个**更像参考音频里的那个声音**？ | 验收标准 3「听起来像原声音」 |
| 3 | A 的 **48 kHz 立体声**对你进视频工程够用吗？ | 与 B 的 16 kHz 单声道是硬差异 |

**如果你觉得样本不够判断**，说一声 —— 我可以：
- 换更长的文本（现在只有 1 句）
- 用**你自己的参考音频**（老板的声音）再生成一轮
- 多生成几段（验收要求「至少 3 段不同文本」）

---

## 2. 闸门 2：torch 依赖（需要你裁决）

### 2.1 实测事实：torch 的依赖面**只有 17 行**，但**绕不过去**

整条 ONNX 推理链里，torch **只被用在一个私有方法**里：

```python
# onnx_tts_runtime.py L445-461
def _load_reference_audio(self, reference_audio_path) -> np.ndarray:
    waveform, sample_rate = torchaudio.load(...)          # ← 唯一碰 torch 的地方
    waveform = waveform.to(torch.float32)
    if sample_rate != target_sample_rate:
        waveform = torchaudio.functional.resample(...)
    ...  # 声道转换
    return waveform.unsqueeze(0).detach().cpu().numpy().astype(np.float32)
```

**之后的每一步都是 numpy + onnxruntime**（`codec_encode.run(...)` 吃 numpy，吐 codes）。

**但覆写这个方法绕不过去** —— 因为 `onnx_tts_runtime.py` **模块顶层** L12-13 就 `import torch` / `import torchaudio`：

```
$ （在没有 torch 的环境里跑官方 ONNX 入口）
  File "onnx_tts_runtime.py", line 12, in <module>
    import torch
ModuleNotFoundError: No module named 'torch'
```

而且 `pyproject.toml` 的 `dependencies` **写死了** `torch==2.7.0` + `torchaudio==2.7.0` —— 正常 `pip install` 会**自动拖 torch**。

### 2.2 体积实测（这是裁决的关键数字）

| 项 | 实测体积 |
|---|---|
| **`torch`** | **1183.0 MB** |
| `torchaudio` | 5.1 MB |
| MOSS ONNX 权重（两个仓库） | 727.8 MB |
| **XBC 现在的 `.venv`** | **760.3 MB**（无 torch；已有 onnxruntime 1.30.0 + numpy 2.5.3） |
| **装上 torch 之后** | **约 1950 MB —— 2.6 倍** |

### 2.3 规则判断：torch **不是架构违规**

这一点很重要，避免在错的问题上纠结 —— [harness-rules.md](harness-rules.md) 的《AI 能力隔离》细化（TASK-010 架构裁决）原文：

> **插件提供的 Provider 实现允许** import **本地推理运行时**（`onnxruntime` 等）
> 本地推理运行时**不在禁词表里**；改为约束"**仅限 Provider 文件使用**"

**判据是"是否绕过能力层去够远端模型"，不是"有没有出现 torch"。**

→ **所以这不是"能不能"的问题，是"值不值"的问题。**

### 2.4 三条路线

| 路线 | 做法 | 运行时增量 | 工作量 | 与 TASK-010 取向 |
|---|---|---|---|---|
| **A 装 torch，用官方 ONNX 路径** | `pip install` 正常装，`import onnx_tts_runtime` 直接用 | **+1188 MB** | **小**（用上游公开入口） | ⚠️ **相反**（TASK-010 刻意避开 torch） |
| **C 不装 torch，自己写 Provider** | `pip install --no-deps` → `import ort_cpu_runtime`（837 行，**torch-free**）→ **自己实现 ~380 行编排**（参考音频加载 + 合成循环 + 文本分段） | **+约 50 MB** | **大** | ✅ **一致** |
| **D 假 torch 替身 + 覆写私有方法** | 只在 import 那一瞬间把 `sys.modules['torch'/'torchaudio']` 换成最小替身，import 完撤掉；再继承 `OnnxTtsRuntime` 覆写 `_load_reference_audio`（用 `soundfile` 读 + numpy 重采样）—— 替身**只为满足顶层 import，从不执行** | +约 50 MB | 小-中（~30 行） | ✅ 一致 |

**另外一条顺带值得注意的**：既然 A 要装 torch，**改用 PyTorch 版 MOSS 反而省 501 MB 权重**（226.8 MB vs 727.8 MB），而且那是上游的**主路径**。ONNX 版唯一的优势（免 torch）**在这个前提下不存在了**。

### 2.5 我的建议：**A**

理由：

1. **用上游的公开入口**，不碰任何私有 API（D 依赖 `_load_reference_audio` 这个名字，上游一改就失效）
2. **是官方推荐的 CPU 路径**，我已经有本机实测数据（RTF 4.19、峰值内存 2193 MB）
3. **改动最小、风险最低** —— 第一版应该先跑通，而不是先省 1.2 GB
4. 规则上**不违规**（§2.3）

**但必须说清代价**：运行时从 760 MB 涨到约 1950 MB —— **这与 TASK-010 刻意"运行时不要 torch"的取向相反**。如果这个代价不可接受，那就在 C 与 D 之间选：
- 想要**干净**（不碰私有 API、不注假模块）→ **C**，代价是自己实现约 380 行编排，**音频质量的调试风险要认**
- 想要**省事**（~30 行）→ **D**，代价是**注入假模块**这个全局副作用 + 依赖上游私有方法名

**⚠️ 我不替你选。** 这是"运行时体积 vs 实现风险"的产品取舍，任务书也明确要求"先报告等裁决"。

---

## 3. Provider 机制：**可复用，不需要新抽象**

任务书问：「如果发现 TASK-010 的 embedding Provider 机制可复用，复用；如果需要新抽象，先报告」

**结论：可复用，不需要新抽象。** 现有机制的形状：

| 件 | 现状 | 加 speech 要做什么 |
|---|---|---|
| `AICapability`（`ai/types.py:52`） | `TEXT` / `VISION` / `EMBEDDING` / `IMAGE_EMBEDDING` | **加一个 `SPEECH`** —— 与 `IMAGE_EMBEDDING` 同级的兄弟，不是 embedding 的模态 |
| `ModelProvider`（`ai/provider.py:43`） | `name` / `capabilities` / `available()` / `bind_models()` / `configured()` / `supports()` / `describe()` + 各能力方法槽 | **加方法槽**（如 `synthesize_speech(request)` / `clone_voice(request)`），其余照旧 |
| `AIService.provider(name, capability=)`（`ai/service.py:144`） | 按 `capability` 路由，只查 `supports()` | **一行不用改** |
| 模型路径解析 | `AIService(model_dir)` + 注册时 `provider.bind_models(models_dir)`；`AIService.model_dir(model_id)` | **一行不用改** |
| `AppPaths.models_dir`（`core/paths.py:68`） | `<数据根>/models` | **一行不用改** |

**实测核查过的风险**：加 `AICapability.SPEECH` **不会破坏任何现有代码** ——
全项目只有两处遍历 `capabilities`（`provider.py:123` 与 `:134` 的 `describe`/`__repr__`），
遍历的是**每个 Provider 自己的 frozenset**，不是枚举类本身。

**没发现任何需要"新抽象"的地方。** 所以按任务书，这一项**不需要停下来问**。

---

## 4. 需要你裁决的 4 个问题

### 4.1 `clone_voice` 的「注册」由谁落盘？（**与硬约束 6 有冲突**）

任务书写：

> `clone_voice(reference_audio, name)` → **注册一个新声音**
> **声音存用户数据目录，不进 Core**
> **Core 禁止业务语义**（不出现"老板""公司""配音"等词）

**这里有张力**：如果 Core 的 `clone_voice` 负责"注册"（= 持久化一条「名称 ↔ 参考音频」记录），
那 Core 就必须知道"**声音库**"这个结构 —— 而"声音库"是业务概念。

**我的建议**（需要你确认）：

| 层 | 职责 |
|---|---|
| **Core `clone_voice`** | **无状态**：校验参考音频能不能用（读得动、时长够、采样率可转），返回**音色标识**（如参考音频的内容哈希 + 编码后的音色 codes） |
| **插件侧** | 拿这个标识落盘成"声音档案"（名称 ↔ 参考音频路径 ↔ 标识），数量上限（默认 3）也在插件侧配置 |

这样 Core 只管"这段音频能不能当音色用"，**不知道"声音库"是什么** ✓

### 4.2 接口形态：裸函数签名 vs Request/Result 对象

任务书写的是 `synthesize(text, voice, output_path)` / `clone_voice(reference_audio, name)` —— **裸函数签名**。

但 Core 现有约定是 **Request / Result 对象**（`TextRequest`/`TextResult`、`EmbeddingRequest`/`EmbeddingResult`），
`ModelProvider` 的方法槽也都收 request 对象。

**建议照现有约定**做成 `AIService.synthesize(SpeechRequest) -> SpeechResult`。
**如果你要的就是裸函数签名，说一声** —— 那会出现两套风格并存的 Core。

### 4.3 路线 A / C / D 选哪个（见 §2.4–2.5）

### 4.4 导出脚本的口径需要澄清

任务书 §4 写：「导出脚本：`scripts/export_moss_tts_onnx.py`（参照 TASK-010 的导出脚本）」

**但 MOSS 官方已经把 ONNX 产物发布在 HF 上了**（`MOSS-TTS-Nano-100M-ONNX` 641.5 MB +
`MOSS-Audio-Tokenizer-Nano-ONNX` 86.4 MB），**不需要我们自己导出**。

**所以脚本应该是「下载 + 校验（SHA/文件齐全）+ 放到 `models_dir/moss-tts-nano/`」，不是「导出」。**
如果你要的是真导出（从 PyTorch 权重转 ONNX），那是另一件事 —— 官方仓库里有 `onnx/export_hf_to_tts_onnx.py`，
但那要装 torch + 跑导出，**而且产物未必和官方发布的一致**。

**建议：脚本改名/改职能为「获取 + 校验」。等你确认。**

---

## 5. 发现但未做

| 发现了什么 | 为什么该处理 | 为什么这次不做 |
|---|---|---|
| MOSS 官方 README 称 ONNX 版「No PyTorch dependency during inference」，**与代码不符** | 会误导后来的人 | 属上游文档问题；已写进 TASK-013b 报告，本次已在本报告 §2.1 复述 |
| `WeTextProcessing` 是**硬依赖**（`No module named 'tn'` 直接崩），Windows 上 `pynini` 无 wheel | 不绕行跑不起来 | 实现时才需要处理（`--disable-wetext-processing` 可绕） |
| `huggingface_hub` 未列入 MOSS 的 `requirements.txt` | 自动下载模型会失败 | 同上 |
| `torch` 装了 1183 MB，**其中绝大部分是 CPU 算子库，我们只用一个重采样** | 极端浪费 | 属路线取舍，已在 §2.4 作为 C/D 的理由 |
| 验收标准 2 要求「至少 3 段不同文本」听测，但现有样本只有 1 句 | 不满足验收 | **等你确认走 MOSS 之后**再生成 —— 现在生成等于白做 |

---

## 6. 等你回复的清单

**必须先回答这三条，我才能开工：**

1. **听测结果**：`reports/task-013b-samples/` 里 A（MOSS）能用吗？像不像原声？（§1 的三个问题）
2. **torch 路线**：A（装 torch，+1188 MB）/ C（自己写，+50 MB）/ D（假 torch，+50 MB 但有耦合）？（§2.4）
3. **`clone_voice` 落盘归属**：同意"Core 无状态、插件落盘"吗？（§4.1）

**可以顺带一起回答的：**

4. 接口形态用 Request/Result 对象还是裸函数？（§4.2）
5. 脚本职能改成「获取 + 校验」可以吗？（§4.4）

**你回复后我会**：按选定路线实现 Core 的 `speech` 能力 + `providers/moss_tts.py` + 插件侧声音管理 + `plugins/voice_test_plugin`，
并交付含**至少 3 段中文听测样本**与**克隆质量 A/B 样本**的报告。

---

## 7. 本轮改动

**零。** 只新增本报告。
（另：`plugins/knowledge_base/` 仍按原样未跟踪，等你此前那三个选项的裁决。）
