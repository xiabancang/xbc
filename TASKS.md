# 任务台账（TASKS）

> 规则：每个任务有明确范围、验收标准和**可复现的验证证据**。
> 状态：`TODO` / `DOING` / `DONE` / `BLOCKED`
>
> 一次只做被点名的任务，不顺手扩大范围。

---

## 常驻规则：调研结果的使用

调研找到的项目，**禁止直接复制代码进夏半仓**。使用方式仅限三种：

| 方式 | 含义 | 附加要求 |
|---|---|---|
| **采用** | 完整集成 | 必须按夏半仓插件规范**重新封装**；许可证必须允许商业使用 |
| **参考** | 只参考设计思路或算法 | **代码自己写**，不搬运 |
| **从零** | 调研后确认没有合适的 | 自己实现，并记录"为什么现有方案不合适" |

无论哪种，都必须：

1. **检查许可证是否允许商业使用**（不能猜，要读原文或经权威来源确认）
2. 封装为符合夏半仓规范的插件（`plugin.json`、工具注册）
3. 插件通过 **Core Capability** 调 AI，**不直接调模型**
4. **不破坏已有 Core 的隔离规则**（能力白名单、作用域释放、零第三方依赖）

**调研项目的架构与夏半仓冲突时，必须在调研报告中明确指出，不要强行整合。**

📋 逐项目的许可证 / 使用方式 / 架构冲突登记表：[docs/research/README.md](docs/research/README.md)

> **已记录的一条硬约束**：`PyQt-Fluent-Widgets` 是 **GPL-3.0**，
> **采用即传染**（整个衍生作品被迫 GPL），与商业产品冲突 —— **不得采用**。
> 做界面时不要顺手 `pip install PyQt-Fluent-Widgets`。

---

## TASK-001　恢复开发环境 —— DONE

**范围**：只恢复开发环境，不做任何功能开发。不得修改 V18 参考基线。

### 验收标准与结果

| 验收项 | 结果 | 证据 |
|---|---|---|
| 工具链可用 | ✅ | Python 3.13.15 / PySide6 6.11.2 / git 2.55.0 / FFmpeg 9.0.1 / Ollama 0.35.1 |
| 环境状态可观测 | ✅ | `python run.py env` → `"status": "ok"`（三级检查，不可用时退出码 1） |
| 依赖可复现 | ✅ | 隔离环境 `.venv`（`pip install -e ".[gui]"`，`pip check` 干净） |
| 隔离环境真正可用 | ✅ | 修复完整性标签后，venv 内 23 项测试 / `env` / `smoke` 全部通过 |
| 测试不污染环境 | ✅ | 修复日志句柄泄漏后，测试前后 `%TEMP%` 与仓库内遗留均为 0 |
| 全部测试通过 | ✅ | `python -m unittest discover -s tests` → 23 tests OK（系统 Python 与 venv 均通过） |
| V18 未被修改 | ✅ | 其 `LastWriteTime` 仍为 2026/9/22 23:38:20（今日 2026/10/04） |

### 交付物

- `src/xbc/core/diagnostics.py`　环境自检（required / recommended / optional 三级）
- `src/xbc/__main__.py`　`env` 子命令改为返回真实健康状态（不可用时退出码 1）
- `src/xbc/core/logging_setup.py`　新增 `shutdown_logging()`
- `src/xbc/core/context.py`　新增 `AppContext.close()`，释放日志文件句柄
- `tests/test_diagnostics.py`　诊断逻辑测试
- `.venv/`　隔离环境（已被 `.gitignore` 忽略）
- `README.md` / `TASKS.md`　环境说明与本台账

---

## 关键调查记录

### 1. 受限沙箱下命令行崩溃（平台层，未解决）

**现象**：会话处于受限文件沙箱时，**所有**命令行调用以 `0xC0000142`
（STATUS_DLL_INIT_FAILED）崩溃，连 `Write-Output "probe"` 都无法执行；
切到完全访问模式后一切正常。

**假设一：工作区权限缺失 —— 已证伪。**
用平台自带的沙箱权限诊断工具对 `F:\Downloads\XBC` 做一次性诊断+修复：

```
VERDICT=NOT_THIS_CLASS
writeDac=True   writeOwner=True   packageObjects=[]
FIXED=0  GRANTED=0  REFUSED=0  changes=[]
```

所需权限都在，无异常授权项，**未做任何修改**（`nextAction=stop`）。
证据：`F:\Downloads\XBC-acl-recovery\acl-report-09c9847a9b774753916f9bf36767366d.jsonl`

处置：属沙箱**进程启动**环节，超出该工具可判定范围。完全访问模式下开发可正常进行。

### 2. 工作区完整性标签导致新文件以 Low 完整性运行（已修复）

**现象**：`.venv` 内的 Python 报 `PermissionError [WinError 5]`，
无法在 `%TEMP%` 建目录；而系统 Python 同一操作正常。

**根因（实测）**：

| 对象 | 完整性标签 |
|---|---|
| `F:\Downloads\XBC` | `Low Mandatory Level:(OI)(CI)(NW)`　← 沙箱写入 |
| `F:\Downloads\XBC\run.py` | `Low Mandatory Level:(I)(NW)`　← 继承 |
| `F:\Downloads\XBC\.venv\Scripts\python.exe` | `Low Mandatory Level:(I)(NW)`　← 继承 |
| 同名副本（复制到工作区外） | 无标签 |

进程实测：venv Python = `S-1-16-4096 (Low)`，系统 Python = `S-1-16-12288 (High)`。
Low 完整性进程被拒写 `%TEMP%`、用户目录、`C:\Windows\Temp`，
于是 `tempfile.gettempdir()` 静默退化为当前目录（`F:\Downloads\XBC`），
pip 与构建工具随机失败。

**修复**：只把可丢弃的 `.venv` 目录树标签重置为 Medium，
**保留工作区根目录的标签**（受限模式依赖它）：

```powershell
icacls .venv /setintegritylevel '(OI)(CI)M' /T /C
```

修复后 venv Python = `S-1-16-8192 (Medium)`，四处写入探测全部通过。
回滚方式：删除 `.venv` 重建即可，无残留副作用。

### 3. 日志文件句柄泄漏导致临时目录无法删除（已修复）

**现象**：测试跑完 `%TEMP%` 里堆积 78 个 `xbc-*` 目录，
仓库内也残留 18 个（当时 `tempfile` 退化到当前目录所致）。

**根因**：`RotatingFileHandler` 持有 `logs/xbc.log` 句柄，
`AppContext` 从不释放；Windows 上句柄未关闭则目录删不掉，
而清理用的是 `ignore_errors=True`，于是**静默失败**。

**修复**：新增 `shutdown_logging()` 与 `AppContext.close()`，测试在清理阶段显式调用。
修复后重跑测试：`%TEMP%` 与仓库内遗留均为 **0**。历史遗留目录已全部清除。

---

## TASK-002　设计夏半仓 Plugin Runtime V1 —— DONE

**范围**：只做架构设计，不写业务代码；不设计商城 / 支付 / 云端 / 用户系统 / 多租户。

**交付物**：[docs/plugin-runtime-v1.md](docs/plugin-runtime-v1.md) ——《夏半仓 Plugin Runtime 技术方案 V1》

**验收对照**：

| 要求 | 章节 | 状态 |
|---|---|---|
| Core 职责定义（做什么 / 禁止做什么） | 第 2 章（12 项职责 / 10 项禁止 / 3 条边界判定规则） | ✅ |
| Plugin 接口设计（生命周期 / 注册 / 配置 / 通信） | 第 3 章（含 3.10 安全底线） | ✅ |
| Skill / Agent / Plugin 关系 | 第 4 章（四层模型 + 调用链 + 调用策略矩阵 + 边界纪律） | ✅ |
| 目录结构设计 | 第 5 章（源码 / 运行期数据 / 插件标准结构） | ✅ |
| 第一阶段 MVP 范围 | 第 6 章（10 项必做 / 7 项不做 / 10 条可执行验收） | ✅ |
| 研究 5 个参考 | 第 1 章 + 附录 A | ✅ |
| 不设计商城 / 支付 / 云端 / 用户系统 / 多租户 | 2.2 N6/N8 + 6.3 | ✅ |

**参考调研的原始材料**：

- **DeepSeek Harness**：直接从发行版 `app.asar` 中抽出其自带的插件开发指南（`cordis-plugin-development` 技能 + 4 份 references、`cordis-composition-reference` 技能、`dsh-skill` / `dsh-tool-skill` 包文档）。**这是最深的一份参考** —— DSH 内核基于 Cordis 依赖注入插件框架。
- **pluggy**：读源码（`_manager.py` / `_hooks.py` / `_execution.py` / `_decorators.py`），提炼 hookspec/hookimpl 契约分离、注册时签名校验、`optionalhook`、`set_blocked`。
- **uTools**：委派子代理调研官方开发者文档与官方 JSONSchema，报告见 [docs/research/utools-plugin-mechanism.md](docs/research/utools-plugin-mechanism.md)。
- **Dify Plugin**：委派子代理核对官方文档与 GitHub 源码（manifest 字段、daemon 架构、协议、凭证链路）。
- **MCP**：委派子代理抓取官方规范原文（覆盖 `2024-11-05` 至当前 `2026-07-28`，含两份 schema.ts 与 SEP-1024）。报告：[docs/research/mcp-spec-facts.md](docs/research/mcp-spec-facts.md)；原始材料在 `mcp-research/`（未入库）。

**调研纠正了 4 处原有判断**（已写入方案）：

1. uTools **并非"能力对用户可见"** —— 它没有任何声明式权限清单，是"安装即全权"。原判断有误，已改为反面教材。
2. Dify 的 daemon **不支持 Windows**（其 README 明确）—— 这直接印证我们不该照搬进程外架构。
3. Dify 的凭证是**加密存储但明文下发**给插件 —— 这被记为"可做得更好的地方"，而非可学范式。
4. MCP **当前规范版本是 `2026-07-28`，不是 `2025-06-18`**：该版本**删除了 `initialize` 握手、session 与 `ping`**，改为完全无状态。我最初按旧版写法描述"握手协商能力"，已更正。同时 `annotations` 的语义被我写反了 —— 它只是**提示**，规范明确要求**不可信来源的 annotations 不得作为决策依据**。这条推论已写进安全底线：**插件自报的风险等级同样不可信，约束必须由内核在执行点强制**。

---

## TASK-003　实现 Plugin Runtime V1 MVP —— DONE

**范围**：按方案实现 MVP。**不做 UI、不做 AI 视频、不做商城、不做云端**（用户明确约束）。

**交付物**：
- 代码：`src/xbc/core/` 共 3402 行（契约层 / 运行时 / 能力层 / 工具 / 技能 / 配置 / 上下文 / 诊断 / CLI）
- 插件：`hello_xbc`（机制验证）、`text_toolbox`（第一个真实业务插件，纯本地文本处理）
- 报告：[docs/plugin-runtime-v1-test-report.md](docs/plugin-runtime-v1-test-report.md)

**验收对照**：

| 验收标准 | 结果 |
|---|---|
| A1 未激活即可列出插件、不导入代码 | ✅ |
| A2 禁用持久化（跨进程） | ✅ |
| A3 三层配置装配与来源上报 | ✅ `config_source=host` |
| A4 单插件失败被隔离 | ✅ |
| A5 停用后注册无残留 | ✅ `tools=0 skills=0` |
| A6 缺依赖保持 INACTIVE 而非报错 | ✅ |
| A7 技能目录/按需加载 + 工具调用 | ✅ |
| A8 参数校验返回可读错误 | ✅ `invalid_arguments` |
| A9 全部测试通过 | ✅ **123 项 OK**（系统 Python 与 venv 均通过） |
| A10 `doctor` 无 error | ✅ `problems=0` |
| 安全底线 S1 能力白名单 | ✅ |
| 安全底线 S2 内核侧风险强制 | ✅ `consent_denied` / `--yes` 授权 |

**7 处与方案的偏差**已在测试报告第 2 章逐条声明（其中 D1 不做 UI、D2 不做 AI 视频 为用户指定；
D3 配置用 JSON 而非 YAML 是为守住"内核零第三方依赖"）。

**开发中发现并修复 7 个缺陷**（3 个由测试抓到），详见测试报告第 5 章。

---

## TASK-004　Desktop Shell MVP —— DONE

**范围**：只做桌面管理入口。**禁止**登录 / 云端 / 商城 / 支付 / **UI 美化** / 业务插件。

**必须实现 7 项 —— 全部达成**：

| # | 要求 | 实现 |
|---|---|---|
| 1 | PySide6 窗口 | `src/xbc/ui/shell.py`，`python run.py ui` 启动 |
| 2 | 显示插件列表 | 左栏列表，插件名 + id，按状态排序 |
| 3 | 显示插件状态 | 每项显示 `state` + 启用/已禁用；失败显示错误原因 |
| 4 | 启用插件 | 「启用」按钮 → `PluginManager.enable()` |
| 5 | 停用插件 | 「停用」按钮 → `PluginManager.disable()` |
| 6 | 查看 Tool 列表 | 右上列表：名称 / 风险等级 / 归属插件 / 描述，标题带总数 |
| 7 | 查看 Skill 列表 | 右下列表：名称 / model·user 调用策略 / 描述，标题带总数 |

**验收对照**：

| 验收标准 | 结果 | 证据 |
|---|---|---|
| **CLI 已有功能不能退化** | ✅ | 135 项测试全通过；`smoke` / `doctor` / `plugin list` / `tool list` / `skill list` / `tool call` 全部正常（实测输出见提交说明） |
| **桌面操作结果必须与 Runtime 状态一致** | ✅ | 13 项 Shell 测试；测法是**拿 Runtime 真实状态反向校验界面内容**，而非硬编码期望 |

**一致性是怎么保证的（结构性，而非靠自觉）**：

- 界面**不缓存任何状态**，每次刷新都重新从 Runtime 读取（`manager.records()` / `tool_registry.for_agent()` / `skill_catalog.specs()`）；
- 启用/停用调用的是**与 CLI 完全相同的方法**（`PluginManager.enable/disable`），不重写业务逻辑；
- 关键测试：**停用插件后工具列表必须清空**（验证"注册即资源"在界面侧同样成立）、**另一个 Runtime 实例观察到相同状态与工具集**（验证桌面与 CLI 等价）。

**UI 美化**：零。没有 `setStyleSheet`、没有自定义字体/图标/自绘控件，全部 Qt 默认外观。

---

## TASK-005　第一个真实业务插件 video_analyzer —— DONE

**目的**：验证 Plugin Runtime 能否承载**复杂业务能力** —— 一个工具内部要跑几十次
FFmpeg 子进程、产出多个文件、返回嵌套结构化结果，并且停用时干净释放。

**范围（实现 6 项，全部达成）**：

| # | 要求 | 实现 |
|---|---|---|
| 1 | video_analyzer 插件 | `plugins/video_analyzer/`（清单 + 入口） |
| 2 | 使用 FFmpeg 获取视频信息 | `video_probe` → ffprobe JSON（新增内核能力 `FFmpegService.probe_media`） |
| 3 | 镜头切分 | `video_shots` → `select='gt(scene,T)',showinfo` + 解析 `pts_time` |
| 4 | 抽取关键帧 | `video_extract_keyframes` → `-ss` 精确定位逐镜头抽帧 |
| 5 | 注册 Tool | 4 个：`video_probe` / `video_split_shots` / `video_extract_keyframes` / `video_analyze` |
| 6 | 返回结构化 JSON | 每个工具都声明 `output_schema`，输出受内核校验 |

**禁止（6 项全部未做）**：AI 生成视频、Ollama、云服务、自动发布、UI 扩展、商业功能。
插件只用 `files` / `ffmpeg` / `settings` 三个能力，**零第三方依赖**（有测试断言）。

**验收对照**：

| 验收标准 | 结果 | 证据 |
|---|---|---|
| 1. 插件独立安装 | ✅ | 复制到用户插件目录、**把内置目录指向空目录**后仍能发现并运行；测试 `test_standalone_install_into_user_plugin_dir` |
| 2. Runtime 发现插件 | ✅ | `test_runtime_discovers_plugin`；未激活时不导入代码 |
| 3. Tool 可调用 | ✅ | 4 个工具全部调用成功（CLI 与 Python API 双路径） |
| 4. 输出结果正确 | ✅ | 用**带 2 个硬切的合成视频**断言：时长 6.0s、切点 2.0/4.0、3 个镜头、关键帧文件真实存在且是 JPEG |
| 5. 禁用插件无残留 | ✅ | 停用后工具/技能/事件订阅全部为 0，作用域 `effect_count == 0` |
| 6. 全量测试通过 | ✅ | **153 项**（新增 18 项），系统 Python 与 venv 双环境通过 |

**测试视频**：运行时用 FFmpeg lavfi 合成（`testsrc` + `smptebars` + `testsrc2` 各 2 秒拼接），
**不往仓库里塞二进制**；无 FFmpeg 时整体跳过。

**开发中发现并修复 1 个真 bug**：重新激活插件时 `ctx.config` 与 `record.config_source`
仍是上一次的旧值 —— 用户改了配置、重新启用却不生效。已修（激活时重新读取配置并同步
`ctx.config`），并由 `test_threshold_from_config_changes_result` 锁定：
把阈值调到 0.95 后切分结果从 3 个镜头变成 1 个，证明配置真的能影响业务结果。

**顺带修正 5 条脆弱测试**：旧测试硬编码了"2 个插件 / 7 个工具"，新插件一加就假失败。
已改为断言**关系**（发现结果 == 内置目录实际内容、注册数 == 清单声明数、停用后归零），
这样以后加插件不会再触发假失败。

---

## TASK-006　插件产品化基础：安装 / 版本 / 数据隔离 —— DONE

**范围**：只做插件安装、版本、用户数据隔离。**禁止** AI、商城、支付、云端、用户系统、UI 美化。

**实现 6 项（全部达成）**：

| # | 要求 | 实现 |
|---|---|---|
| 1 | 插件包格式设计 | `.xbcplugin`（ZIP）；**根目录一个 `plugin.json`**，不引入新清单概念；含 zip-slip / 符号链接 / zip bomb 防护 |
| 2 | 插件安装 | `PluginInstaller.install()` + `xbc plugin install <包>`；先解到 `.staging-*`、校验后再**原子替换** |
| 3 | 插件卸载 | `uninstall()` + `xbc plugin uninstall <id>`；默认**保留用户数据**，`--purge` 才清除 |
| 4 | 插件升级 | `upgrade()` + `xbc plugin upgrade <包>`；默认拒绝降级与同版本，`--force` 可覆盖 |
| 5 | 插件版本管理 | `PluginVersion`（自研 SemVer 子集，零依赖）+ `installed.json` 安装台账（来源、时间、升级历史） |
| 6 | 用户数据目录分离 | 卸载删 `plugins/<id>` 与 `cache/<id>`，**保留 `data/<id>` 与配置**；升级只替换代码目录 |

**验收对照**：

| 验收标准 | 结果 | 证据 |
|---|---|---|
| video_analyzer **安装 → 运行 → 升级 → 卸载** | ✅ | 端到端演示脚本逐步骤输出（见下） |
| **Runtime 不修改核心代码** | ✅ | 全程结束后 `src/xbc/` 下 **38 个 .py 文件哈希完全一致**；测试 `test_full_flow_does_not_modify_core_sources` 与 `test_runtime_discovers_plugins_without_core_changes` |

**端到端验收结果**（实测输出）：

```
1) 打包      video_analyzer-0.1.0.xbcplugin (6664 字节)
2) 安装      ok=true  action=install  version=0.1.0
3) 运行      发现 3 个插件，4 个工具；切分得到 2 个镜头
4) 用户数据  data/video_analyzer/user_notes.txt；插件配置 runs=42
5) 打包 0.2.0（版本 +1，新增一个工具）—— 只改插件，不改内核
6) 升级      同版本被拒；0.1.0 → 0.2.0 成功
7) 升级后    版本=0.2.0  工具含 video_probe_v2  数据仍在  配置 runs=42  新工具可调用
8) 卸载      插件目录已删，用户数据保留
9) 重装+purge 数据未丢；purge 后数据与配置清除
10) 台账     installed.json（版本 / 来源 / 时间 / 升级历史）
11) 核心源码 38 个 .py 文件哈希无变化 ✅
```

**开发中发现并修复 3 个真缺陷**：

1. **升级"成功"但 Runtime 仍加载旧版本** —— `discover()` 先扫内置目录且先到先得，
   内置版把用户安装的升级版挡住了。方案 5.4 明确要求"重名时用户获胜"，
   已改为**用户目录优先级高于内置目录**（同目录内两条同名 id 才是真冲突）。
   这条不修，TASK-006 的验收根本过不了。
2. **`--purge` 没清干净** —— 只删了 `config/plugins.json` 的行，
   漏了 `config.json` 里插件的运行期设置；且直接改文件会让宿主内存副本变陈旧。
   改为通过 `Config.remove_plugin_section()` 清理并触发重新合并。
3. **缺少入口文件的包能装上** —— 装上去只会在加载时失败，用户会以为工具箱坏了。
   现在 `build` 与 `install` 都会在**安装前**校验清单声明的入口文件确实存在。

**测试**：157 → **203 项**（新增 46 项：版本比较、包格式与安全、安装/升级/卸载、数据隔离、核心不变）。

---

## TASK-007　AI Capability Layer V1 —— DONE

**目的**：让所有插件通过统一接口调用 AI 能力。**要求**：AI 属于 Core Capability，
**业务插件不得直接调用模型**。

**设计原则落实**：

| 原则 | 落实方式 |
|---|---|
| AI 属于 Core Capability | 位于 `src/xbc/core/capabilities/ai/`，通过 `ctx.ai` 暴露 |
| Plugin 禁止直接调用模型 SDK / HTTP 客户端 | 字面 grep + `ast` 双重扫描测试强制，不是文档约定 |
| Provider 可替换 | `ModelProvider` 抽象 + 配置驱动装配；换 Provider 只改配置 |
| 本地模型优先 | `ai.provider` 默认 `ollama` |
| 不绑定单一厂商 | 请求/响应结构中立；`openai_compatible` 是通道，覆盖 DeepSeek/OpenAI/Claude/自建网关 |
| Core 保持最小化，不做预留扩展点 | **删除了 Provider 注册表**；无按调用可选参数、无旧版兼容方法；零第三方依赖（有测试断言） |
| 不引入业务语义 | 18 条业务词表 + 词边界规则扫描 `capabilities/ai/` |

**实现范围（全部达成）**：

| # | 要求 | 实现 |
|---|---|---|
| 1 | AI Capability 接口 | `AIService` + `ModelProvider` + 三个能力各自的 Request / Response 结构 |
| 2 | Provider 机制 | `providers/ollama.py`（第一实现，真实可用）、`providers/openai_compatible.py`（第二实现，验证可替换性） |
| 3 | 第一批能力（仅三个） | `text_generate` / `vision_analyze` / `embedding` |
| 4 | 配置系统 | `ai.provider` / `ai.model` / `ai.options`；密钥走 `secrets.json` |
| 5 | 测试插件 | `plugins/ai_test_plugin/`（单工具 `ai_selftest`，依次调用三个能力） |

**禁止**：Agent 系统、自动决策、工作流、Prompt 商城、云端账号、UI 美化、业务逻辑、
任何"为将来预留"的空接口 —— 全部未做。

**验收对照（8/8 通过）**：

| # | 验收标准 | 结果 | 证据 |
|---|---|---|---|
| 1 | 测试插件可调用三个能力，Ollama 环境真实跑通 | ✅ | 真机三步全成功（§报告 2） |
| 2 | 切换 Provider 不改插件代码 | ✅ | **全文件哈希**：`plugin.json`+`plugin.py` 两次运行逐字节一致 |
| 3 | Ollama 不可用时错误明确 | ✅ | 含 `provider=ollama` + `原因：` + `检查建议：` |
| 4 | AI Capability 属于 Core | ✅ | 路径正确；`ctx.ai` 为 `...ai.service.AIService`；零第三方依赖；无 `registry.py` |
| 5 | 插件无直接模型调用 | ✅ | 9 个插件源码：字面 grep 0 命中、`ast` 0 命中 |
| 6 | Core 无业务耦合 | ✅ | 18 条业务词表（含词边界）扫描 `capabilities/ai/`：0 命中 |
| 7 | 跨 Provider 约束写入文档与注释 | ✅ | `types.py` 模块文档 + 类注释 + README，三处均有测试断言 |
| 8 | 全部测试通过 | ✅ | `Ran 283 tests ... OK`，双环境 |

**真实环境验证**（本机 Ollama，非 mock）：

```
text_generate    provider=ollama model=qwen2.5vl:3b    → 模型实际输出（含 usage 计数）
vision_analyze   provider=ollama model=qwen2.5vl:3b    → description + labels 结构化结果，与测试图相符
embedding        provider=ollama model=nomic-embed-text → count=2  dim=768
换 Provider      ollama(/api/generate) → openai_compatible(/v1/chat/completions)
                 同一份插件、同一组参数，插件目录全文件哈希一致 ✅
```

**怎么保证约束不是空话**：五条测试守着 —— 插件字面 grep、插件 `ast` import 扫描、
插件只能从 `xbc.core.capabilities.ai` 取 AI、模型端点只允许出现在 `capabilities/ai/` 下、
AI 能力层不得引入第三方依赖。**约束写进测试才守得住。**

**本轮按规格对齐时报告并裁决的 3 个冲突**：

1. 原则 6「不做任何为未来预留的扩展点」与为"未来增加厂商"而建的 `registry.py` 冲突
   → **裁决：删掉 registry，改显式装配**（加厂商改内核一处，但仍不改任何插件）。
2. 验收 5 要求字面 grep 干净，但解释这条规则的文档字符串本身要写出被禁的词
   → 改写文档字符串为"不 import 任何模型 SDK 或网络库"，使证据自洽。
3. 「业务词」未界定且 `客户` 会误命中 `客户端` → 采用 18 条词表 + `客户(?!端)` 词边界。

**为满足原则 6 而移除的项**（需要知晓）：`registry.py`、`AIService.set_default()`、
按调用的 `provider=` / `model=` / `temperature=` / `max_tokens=` / `json_mode=` 参数、
旧版兼容方法 `generate()`。参数统一由 `ai.options` 提供，避免同一参数两个来源。

**开发中发现并修复 9 个真缺陷**：

1. **`base_url()` 把 OpenAI 的 `/v1` 后缀剥掉了** —— 请求打到 `/chat/completions` 上直接 404，
   `openai_compatible` 整条链路不可用。`/v1` 是基址惯例，不能动。已修 + 回归测试。
2. **`status()` 默认做网络探测，一次要 4 秒** —— 状态查询在热路径上（插件列表、界面刷新）。
   改为默认只报结构信息，`probe=True` 才联网。
3. **默认地址用 `localhost`** —— Windows 上先试 IPv6 `::1`、失败再回退 IPv4，
   **实测每次探测 2.07s vs 127.0.0.1 的 0.004s**。默认改为 `127.0.0.1`。
4. **本地模型请求会走系统代理** —— 开发机装了 Clash 类代理时 `getproxies()` 会返回它，
   连本地模型的请求可能绕到代理上。改为**回环地址显式绕过代理**，远端仍走系统代理。
5. **每次生成前都做一次可用性预探测** —— 白多一个往返；且本机"连没人监听的端口"不退化
   为拒绝而是挂满超时（2s），代价直接加到每次调用上。移除预探测。
6. **自检插件里为生成向量预览重复调用了一次 embedding** —— 白多一次往返。改为复用同一结果。
7. **`ai.embedding_model` 的检查建议给的是对话模型的示例** —— 照做会把对话模型设成向量模型、
   然后继续失败。示例值改为按能力区分，并把具体模型名换成占位符。
8. **插件只在失败时打印，三步全成功时控制台看不到结果** —— 任务书第 5 节要求「打印结果」。
   改为每个成功步骤也打印一行结果摘要。
9. **修第 7 项时我把 `raise` 写成了 `return`**，异常对象被当成模型名返回 —— 提交前被新增的
   4 条测试捕获。记录在此，因为它说明测试网确实起作用。

修完以上与测试自身的服务关闭轮询优化后，整套测试从 **37s → 14.5s**。

**测试**：203 → **283 项**（`tests/test_ai_capability.py` **80 项**：接口契约、视觉结果解析、
两个 Provider 的协议级验证、插件端到端、Provider 全文件哈希对比、配置错误、
代理绕过、跨 Provider 约束、业务词扫描、两条无直连证据）。

**测试方式说明**：Provider 的正确性用**本地 mock 服务**验证（标准库 `http.server`，
同时实现两套协议形状），**不依赖真 Ollama、不连任何外部服务**，因此可确定性复现。
真机 Ollama 用于端到端确认，两者互补。

📄 完整报告：[docs/ai-capability-v1-test-report.md](docs/ai-capability-v1-test-report.md)

---

## TASK-008　AI Capability 首次真实消费：vision 定向提问 + video_analyzer 接入 —— DONE

**定位**：不是给 video_analyzer 加业务功能，而是**验证 TASK-007 的接口设计是否可用**。

**范围与约束**：Core 只允许扩展 `vision_analyze` 一处；扩展必须向后兼容；
视频业务逻辑全留在插件内；不做任何"为将来预留"的扩展点。

**实现**：

| # | 任务书要求 | 实现 |
|---|---|---|
| 1 | `vision_analyze` 新增 `question`（唯一 Core 改动） | 新增 `VisionAnswer` 类型；`vision_prompt()` 按模式出提示词；`parse_vision_payload(field=...)` 增加默认参数保证原行为不变 |
| 2 | video_analyzer 接入 AI Capability | 新增工具 `video_annotate`：镜头切分 → 抽关键帧 → 逐帧 `ctx.ai.vision_analyze(question=...)` → 按镜头汇总标签 |
| 3 | 报告接口设计反馈 | 报告第 8 节（本任务核心产出） |

**两种模式用两个类型，不用同一字段承载两种语义**：

| 模式 | 入参 | 返回 | 承载答案的字段 |
|---|---|---|---|
| 描述 | 不传 `question` | `VisionResult` | `description` |
| 定向提问 | 传 `question` | `VisionAnswer` | `answer` |

**验收对照（7/7 通过）**：

| # | 验收标准 | 结果 | 证据 |
|---|---|---|---|
| 1 | 不带 question 与 TASK-007 一致；带 question 返回 `{answer, labels}` | ✅ | TASK-007 的 80 项测试**一条未改全部通过**；真机两种模式字段集合已列出 |
| 2 | video_analyzer 真实跑通，每镜头带 AI 标签 | ✅ | 6 秒真实 MP4 / 3 镜头；**3 组标签互不相同**（真 Ollama） |
| 3 | 插件无直接模型调用（grep + ast） | ✅ | 两插件：大小写不敏感 grep 0 命中、ast 0 命中 |
| 4 | Core 无业务耦合 | ✅ | 18 条业务词表扫描 `capabilities/ai/` 0 命中；`FRAME_QUESTION` 只在插件里 |
| 5 | Core 改动范围锁定 | ✅ | 17 个非 vision 代码单元 + 37 个 AI 层外文件**哈希零变化** |
| 6 | 全部测试通过 | ✅ | `Ran 305 tests ... OK`，双环境 |
| 7 | 报告含「接口可用性反馈」 | ✅ | 报告第 8 节，含"是否需要返工"的明确结论 |

**真实链路记录**（本机 Ollama `qwen2.5vl:3b`）：

```
输入  distinct_scenes.mp4  6.0s 320x240 437807 字节（mandelbrot / smptebars / life 三段）
镜头0 [0,2) 回答"复杂的几何图形，属于抽象艺术场景"  标签 几何图形/抽象艺术/复杂图案/色彩斑斓…
镜头1 [2,4) 回答"测试画面"                        标签 测试画面/彩色条纹/电视测试/色彩校准…
镜头2 [4,6) 回答"主要内容是星空，属于夜空场景"      标签 星空/夜空/宇宙/天体/天文…
标签集合互不相同 3/3 组 —— 来自对各自画面的理解，非硬编码
耗时：首次 33.0s（模型冷加载），模型常驻后 2.1s
```

**接口可用性反馈结论**：**不需要返工**。理由与 3 处可改进项见报告第 8 节。
按任务书要求**未私自改 Core 接口**，改进项只记录待裁决。

**本轮修的真缺陷**：

1. **TASK-007 的字面 grep 是大小写敏感的** —— `video_analyzer` 文档字符串里的
   「不接 **Ollama**」（大写 O）躲过了检查。本任务做真实接入时发现：
   改掉该措辞 + 新增大小写不敏感扫描测试。
2. **`test_video_analyzer.py` 硬编码工具数与能力集** —— 插件加一个工具后 3 条测试假失败。
   改为从清单推导，并新增"声明了 ai 就必须真能拿到"的测试。

**范围外只报告**：全量 `plugins/` 大小写不敏感扫描会命中
`plugins/knowledge_base/plugin.py`（含 `ollama`，但无真实 import）。
该插件非本任务产物，**未修改**。

📄 完整报告：[docs/task-008-ai-capability-first-consumption-report.md](docs/task-008-ai-capability-first-consumption-report.md)

---

## TASK-009　视频素材资产库 V1 —— DONE

**定位**：把 video_analyzer 从"一次性分析工具"升级为"可持续积累、可检索、可复用的素材资产库"。
**建库，不做匹配** —— 文案→镜头匹配是 TASK-010。

**实现**：

| # | 任务书要求 | 实现 |
|---|---|---|
| 1 | 修 TASK-008 两处接口问题 | `types.py` 声明 `labels` 为自由文本；`vision_prompt(image_count=...)` 让措辞与 `images` 列表自洽 |
| 2 | 素材库（插件内） | `xbc_va_library.py`：SQLite 5 表（videos / shots / frames / labels / vectors） |
| 3 | 扫描与增量 | 内容哈希判定；失败隔离；`library_retry` / `library_rebuild` |
| 4 | 检索 | `library_search_labels`（exact/fuzzy）、`library_search_semantic`（embedding + 余弦） |
| 5 | 导出 | `library_export`：H.264/MP4 + faststart，输出侧精确定位 |
| 6 | 失败与重建 | 失败留在库里可重试；`pending` 状态支持中断续跑；库可整体重建 |

**验收对照（10/10 通过）**：

| # | 验收标准 | 结果 | 证据 |
|---|---|---|---|
| 1 | 两处接口问题已修，其余 Core 哈希一致 | ✅ | 4 个白名单文件变化；37 个 AI 层外内核文件**零变化** |
| 2 | 素材库可用，重复扫描不重复分析 | ✅ | 8 视频入库；重复扫描 **0.0s / 8 个全部按内容哈希跳过** |
| 3 | 增量与容错，失败可重试 | ✅ | 新增只分析新文件；坏文件不阻塞其余；修好后重试成功 |
| 4 | 标签检索 + 语义检索，不只是字符串匹配 | ✅ | 7 组查询记录；"星空"标签检索 0 命中 → 语义检索 top-1 命中 |
| 5 | 导出片段可播放、时间码正确 | ✅ | ffprobe 实测 3.000000s（镜头 [0.0, 3.0]） |
| 6 | 删除库文件后可重建 | ✅ | 删文件后重建 11.6s；**库文件损坏**也能救回来 |
| 7 | 插件无直接模型调用 | ✅ | 大小写不敏感 grep 0 命中 + ast 0 命中 |
| 8 | Core 无业务耦合 | ✅ | 18 条业务词表扫描 `capabilities/ai/` 0 命中 |
| 9 | 全部测试通过 | ✅ | `Ran 348 tests ... OK`，双环境 |
| 10 | 报告含《真实使用反馈》 | ✅ | 报告第 8 节 |

**真实素材入库记录**：

```
素材     8 个视频 / 1,433,709 字节（ffmpeg lavfi 合成，本机无实拍素材）
入库     14 个镜头 / 14 帧 / 88 个标签（去重 43）/ 28 个向量 / 768 维
首次     65.1s（≈4.6s/镜头，含模型冷加载）
重建     11.6s（模型常驻后 ≈0.8s/镜头）
重复扫描 0.0s，零次模型调用
```

**检索质量（7 组真实查询，4 组 top-1 正确 / 3 组不正确）**：

| 查询 | top-1 | 正确 |
|---|---|---|
| 星空 | v02镜头0（夜空/星云/宇宙）0.8187 | ✅ |
| 宇宙星云 | v02镜头0，0.8698 | ✅ |
| 抽象的彩色几何图案 | 抽象/几何类，0.7504 | ✅ |
| 色彩渐变背景 | 色彩渐变类，0.7328 | ✅ |
| 电子游戏画面 | 泛化描述镜头 0.8276 | ❌ 正确项未进前 4 |
| 电视信号测试图 | 分形镜头 0.6456 | ❌ 正确项第 3 |
| 夜景 | 分形镜头 0.6526 | ❌ 正确项第 11–12 |

**根因已定位并记录**：库里的向量是 **AI 描述文本的 embedding**，不是图片 embedding，
所以**检索质量上限 = 描述质量上限**。描述泛化（如"测试/测试画面/测试图像"）就到处都像；
描述里没有的概念就永远搜不到。按任务书要求**未自行做检索调优**。

**本轮修的真缺陷（5 个）**：

1. **`sqlite3.Connection` 的 `with` 只管理事务、不关闭连接** → 库文件一直被锁，
   `library_rebuild` 直接 `WinError 32`。新增 `Library.session()` 负责关闭。
2. **库文件损坏时 `library_rebuild` 也起不来**（先 `initialize()` 打开损坏文件就炸）
   → 重建永远救不回来。改为先删文件、不依赖打开成功。
3. **在 `upsert_video` 之前失败的视频不写库** → 用户看到 `failed=1` 却找不到那条。
4. **标签检索一个镜头命中多个标签时重复出现** → 改为按 `shot_id` 分组。
5. **代码注册 12 个工具但清单只声明 5 个** → 违反"零成本清单"契约。已补齐。

**范围外只报告**：`plugins/knowledge_base` 含被禁词（无真实 import），未修改；
`ctx.files` 缺二进制读取/哈希能力，插件做内容哈希只能直接 `open()`，未新增 Core 能力。

📄 完整报告：[docs/task-009-video-library-v1-report.md](docs/task-009-video-library-v1-report.md)
🔍 技术选型调研：[docs/research/vector-search-options.md](docs/research/vector-search-options.md)
（向量检索候选与中文标签检索的补调研：六个候选全部可商用，但都因"插件零依赖"被排除；
实测更正了交付报告里低估 4–22 倍的性能估算）

---

## 待办（尚未开始）

| ID | 任务 | 前置 | 备注 |
|---|---|---|---|
| TASK-010 | **文案 → 镜头匹配**（⛔ 前置的检索质量修复已完成，等它开工） | TASK-009 | 检索 top-1 **2/10 → 10/10**；见 [交付报告](docs/task-010-retrieval-quality-report.md) |
| TASK-011 | 图形界面二期：插件安装/卸载入口 + 配置编辑 | TASK-006 | 安装器是纯数据操作，界面直接调 `create_installer()` |
| TASK-012 | 打包分发（PyInstaller） | TASK-008 | 注意：工作区内产物带 Low 完整性标签，需先处理 |
| TASK-013 | 数据层迁移机制 | 出现真实业务库时 | 现在做属于过度设计 |
| TASK-014 | 平台化：账号、插件授权、插件商城、云端 AI 网关 | TASK-012 | 包格式、台账、AI 能力层均已就位 |

---

## 变更记录

| 日期 | 任务 | 说明 |
|---|---|---|
| 2026-10-04 | TASK-001 | 建立 XBC 最小内核（Core + 插件机制 + 验证插件 + 最小宿主） |
| 2026-10-04 | TASK-001 | 恢复开发环境：环境自检、隔离环境、诊断测试 |
| 2026-10-04 | TASK-001 | 证伪 ACL 假设；定位并修复完整性标签与日志句柄泄漏两个真实缺陷 |
| 2026-10-04 | TASK-002 | 产出《夏半仓 Plugin Runtime 技术方案 V1》（5 个参考实地调研） |
| 2026-10-04 | TASK-003 | 实现 Plugin Runtime V1 MVP：分层注册表、作用域、工具/技能注册表、三层配置、CLI、真实插件；123 项测试通过 |
| 2026-10-04 | TASK-004 | Desktop Shell MVP：插件列表/状态、启用停用、Tool/Skill 列表；界面不缓存状态，操作复用 Runtime 同一方法；测试增至 135 项 |
| 2026-10-04 | TASK-005 | 第一个真实业务插件 video_analyzer：FFmpeg 媒体信息 + 镜头切分 + 关键帧抽取，4 个 Tool 均返回结构化 JSON；新增内核能力 probe_media；修 1 个配置重载 bug；测试增至 153 项 |
| 2026-10-04 | 修复 | 重复扫描被误报为"插件 id 重复"错误（长驻界面每点一次刷新就刷一屏 ERROR） |
| 2026-10-04 | TASK-006 | 插件产品化基础：`.xbcplugin` 包格式（含 zip-slip 防护）、安装/卸载/升级、SemVer 版本比较、安装台账、用户数据目录分离；修 3 个真缺陷（其中"升级不生效"直接卡验收）；测试增至 203 项 |
| 2026-10-05 | TASK-007 | AI Capability Layer V1（按规格对齐版）：`AIService` + `ModelProvider` + Request/Response 结构；`providers/`（ollama / openai_compatible）；`text_generate` / `vision_analyze`（结构化 description+labels）/ `embedding`（dim）；配置 `ai.provider`/`ai.model`/`ai.options`；验收插件 `ai_test_plugin`（单工具 `ai_selftest`）；**删除 Provider 注册表**（原则 6）；修 9 个真缺陷；测试增至 283 项；产出《AI Capability Layer V1 测试报告》 |
| 2026-10-05 | TASK-008 | AI Capability 首次真实消费：`vision_analyze` 新增 `question`（向后兼容，返回 `VisionAnswer{answer,labels}`）；video_analyzer 新增 `video_annotate`（关键帧 → `ctx.ai` → 逐镜头标签）；Core 改动锁定（17 单元 + 37 文件哈希零变化）；修 2 个真缺陷（含 TASK-007 大小写敏感 grep 漏洞）；测试增至 305 项；产出《TASK-008 交付报告》含接口可用性反馈 |
| 2026-10-05 | TASK-009 | 视频素材资产库 V1：插件内 SQLite（5 表）、内容哈希增量、失败隔离与重试、标签检索 + 语义检索、片段导出、整库重建；修 TASK-008 两处接口问题（Core 改动锁定 4 文件 / 37 文件零变化）；修 5 个真缺陷；测试增至 348 项；产出《TASK-009 交付报告》含真实使用反馈 |
| 2026-10-05 | 待办调整 | TASK-009 编号被"视频素材资产库"占用。**TASK-010 按 TASK-009 任务书原文保留给「文案 → 镜头匹配」**（任务书里明确写了"文案→镜头匹配是 TASK-010"），原界面二期顺延为 TASK-011，其后依序顺延 |
| 2026-10-05 | TASK-010 前置调研 | 视频语义检索方案选型：**本机活体实测三条路线**（同批 14 帧 / 同样 10 个中文查询）——中文 CLIP 6/10、现有 Caption 路线 2/10、MobileCLIP2 1/10（英文对照 5/5）；MobileCLIP2 中文失效已实证到 tokenizer 机制层；逐一核实 8 个项目的许可证（**MaterialSearch GPL-3.0 / VideoSeek AGPL-3.0 不可采用**；`chinese-clip-vit-base-patch16` **权重未声明许可证**）；产出《视频语义检索方案选型》调研报告 |
| 2026-10-05 | TASK-010 第一部分 | **存储缺陷修复完成**：库 schema v1→v2，`vectors` 补 `embed_text`（真正被嵌入的原文）/ `kind` / `created_at`；新增 `library_audit` 工具（第 13 个）；旧库迁移后**如实标注 `auditable: false`**（旧向量无法反推原文）；重新入库后 28/28 可审计；实测坐实 TASK-009 缺陷（原来看不见的 `answer` 一直参与向量计算）；测试增至 **360 项** |
| 2026-10-05 | TASK-010 第二/三部分 | **按任务书要求停下报告**：`chinese-clip-rn50`（Apache-2.0）已导出 ONNX 并验证**与 torch 余弦 1.00000000 精确等价**、**运行时不需要 torch**、维度 1024=1024、中文 top-1 **10/10**；但 Core 的 `EmbeddingRequest` 只收文本，**图片嵌入无法经 Core Capability** → 触发"改动 Core 需先报告"的闸门，未自行扩展。另：**等权 RRF 融合实测把 top-1 从 10/10 拉到 8/10**，如实报告建议**不做融合** |
| 2026-10-05 | TASK-010 交付 | 按裁决（**扩 Core 支持图片嵌入 / 不做融合 / 294MB 可接受**）完成：Core 新增 `AICapability.IMAGE_EMBEDDING` + `ImageEmbeddingRequest` + `ModelProvider.embed_images()` + `AIService.embed_images()`；插件新增本地 ONNX Provider（`xbc_va_clip.py`，含**独立实现的 BERT 分词器，与官方 23/23 条等价**）；`vectors` 唯一键改为含 `provider+model`，**两个向量空间共存但不混算**；新增 `library_audit` 工具与 `scripts/export_chinese_clip_onnx.py`；**端到端实测 top-1 2/10 → 10/10**（目标 ≥6/10）；修 2 个真缺陷（能力声明与可用性不一致、插件自己判断能力） |
| 2026-10-05 | 规则细化（只动文档） | 区分"远端模型 SDK"与"本地推理运行时"：**业务代码**禁止 import 远端 SDK（`openai`/`ollama`/`anthropic`/`litellm`）与 HTTP 客户端（`requests`/`httpx`/`aiohttp`/`urllib`/`http.client`）、禁止直接发起远端模型调用；**插件提供的 Provider 实现**允许 import 本地推理运行时（`onnxruntime` 等），但业务代码不得直接调用该 Provider，必须经 `ctx.ai.*`。判据是"**是否绕过能力层去够远端模型**"，不是"有没有出现某个词"。落盘到 [docs/harness-rules.md](docs/harness-rules.md)（《Harness 工作规则》原本只是会话注入，磁盘上没有这个文件）；调研台账新增本裁决与"库的类型决定能不能 import"的登记判定。**核对结果：现有禁词表本来就不含本地推理运行时，无需改代码** |
| 2026-10-05 | TASK-010 补丁（模型分发策略） | 按补充约束调整：**模型不随 `.xbcplugin` 包分发**，放 **Core 级共享目录** `AppPaths.models_dir`（`<数据根>/models/<model_id>/`，插件数据目录是按插件隔离的，共用模型会重复 294MB）；**插件只声明"需要 embedding 能力"，不声明"需要模型文件"** —— 清单里已删除 `clip_model_dir`；路径解析落在**注册机制**：`AIService.register()` 调 `ModelProvider.bind_models(models_dir)` 注入共享目录，Provider 用自身 `MODEL_ID` 解析，**插件那行代码连模型名都不提**；模型缺失时三层递进（`configured()`→`available()`→**不声明能力**），调用时抛含**绝对路径 + 获取命令**的错误，`library_status.image_embedding` 展示状态。端到端复测：**插件零配置下 top-1 仍 10/10**。Core 再增 2 个装配层文件（`paths.py` / `context.py`）。测试增至 **378 项** |
