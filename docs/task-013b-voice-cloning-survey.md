# TASK-013b 前置调研 —— 声音克隆方案选型

| 项 | 内容 |
|---|---|
| 类型 | **前置调研（选型建议）**，未改任何代码 |
| 核心问题 | 在「**纯本地 + Python + 无 GPU 假设 + 中文 + 可商用**」下，选哪个声音克隆方案？ |
| **结论** | **推荐 MOSS-TTS-Nano 为主选**（许可证干净 + 官方 ONNX + 227 MB + 本机实测最快 + 48 kHz 立体声），**但它的中文质量没有任何官方数据 —— 必须先用听测补上这个空白再定稿** |
| 已排除 | **OmniVoice**（权重 CC-BY-NC + 第三个子模块许可）、**ebook2audiobook**（默认引擎 CPML / MMS 是 CC-BY-NC）|
| 实测 | 本机跑通 2 个方案，**RTF 与输出规格是自己量的**（官方全都没有 x86 CPU 数据）|
| 对照样本 | `reports/task-013b-samples/`（同参考音频、同文本，MOSS vs VoxCPM 各一份，**请你听**）|

---

## 0. 为什么这份报告有"未定量"的部分

**5 个候选里，没有一个给出过 x86 CPU 的推理速度数字**。官方数据要么是 GPU（4090/H100），
要么是 Apple M4。所以速度这一栏我**全部自己实测**，并且如实标注哪些是实测、哪些是官方引用。

**中文质量更麻烦**：我**没有耳朵，不能替甲方判断语音好不好听**。
所以我把同参考音频、同文本的两份样本生成出来交给你听（见 §5）。

---

## 1. 逐项目登记表

| 项目 | 代码许可证 | **权重许可证** | 可商业使用 | 体积 | 官方中文质量 | **本机实测 CPU RTF** | 输出规格 | 在夏半仓的使用方式 | 架构冲突 | 维护状态 |
|---|---|---|---|---|---|---|---|---|---|---|
| **MOSS-TTS-Nano** | Apache-2.0 | **Apache-2.0** | ✅ | **226.8 MB**（PyTorch）/ 727.9 MB（ONNX） | ❌ **无 TTS 级数字** | **1.06**（热态）/ 3.04（冷启动） | **48 kHz 立体声** | **采用**（重新封装为插件 + Provider） | 有，见 §6 | 活跃（2026-09-06） |
| **GPT-SoVITS** | MIT | **MIT** | ✅ | **5.30 GB**（+ 配套 ~4.3 GB） | WER 1.3% / SIM 74.5（v4） | 未实测（**官方 M4 CPU 0.526**） | 48 kHz（v4） | **参考 / 备选** | 有，见 §6 | 活跃（2026-10-04） |
| **VoxCPM-0.5B** | Apache-2.0 | **Apache-2.0** | ✅ | 1.50 GB | **CER 0.93% / SIM 77.2%** ← 最好 | **13.99 / 13.27** | ⚠️ **16 kHz 单声道** | **参考 / 对照** | 有，见 §6 | 活跃（2026-09-30） |
| **OmniVoice** | Apache-2.0 | ⚠️ **CC-BY-NC**（禁商用）+ 子模块另许可 | ❌ | 3.04 GB | 论文 CER 0.84 / SIM-o 0.777 / UTMOS 3.11 | 未实测 | — | **不采用** | **许可证硬冲突** | 活跃（2026-09-28） |
| **ebook2audiobook** | Apache-2.0（`pyproject.toml` 却写 MIT ⚠️） | ⚠️ **XTTS=Coqui CPML / MMS=CC-BY-NC** | ❌ | 1.95 GB（仅 XTTSv2） | ❌ 无 | 未实测（官方自述 "CPU is slow"） | — | **不采用** | **许可证硬冲突 + 是应用不是库** | 活跃（2026-10-05） |

> **"体积"口径**：模型权重文件的真实字节数（HuggingFace API `?blobs=true` 或 `?recursive=true` 逐个文件求和），不是估算。
> **"RTF"口径**：本机实测，见 §4 的方法与原始数据。**RTF < 1 才是快于实时**。

---

## 2. 逐项详述

### 2.1 MOSS-TTS-Nano —— 推荐主选

| 项 | 事实 | 出处 |
|---|---|---|
| 代码许可 | **Apache-2.0**，LICENSE 11,374 字节，署名 `Copyright 2026 OpenMOSS Team, Fudan University, SII and MOSI` | [LICENSE](https://raw.githubusercontent.com/OpenMOSS/MOSS-TTS-Nano/main/LICENSE) |
| 权重许可 | **Apache-2.0** —— 4 个官方仓库一致（`OpenMOSS-Team/MOSS-TTS-Nano-100M`、`...-100M-ONNX`、`MOSS-Audio-Tokenizer-Nano`、`...-ONNX`） | [HF API](https://huggingface.co/api/models/OpenMOSS-Team/MOSS-TTS-Nano-100M) |
| ⚠️ 文档瑕疵 | README 的 License 段还留着旧话："If you are reading this before that file is published, please treat the repository as **not yet licensed for redistribution**" —— **LICENSE 文件已经发布了**，这句话是过时的 | [README](https://raw.githubusercontent.com/OpenMOSS/MOSS-TTS-Nano/main/README.md) |
| 参数量 | 官方 0.1B；tokenizer 约 20–22M | 模型卡 |
| 体积 | **PyTorch 双权重 307.67 MB**（TTS 223.82 + tokenizer 83.85）；**ONNX 双仓库 727.85 MB** | HF API `?blobs=true` |
| 部署形态 | **官方 ONNX CPU 版**（2026-04-17 发布），入口 `infer_onnx.py` / `app_onnx.py` / CLI `--backend onnx`；默认 `--execution-provider cpu` | [README ONNX 段](https://raw.githubusercontent.com/OpenMOSS/MOSS-TTS-Nano/main/README.md) |
| ⚠️ **ONNX 声称与代码不符** | README 写 "**No PyTorch dependency during inference**"，但 `onnx_tts_runtime.py` **第 12–13 行硬 import torch / torchaudio**，用途是加载参考音频（`torchaudio.load` + `resample`）。**我实测验证：在没有 torch 的环境里跑 `infer_onnx.py` → `ModuleNotFoundError: No module named 'torch'`** | 实测，见 §4.2 |
| ⚠️ WeTextProcessing 是**硬依赖** | 代码里有 `_available` 字段看似可降级，**实际不是**：默认会 `RuntimeError: No module named 'tn'` 直接崩。必须显式 `--disable-wetext-processing` 才能跑（`pynini` 在 Windows 上没有 wheel，这条绕行是**必需**的） | 实测，见 §4.2 |
| ⚠️ 缺依赖声明 | 自动下载模型需要 `huggingface_hub`，但 `requirements.txt` **没有列**它 | 实测 |
| 中文 | ✅ 支持列表**第一行就是中文**；HF `cardData.language` 首项 `zh` | 模型卡 |
| ❌ 中文质量数据 | **官方没有给任何 TTS 级中文 WER/CER/MOS**。唯一的中文数字是**音频 tokenizer 的重建指标**（AISHELL-2，SIM 0.88/0.81、STOI 0.95/0.91 等）—— **那是编解码质量，不是语音合成质量** | [tokenizer 模型卡](https://huggingface.co/OpenMOSS-Team/MOSS-Audio-Tokenizer-Nano/raw/main/README.md) |
| 克隆样本量 | 官方只说"short reference clip"；官方示例音频实测 **2.6–17.4 秒** | 实测解析官方 assets |
| 官方 CPU 说法 | 只有**相对**表述：ONNX 版"**nearly 2×** the processing efficiency"；"MacBook Air M4 上**单核**流畅"；"streaming can run on a 4-core CPU"。**没有任何 RTF 数字** | README |

### 2.2 GPT-SoVITS —— 备选（质量优先时）

| 项 | 事实 |
|---|---|
| 代码 / 权重许可 | **MIT / MIT**（HF `lj1995/GPT-SoVITS` cardData = `mit`）—— **最干净的一档** |
| 体积 | 权重仓 **5.30 GB**（27 文件）；官方一键包 `pretrained_models.zip` **4.25 GB**；**另需** G2PWModel 562 MB / funasr 1.04 GB / faster-whisper 2.72 GB（后两个是否必需取决于用法） |
| 部署 | **必须 PyTorch**，官方**没有任何免 PyTorch 推理路径**（`onnx_export.py` 是导出脚本，自己也要 torch）；依赖里有 `pyopenjtalk`（Windows 编译困难）、`pytorch-lightning`、`gradio` |
| 中文质量（官方） | SeedTTS 中文集：v4 **WER 0.013 / SIM 0.735**；v2ProPlus WER 0.016 / SIM 0.737；GT 0.013 / 0.750 |
| CPU（官方） | **全仓库唯一一个 CPU 数字**：*"RTF ... **0.526 in M4 CPU**"*（v2ProPlus）—— **低于 1，即快于实时**。但**没有 x86 数字** |
| 克隆样本 | zero-shot **5 秒**；few-shot 微调 **1 分钟** |
| 维护 | `pushed_at` 2026-10-04，62,343 stars |

### 2.3 VoxCPM-0.5B —— 对照（中文数字最好，但输出规格是短板）

| 项 | 事实 |
|---|---|
| 代码 / 权重许可 | **Apache-2.0 / Apache-2.0**（3 个官方仓库一致）。⚠️ 模型卡另有一句"released for research and development purposes only"，那是**建议性免责声明**，与同文件的 "weights and code are open-sourced under the Apache-2.0 license" 并存 —— **文义有张力，建议法务过一眼** |
| 体积 | **1.50 GB**（`pytorch_model.bin` 1.24 GB + `audiovae.pth` 287.5 MB） |
| 部署 | **必须 PyTorch**（`torch>=2.5.0`）；**官方无 ONNX**（只有第三方 `blurvar/VoxCPM-ONNX`）；依赖 `torchcodec`，**本机实测装不上（OSError，需要 FFmpeg 共享库）** |
| 中文质量（官方） | **test-ZH CER 0.93% / SIM 77.2%** —— 五个候选里**官方数字最好的** |
| CPU（官方） | **无任何 CPU RTF**；官方 RTF 全是 4090/H100 |
| 克隆样本 | 官方文档给 **5–30 秒**；⚠️ **0.5B 是 continuation-only**，必须**同时提供参考音频和它的文字转写** |
| ⚠️ **输出规格** | **本机实测输出 16 kHz 单声道** —— 对**视频配音**是明显短板（我们现在的素材是 48 kHz 立体声） |

### 2.4 OmniVoice —— **排除**

| 项 | 事实 |
|---|---|
| 代码 | Apache-2.0（`Copyright 2026 Xiaomi Corp.`）|
| **权重** | ⚠️ **HF `cardData.license` 是空字符串**，README front-matter 里**没有 `license:` 字段** —— **任何自动读许可证的工具都会得到"未声明"**。真实条款**只以散文写在模型卡里**：*"The pre-trained model is licensed under the **CC-BY-NC**"* |
| **第三层许可** | ⚠️⚠️ 权重包里的 `audio_tokenizer/` 子模块（201M 参数 / 768 MB，约占权重 1/4）自带 **BOSON HIGGS AUDIO 2 COMMUNITY LICENSE**（基于 Meta Llama 3 Community License），其中 **Additional Commercial Terms：年活跃用户 > 100,000 必须向 Boson AI 申请扩展许可** |
| 结论 | **CC-BY-NC = 禁止商业使用**，且**三重许可叠加**。对我们的场景（给公司做商用视频）**直接排除** |
| 中文（仅供参考） | 论文 test-zh CER 0.84 / SIM-o 0.777 / UTMOS 3.11（GitHub README **不含任何质量数字**，只有 RTF 表） |

### 2.5 ebook2audiobook —— **排除**

| 项 | 事实 |
|---|---|
| 代码 | Apache-2.0；⚠️ 但 `pyproject.toml` 和 PyPI 元数据写的是 **MIT classifier** —— **元数据与 LICENSE 文件不一致** |
| **权重** | ⚠️ 仓库**不含权重**，按引擎下第三方：**默认 XTTS = Coqui Public Model License（非 OSI）**；**Fairseq/MMS 路径 = CC-BY-NC-4.0**；其余引擎许可证未列举 |
| 形态 | **是个应用**（Gradio GUI + Docker 镜像 + CLI），不是可嵌入的库；`requirements.txt` 53 行，含 `gradio`、`coqui-tts`、`piper-tts`、`pyannote-audio`、`demucs-simple` 等 |
| ONNX | **没有任何 ONNX 路径** |
| CPU | 官方自述 *"CPU is slow (better on server smp CPU)"*、*"Modern TTS engines are very slow on CPU"*，**无数字** |
| 结论 | 许可证不干净 + 形态是应用 + 无 ONNX → **不采用** |

---

## 3. 推荐

### 3.1 主选：**MOSS-TTS-Nano**（Apache-2.0 的 ONNX CPU 版）

**选它的理由（都是硬事实）**：

1. **许可证最干净**：代码 + 权重**都是 Apache-2.0**，可商用，无附加条款 —— 五个候选里只有三个满足，它是其中一个
2. **唯一有官方 CPU/ONNX 部署路径的**：读它的 ONNX 段就知道作者是奔着"CPU 无 GPU 本地部署"设计的
3. **体积小一个数量级**：227 MB（PyTorch）/ 728 MB（ONNX）vs 别人 1.5–5.3 GB
4. **本机实测最快**：热态 RTF **1.06**（≈实时），比 VoxCPM 快 **12 倍**
5. **输出规格最好**：**48 kHz 立体声**，直接能进视频工程；VoxCPM 只有 16 kHz 单声道
6. **克隆样本量宽松**：官方示例音频 2.6–17.4 秒，零样本、不用微调

**必须同时接受的两个代价**：

- ❌ **中文质量没有任何官方数据** —— 这是它最大的空白，**不能用估算补**
- ⚠️ 官方 README 的"免 PyTorch"说法**与代码不符**（ONNX 版仍要 torch 加载参考音频）

### 3.2 所以：**TASK-013 应先做一个"中文听测"再定稿**

**这是本次调研最实在的行动项。** 理由：

- **速度与体积的差距我已经量出来了**（热态 RTF 1.06 vs 13.99；227 MB vs 1.5 GB）—— 这部分不用再测
- **质量差距没有任何可用客观数据**：MOSS 一个数字都没有；VoxCPM 的 CER 0.93% 是它自己的 benchmark，**不能直接推到我这个场景**（音色是老板的声音，不是 benchmark 说话人）
- **只有耳朵能定**

我已经生成好对照样本（**同参考音频、同文本**），放在：

```
F:\Downloads\XBC\reports\task-013b-samples\
├── 参考音频_共同.wav        48 kHz 立体声 5.84s（两段样本共用的音色来源）
├── A_MOSS-TTS-Nano.wav     48 kHz 立体声 5.12s   ← RTF 见 §4.3 更正
└── B_VoxCPM-0.5B.wav       16 kHz 单声道 6.48s   ← RTF 13.99
```

**走法**：先让老板录 10 秒参考音频 → 两个模型各生成同一段配音文案 → 甲方听 → 定。
监听成本不到 10 分钟，能省掉"选错了返工"的数天。

### 3.3 若听测否决 MOSS → 用 **GPT-SoVITS**

理由：MIT 最干净、48 kHz 输出、中文数字扎实（WER 1.3% / SIM 73.5）、且**是唯一有"CPU 快于实时"官方数据的**（M4 RTF 0.526）。
代价是 **5.3 GB 权重 + 必须 PyTorch**，分发成本高一个量级。

### 3.4 不建议做默认的

- **VoxCPM**：中文数字最漂亮，但 ①输出**只有 16 kHz 单声道**（素材短板）②CPU 实测**慢 3.3 倍** ③`torchcodec` 在本机**装不上**。可作为**质量对照组**保留
- **OmniVoice / ebook2audiobook**：许可证不干净，**商用场景直接排除**

---

## 4. 实测方法与原始数据

### 4.1 测试机（**这点很重要** —— 我的数字不能直接外推到别的机器）

| 项 | 值 |
|---|---|
| CPU | Intel Xeon E5-2686 v4 @ 2.30 GHz，**18 核 / 36 线程**（2016 年服务器 U，单核偏弱） |
| 内存 | 64 GB |
| GPU | RTX 3060 Laptop 4 GB（**本次全程 CPU 推理，未使用**） |
| OS / Python | Windows / CPython 3.13 |
| 环境 | `F:\Downloads\xbc-research\moss-onnx-venv`（onnxruntime 1.30.0、torch 2.7.0+cpu、torchaudio 2.7.0+cpu） |

> **为什么不外推**：这是一颗 2016 年的低主频服务器 CPU。现代桌面 CPU 单核会更快，
> 但**快多少我不知道，不做估算**。要别的机器上的数字，就在那台机器上跑一遍 §4.3。

### 4.2 两个"说到做不到"的实测

**① 官方说 ONNX 版不需要 PyTorch —— 实际需要**

```
$ moss-onnx-venv\python.exe infer_onnx.py --prompt-audio-path assets/audio/zh_1.wav --text "..."
  File "onnx_tts_runtime.py", line 12, in <module>
    import torch
ModuleNotFoundError: No module named 'torch'
```

**② WeTextProcessing 是硬依赖，不是可选**

```
ERROR root: WeTextProcessing preload failed
    from tn.chinese.normalizer import Normalizer as ZhNormalizer
ModuleNotFoundError: No module named 'tn'
RuntimeError: No module named 'tn'          ← 直接崩，没有降级
```

绕行方式（**必需**）：`--disable-wetext-processing --enable-normalize-tts-text`

### 4.3 MOSS-TTS-Nano · 本机 CPU 实测

> ### ⚠️ 更正（2026-10-05，TASK-013 实现期间）
>
> **本节原来的 RTF 4.19 是错的 —— 它把模型加载时间算进了每一次推理。**
>
> 原因：当时每次都**新开一个进程**跑官方 `infer_onnx.py`，
> 于是每次都要重新加载约 728 MB 的 ONNX 模型（实测约 **14.1 秒**），
> 那个固定开销被摊进了 RTF。
>
> TASK-013 实现后，在**同一个进程里连跑 4 次**做冷/热分离：
>
> | 阶段 | 墙钟 | 音频 | **RTF** |
> |---|---|---|---|
> | 第 1 次（冷，含 14.1 s 加载） | 21.64 s | 7.12 s | **3.040** |
> | 第 2 次（热） | 5.85 s | 5.36 s | **1.092** |
> | 第 3 次（热） | 6.51 s | 6.16 s | **1.057** |
> | 第 4 次（热） | 6.04 s | 5.84 s | **1.033** |
>
> **热态 RTF ≈ 1.06（≈ 实时），冷启动 3.04。**
> 对配音场景的含义：5 分钟视频约等 5 分钟（首次多等十几秒加载）——
> 比本节原来的结论**好得多**。
>
> 顺带：当时那 4 次测量（1/4/18 线程）**全部是冷启动**，
> 所以"4 线程最快"这个结论也只在冷启动口径下成立，**热态没有复现线程数差异**。

命令（文本 30 字，参考音频为官方 `zh_1.wav`）：

```powershell
python infer_onnx.py --prompt-audio-path assets/audio/zh_1.wav `
  --text "公司年会即将开始，请大家有序入场，感谢各位一年来的辛勤付出。" `
  --output-audio-path out.wav `
  --disable-wetext-processing --enable-normalize-tts-text `
  --execution-provider cpu --cpu-threads <N>
```

| 线程数 | 墙钟 | 音频时长 | **RTF** | 备注 |
|---|---|---|---|---|
| 1 | 31.36 s | 5.84 s | **5.370** | |
| **4** | **24.44 s** | 5.84 s | **4.185** | ← 最快 |
| 18 | 31.99 s | 5.84 s | 5.478 | 线程更多反而更慢 |
| 18（重复） | 31.76 s | 5.84 s | 5.438 | 复现性良好 |
| 4（复测） | 24.91 s | 5.84 s | **4.266** | 峰值内存测量在这一轮 |

- **峰值内存：2193 MB**（整棵 python 进程树之和，400 ms 采样；曲线 43 → 2170 → 2137 MB）
- 输出：**48000 Hz / 2 声道 / 16 位**

### 4.4 VoxCPM-0.5B · 本机 CPU 实测

命令：

```powershell
voxcpm clone --hf-model-id openbmb/VoxCPM-0.5B `
  --prompt-audio <参考.wav> --prompt-text "<参考音频的文字>" `
  --text "今天的会议到此结束，请大家下周同一时间准时参加。" `
  --device cpu --output out.wav
```

| 次 | 墙钟 | 音频时长 | **RTF** |
|---|---|---|---|
| 1 | 75.0 s | 5.36 s | **13.99** |
| 2 | 79.6 s | 6.00 s | **13.27** |
| 3（生成对照样本时） | 84.9 s | 6.48 s | **13.10** |

- 输出：**16000 Hz / 1 声道 / 16 位**
- 依赖 `torchcodec` 在本机 **装不上**（`OSError`，需 FFmpeg 共享库），但推理路径未受影响（可用）

### 4.5 与官方数字的对照

| 方案 | 官方 CPU 数字 | 本机实测 | 差异说明 |
|---|---|---|---|
| MOSS-TTS-Nano | **无**（只有"2× 效率"和"M4 单核流畅"） | **热态 1.06 / 冷 3.04** | 官方从未给出可对照的数值 |
| VoxCPM-0.5B | **无** | RTF 13.3–14.0 | 同上 |
| GPT-SoVITS | M4 CPU RTF **0.526** | 未实测 | 官方唯一 CPU 数字，**未在 x86 上复现** |
| OmniVoice / ebook2audiobook | 无 | 未实测（已排除） | — |

> ⚠️ **GPT-SoVITS 的 M4 数字不能拿来跟我的 x86 数字横向比** —— 不同架构、不同指令集、不同内存带宽。

---

## 5. 与夏半仓架构的冲突点

### 5.1 共同冲突（三个候选都有）

| # | 冲突 | 说明 |
|---|---|---|
| 1 | **运行时需要 PyTorch** | TASK-010 刚为了"运行时不要 torch"把中文 CLIP 导成 ONNX。现在三个候选**都还要 torch** —— MOSS 的 ONNX 版也不例外（见 §4.2）。**这意味着 TASK-013 要重新面对"装不装 torch"这个决定** |
| 2 | **插件不得直接调模型** | 按《调研结果的使用规则》第 3 条，TTS 必须做成 **Core Capability + ModelProvider**（照 `ChineseClipProvider` 的样子），插件只声明"需要语音合成能力"。**不能**让插件 `import voxcpm` 自己跑 |
| 3 | **模型不能随插件包分发** | 按 TASK-010 补充约束，权重放 `AppPaths.models_dir`（`<数据根>/models/<model_id>/`），由 Core 的 `bind_models` 解析路径。**插件不知道模型路径** |
| 4 | **内核零第三方依赖** | 重依赖只能落在插件侧；kernel 一行都不能碰 |

### 5.2 逐项的额外冲突

| 候选 | 额外冲突 |
|---|---|
| **MOSS-TTS-Nano** | ① 官方"免 torch"是假的，**实际要 torch + torchaudio**（只为一件事：读参考音频）→ 我们可以选择**自己用 `soundfile` 重采样**来绕开，但那就不是"直接用它"了；② `WeTextProcessing` 默认硬依赖，**Windows 上装不了**（`pynini` 无 wheel），必须走 `--disable-wetext-processing`；③ `huggingface_hub` 未声明 |
| **GPT-SoVITS** | ① **5.30 GB + 配套 ~4 GB**，与"本地轻量分发"取向严重冲突；② `pyopenjtalk` 在 Windows 上编译困难；③ 依赖 `gradio`/`pytorch-lightning`/`funasr` 一整套 —— 是**一个应用**，不是库；④ 无官方免 torch 路径 |
| **VoxCPM** | ① 官方无 ONNX，社区版不算；② `torchcodec` 依赖 FFmpeg 共享库（本机装不上）；③ **0.5B 需同时给参考音频和它的文字转写** —— 对"让老板录 10 秒"这个交互是**额外要求**；④ 16 kHz 单声道输出与我们的 48 kHz 素材链不匹配 |
| **OmniVoice** | 许可证（CC-BY-NC + Boson 商用门槛）→ **不必再谈技术** |
| **ebook2audiobook** | 它是应用不是库 + 权重许可不干净 + 无 ONNX |

### 5.3 场景相关的两点（用户场景：给公司视频配音、克隆老板声音、每公司 3 个声音）

1. **"3 个声音"不是负担**：三个候选都是**零样本克隆** —— 每个声音是一段**参考音频**（10 秒级），
   **不需要为每个声音训模型**。3 个声音 = 3 个 wav + 3 个"声音档案"记录。**与候选无关，都是这么做的**
2. **离线批量场景下 RTF 的权重应该降低**：配音是"生成一次、然后剪辑"的离线活，
   RTF 1.06 与 13.99 的差别是"5 分钟视频等 5 分钟 vs 等 70 分钟" —— **都能接受**，
   而 MOSS 这一侧其实已经接近实时。
   **但 16 kHz 单声道是成品质量问题，会一直留在成片里**。所以 §3 把输出规格的权重排在速度前面

---

## 6. 发现但未做

| 发现了什么 | 为什么觉得该改 | 为什么这次不做 |
|---|---|---|
| MOSS 的 ONNX 版**仍然需要 torch**（官方说法与代码不符） | 直接影响"要不要装 torch"这个决定 | 属**上游取舍**。本次是选型调研，改上游代码或写适配层都要另开任务 |
| MOSS 的 `WeTextProcessing` 硬依赖在 Windows 上装不了 | 不绕行就跑不起来 | 同上；已在报告里给出可用绕行参数 |
| MOSS 中文质量**零官方数据** | 主推一个质量未证的东西有风险 | **不是我能补的** —— 只能听测。已生成对照样本交甲方 |
| VoxCPM 的 `torchcodec` 在 Windows 装不上 | 部署摩擦 | 推理路径未受影响，属依赖管理问题 |
| VoxCPM 模型卡里"research purposes only"与"Apache-2.0"文义有张力 | 商用前应确认 | **需要法务判断，不是技术判断**。已在 §2.3 标出 |
| OmniVoice 的**第三层许可**（audio_tokenizer 子模块） | 极易漏读 —— 只读主权重许可会得出错误结论 | 已排除该方案，无须再深入 |
| ebook2audiobook 的 LICENSE 与 `pyproject.toml`/PyPI 元数据**不一致**（Apache vs MIT） | 元数据不可信 | 已排除该方案 |

---

## 7. 需要你定

1. **先听样本**：`reports/task-013b-samples/` 里 A（MOSS）/ B（VoxCPM）两份，
   同一段参考音频、同一句文本。听完告诉我哪个能用在公司视频里
2. **要不要把 MOSS 的中文听测做成正式验收项**（TASK-013 里加一条"中文可懂度/自然度听测"）？
3. **要不要在更接近办公机的 CPU 上再测一遍 RTF**（我这颗是 2016 年的 Xeon，
   数字偏悲观）？给我一台目标机的访问就能测
4. 确认后我再动代码 —— **本次零改动**
