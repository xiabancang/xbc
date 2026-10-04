# Plugin Runtime V1 —— 测试报告与验收结果

> 任务：TASK-003（按《夏半仓 Plugin Runtime 技术方案 V1》实现 MVP）
> 日期：2026-10-04
> 结论：**全部通过**（123 项测试 OK；A1–A10 验收标准全部达成；无未解决缺陷）

---

## 0. 结论摘要

| 项目 | 结果 |
|---|---|
| 测试 | **123 项全部通过**（系统 Python 与 `.venv` 两个环境各自跑通） |
| 验收标准 A1–A10 | **10/10 达成**（逐条证据见第 3 章） |
| 安全底线 S1/S2 | 已实现并验证（S3 在 MVP 范围内不适用，见第 2 章偏差 D2） |
| 端到端冒烟 | 2 个插件、7 个工具、2 个技能；`failed=0`；停用后残留 `tools=0 skills=0` |
| 未解决缺陷 | 无 |
| 范围偏差 | 7 处，**全部有据可查且已在第 2 章声明**（其中 2 处由用户明确要求） |

---

## 1. 交付物

### 1.1 代码规模

内核 `src/xbc/core/` 共 **3402 行**（35 个文件），其中新增/重写的运行时占主体：

| 模块 | 文件 | 行数 | 职责 |
|---|---|---|---|
| **契约层** `core/contract/` | manifest.py | 266 | 清单 v2：双版本字段、能力白名单、工具/技能/命令声明、图标路径约束 |
| | hookspec.py | 179 | 钩子契约与**注册时签名校验**（借鉴 pluggy） |
| | plugin.py | 55 | 插件基类与生命周期回调契约 |
| | kernel_hooks.py | 20 | 内核扩展点契约 |
| **运行时** `core/runtime/` | manager.py | 371 | 发现 / 加载 / 激活 / 停用 / 卸载的编排与错误隔离 |
| | registry.py | 132 | 分层注册表与重名裁决（内核/插件/用户三层 + 先到先得） |
| | scope.py | 78 | 作用域与 effect —— **注册即资源** |
| **能力层** `core/capabilities/` | ai / ffmpeg / files / settings / events | 464 | 内核提供给插件的公共能力（需在清单声明） |
| **工具** `core/tools/` | registry.py | 178 | 工具注册、入参/出参校验、**风险授权**、统一错误信封 |
| | schema.py | 233 | JSON Schema 子集校验器（自研，保持零第三方依赖） |
| **技能** `core/skills/` | catalog.py | 254 | 合并多来源、目录/正文分离、调用策略矩阵 |
| **配置** `core/config/` | layers.py / store.py | 261 | 三层装配 + 整对象替换语义 |
| **上下文** | context.py | 369 | AppContext（内核）与 PluginContext（受限视图 + 注册门面） |
| **诊断** | diagnostics.py | 174 | 环境体检 + 运行时诊断 |
| **CLI** | __main__.py | 236 | 唯一操作界面（本任务不做 UI） |

### 1.2 插件

| 插件 | 用途 | 工具 | 技能 |
|---|---|---|---|
| `hello_xbc` | 机制验证：生命周期、能力调用、错误隔离 | `hello_probe`(read) / `hello_greet`(write) / `hello_fail`(read，故意失败) | `hello-workflow` |
| `text_toolbox` | **第一个真实业务插件**（纯本地文本处理，不含 AI、不含音视频） | `text_defaults` / `text_stats` / `text_dedupe`(read) / `text_export`(write) | `text-cleanup`（含参考文件） |

---

## 2. 与方案的偏差（7 处，逐条声明）

用户要求"不扩大范围"，所以这里**逐条说明每一处与方案原文的差异及原因**，不做任何隐瞒。

| # | 方案原文 | 实际实现 | 原因 | 性质 |
|---|---|---|---|---|
| **D1** | M8：插件管理器 UI | **改为 CLI**（`plugin` / `tool` / `skill` / `doctor`） | **用户明确要求"不做 UI"** | 用户指定 |
| **D2** | M10：真实插件 `media_analyzer`（镜头切分 + 抽帧 + AI 理解） | **改为 `text_toolbox`**（文本统计/去重/导出） | **用户明确要求"不做 AI 视频"**；但仍需要一个真实插件来证明链路可跑通 | 用户指定 |
| **D3** | 配置载体 `config/plugins.yml` | `config/plugins.json` | 解析 YAML 需引入 PyYAML，与"**内核零第三方依赖**"硬约束冲突。概念（三层补丁）完全不变，只换载体 | 技术约束 |
| **D4** | 目录 `core/services/` | 重命名为 `core/capabilities/` | 按方案 4.1 的术语统一：**Capability = 内核给插件的**，Service = 插件给插件的 | 术语一致性 |
| **D5** | 旧版插件的 `@action` 机制 | **移除**，统一为 Tool | 方案 4.2 定义"Tool 是 Agent 唯一能执行的东西"。两套机制并存会让插件作者困惑 | 消除重复机制 |
| **D6** | pluggy 的 `hookwrapper` / `wrapper` / `historic` | 未实现 | "最弱机制优先"（方案 1.1）：当前没有使用场景，引入只会扩大故障面。已实现 `hookspec` / `hookimpl` / 签名校验 / `optionalhook` / `tryfirst` / `trylast` | 最小实现 |
| **D7** | pluggy 的 `check_pending()` 抛错 | 改为**返回"可选钩子错配"清单** | 非可选的未知钩子在 `register()` 时已被拒绝，原语义的 `check_pending()` **永远不可达**（死代码）。改为报告 `optionalhook` 与内核契约不匹配的情况——这是真实的版本错配信号，已接入 `doctor` | 修正不可达逻辑 |

**未做且明确未做**：插件商城、支付、云端、用户系统、多租户、插件签名、进程外插件、MCP 接入、自动更新、UI。

---

## 3. 验收结果（A1–A10）

| # | 验收标准 | 结果 | 证据 |
|---|---|---|---|
| **A1** | `plugin list` 列出插件，**未激活也显示名称/描述/工具声明**；不导入代码 | ✅ | 实测 `state=discovered`；测试断言 `record.instance is None`、`record.scope is None`、`xbc_plugin_* not in sys.modules` |
| **A2** | `plugin disable` → 重启仍是禁用 | ✅ | CLI 两个独立进程均 `enabled=False`；用户层写入 `{"plugins":{"text_toolbox":{"enabled":false}}}`；测试 `test_disable_persists_across_restart` |
| **A3** | 用户层覆盖生效，且能报告来源；内核/插件默认值不变 | ✅ | `plugin show text_toolbox` → `config_source=host`，`config={"strip_blank_lines":true,"max_lines":500}`（清单默认是 1000）；测试断言用户层覆盖后宿主层被**整体取代** |
| **A4** | 某插件 `apply()` 抛异常 → 它 FAILED，其他插件仍 ACTIVE，宿主正常 | ✅ | 测试 `test_failed_plugin_is_isolated` + `test_broken_import_is_isolated`（导入语法错误也被隔离） |
| **A5** | 插件停用后其注册全部消失，无残留 | ✅ | 冒烟输出 `after_deactivate: {tools: 0, skills: 0}`；测试断言 `scope.effect_count == 0` |
| **A6** | 依赖不存在的服务 → 保持 INACTIVE（**不是报错**） | ✅ | 测试 `test_missing_dependency_stays_inactive_not_failed`：`state=INACTIVE`、`error=""`、`blocked_reason` 说明缺哪个服务 |
| **A7** | 技能目录只给名称+描述；按名加载返回正文；工具可调用 | ✅ | `skill list` 返回 2 条；`skill load text-cleanup` 返回正文与资源清单；`tool call text_defaults` 成功 |
| **A8** | 参数不合 schema → 可读错误（不是栈） | ✅ | `tool call text_stats` → `code=invalid_arguments`、`message="$: 缺少必需字段 'text'"` |
| **A9** | 全部测试通过 | ✅ | **123 项 OK**（详见第 4 章） |
| **A10** | `doctor` 无 error | ✅ | `healthy=True`、`runtime.ok=True`、`problems=0`、`states={"active":2}`、`tools=7`、`skills=2` |

### 3.1 附加验收（方案 3.10 安全底线）

| 底线 | 验收 | 证据 |
|---|---|---|
| **S1** 能力声明 → 执行点校验 | ✅ | 未声明 `ai`/`ffmpeg` 的插件访问时抛 `CapabilityDenied`（测试 `test_undeclared_capability_denied`） |
| **S2** 危险操作内核侧强制 | ✅ | `write` 工具未授权 → `consent_denied`；显式 `--yes` → 成功；显式拒绝 → `consent_denied`。**授权依据来自内核执行点，不依赖插件自报等级**（测试 4 项） |
| **S3** 安装不静默执行命令 | ⚪ 不适用 | MVP 不做安装流程与商城；扩展位与原则已写入方案，待安装功能落地时实现 |
| **S4** 密钥宿主代持 | 🟡 基础版 | `SettingsCapability` 提供独立 `secrets.json` + `mask_secret` 脱敏；完整"宿主代理出站"留待接入真实云端 API 时实现 |
| **S5** 资源限额 | 🟡 基础版 | 工具入参 schema 校验、出参 schema 校验、插件数据目录隔离已实现；CPU/内存配额未实现（Python 进程内难以精确控制，已在方案风险 R1 记录） |

---

## 4. 测试统计

**合计 123 项，全部通过。**

| 测试模块 | 用例数 | 覆盖内容 |
|---|---|---|
| `tests/test_plugin_runtime.py` | **69** | 生命周期、隔离、三层配置、技能策略、工具契约、风险授权、契约校验、注册表裁决、作用域释放、钩子签名校验 |
| `tests/test_tool_schema.py` | **48** | JSON Schema 子集校验器（类型、required、additionalProperties、enum、边界、路径、未知关键字忽略） |
| `tests/test_diagnostics.py` | **5** | 环境体检分级与退出码 |
| `tests/test_ui_shell.py` | **1** | 旧占位宿主仍可渲染（跳过条件：无 PySide6） |

运行环境（两者结果一致）：

```
系统 Python 3.13.15        Ran 123 tests    OK
.venv (隔离环境)            Ran 123 tests    OK
```

### 4.1 关键机制的针对性验证

| 机制 | 验证方式 | 结果 |
|---|---|---|
| **注册即资源** | 激活后 7 工具 2 技能 → 停用后 `effect_count==0`、注册表归零 | ✅ |
| **错误隔离** | 导入失败 / `apply` 抛异常 / 工具执行失败 / 事件处理器失败 / 钩子实现失败 —— 五处都不影响宿主 | ✅ |
| **分层裁决** | 高层获胜；同层先到先得；rank 优先；**获胜者被移除后被遮蔽项重新生效** | ✅ |
| **整对象替换** | 用户层 `{"max_lines":7}` 覆盖后，宿主层的 `strip_blank_lines` **不保留** | ✅ |
| **签名校验** | 钩子参数名写错 → 注册即抛；未知钩子 → 抛（除非 `optionalhook=True`） | ✅ |
| **错误二分** | 协议错误（`invalid_arguments` / `tool_not_found`）与业务失败（`execution_failed`）分开 | ✅ |

---

## 5. 开发过程中发现并修复的缺陷

**如实记录**，其中 3 项是测试抓到的真 bug，不是"顺手改改"：

| # | 缺陷 | 后果 | 如何发现 |
|---|---|---|---|
| **B1** | `LayeredRegistry._remove` 判断条件写反 | **获胜者被移除后，该名称凭空消失**（被遮蔽项不会重新生效） | 单元测试 `test_shadowed_becomes_effective_after_winner_removed` |
| **B2** | 运行时注册的技能没有资源清单 | `skill load` 返回 `resources=[]`，界面无法预览参考文件 | 单元测试 `test_load_reports_resource_files` |
| **B3** | `reload_plugin_config` 在宿主层目录不存在时，把**用户层当成宿主层** | 用户层被自己覆盖，三层语义失效 | 代码复查（打包后宿主层目录会消失，是真实场景） |
| **B4** | `check_pending()` 原实现**永远不可达**（死代码） | 机制形同虚设 | 测试编写时发现"无法触发"，改为有意义的报告语义（偏差 D7） |
| **B5** | `cmd_doctor` 只 `load` 不 `activate` | 诊断永远显示 `tools=0 skills=0`，与真实运行状态不符 | CLI 验收时发现 |
| **B6** | `cmd_plugin_show` 的 `config_source` 在未加载时报告错误层次 | 用户看到 `default`，实际配置来自宿主层 | CLI 验收时发现（A3） |
| **B7** | Agent 视图缺风险标注 | Agent 无法感知工具风险等级 | CLI 验收时发现，已补 `annotations`（对齐 MCP） |

---

## 6. 未覆盖与已知限制

| # | 限制 | 说明 |
|---|---|---|
| L1 | **Python 进程内无法真正隔离** | 插件与宿主同进程；C 扩展崩溃仍会带走宿主。缓解：五处边界捕获；根治需进程外插件（方案 R1，扩展位已留） |
| L2 | CPU / 内存配额未实现 | 仅实现了入参/出参 schema 校验与超时（S5 基础版） |
| L3 | 插件间 Service 无版本协商 | `requires` 只校验"存在"，未校验版本。方案 R5 已记录 |
| L4 | `config` 整对象替换对用户可能反直觉 | 已由 `config_source` 与 `plugin show` 暴露；UI 侧的"逐项显示来源"待界面重建时做（方案 R2） |
| L5 | `commands`（命令面板索引）只做了声明与校验 | 未实现匹配与拉起——那属于界面/交互层，本任务不做 UI |
| L6 | 技能提供方**串行列举** | 一个慢的提供方会阻塞后续；MVP 只有文件系统提供方，影响可忽略 |
| L7 | 未做插件签名与信任分级 | 明确不在 MVP 范围（方案 6.3） |

---

## 7. 复现步骤

```powershell
Set-Location 'F:\Downloads\XBC'

# 1) 全部测试
python -m unittest discover -s tests          # 期望 Ran 123 tests / OK
.\.venv\Scripts\python -m unittest discover -s tests

# 2) 端到端冒烟（发现→加载→激活→调用只读工具→停用→卸载）
python run.py --root "$env:TEMP\xbc-demo" smoke

# 3) 完整体检
python run.py --root "$env:TEMP\xbc-demo" doctor

# 4) 验收要点
python run.py --root "$env:TEMP\xbc-demo" plugin list          # A1
python run.py --root "$env:TEMP\xbc-demo" plugin show text_toolbox   # A3：config_source=host
python run.py --root "$env:TEMP\xbc-demo" plugin disable text_toolbox # A2
python run.py --root "$env:TEMP\xbc-demo" plugin list          # A2：enabled=False 持久
python run.py --root "$env:TEMP\xbc-demo" tool list            # A7
python run.py --root "$env:TEMP\xbc-demo" skill list           # A7
python run.py --root "$env:TEMP\xbc-demo" tool call text_stats        # A8：invalid_arguments
python run.py --root "$env:TEMP\xbc-demo" tool call hello_greet       # S2：consent_denied
python run.py --root "$env:TEMP\xbc-demo" --yes tool call hello_greet # S2：授权后成功
```

---

## 8. 是否达成 TASK-003 的目标

> 用户要求：**按《夏半仓 Plugin Runtime 技术方案 V1》实现 MVP。不扩大范围，不做 UI，不做 AI 视频，不做商城，不做云端。完成后必须提供测试报告和验收结果。**

| 要求 | 结果 |
|---|---|
| 按方案实现 MVP | ✅ M1–M7、M9 全部实现；M8/M10 按用户约束调整（D1/D2） |
| 不扩大范围 | ✅ 未做商城、支付、云端、用户系统、多租户、签名、进程外插件、MCP |
| 不做 UI | ✅ 未写界面代码；旧占位宿主仅修好导入使其不失效，未做任何 UI 开发 |
| 不做 AI 视频 | ✅ 真实插件是纯本地文本处理，未接 Ollama、未做镜头/视频 |
| 不做商城 | ✅ |
| 不做云端 | ✅ |
| 提供测试报告 | ✅ 本文档 |
| 提供验收结果 | ✅ 第 3 章（A1–A10 逐条 + 证据） |
