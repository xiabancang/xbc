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

| 项目 | 许可证 | 可商业使用 | 在夏半仓的使用方式 | 架构冲突 |
|---|---|---|---|---|
| [pluggy](#pluggy) | MIT | ✅ | **参考**（钩子机制设计；实现自写） | 部分：其大量机制我们不需要 |
| [PySceneDetect](#pyscenedetect) | BSD-3-Clause | ✅ | **从零**（改用 FFmpeg 原生滤镜） | **有**：引入依赖与"内核零依赖"冲突 |
| [ollama-python](#ollama-python) | MIT | ✅ | **从零**（自写 HTTP 客户端） | **有**：封装模型调用会诱导插件绕过 Capability |
| [PyQt-Fluent-Widgets](#pyqt-fluent-widgets) | **GPL-3.0** | ❌ | **参考**（仅看设计；界面用原生 Qt） | **有**：GPL 传染 + 与"不做 UI 美化"取向相反 |
| [Dify](#dify) | NOASSERTION（非标准） | ⚠️ 采用前须逐条读 LICENSE | **参考**（只看公开架构描述，未取代码） | **有**：平台型架构与单机内核冲突 |
| [uTools](#utools) | 闭源商业软件 | — | **参考**（只读公开开发者文档） | **有**：Electron 宿主模型与我们不同 |
| [MCP](#mcp) | NOASSERTION（非标准） | ⚠️ 只读公开规范 | **参考**（只读公开规范文本） | **有**：进程协议 vs 同进程插件 |
| [DeepSeek Harness](#deepseek-harness) | 未公开（私有发行包） | — | **参考**（只读其插件开发说明） | **有**：Cordis DI 框架 vs 显式契约 |

> **"可商业使用"栏的判定依据**：MIT / BSD-3-Clause 允许商业使用；
> **GPL-3.0 不允许**（采用即要求衍生作品整体 GPL）；
> `NOASSERTION` 表示 GitHub 无法归类，**必须逐条读 LICENSE 才能判定**。

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
