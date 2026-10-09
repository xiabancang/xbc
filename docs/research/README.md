# 调研台账

本目录收录夏半仓的调研产物。**所有调研结果的使用都受下面这条规则约束。**

---

## 调研结果的使用规则（常驻）

调研找到的项目，**禁止直接复制代码进夏半仓**。使用方式仅限三种：

| 方式 | 含义 | 附加要求 |
|---|---|---|
| **采用** | 完整集成 | 必须按夏半仓插件规范**重新封装**；许可证必须允许商业使用 |
| **参考** | 只参考设计思路或算法 | **代码自己写**，不搬运 |
| **从零** | 调研后确认没有合适的 | 自己实现，并在报告中记录"为什么现有方案不合适" |

无论哪种，都必须：

1. **检查许可证是否允许商业使用**，并记录在下表
2. 封装为符合夏半仓规范的插件（`plugin.json`、工具注册）
3. 插件通过 **Core Capability** 调 AI，**不直接调模型**
4. **不破坏已有 Core 的隔离规则**（能力白名单、作用域释放、零第三方依赖）

**如果调研项目的架构与夏半仓冲突，在本报告与登记表中明确指出，不要强行整合。**

---

## 逐项目登记表

| 项目 | 许可证 | 可商业使用 | 在夏半仓的使用方式 | 架构冲突 | 时效性：上游维护状态 |
|---|---|---|---|---|---|
| [pluggy](#pluggy) | MIT | ✅ | **参考**（钩子机制设计；实现自写） | 部分：其大量机制我们不需要 | 活跃（本地副本最后提交 2026-09-30） |
| [PySceneDetect](#pyscenedetect) | BSD-3-Clause | ✅ | **从零**（改用 FFmpeg 原生滤镜） | **有**：引入依赖与"内核零依赖"冲突 | 活跃（2026-09-21） |
| [ollama-python](#ollama-python) | MIT | ✅ | **从零**（自写 HTTP 客户端） | **有**：封装模型调用会诱导插件绕过 Capability | 活跃（2026-09-29） |
| [PyQt-Fluent-Widgets](#pyqt-fluent-widgets) | **GPL-3.0** | ❌ | **参考**（仅看设计；界面用原生 Qt） | **有**：GPL 传染 + 与"不做 UI 美化"取向相反 | 上游活跃（2026-08-01）；**本地副本已失效** |
| [Dify](#dify) | NOASSERTION（非标准） | ⚠️ 采用前须逐条读 LICENSE | **参考**（只看公开架构描述，未取代码） | **有**：平台型架构与单机内核冲突 | 活跃（2026-10-04） |
| [uTools](#utools) | 闭源商业软件 | — | **参考**（只读公开开发者文档） | **有**：Electron 宿主模型与我们不同 | 闭源产品，无法从仓库判断 |
| [MCP](#mcp) | NOASSERTION（非标准） | ⚠️ 只读公开规范 | **参考**（只读公开规范文本） | **有**：进程协议 vs 同进程插件 | 活跃（2026-10-03） |
| [DeepSeek Harness](#deepseek-harness) | 未公开（私有发行包） | — | **参考**（只读其插件开发说明） | **有**：Cordis DI 框架 vs 显式契约 | 本机发行包，无独立仓库可查 |

> **"可商业使用"栏的判定依据**：MIT / BSD-3-Clause 允许商业使用；
> **GPL-3.0 不允许**（采用即要求衍生作品整体 GPL）；
> `NOASSERTION` 表示 GitHub 无法归类，**必须逐条读 LICENSE 才能判定**。

> **"时效性：上游维护状态"栏的判定依据**：本地参考副本的 `git log -1` 日期，或 GitHub API 的 `pushed_at`。
> **八个项目没有一个处于"已停维护"状态** —— 全部在 2026-09/10 有提交。这一栏是如实填写，不是凑数。
> 「已过时 / 已放弃」的项另见下一节。

---

## 架构决定：语音合成走「路线 D」（TASK-013，2026-10-05）

TASK-013 要把**声音克隆**接进 Core 的 `speech` 能力。选型定的是 **MOSS-TTS-Nano**
（Apache-2.0 代码 + 权重、官方 ONNX、226.8 MB、48 kHz 立体声），
但它的官方 ONNX 版有一个说不通的地方，于是产生了三条实现路线，**甲方选了 D**。

### 三条路线是什么

| 路线 | 做法 | 运行环境增量 | 状态 |
|---|---|---|---|
| **A** | 装 PyTorch + torchaudio，直接用官方 ONNX 入口 | **+1188 MB** | 未选 |
| **C** | 不装 PyTorch，以 `ort_cpu_runtime` 为底座**自己实现约 380 行编排** | +约 10 MB | **回退路径** |
| **D** ✅ | 给上游一个"空壳 torch"，**只覆写一个私有方法**，其余走上游代码 | **+7.1 MB** | **已采用** |

### 为什么需要 D：上游的说法与代码不符

官方 ONNX 版 README 写 *"No PyTorch dependency during inference"*，
**但 `onnx_tts_runtime.py` 第 12-13 行硬 `import torch` / `import torchaudio`**。

实测（无 torch 环境跑官方入口）：

```
File "onnx_tts_runtime.py", line 12, in <module>
  import torch
ModuleNotFoundError: No module named 'torch'
```

而 torch 在整条链里**只被用来做一件事**：`_load_reference_audio` 读参考音频
（加载 + 重采样）。装 1183 MB 的 torch 只为读一个 wav，代价不成比例。

**规则上 torch 并不违规** —— [《AI 能力隔离》](../harness-rules.md) 的细化明确允许
*"插件提供的 Provider 实现允许 import 本地推理运行时（仅限 Provider 文件使用）"*。
所以这是**体积决策**，不是架构越界。

### ⚠️ 本决定依赖的上游**私有** API（这是 D 的风险点）

| 项 | 值 |
|---|---|
| 仓库 | `OpenMOSS/MOSS-TTS-Nano` |
| **提交** | **`8b7bcc9341b3b4ef3a3a58ba1338a7d85ff133eb`**（2026-09-06） |
| `pyproject` 版本 | `0.1.0` |
| 模块 / 类 | `onnx_tts_runtime.OnnxTtsRuntime` |
| **私有方法** | **`_load_reference_audio(self, reference_audio_path)`**（该文件 L445） |
| 该文件 SHA256(前 16) | `f4169a3ef0fdfb5e` |
| 我们的实现 | `plugins/voice_tts/providers/moss_tts.py` |

**依赖它的方式是"覆写"**：继承上游运行类，把 `_load_reference_audio` 换成
`soundfile` + `soxr` 的实现（不做任何代码搬运，只覆写一个方法）。
另外在**导入上游的那一瞬间**把 `torch` / `torchaudio` 换成空壳，导入完立刻撤除 ——
空壳里那两个函数**故意会抛异常**，万一上游多出 torch 用法、覆写没盖住，会当场炸出来。

### 失效触发条件（满足即重开）

| # | 触发条件 | 为什么是它 |
|---|---|---|
| 1 | **上游 `onnx_tts_runtime.OnnxTtsRuntime._load_reference_audio` 改名 / 改签名 / 被删除** | 这是我们依赖的唯一私有 API；它没了，覆写就落空 |
| 2 | **上游在 `onnx_tts_runtime.py` 里新增了别处对 `torch` / `torchaudio` 的使用** | 空壳会抛 `AssertionError` → 转成 `UpstreamContractError`；覆写只盖住了原有那一处 |
| 3 | **上游发布真正的 torch-free 版本**（README 那句话真的兑现了） | 那就不需要 D 了，直接回到官方入口 |
| 4 | `moss-tts-nano` 的许可证发生变化（代码或权重不再是 Apache-2.0） | 商用前提变了，选型要重算 |

**守卫**：`plugins/voice_tts/providers/moss_tts.py::assert_upstream_contract()`
在导入时检查那个私有方法在不在；不在就抛 `UpstreamContractError`，
**消息里写明上游提交、失效原因、以及下面两条回退路径**。
`tests/test_speech_capability.py::UpstreamContractGuardTests` 守着这条守卫
（含"方法被改名时必须报错"的用例）。

### 回退路径

| 顺序 | 路线 | 代价 | 什么时候走 |
|---|---|---|---|
| 1 | **C —— 不装 PyTorch，自己实现编排** | 约 380 行编排（参考音频加载 + 文本分段 + 合成循环），**音频质量的调试风险要认** | 私有 API 消失，但不想装 PyTorch |
| 2 | **A —— 装 PyTorch + torchaudio，用官方入口** | **+1188 MB** 运行环境 | 私有 API 消失，且宁可花磁盘也不想自己写编排 |

两条都会**先在交付报告里报告，再动手**（A 类前置）—— 换路线是架构决定，不是实现细节。

### 附带记录：两个实测到的上游坑

| 坑 | 现象 | 我们的处理 |
|---|---|---|
| **WeTextProcessing 是硬依赖** | 默认路径 `No module named 'tn'` **直接崩**（不是降级）；它依赖的 `pynini` 在 Windows 上没有 wheel | 显式 `enable_wetext=False`，改用上游自带的 robust 归一化 |
| **sentencepiece 打不开含非 ASCII 的路径** | 实测：纯 ASCII 路径 ✅ / 含中文路径 ❌ `NOT_FOUND Error #2`。而**默认数据根就是 `夏半仓工具箱`（含中文）** | 把 tokenizer 暂存到 ASCII 目录再加载；**对照实测 `onnxruntime` 能正常读中文路径**，所以只有 sentencepiece 需要绕 |

> **附带发现一处既有命中**：`src/xbc/core/capabilities/ai/provider.py` 讲系统代理时写了
> "Clash / 公司网关"。那里的"公司"是**网络代理**含义，与业务域无关；
> 它早于 TASK-013，**本次未改**（范围外），在测试里明确记为"已知且不相关"。
> 要不要一并改成"企业网关"之类，等你一句话。

---

## 已过时 / 已放弃的项

按"如某项已时过境迁，如实标注，不要强行填写"逐项核实：

| 项 | 原状态 | 现状 | 标注 |
|---|---|---|---|
| TASK-002 方案 **§8「与现有 XBC 最小内核的差距」** | TASK-002 时点的"未实现"清单 | 差距已被 TASK-003 / 004 / 005 **全部关闭** | **已过时**（保留作历史记录） |
| TASK-002 方案 **"为进程外传输留扩展位"** | "这个扩展位现在就留出来，但不实现" | TASK-007 原则 6 明确「**不做任何为将来预留的扩展点**」，该决策**已被推翻** | **已放弃** |
| TASK-002 方案 **把 MCP 定位为"将来的进程外插件通道"** | 将来扩展方向 | 至今未做，且与原则 6 相悖 | **已搁置** |
| TASK-002 方案 **"用户 / 将来商城"**（插件来源） | 插件来源规划 | 商城已列为明确禁止项 | **已放弃** |
| TASK-002 方案 **全文 9 处 `media_analyzer`** | MVP 第一个真实插件名 | 实际交付的是 `video_analyzer`（TASK-005） | **与实际不符**（名字从未对齐） |
| TASK-002 方案 **§6 第一阶段 MVP 范围** | 待实现范围 | 已实现，并被后续任务扩展 | **已完成** |
| `F:\Downloads\xbc-refs\Fluent-Widgets` **本地副本** | 计划用于 UI 参考 | 检出内容 **0 个文件**，目录只剩 `.git`（当初 HTTPS 克隆失败） | **参考副本已失效**（且 GPL 已禁止采用） |
| `mcp-research/` **原始材料目录** | MCP 规范抓取原文 | 仍在本地（47 个文件），但被 `.gitignore` 排除 | **不入库的本地材料** |
| `F:\Downloads\AI企业内容操作系统_V18...py` **参考基线** | TASK-002 的首要功能参考 | TASK-005 已把它插件化为 `video_analyzer`；现在只剩"命令形态来源"的历史作用 | **部分过时**（仍只读、未修改，`LastWriteTime` 保持 2026-09-22 23:38:20） |
| **`plugins/knowledge_base/`**（内置插件） | 未跟踪的工作区目录，TASK-007～TASK-013 期间在 **6 份报告**里被反复记为"归属待裁决" | **已存档并从仓库删除**（2026-10-05） | **已删除**（存档可恢复，见下节） |

### `plugins/knowledge_base/` —— 已存档并删除（2026-10-05）

| 项 | 内容 |
|---|---|
| **状态** | **已存档，并从仓库删除** |
| **存档路径** | `F:\Downloads\xbc-archive\knowledge_base-2026-10-05.zip` |
| **存档 SHA256** | `8fa037b558a4f12fae2f9f3a5ff767f3d136167993447aae61738be9c4e258a7` |
| 存档大小 / 条目数 | 33,947 字节 / 8 个条目 |
| 删除日期 | 2026-10-05 |
| 原先的版本管理状态 | **从未被 git 跟踪**（`git ls-files plugins/knowledge_base` 为空）→ 删除**不涉及 git 历史**，无需 `git rm` |

#### 删除原因

1. **身份一直没定性。** 它不是夏半仓任何任务书的产物，却在 TASK-007 到 TASK-013 的
   **6 份报告**里反复出现，每次都记成"归属待裁决"。
   **悬着的东西比删掉的东西贵** —— 每轮都要重新解释一遍它是什么、为什么留着、算不算范围内。
2. **它带着一个被禁词命中。** `plugin.py` 的文档字符串里出现 `ollama`
   （`ast` 扫描确认**没有真实 import**）。这迫使 `tests/test_ai_capability.py` 里
   长期挂着一条**豁免特例**，让"全局扫描干净"这句话变成半真 —— 而规则的判据本是"**0 命中**"。
3. **它不是可用的功能。** 没有随任何任务书交付、没走过验收、没有调用方。
   留着不产生价值，只产生解释成本。
4. **删了可恢复。** 存档含全部源文件，SHA256 已核对一致。

#### 连带处理

| 项 | 处理 |
|---|---|
| `plugins/knowledge_base/` 目录 | **已删除**（12 个文件 / 149.1 KB；比存档多出的 4 个是 `__pycache__` 生成物） |
| `tests/test_ai_capability.py` 的豁免特例 | **已取消** —— `test_global_scan_known_exception_is_documented` 改为 `test_global_scan_is_clean`，恢复严格 0 命中 |
| `README.md` 的目录树 | 去掉 `knowledge_base/` 一行 |
| `.gitignore` | **无需改动** —— 本来就没有 `knowledge_base` 规则（已核对） |
| 仓库的 `config/plugins.json` | **无需改动** —— 本来就没有它的条目（已核对） |
| **用户数据根**的 `config/plugins.json` | ⚠️ **仍有一条 `knowledge_base` 条目**（属用户数据，本次未动）。插件删掉后它是**悬空配置**，不影响运行 |
| 历史报告（TASK-007 / 008 / 009 / 011 / 013 等） | **不改** —— 那是当时的记录，如实保留；本节的结论以本台账为准 |
| `tests/test_ai_capability.py` 禁词表里的 `知识库` / `文档库` | **保留** —— 那是**被禁的业务词**，不是关于这个插件的豁免 |

#### 重新评估触发条件（满足任意一条即重开）

| # | 触发条件 | 为什么是它 |
|---|---|---|
| 1 | 出现**明确的业务需求**要把本地文档变成可检索知识库，**且下达了任务书** | 那就按那时的任务书重新评估 —— **不是简单解压回去**（解压回去等于回到"没定性"的状态） |
| 2 | 需要复用它里面的某个**算法 / 设计**（分块、检索、引用溯源） | 按《调研结果的使用规则》的「**参考**」方式**重新实现**，不直接搬代码 |

> 反例（**不构成**触发条件）：删了以后觉得可惜、报告里还有引用、star 变多。

### 代码层面的实际核验

夏半仓的内核与插件**均未引入上述任何项目的代码或依赖**（`ast` 扫描 45 个内核文件 + 10 个插件文件）：

| 项目 | 在夏半仓代码中的痕迹 |
|---|---|
| pluggy | 仅出现在 `src/xbc/core/contract/hookspec.py` 的文档字符串（"借鉴 pluggy"），**无 import** |
| PySceneDetect | 代码中**无 `scenedetect`**；切分用 `ffmpeg -vf "select='gt(scene,T)',showinfo"` |
| ollama-python | 代码中**无 `import ollama`**；用标准库 `urllib` 直连 HTTP |
| PyQt-Fluent-Widgets | `src/xbc/ui/shell.py` 中**无 `fluent`**；只用原生 Qt 控件 |
| Dify / uTools / MCP / DSH | 未取任何代码，只读公开文档与规范 |

内核唯一的第三方依赖是 **PySide6**（仅 `src/xbc/ui/shell.py`），这是**声明过的可选 GUI 依赖**
（`pip install -e ".[gui]"`），与调研项目无关。

---

## 向量检索候选（TASK-009 补调研）

TASK-009 做技术选型时没做调研，**2026-10-05 补做**。完整报告：
**[vector-search-options.md](vector-search-options.md)**

| 候选 | 许可证 | 可商业使用 | 使用方式 | 架构冲突 | 时效性 |
|---|---|---|---|---|---|
| **当前实现：全表扫描 + Python 余弦** | 不适用（自写） | — | **从零**（采用） | 无 | — |
| sqlite-vec | Apache-2.0 | ✅ | **从零**（本次不采用） | **有**：需加载 C 扩展 `.dll` | 2026-05-18 |
| hnswlib | Apache-2.0 | ✅ | **从零** | **有**：C++ 扩展，需编译 | 2026-09-15 |
| faiss | MIT | ✅ | **从零** | **有**：重量级 + BLAS 依赖 | 2026-10-03 |
| chromadb | Apache-2.0 | ✅ | **从零** | **有，原则性**：自带 embedding function，**与"插件经 Core Capability 调 AI"冲突** | 2026-10-04 |
| USearch | Apache-2.0 | ✅ | **从零** | **有**：需 C++ 轮子 | 2026-10-02 |
| annoy | Apache-2.0 | ✅ | **从零** | **有**：需构建 | **2025-10-29（近一年无提交）** |

**六个候选全部允许商业使用**（Apache-2.0 × 5、MIT × 1）—— **许可证不是不采用的理由**，
架构冲突才是（每个都要往插件里塞编译产物，与"插件纯 Python 单目录"冲突）。

**顺带调研的结论**：中文标签检索**不引入 FTS5**。实测 `LIKE` 7/7 全对，
而 FTS5 的 `unicode61` 与 `trigram` **各只有 4/7**（2 字中文词两种都失效）。

**实测更正**：交付报告初版把全表扫描的性能**低估了 4–22 倍**
（估"10,000 镜头 0.1–0.5s"，实测 **2,199ms**）。已更正，触发条件明确为"**镜头数接近 1,000**"。

---

## 视频语义检索候选（TASK-010 前置调研）

2026-10-05 完成。**网络调研 + 本机活体实测**（装了 3 个独立 venv，
在 TASK-009 的 14 帧真实语料上跑了三条路线）。完整报告：
**[video-semantic-retrieval-options.md](video-semantic-retrieval-options.md)**

| 候选 | 许可证 | 可商业使用 | 使用方式 | 架构冲突 | 时效性 |
|---|---|---|---|---|---|
| Chinese-CLIP（**代码**） | **MIT** | ✅ | **采用**（模型，非代码） | **有**：需 torch（~200MB）或自导 ONNX | 2026-03-31 |
| ↳ `chinese-clip-vit-base-patch16` **权重** | **未声明** | ⚠️ **必须先澄清** | — | — | 2022-12-09 |
| ↳ `chinese-clip-rn50` 权重 | **Apache-2.0** | ✅ | 备选（77M，更小） | — | 2022-11-09 |
| AltCLIP | **creativeml-openrail-m** | ⚠️ **有使用限制** | 不采用 | 有 | 2025-04-16 |
| Taiyi-CLIP | **Apache-2.0** | ✅ | 参考（R@1 低约 10 个点） | 有 | 2023-05-25 |
| amon-hen | **MIT** | ✅ | **参考**（融合思路） | **有**：`onnxruntime` + `sqlite_vec` 两个平台二进制 | 2026-09-02 |
| ↳ `felixhrdyn/mobileclip2-s0-onnx` | **MIT** | ✅ | 不采用（**中文不可用**） | — | 下载量 0 |
| ↳ 上游 `apple/MobileCLIP2-S0` | **apple-amlr**（自定义） | ⚠️ | 不采用 | — | 2025-10-09 |
| ↳ 训练数据 DFNDR | **CC-BY-NC-ND** | ❌ 非商用 | — | — | — |
| MaterialSearch | **GPL-3.0** | ❌ **传染** | **参考**（只读设计） | 有 | 活跃 |
| arkiv | **PolyForm Perimeter 1.0.1** | ⚠️ **禁止做竞品** | **参考** | **有，重** | 2026-10-04 活跃 |
| VideoSeek | **AGPL-3.0** | ❌ **传染（含网络）** | **参考** | **有，重** | 2026-10-04 活跃 |
| VideoChain | — | — | **未能核实该项目** | — | — |

**核心实测结论（同一批 14 帧、同样 10 个中文查询）**：

| 路线 | 中文 top-1 | 中文前 3 | 图像编码 |
|---|---|---|---|
| **中文 CLIP ViT-B/16** | **6/10** | **8/10** | 116 ms/帧 |
| Caption-then-Embed（现有） | 2/10 | 3/10 | ~830 ms/镜头 |
| **MobileCLIP2-S0（amon-hen）** | **1/10** | 3/10 | **26 ms/帧** |
| MobileCLIP2-S0 英文对照 | **5/5** | 5/5 | — |

**MobileCLIP2 中文失效已实证到机制层**：其 BPE 词表是英文的，汉字被拆成 UTF-8 字节碎片
（`星` = `e6 98 9f` → `['æĺ','Ł</w>']`，对照 `star` → `['star</w>']`）。
**不是模型弱，是语言不匹配。**

**Caption vs Joint Embedding 的结论是"都要，不能二选一"**：
CLIP **8 胜 1 平 1 负**，但那 1 负（`宇宙星云`：Caption 排名 1 vs CLIP 的 4）
说明 Caption 在"抽象概念 + 合成画面"上不可替代。**建议走双路召回 + 融合**。

**Q3 的答案**：调研范围内**没有找到"图片嵌入 + 画面描述嵌入"分数融合的产品级先例**。
最接近的是 amon-hen 的 `--mode hybrid`（视觉 + **语音**，不是画面描述）。
**这是空白点，也是差异化机会。**

**新发现的一个设计缺陷（比换模型更紧急）**：现有 Caption 路线的向量由
`answer + labels` 算出，但**库里只存了 `labels`** —— 检索行为无法从库内审计，
且长回答稀释了标签区分度。已记入报告 §1.4。

**许可证提醒**：`chinese-clip-vit-base-patch16` 权重**在 HF 上未声明许可证**，
而它正是 MaterialSearch 与调研方向都指向的那一个 —— **采用前必须先澄清**。

---

## TASK-010 采用记录：chinese-clip-rn50

TASK-010（检索质量修复）决定采用 **`OFA-Sys/chinese-clip-rn50`**。按规则登记六栏：

| 项 | 内容 |
|---|---|
| **项目 / 来源** | 代码 `OFA-Sys/Chinese-CLIP`（MIT，6,013★）；权重 `OFA-Sys/chinese-clip-rn50` |
| **许可证** | **Apache-2.0**（HF `model_info().cardData['license'] == 'apache-2.0'`，`license:apache-2.0` 标签）；代码 MIT |
| **可商业使用** | ✅ **可以** —— Apache-2.0 无 copyleft 传染（对比：MaterialSearch GPL-3.0、VideoSeek AGPL-3.0 均不可用） |
| **使用方式** | **采用**（集成**模型**，不搬代码）：用官方 `cn_clip` 包**一次性导出 ONNX**，运行时不依赖 `cn_clip`/`torch` |
| **架构冲突** | **有**：Core 的 embedding 能力只接受文本，图片嵌入无法经 Core Capability → **已停下报告待裁决**（见 TASK-010 报告 §4） |
| **时效性** | 权重 2022-11-09 上传（较早，但模型本身不需要频繁更新）；代码最后提交 2026-03-31 |

**为什么不用 `vit-base-patch16`**：它的**权重在 HF 上未声明许可证**，
而 TASK-010 禁止项明确列了"❌ 使用 chinese-clip-vit-base-patch16（权重许可证未声明）"。
**rn50 是唯一许可证干净的候选**，代价是它**不是 transformers 格式**
（HF 上只有 `clip_cn_rn50.pt`，需要 `cn_clip` 包加载后自行导出 ONNX）。

**部署形态（实测）**：

| 环节 | 依赖 | 说明 |
|---|---|---|
| 导出（一次性，开发机） | `torch` + `cn_clip` + `torchvision` + `timm` + `onnx` | **不需要在用户机器上做** |
| 运行 | **`onnxruntime` + `numpy` + `pillow`** | **运行时不需要 torch** |

产物：`vision_model.onnx` 146.1 MB + `text_model.onnx` 147.6 MB + `vocab.txt` 0.1 MB ≈ **294 MB**。

**导出正确性**：ONNX 与 torch 在 14 帧图像 + 10 条中文查询上**余弦全部 = 1.00000000**（精确等价）。

**性能与质量（本机实测）**：视觉塔 **27 ms/帧**、文本塔 **10.5 ms/查询**；
中文检索 **top-1 10/10、前 3 10/10**（同批 14 帧 / 10 个查询）。

> ⚠️ **预处理必须与官方逐字一致**：官方 `image_transform(224)` 是
> `Resize((224,224))`（**直接拉成正方形**），不是标准 CLIP 的 shortest-edge + center-crop。
> 用错预处理实测 **top-1 从 10/10 掉到 7/10**。
> （对照：amon-hen 的 MobileCLIP2 结论正好相反 —— 它发现拉伸会损失 0.03 余弦而改用裁切。
> **不同模型的预处理要求相反，必须按各自官方实现来。**）

---

## TASK-010 架构裁决：远端模型 SDK vs 本地推理运行时

**裁决日期**：2026-10-05　**触发**：TASK-010 引入"插件提供本地模型 Provider"

TASK-010 §7 判定：**插件提供 Provider 实现时 `import onnxruntime` 不构成"插件直接调用模型 SDK"**。
据此把 AI 能力隔离规则细化，并**落盘**到 [docs/harness-rules.md](../harness-rules.md)。

### 裁决内容

| # | 类别 | 规则 |
|---|---|---|
| 1 | **插件业务代码** | 禁止 import 远端模型 SDK（`openai`/`ollama`/`anthropic`/`litellm` 等）、禁止 import HTTP 客户端（`requests`/`httpx`/`aiohttp`/`urllib`/`http.client`）、禁止直接发起远端模型调用 |
| 2 | **插件提供的 Provider 实现** | **允许** import 本地推理运行时（`onnxruntime` 等）；但**业务代码不得直接调用该 Provider**，必须经 `ctx.ai.*` 走 Core Capability |
| 3 | **禁词表** | 远端 SDK / HTTP **保留**；本地推理运行时**不在禁词表内**，改为约束"**仅限 Provider 文件使用**" |

### 判据

**"是否绕过能力层去够远端模型"，不是"有没有出现某个词"。**

### 本项目的实测核对（无需改代码）

现有禁词表**本来就不含本地推理运行时**：

```python
FORBIDDEN_IN_PLUGINS = [
    "openai", "ollama", "anthropic", "litellm",
    "requests", "httpx", "aiohttp", "urllib", "http.client",
]
```

所以第 3 条的"从禁词表移除"**在现有表上已经成立**。

### 对登记表的影响（新增一栏判定）

今后登记调研项目时，"架构冲突"栏若涉及"是否可以直接 import 某个库"，按本裁决分类：

| 库的类型 | 例子 | 业务代码 | Provider 文件 |
|---|---|---|---|
| 远端模型 SDK | `openai` / `ollama` / `anthropic` | ❌ 禁止 | ❌ 禁止（改用 Core 的 Provider 抽象） |
| HTTP 客户端 | `requests` / `httpx` | ❌ 禁止 | ⚠️ 仅当该 Provider 确实要连远端服务（Core 自带的 `providers/ollama.py` 就是这种，用的是标准库 `urllib`） |
| **本地推理运行时** | `onnxruntime` | ❌ 禁止 | ✅ **允许** |
| 通用计算库 | `numpy` / `pillow` | ⚠️ 视用途 | ✅ 允许 |

### 本条对应的登记项

`xbc_va_clip.py`（中文 CLIP 本地 ONNX Provider）—— **使用方式：采用**；
**架构冲突：无**（import `onnxruntime` 属本裁决允许的第 2 类）。

### 尚未机械化的部分

"本地推理运行时仅限 Provider 文件使用"**目前只写在文档里，没有测试守着**。
机械 enforce 需要新增检查（如：含本地运行时 import 的 `.py` 必须同时定义 `ModelProvider` 子类），
属于新增代码 —— **本次未做**（要求只动规则文档）。

---

## 每日扫描判断记录

每日 GitHub 扫描（`python tools/github_daily_scan.py`）扫到的项目，**判断过就要留痕**，
免得同一个项目明天再被"重新发现"一次。

> 机制与范围见[《Harness 工作规则》的「每日 GitHub 扫描（广度兜底）」](harness-rules.md)。
> **判断里的结论分"定论 / 当前判断"两档**：写成定论**必须附可观察的重新评估触发条件**，
> 没有触发条件的一律降级为"当前判断"。

| 项目 | 许可证 | 可商用 | 使用方式 | 架构冲突 | 结论 |
|---|---|---|---|---|---|
| `zenstory-ai/video-recap-skills` | MIT | ✅ | **参考** —— 只读其产物契约设计，**未取任何代码** | **有，明确**：它是**编程 Agent 的 skill 包**（Claude Code / Codex / OpenCode），AI 部分走**云端小米 MiMo API**；XBC 是"本地能力层 + 插件"，**方向不同**。它的**场景检测同样是 ffmpeg**（README 自述"本地只要 ffmpeg"）→ **没有可替代 V18 镜头切分的算法** | **不引入**（2026-10-05） |
| `Aseiel/VideoHighlighter` | **AGPL-3.0** | ❌ | **忽略** | **强 copyleft 硬冲突**：网络提供服务也要开源全部源码；该项目自身另有付费 Pro 版双授权 | **不引入**（2026-10-05） |
| `line/lighthouse` | Apache-2.0 | ✅ | **参考（低优先）** —— 只记它的**输出形态**，未取代码 | **有**：① 任务是**视频内时间窗检索**（文本 → `[start, end, score]`），与我们的"整库选镜头"粒度不同；② 运行时**必须有 torch + torchaudio**（+ Slowfast / PANNs 权重）—— 我们刚在 TASK-010 费力做到运行时不要 torch；③ **视频上限 150 秒**（其 benchmark 决定），真实素材直接卡住 | **不引入**（2026-10-05）。可看的是它给每段打 `pred_saliency_scores`（显著性）的思路 |
| `nebulabroadcast/nebula` | **GPL-3.0** | ❌ | **忽略** | 强 copyleft；且是 TypeScript 广播级 MAM，技术栈与形态都不符 | **不引入**（2026-10-05） |
| `debpalash/VoiceStudio` | **AGPL-3.0** | ❌ | **忽略** | 强 copyleft 硬冲突 | **不引入**（2026-10-05） |
| `FurkanGozukara/Stable-Diffusion` | **GPL-3.0** | ❌ | **忽略** | 强 copyleft；且是个 **Jupyter 杂货铺仓库**（SD / TTS / 深伪什么都塞），不是可依赖的工程 | **不引入**（2026-10-05） |
| `pnnbao97/VieNeu-TTS` | Apache-2.0 | ✅ | **忽略** | 无冲突，但**只支持越南语** —— 我们用不上 | **不引入**（2026-10-05） |
| `FireRedTeam/FireRedTTS3` 🆕 | Apache-2.0（**代码与权重都是**，已分别查） | ✅ | **忽略** | **有，且是硬的**：① 依赖 **`flash_attn`**（要 CUDA 编译）→ **实质是 GPU-only**，直接撞"无 GPU 假设"；② **权重 19,814 MB（19.3 GB）** —— 是 MOSS 的 27 倍；③ 无 ONNX 路径；④ 模型卡 `language` 字段为空，**中文支持未声明** | **不引入**（2026-10-06） |
| （一批）`oterm` / `mcp-client-for-ollama` / `conduit` / `chatbox` / `gpt_mobile` / `ollama-app` / `oriveo` / `rust-genai` | MIT / MIT / GPL-3.0 / GPL-3.0 / GPL-3.0 / Apache-2.0 / AGPL-3.0 / Apache-2.0 | 部分 ❌ | **忽略（整类）** | **无架构冲突可言 —— 因为它们不是这个品类**：8 个全是**面向最终用户的 LLM 聊天客户端**（终端 / 移动端 / 桌面）。XBC 是**插件平台 + 能力层**，不是聊天 UI；我们自己的 `OllamaProvider` 已经直连本地 Ollama，不需要中间客户端 | **不引入**（2026-10-06） |
| `fluxions-ai/vui` 🆕 | Apache-2.0（**代码与权重都查过**：GitHub 标 `NOASSERTION` 是它自己的归类怪癖，LICENSE 文件是标准 Apache 2.0；权重卡 `apache-2.0`） | ✅ | **忽略** | **有，且是决定性的**：权重卡 `language = ['en']`、tags 也是 `en` —— **只支持英文**；整篇 46K README **一次都没提** language / 多语言 / 中文。另外其定位是**实时对话语音**（Pipecat 集成、streaming、`flash-attention`、cu12/cu13），**GPU 向**，且要 `torch>=2.11` + torchaudio + torchcodec | **不引入**（2026-10-08）。**中文配音用不上** |
| `OpenAssetIO/OpenAssetIO` 🆕 | Apache-2.0 | ✅ | **忽略** | 无冲突，但**不是这个场景**：它是给**影视/后期 CGI 流水线**用的「工具 ↔ 资产管理系统」**互通标准**（C++，host + manager 插件模型，对标 Maya/Nuke ↔ ShotGrid 那一层）。XBC 是**单机本地平台 + 自己的 SQLite 素材库**，规模与问题域都不同 | **不引入**（2026-10-08） |

### 每日扫描的逐日留痕

**只记"今天有没有新东西"** —— 新增项各自在上表有完整判断，这里不重复。
（早先几天的结论本来是分段落写的，2026-10-09 合并成这张表：
每天新增一段会让台账迅速变垃圾，而绝大多数日子的结论是"新增 0"。**事实一条没丢。**）

| 日期 | 命中 | 新增 | 新增项判成什么 | 备注 |
|---|---|---|---|---|
| 2026-10-06 | 23 | **1** | FireRedTTS3 → **忽略** | 其余 22 项全是此前判过的 |
| 2026-10-07 | 23 | 0 | — | **集合与 10-06 完全一致**（只有 star 数在涨） |
| 2026-10-08 | 24 | **2** | vui → **忽略**（权重 `language=['en']`，只支持英文）<br>OpenAssetIO → **忽略**（影视流水线互通标准，非本场景） | FireRedTTS3 同日**消失**：`pushed_at=2026-09-08` 掉出 30 天窗口 —— **窗口滚动，不是它消失了** |
| 2026-10-09 | 24 | 0 | — | **集合与 10-08 完全一致** |

**关于"连续多天一模一样"**：扫描器的过滤条件是「最近 30 天内更新」，
所以同一批活跃项目会连续出现多日 —— **这是正常的，不代表扫描失效**。
真正的信号是**新增项数**与**集合差集**（本表后两列）。

---

**另有 5 个 TTS / 声音克隆项目**（GPT-SoVITS / VoxCPM / ebook2audiobook / OmniVoice /
MOSS-TTS-Nano）**不在本表** —— 它们不是"要不要引入"，而是 **TASK-013 的候选**。

> **该项已于 2026-10-05 结项**：选 **MOSS-TTS-Nano**，实现走
> [「路线 D」](#架构决定语音合成走路线-dtask-0132026-10-05)。
> **两个候选被许可证排除**：**OmniVoice**（权重 CC-BY-NC，且 `audio_tokenizer`
> 子模块另叠 Boson Higgs Audio 2 Community License）、**ebook2audiobook**
> （默认引擎 XTTS = Coqui CPML、MMS 路径 = CC-BY-NC-4.0）。
> **GPT-SoVITS 留作回退**（中文听测不合格时按任务书改用它）。

### 「有没有能替代 V18 手写镜头切分的项目」——当前判断：没有

> ⚠️ **这是「当前判断」，不是定论。**
> 按[《Harness 工作规则》的定论机制](harness-rules.md)，写成定论必须附
> **可观察的重新评估触发条件**；本条目前**尚无满足条件**，故降级为"当前判断"。

只看两个方向：

1. **算法库**（PySceneDetect 是代表）：**已判过**（见 [PySceneDetect](#pyscenedetect)）——
   它是一整套含 OpenCV 的重依赖，与"内核零依赖、插件尽量零依赖"冲突，**从零自写**。
   XBC 现在用的是 **FFmpeg 原生 `select='gt(scene,T)',showinfo`** 解析 `pts_time`，
   零额外依赖，已交付并被 TASK-009/011 实际使用。
2. **端到端工具**（video-recap-skills / VideoHighlighter）：它们**没有比我们更强的切分算法**，
   一个用 ffmpeg、一个是 AGPL，**都不构成替代方案**。

**当前判断：V18 的镜头切分暂时不需要换实现** —— 现有 FFmpeg 方案在"零依赖"这条约束下是最优解。

#### 重新评估触发条件（满足任意一条即重开）

| # | 触发条件 | 为什么是它 |
|---|---|---|
| 1 | **"内核零依赖 / 插件尽量零依赖"这条约束被放宽**（允许引入 OpenCV 级依赖） | 现有结论的大前提就是这条约束；前提没了，结论要重算 |
| 2 | **FFmpeg `select='gt(scene,T)'` 在真实素材上出现可复现的漏切 / 错切**（附具体样本与数据） | 这是唯一能证明"现有方案不够用"的实测证据 |
| 3 | 出现**零依赖（纯标准库）**且切分质量**明显优于**现有方案的可引入实现 | 直接推翻"零依赖下没有更好的"这个前提 |

> 反例（**不构成**触发条件）：star 变多、出了新版本、README 写得更好、
> 别人的项目也在用。这些不改变我们的任何前提。


---

## 逐项目说明

### pluggy

- **仓库**：`pytest-dev/pluggy`（本地副本 `F:\Downloads\xbc-refs\pluggy`）
- **许可证**：MIT（已读 `LICENSE` 原文）
- **使用方式**：**参考** —— 只借鉴 `hookspec` / `hookimpl` 的机制设计
- **冲突**：pluggy 还提供 `wrapper` / `hookwrapper` / `historic` / entrypoints 加载等大量机制，
  夏半仓用不上。**只取了真正需要的一小部分**（`tryfirst` / `trylast` / `optionalhook` / `check_pending`），
  实现全部自写，内核不依赖 pluggy。
- **代码**：`src/xbc/core/contract/hookspec.py`

### PySceneDetect

- **仓库**：`Breakthrough/PySceneDetect`（本地副本 `F:\Downloads\xbc-refs\PySceneDetect`）
- **许可证**：BSD-3-Clause（已读 `LICENSE` 原文）
- **使用方式**：**从零** —— 确认不采用
- **冲突（明确）**：它是重量级库（含 OpenCV 等依赖），与夏半仓"**内核零依赖、插件尽量零依赖**"
  的硬约束冲突。**没有强行整合**。
- **替代实现**：`plugins/video_analyzer/plugin.py` 用 FFmpeg 原生的
  `select='gt(scene,T)',showinfo` 解析 `pts_time`，零额外依赖。

### ollama-python

- **仓库**：`ollama/ollama-python`（本地副本 `F:\Downloads\xbc-refs\ollama-python`）
- **许可证**：MIT（已读 `LICENSE` 原文）
- **使用方式**：**从零** —— 确认不采用
- **冲突（明确）**：它把"调模型"封装成一个**插件可以直接 import 的库**，
  与"**插件禁止直接调用模型 SDK，只能经 Core Capability**"这条硬约束**方向相反**。
  一旦作为依赖引入，就等于给插件开了一条绕过能力层的路。
- **替代实现**：`src/xbc/core/capabilities/ai/providers/ollama.py` 用标准库 `urllib` 直连 HTTP。

### PyQt-Fluent-Widgets

- **仓库**：`zhiyiYo/PyQt-Fluent-Widgets`（本地副本 `F:\Downloads\xbc-refs\Fluent-Widgets`，**克隆未完成，仅 `.git`**）
- **许可证**：**GPL-3.0**（GitHub API `license.spdx_id = "GPL-3.0"`）
- **使用方式**：**参考** —— 只可看设计思路
- **冲突（明确，且是最硬的一条）**：
  1. **GPL-3.0 是强 copyleft**：一旦采用（哪怕重新封装），整个衍生作品都要以 GPL-3.0 发布 ——
     **与商业产品直接冲突**。作者另有**付费**商业授权（qfluentwidgets.com）。
  2. 它主打"重美化"，与夏半仓当前"**只做管理、不做美化**"的取向相反。
- **⚠️ 给未来 UI 任务的提醒**：做界面二期时**不要**顺手 `pip install PyQt-Fluent-Widgets`。
  要美化就自己写样式，或购买商业授权后再评估。

### Dify

- **仓库**：`langgenius/dify`
- **许可证**：**NOASSERTION**（GitHub 无法归类为非标准许可）→ **采用前必须逐条读 `LICENSE`**
- **使用方式**：**参考** —— 只读官方文档与公开的 manifest 字段说明，**未取任何代码**
- **冲突（明确）**：Dify 是"前后端一体的工作流 / RAG 平台"，内含**账号、租户、云部署**。
  夏半仓是**单机内核 + 插件**。强行整合会把平台复杂度整个引进内核，
  与"Core 保持最小化"冲突。**只参考它的插件 manifest 与权限声明思路。**

### uTools

- **来源**：官方开发者文档 + `utools-api-types` 的 `utools.schema.json`
- **许可证**：**闭源商业软件**（无可采用的代码；只读了公开文档与官方类型定义）
- **使用方式**：**参考**
- **冲突（明确）**：宿主是 Electron（Node/Web），插件是 HTML/JS + preload；
  夏半仓是 Python 桌面。其 **preload 全权模型**（安装即全权）与我们
  "**能力白名单 + 内核强制风险**"的模型**相反**。**未强行整合。**
- **报告**：[utools-plugin-mechanism.md](utools-plugin-mechanism.md)

### MCP

- **来源**：官方规范原文与权威 schema（`schema/2026-07-28/schema.ts`）
- **许可证**：**NOASSERTION**（非标准许可）；我们**只读公开规范文本**，未取代码
- **使用方式**：**参考**
- **冲突（明确）**：MCP 是**客户端-服务端进程协议**（宿主拉起 stdio 子进程、`server/discover` 握手）；
  夏半仓是**同进程插件 + 作用域隔离**。两者的生命周期与信任模型不同，**未强行整合。**
  只参考了工具的 `inputSchema` 形态与 `elicitation` 的"向用户补信息"交互。
- **报告**：[mcp-spec-facts.md](mcp-spec-facts.md)

### DeepSeek Harness

- **来源**：其发行包 `app.asar` 内自带的插件开发指南
- **许可证**：**未公开**（私有发行包）→ **不得复制其中任何代码**
- **使用方式**：**参考** —— 只读了它的插件开发说明，用于理解分层
- **冲突（明确）**：它的内核建在 **Cordis DI 框架**上，能力通过依赖注入声明；
  夏半仓选择了更轻的**显式契约**（清单 + Context 门面 + 能力白名单）。
  **只借鉴了 Skill 分层与"能力声明"的思路，没有搬 DI 容器。**

---

## 新调研怎么登记

新增一项调研时，在"逐项目登记表"里加一行，并补一节说明，必须包含：

1. 仓库 / 来源
2. **许可证**（读过原文或经权威来源确认，不能猜）
3. 是否允许商业使用
4. 使用方式（采用 / 参考 / 从零）
5. **架构冲突**（没有就写"无"）
6. 若"采用"：说明重新封装到了哪个插件的哪个文件

**没有这六项，调研不算完成。**

### 第 5 栏（架构冲突）的判定补充：库的类型决定能不能 import

TASK-010 裁决（见上文《TASK-010 架构裁决》、
[harness-rules.md](../harness-rules.md)）之后，
"架构冲突"栏在**涉及"能不能直接 import 某个库"**时，按库的类型判定：

| 库的类型 | 业务代码 | Provider 文件 | 登记时怎么写 |
|---|---|---|---|
| **远端模型 SDK**（`openai`/`ollama`/`anthropic`/`litellm`） | ❌ 禁止 | ❌ 禁止 | 架构冲突栏写"**有**：与 AI 能力隔离冲突" |
| **HTTP 客户端**（`requests`/`httpx`/`aiohttp`） | ❌ 禁止 | ⚠️ 仅当该 Provider 确实连远端服务 | 写清"用在哪一层" |
| **本地推理运行时**（`onnxruntime` 等） | ❌ 禁止 | ✅ **允许** | 架构冲突栏写"**无**"（属允许项），但**必须写明放在哪个 Provider 文件** |
| **通用计算库**（`numpy`/`pillow`） | ⚠️ 视用途 | ✅ 允许 | 按实际情况写 |

**判据是"是否绕过能力层去够远端模型"，不是"有没有出现某个词"。**

登记"采用"本地推理运行时的时候，**第 6 栏要写全三件事**（缺一不算登记）：

1. 哪个 Provider 文件用它
2. 业务代码如何经 `ctx.ai.*` 使用该 Provider（**不得直接调用**）
3. 模型 / 运行时的**分发方式**（是否随插件包分发、放哪、缺失时怎么办）

第 3 条见 TASK-010 的《模型分发策略》：模型是 **Core 级共享资源**
（`AppPaths.models_dir`），**不随 `.xbcplugin` 包分发**，插件不声明"需要模型文件"。
