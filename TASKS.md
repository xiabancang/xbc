# 任务台账（TASKS）

> 规则：每个任务有明确范围、验收标准和**可复现的验证证据**。
> 状态：`TODO` / `DOING` / `DONE` / `BLOCKED`
>
> 一次只做被点名的任务，不顺手扩大范围。

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

## 待办（尚未开始）

| ID | 任务 | 前置 | 备注 |
|---|---|---|---|
| TASK-005 | 图形界面二期：命令面板 / 配置编辑 / 技能正文预览 | TASK-004 | 命令面板消费 manifest 的 `commands` 索引 |
| TASK-006 | 首个 AI 视频类插件（把 V18 能力插件化） | TASK-003 | V18 仅作功能参考，禁止直接修改；参考 `F:\Downloads\xbc-refs\PySceneDetect` |
| TASK-007 | 打包分发（PyInstaller） | TASK-006 | 注意：工作区内产物带 Low 完整性标签，需先处理 |
| TASK-008 | 数据层迁移机制 | 出现真实业务库时 | 现在做属于过度设计 |
| TASK-009 | 平台化：账号、插件授权、插件商城、云端 AI 网关 | TASK-007 | 当前阶段明确不做 |

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
