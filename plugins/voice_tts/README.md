# 语音合成与声音克隆（voice_tts）

把**本地 TTS 引擎**注册成 Core 的 `speech` 能力，并提供**声音档案**管理。

---

## 一、它提供什么

| 件 | 说明 |
|---|---|
| **一个 Core AI Provider** | 把本地引擎接进 `ctx.ai`。业务代码**永远不直接调它** |
| **声音档案** | "名称 ↔ 参考语音"的档案，存**插件自己的数据目录**，Core 不知道 |

### 工具

| 工具 | 干什么 | 风险 |
|---|---|---|
| `voice_clone` | 用一段参考语音登记一个声音（**零样本**，不训练模型） | write |
| `voice_list` | 列出某个 owner 的声音与剩余名额 | read |
| `voice_remove` | 删一个声音的档案（**不动参考语音文件**） | write |
| `voice_speak` | 用已登记的声音把文本合成成语音文件 | write |

---

## 二、引擎按配置选，加引擎不用改 plugin.py

```json
{ "plugins": { "voice_tts": { "config": {
    "engine": "moss_tts",
    "max_voices_per_owner": 3
} } } }
```

加载约定**只有一条**：`providers/<引擎名>.py` 里要有 `create_provider()` 工厂。

> **新增一个引擎 = 加一个文件 + 改一行配置**，`plugin.py` 一个字节都不用动。

### 目前有哪个引擎

| 引擎名 | 实现 | 说明 |
|---|---|---|
| `moss_tts` | [`providers/moss_tts.py`](providers/moss_tts.py) | MOSS-TTS-Nano 官方 ONNX 版，**路线 D**（不装 PyTorch） |

---

## 三、依赖

### 运行时（要装在跑插件的那个 Python 环境里）

| 包 | 用途 | 体积 |
|---|---|---|
| `onnxruntime` | 跑 ONNX 图 | ~44 MB |
| `numpy` | 数组 | ~31 MB |
| `sentencepiece` | 文本分词 | ~2.5 MB |
| `soundfile` | 读参考语音 | ~0.05 MB |
| `soxr` | 重采样到 48 kHz | ~0.17 MB |
| **上游包** `moss-tts-nano` | ONNX 推理编排 | `--no-deps` 装 |

```powershell
# 1) 推理依赖（不含 PyTorch）
pip install onnxruntime numpy sentencepiece soundfile soxr

# 2) 上游包 —— 必须 --no-deps，否则会把 PyTorch 拖进来
#    钉住我们记录的那个提交（见 providers/moss_tts.py 顶部）
pip install --no-deps "moss-tts-nano @ git+https://github.com/OpenMOSS/MOSS-TTS-Nano@8b7bcc9341b3b4ef3a3a58ba1338a7d85ff133eb"
```

**实测运行环境增量 ≈ 7 MB**（对比：装 PyTorch 要 +1188 MB）。

### 模型（不随插件分发）

```powershell
python scripts/export_moss_tts_onnx.py
```

放到 **Core 的共享模型目录**：`<数据根>/models/moss-tts-nano/`（约 728 MB）。
**插件配置里不写模型路径** —— 路径由 Core 通过 `bind_models` 注入。

---

## 四、这个插件里哪些文件算"模型实现"

依据 [《AI 能力隔离》](../../docs/harness-rules.md)（TASK-010 §7 细化）：

| 文件 | 身份 | 允许 import 本地推理运行时？ |
|---|---|---|
| `providers/*.py` | **模型实现** | ✅ 是 |
| `plugin.py` | **模型使用者** | ❌ 否，只能走 `ctx.ai.*` |

`tests/test_speech_capability.py::PluginPurityTests` 用 `ast` 扫描守着这条边界。

---

## 五、两个实测到的坑（都在 `providers/moss_tts.py` 里处理了）

| 坑 | 现象 | 处理 |
|---|---|---|
| **上游 ONNX 版仍要 PyTorch** | README 写 "No PyTorch dependency"，但代码顶层 `import torch`；不装就 `ModuleNotFoundError` | **路线 D**：导入那一刻给个空壳 torch，导入完撤掉；只覆写 `_load_reference_audio` 一个私有方法 |
| **sentencepiece 打不开含中文的路径** | `C:\...\夏半仓工具箱\...\tokenizer.model` ❌ `NOT_FOUND Error #2`；纯 ASCII 路径 ✅（而默认数据根就含中文） | 把 tokenizer 暂存到 ASCII 目录再加载（`onnxruntime` 不受影响，实测确认） |

完整决定、依赖的上游私有 API、失效触发条件与回退路径：
[`docs/research/README.md`](../../docs/research/README.md) 的
**《架构决定：语音合成走「路线 D」》**。

---

## 六、自检

```powershell
# 只走 ctx.ai，不接触任何模型实现
python run.py tool call voice_selftest --kwargs "{\"reference_audio\": \"<参考语音路径>\"}"
```

或直接调插件工具（需要授权 `write`）：

```powershell
python run.py tool call voice_clone --yes --kwargs "{...}"
```
