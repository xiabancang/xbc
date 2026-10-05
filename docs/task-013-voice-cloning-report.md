# TASK-013 交付报告 —— 声音克隆能力（Core 扩展 + 配音）

| 项 | 内容 |
|---|---|
| 任务 | 建 Core 的 `speech` 能力（合成 + 零样本克隆）+ 插件接入 |
| 实现路线 | **D**（甲方裁定）：不装 PyTorch，空壳顶包 + 只覆写一个上游私有方法 |
| 引擎 | **MOSS-TTS-Nano**（官方 ONNX 版，Apache-2.0 代码 + 权重） |
| 测试 | **536 项通过**（系统 Python 跳 23 项；venv 全跑） |
| 新增 | `speech` 能力 + `voice_tts` 插件 + `voice_test_plugin` + 获取脚本 + 40 项测试 |
| **待你验收** | **中文听测**（≥3 段样本已生成）与**克隆质量 A/B** —— 见 §3 |

---

## 0. 验收对照

| # | 标准 | 结果 |
|---|---|---|
| 1 | `synthesize` 能输出**可播放**的语音文件 | ✅ 真机实测：48 kHz 立体声、回读核对一致 |
| 1 | `clone_voice` 能登记新声音 | ✅ 真机实测：返回规格 + 内容指纹 |
| 2 | **中文听测**（≥3 段不同文本） | ⏳ **样本已生成，等你听**（§3） |
| 3 | **克隆质量**（像不像原声） | ⏳ **A/B 样本已生成，等你听**（§3） |
| 4 | 插件无直接模型调用（grep + ast 0 命中） | ✅ `PluginPurityTests` 6 项守着 |
| 5 | Core 无业务耦合（`capabilities/ai/` 无业务词） | ✅ 已过既有那条测试（过程中被它抓到我一次违规，见 §6） |
| 6 | 模型放 `AppPaths.models_dir/moss-tts-nano/`，插件配置不含模型路径 | ✅ 路径由 Core 经 `bind_models` 注入 |
| 7 | 模型缺失时错误含**路径 + 缺失文件 + 获取方式** | ✅ 单测断言三样都在 |
| 8 | 全部测试通过（双环境） | ✅ **536 项**，exit=0 |
| 9 | 交付报告含选型确认 / 接口说明 / 监听样本 / A-B / B 类清单 | ✅ 本文件 |

---

## 1. `speech` 能力接口

### 1.1 能力与路由

| 件 | 内容 |
|---|---|
| 能力枚举 | `AICapability.SPEECH = "speech"`（与 `IMAGE_EMBEDDING` 同级，**不是** embedding 的模态） |
| 路由 | `AIService.provider(capability=SPEECH)` —— 只会向量化的 Provider **不会被误选** |
| 复用情况 | **复用 TASK-010 的 Provider 机制，未引入新抽象**（见 §0 前置报告 §3 的结论） |

### 1.2 两个能力入口（照现有 Request/Result 约定）

```python
# 合成：文本 + 输出路径 + 可选音色
result: SpeechResult = ctx.ai.synthesize(text, output_path=..., voice=...)

# 登记音色：本地参考语音路径 + 可选名字
profile: VoiceProfile = ctx.ai.clone_voice(reference_audio, name=...)
```

**任务书写的是 `synthesize(text, voice, output_path)` / `clone_voice(reference_audio, name)`** ——
门面方法名与参数名**照任务书**；内部按 Core 现有约定传 **Request 对象**
（`SpeechRequest` / `VoiceCloneRequest`），与 `TextRequest` / `EmbeddingRequest` 一致。

### 1.3 返回结构

| 类型 | 字段 |
|---|---|
| `SpeechResult` | `path`（语音文件路径）、**`duration`**、**`sample_rate`**、**`channels`**、`voice`、`provider`、`model` |
| `VoiceProfile` | `name`、`reference_audio`、**`fingerprint`**（参考语音内容指纹，16 位）、`duration`、`sample_rate`、`channels`、`provider`、`model` |

> `duration` / `sample_rate` / `channels` 是**实现方实测**的（读回文件），不是按文本长度估的 ——
> TASK-014 做音画对齐要靠它。

### 1.4 **Core 不保存任何声音**（B 类决定，见 §5.1）

`clone_voice` **无状态**：只确认"这段语音能不能当音色用"并返回描述。
**声音档案由插件落盘** —— 否则 Core 就得知道"谁有几个声音、怎么分组、上限多少"，
那是调用方的领域概念。

---

## 2. 模型部署

| 项 | 值 |
|---|---|
| 位置 | `<数据根>/models/moss-tts-nano/`（即 `AppPaths.models_dir / "moss-tts-nano"`） |
| 内容 | `MOSS-TTS-Nano-100M-ONNX/` + `MOSS-Audio-Tokenizer-Nano-ONNX/` |
| 体积 | **727.8 MB**（实测） |
| 获取 | `python scripts/export_moss_tts_onnx.py` |
| 插件配置 | **不含任何模型路径** —— 由 Core 注册时经 `bind_models` 注入 |

**本机落位实测**：脚本逐个文件比对 HF 元数据的字节数 ——
删掉一个文件让它真下载一次，其余走"已存在"分支，4 个必需文件全部 ✓，退出码 0。

---

## 3. ⏳ 需要你听的样本（验收 2 与 3）

全部在 `reports/task-013-samples/`（gitignore 内，不入源码库）：

| 文件 | 时长 | 说明 |
|---|---|---|
| `A1_原声参考_未知文本.wav` | 7.9 s | **A 侧**：MOSS 官方 demo 的真实录音（44.1 kHz 单声道） |
| `A2_原声参考_已知文本.wav` | 5.84 s | **A 侧**：另一段参考，**文本已知**（48 kHz 立体声） |
| `01_中文听测_年会通知.wav` | 7.12 s | B：克隆 A1 念"公司年会即将开始……" |
| `02_中文听测_产品介绍.wav` | 6.40 s | B：克隆 A1 念"这款软件可以在本地处理视频素材……" |
| `03_中文听测_客服话术.wav` | 5.20 s | B：克隆 A1 念"您好，您反馈的问题我们已经收到……" |
| `04_克隆对比_与原声同句.wav` | 6.96 s | B：克隆 **A2** 念**与 A2 完全相同**的那句话 |

**怎么听：**

1. **验收 2（中文能不能用）**：顺序听 `01` → `02` → `03`。听得懂、像正常说话吗？
2. **验收 3（像不像原声）**：先听 `A1`，再听 `01/02/03` —— 是不是同一个人的音色？
3. **最强的一组 A/B**：`A2` 与 `04` 是**同一句话**，一个原声、一个克隆，直接来回切着听。

**我判断不了这三条**（没有耳朵）。**如果听测不合格，按任务书回退 GPT-SoVITS。**

---

## 4. 实测数据

### 4.1 端到端（真模型 + 真插件 + 真能力层）

**首次跑通**（**注意**：这一次 `synthesize` 还是 §6 第 1 条那个 bug —— 30.0 秒是跑满帧数上限，
不是这段文本的真实时长）：

```
voice_tts          active
voice_test_plugin  active
moss_tts: configured=True model_ready=True
  model_dir: C:\Users\Administrator\AppData\Local\夏半仓工具箱\models\moss-tts-nano

voice_selftest  ok=True
  clone_voice:  fingerprint=f64a53490acf7337  duration=7.9  sample_rate=44100  channels=1
  synthesize:   path=...\selftest.wav  duration=30.0  sample_rate=48000  channels=2
  verify_audio: 一致=True   报的 {30.0, 48000, 2}   实的 {30.0, 48000, 2}
```

**修掉之后**（同一套调用，时长随文本长短合理变化）：

```
01_中文听测_年会通知   语音 7.12 s / 48000 Hz / 2 声道
02_中文听测_产品介绍   语音 6.40 s / 48000 Hz / 2 声道
03_中文听测_客服话术   语音 5.20 s / 48000 Hz / 2 声道
04_克隆对比_与原声同句 语音 6.96 s / 48000 Hz / 2 声道
```

`verify_audio` 这一步核对的是"返回的规格 == 文件实际规格"，**两次都一致 ✓**。

### 4.2 冷/热分离的 RTF（**更正了 TASK-013b 的错数**）

| 阶段 | 墙钟 | 语音时长 | **RTF** |
|---|---|---|---|
| 第 1 次（冷，含 **14.1 s** 模型加载） | 21.64 s | 7.12 s | 3.040 |
| 第 2 次（热） | 5.85 s | 5.36 s | **1.092** |
| 第 3 次（热） | 6.51 s | 6.16 s | **1.057** |
| 第 4 次（热） | 6.04 s | 5.84 s | **1.033** |

**热态 RTF ≈ 1.06（≈ 实时），冷启动 3.04。**

> TASK-013b 报的 **RTF 4.19 是错的** —— 那次每次都是**新进程**跑官方 CLI，
> 把约 14 秒的模型加载摊进了每一次推理。已在
> [task-013b-voice-cloning-survey.md](task-013b-voice-cloning-survey.md) §4.3
> 插入更正块并改掉所有下游结论。

### 4.3 运行环境增量

| | MB |
|---|---|
| 装 PyTorch（路线 A） | **+1188.0** |
| **路线 D 实际增量** | **+7.1**（实测 `.venv` 760.3 → 767.4） |
| 模型（放数据目录，不算环境） | 727.8 |

### 4.4 路线 D 的等价性验证（敢用 D 的依据）

| 参考语音 | 需要重采样 | 与上游 torchaudio 路径 |
|---|---|---|
| 48 kHz（单/双声道） | 否 | **逐帧 100% 一致** |
| 44.1 kHz 单声道 | 是 | 逐帧 83.67%；**波形余弦 0.99999995、相对误差 0.0506%** |

差异**只来自重采样器**（soxr vs torchaudio 的 sinc）；两个采样器数值上几乎相同，
离散码的少量翻转是量化边界效应。

---

## 5. B 类自主决定清单

| # | 决定 | 为什么这么定 | 影响 |
|---|---|---|---|
| 1 | **`clone_voice` 无状态，声音档案由插件落盘** | 让 Core 持久化档案就得让它知道"谁有几个声音、上限多少" —— 那是调用方的领域概念，与硬约束 6 冲突 | Core 侧只有 `VoiceProfile` 这个描述；插件侧 `voices.json` |
| 2 | **门面用任务书的参数名，内部用 Request 对象** | 任务书写的是裸函数签名；但 Core 现有约定是 Request/Result。两者都要满足 → 门面照任务书，内部照约定 | Core 里不会出现两套风格 |
| 3 | **获取脚本叫 `export_moss_tts_onnx.py` 但干的是"获取 + 校验"** | 官方**已发布现成 ONNX**；自己导出反而要装 PyTorch 且产物未必一致。名字保留任务书口径以免漂移 | 脚本职能与名字有一处不一致，已在脚本 docstring 与插件 README 写明 |
| 4 | **默认 3 个声音/owner，但走配置 `max_voices_per_owner`** | 任务书说"数量可配置，不写死" | 单测同时断言默认值与"上限来自配置" |
| 5 | **`providers/` 按文件路径加载，不起顶层包名** | 插件加载器把插件目录临时加进 `sys.path`；`providers` 是通用名，别的插件同名会静默拿到我们的模块 | 模块名带插件前缀 `xbc_voice_tts_provider_*` |
| 6 | **引擎起不来时插件不 FAILED，降级并记住原因** | 引擎依赖（numpy/soundfile…）缺失时，若整个插件 FAILED，连"看有哪些声音"都做不到，用户也拿不到原因 | `voice_list` 返回 `engine_error`；出声时才报明确错误 |
| 7 | **`do_sample=True` + `sample_mode="fixed"`**（不做成可配置项） | 实测 `do_sample=False`（greedy）时模型不吐结束符，26 个字也输出满 30 秒。上游 CLI 默认就是这组 | 见 §6 第 1 条 |
| 8 | **Core 里把"音频"改成"语音"** | 既有验收规则把 `音频` 列为业务词（视频业务域用词），而 speech 讲的就是语音合成 | 既合规又更准确 |

---

## 6. 过程中撞到的三个真问题（都修了）

| # | 现象 | 根因 | 怎么发现的 |
|---|---|---|---|
| 1 | 四段不同长度的文本，输出**全是整 30.0 秒** | 我传了 `do_sample=False` → greedy → **模型不吐结束符**，一路跑到 375 帧上限 | **靠"四段时长一模一样"发现的** —— 不是靠听。改回上游默认后时长变成 7.12/6.40/5.20/6.96 秒 |
| 2 | `sentencepiece` 报 `NOT_FOUND Error #2`，但**文件明明在** | sentencepiece 在 Windows 上**打不开含非 ASCII 的绝对路径**；而默认数据根就是 `夏半仓工具箱` | 对照实验：ASCII 路径 ✅ / 中文路径 ❌ / 相对路径 ✅ / ASCII 临时目录 ✅。**对照实测 `onnxruntime` 能正常读中文路径** |
| 3 | 我自己**在 Core 里写了业务词**（`types.py` 的 docstring 里出现"公司"） | 写文档时顺手拿业务场景举例 | **被既有那条 `test_no_business_words_in_ai_capability` 抓住** —— 规则的机械执行确实有效 |

---

## 7. 发现但未做

| 发现了什么 | 为什么该处理 | 为什么这次不做 |
|---|---|---|
| **上游 README 的 "No PyTorch dependency" 与代码不符** | 会误导所有想用 ONNX 版的人 | 属上游文档问题；已在 `docs/research/README.md` 与 Provider docstring 记录 |
| **`provider.py` 里的既有命中**："Clash / **公司**网关" | 与 `test_no_business_words_in_ai_capability` 的词表冲突（那里的"公司"是网络代理含义，不是业务域） | **早于 TASK-013**，范围外。测试里明确记为"已知且不相关"。要不要改成"企业网关"，**等你一句话** |
| **上游 `requirements.txt` 缺 `huggingface_hub`** | 官方自动下载模型会失败 | 我们的脚本用标准库自取，不依赖它 |
| **上游 `WeTextProcessing` 是硬依赖**，Windows 装不了（`pynini` 无 wheel） | 默认路径直接崩 | 已用 `enable_wetext=False` 绕过并在 Provider 注释里写明 |
| **torch 空壳是"临时替换全局模块名"** | 有极小的副作用窗口 | 已把窗口压到**一次 import**，导入完立刻撤除，并有用例断言撤除 |
| **热态没有复现"4 线程最快"** | 线程数调优可能还有空间 | 本次实测样本少（4 次），不足以定论；**不做估算** |
| 界面入口（主界面加"配音"页） | 用户要能点 | **任务书禁止项含"界面美化"，且 TASK-013 定位是"建能力 + 插件接入"** |

---

## 8. 复现

```powershell
# 依赖（不含 PyTorch）
pip install onnxruntime numpy sentencepiece soundfile soxr
pip install --no-deps "moss-tts-nano @ git+https://github.com/OpenMOSS/MOSS-TTS-Nano@8b7bcc9341b3b4ef3a3a58ba1338a7d85ff133eb"

# 模型（约 728 MB，放共享模型目录）
python scripts/export_moss_tts_onnx.py

# 自检（只走 ctx.ai）
python run.py tool call voice_selftest --yes --kwargs "{...}"

# 测试
python -m unittest discover -s tests
```

---

## 9. 说明

- **Core 只动了** `src/xbc/core/capabilities/ai/` 下 5 个文件（types / request / provider / service / `__init__`）
- **新增插件 2 个**：`plugins/voice_tts/`、`plugins/voice_test_plugin/`
- **新增脚本 1 个**、**新增测试 1 个**（40 项）
- 决定 D 的上游私有 API、失效触发条件、回退路径：`docs/research/README.md`
