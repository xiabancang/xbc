# 夏半仓 Plugin Runtime 技术方案 V1

> 状态：**设计稿**（TASK-002 交付物）
> 范围：只做架构设计，不含业务实现
> 时间：2026-10-04

---

## 0. 范围与术语

### 0.1 本方案要解决什么

让「夏半仓工具箱」具备一个**稳定的插件运行时**：插件可以安装、发现、激活、配置、停用、卸载；插件之间、插件与宿主之间通过明确契约交互；**持续增加插件而不需要修改内核**。

### 0.2 明确不做（本阶段）

商城、支付、云端、用户系统、多租户。这五项在本文中**不出现任何实现**，只在必要处留出扩展位并标注"V2+"。

### 0.3 术语表（先定义，后使用）

| 术语 | 英文 | 定义 | 谁拥有 |
|---|---|---|---|
| **能力** | Capability | 内核提供给插件的公共能力，如文件、AI、FFmpeg、配置 | **内核** |
| **服务** | Service | 插件向其他插件暴露的能力 | **插件** |
| **插件包** | Bundle | 可安装/卸载的单位：一个目录 + 一份清单 | 插件作者 |
| **插件** | Plugin | 插件包中被激活的代码单元（一个包可含多行） | 插件作者 |
| **工具** | Tool | 有 JSON Schema、可被 Agent 调用的具体动作 | 内核或插件 |
| **技能** | Skill | 声明式指令包，按需加载，**不是代码** | 内核或插件 |
| **宿主** | Shell | 图形界面，内核的消费者 | 内核 |
| **Agent** | Agent | 唯一的目标决策者 | 内核 |

> **必须澄清的一处概念**：用户需求写的链条是「Agent调用Skill / Skill调用Plugin / Plugin提供Capability」。方向大体正确，但有一处需要精确化 —— **Capability 是内核提供给插件的，不是插件提供的**；插件向外提供的是 **Service** 和 **Tool**。详见第 4 章。

---

## 1. 参考调研结论

调研了 5 个参考，**结论先说**：对夏半仓最有价值的不是 Dify 或 uTools，而是 **DeepSeek Harness（其内核是 Cordis 依赖注入插件框架）** 与 **pluggy** —— 因为它们和我们的形态最接近：**本地单进程 + 代码级插件 + 声明式契约**。uTools 和 Dify 提供了"产品形态"和"隔离策略"层面的启发，MCP 提供了"工具描述"与"能力协商"的标准化范本。

### 1.1 DeepSeek Harness（Cordis）—— 最深的参考

DSH 本体是一个 Cordis 宿主 + 技能 + 工具的组合体。我们直接读取了其随发行版打包的插件开发指南（`cordis-plugin-development` 技能及其 references），提炼出以下**可直接借用**的机制：

| 机制 | 内容 | 对我们的价值 |
|---|---|---|
| **插件导出形式** | `export function apply(ctx, config) {}`，可带 `export const inject = ['tools']` 与 `export const Config`；或默认导出服务类 | 极简：插件就是"一个带配置参数的函数" |
| **注册即资源** | 在 `apply` 内用 `ctx.effect` / `ctx.on` 注册，**返回 disposer**；卸载即释放 | 解决"停用插件后残留资源"这个经典难题 |
| **声明式依赖** | `inject` 声明依赖的服务；**依赖不存在时插件保持 inactive，而不是抛异常** | 插件不会因为环境缺东西而把宿主搞崩 |
| **配置校验** | 插件声明 `Config`（JSON Schema），激活时校验该行的 `config` | 配置错误在激活前就被拦住 |
| **分层注册表** | 注册按 scope 落入不同层（全局层 / preset 层）；**最近的层在重名时获胜**，层内按 rank → 注册顺序裁决 | 内置插件与用户插件重名冲突有明确裁决规则 |
| **补丁分层配置** | 配置是一串"补丁层"叠加在条目列表上；**用户补丁层跨版本存活**；`config` 整体替换、**从不深合并** | 用户配置不会被升级覆盖，且语义可预测 |
| **最弱机制优先** | 扩展点按能力从弱到强排列：`restrict`（只能移除）< `guard`（只能拒绝）< waterfall（可改写）< 整体替换。**能用弱机制就不要用强机制** | 防止插件之间互相破坏，是"扩展点设计"的核心纪律 |
| **零成本清单** | 插件管理器展示标题/描述/图标**不需要激活插件**，元数据从 manifest 与 locale 文件读取 | 插件多了以后列表仍然秒开 |
| **安装状态语义** | 区分 `applied` / `failed` / `overridden` / `restart-required`；**不能从进程日志推断成功** | 状态必须显式、可观测 |
| **纯配置插件** | 连接一个 MCP 服务器**不需要写任何代码**，只需一个声明行（serverName / transport / url） | 大量"集成类"需求可以由配置完成 |
| **Skill 调用策略** | `modelInvocable` × `userInvocable` 四种组合 | 见 4.3 |
| **Skill 按需加载** | 会话开始只给**目录**（名称 + 有长度上限的描述），正文要用工具按名加载；并明确告知模型"不得仅凭摘要推断指令" | 上下文成本可控，且防止模型瞎猜 |

**最小插件长什么样**（DSH 的模板，共 4 个文件）：

```jsonc
// package.json —— 清单
{ "name": "@local/my-decoration", "version": "1.0.0",
  "dsh": { "bundle": { "patch": "./cordis.patch.yml" } } }
```
```yaml
# cordis.patch.yml —— 声明一行
- insert:
    - id: my-decoration
      name: '@local/my-decoration'
```
```js
// index.js —— 宿主半边
export function apply() {}
```

**这个"小"本身就是设计目标**：插件作者要写的东西越少，生态才可能长起来。

### 1.2 pluggy —— Python 侧最成熟的形态

我们读了 pluggy 的实际源码（`_manager.py` / `_hooks.py` / `_execution.py` / `_decorators.py`）。它是 pytest 的插件内核，经过极端规模的验证：

| 机制 | 源码要点 | 对我们的价值 |
|---|---|---|
| **hookspec / hookimpl 分离** | 宿主用 `add_hookspecs()` 声明契约，插件用 `@hookimpl` 实现 | **契约与实现分离**：内核只管声明，插件只管实现 |
| **签名校验** | `register()` 时 `_verify_hook()` 校验插件函数签名是否符合 spec，不符抛 `PluginValidationError` | 插件写错**在注册时就报错**，而不是运行到一半才炸 |
| **未声明实现检查** | `check_pending()`：存在 hookimpl 但无对应 hookspec 时报错 | 防止"插件以为注册了、其实没人调用" |
| **可选钩子** | `@hookimpl(optionalhook=True)` —— 契约里没这个方法也不报错 | **前向/后向兼容的关键**：新插件能在老内核上跑 |
| **排序控制** | `tryfirst` / `trylast` 控制调用顺序 | 同名 Hook 的执行顺序可控 |
| **包裹式钩子** | `hookwrapper` / `wrapper` 能把其他实现包在中间 | 拦截、计时、审计 |
| **结果协议** | `firstresult`（首个非 None 即返回并**中止后续调用**）、`historic`（调用后注册的插件能回放） | 两种常用的"多插件协作"语义 |
| **可屏蔽** | `set_blocked()` / `unblock()` —— **不禁用插件，只屏蔽它** | 排障利器：怀疑某插件时先屏蔽而不是卸载 |
| **入口点发现** | `load_setuptools_entrypoints()` 借助包元数据自动发现 | 与 Python 打包生态一致，不发明新机制 |
| **可观测** | `add_hookcall_monitoring()` / `enable_tracing()` | 插件调用可追踪 |

**对夏半仓最值得抄的一条**：**hookspec（内核声明契约）+ hookimpl（插件实现）+ 注册时签名校验**。它用很小的成本，换来了"插件写错立刻知道"。

### 1.3 uTools —— 声明式指令索引与"输入即用"

uTools 是"本地工具箱"这个品类最成功的产品。它的插件是 **Electron 渲染进程里的网页应用**，宿主把能力注入成全局对象（`window.utools`）。以下字段逐条核对过官方 `plugin.json` 的 JSONSchema 与开发者文档：

| 机制（已核实） | 内容 | 对我们的意义 |
|---|---|---|
| **包结构** | `plugin.json` 必填 `logo`；`main`（.html）与 `preload`（.js，可用 Node 原生能力）**至少存在一个** | 极简清单。**注意**：没有 `main` 只有 `preload` 是合法形态 —— 即"纯后台插件" |
| **声明式指令索引** | `features[].cmds[]` 声明匹配规则，类型含 `regex` / `over`（划词）/ `img` / `files` / `window`（活动窗口） | **宿主只维护索引，命中才拉起插件** —— 插件不必常驻。这是效率工具箱手感的关键 |
| **AI 工具原生支持** | `tools` 字段声明 `description` / `inputSchema` / `outputSchema`，运行时用 `utools.registerTool` 注册 | 与我们"Tool 注册表 + JSON Schema"**完全同构**，方向被独立验证 |
| **开发态热更新** | `development.main` 可指向本地开发服务器 URL | 插件开发体验必须早做，否则没人愿意写插件 |
| **"隐藏到后台" ≠ "结束运行"** | 两者是不同状态，用户可分别控制 | 对应我们的 `INACTIVE`（释放资源）与真正的进程结束 |
| **无版本字段** | `plugin.json` 里**没有** `version`；打包版本与发布版本互不关联 | 反面教材：版本必须只有一个真源 |
| **⚠️ 无权限模型** | 文档与 schema 中**没有任何声明式权限清单，也没有运行时授权**；靠"preload 必须可读不可混淆 + 市场人工审核 + 安装提示"兜底，实际是**安装即全权** | **这是我们要明确避免的**：能力要分级声明，危险操作要首次授权 |

**取舍**：借它的**声明式指令索引 + 输入即用**与**开发态热更新**；**明确不借**它"安装即全权"的权限模型。

### 1.4 Dify Plugin —— 隔离、版本与凭证的参考

Dify 是云端 LLM 平台，插件体系是五个参考里**工程化最重**的。核实要点：

| 机制（已核实） | 内容 | 对我们的意义 |
|---|---|---|
| **插件类型化** | 六类：Tool / Model / Agent Strategy / Extension(Endpoint) / Datasource / Trigger；组合有硬约束（如 Tool 与 Model 不可共存） | 类型化让宿主差别对待；V1 只需一类（工具型），但清单**要预留 `type`** |
| **独立 plugin daemon** | 插件跑在独立 Go 服务中，三种 runtime：`local`（子进程 STDIN/STDOUT）、`debug`（TCP）、`serverless`（HTTP/Lambda）；API 只与 daemon 交互 | 隔离彻底，代价高：多一跳、stdio 缓冲上限、依赖共享目录 |
| **⚠️ daemon 不支持 Windows** | 官方 README 明确 daemon 仅支持 Linux/macOS，Windows 需大量适配 | **直接印证我们的选择**：Windows 本地桌面软件不该照搬进程外 daemon |
| **双版本字段** | `meta.version`（清单规范版本，未知字段降级可用）+ `meta.minimum_dify_version`（最低平台版本）；SDK 另走 SemVer | **值得直接移植**：规范版本与运行时版本分开表达 |
| **统一信封协议** | 响应统一 `{code, message, data}`，错误按 `error_type` 映射（权限拒绝 / 运行时错误等），支持流式分帧 | 统一信封让错误**可分类、可诊断**，而不是一团字符串 |
| **反向调用 + 能力门控** | 插件 → daemon → 平台 inner API（`session.app/model/tool/node`），由 manifest 的 `resource.permission` 门控 | "声明 → 执行点校验"的正确做法 |
| **⚠️ 凭证明文下发** | 静态存储是 tenant 级加密，但 invoke 时**把明文凭据放进请求体交给插件**；凭据校验逻辑本身也跑在插件进程内 | **我们要做得更好**：宿主代持密钥并代理出站请求，插件不持有长期密钥 |
| **量化资源限制** | 执行 600s、包 50MB、stdio 缓冲 5MB、环境初始化 120s | 限制要**早定并量化**，否则失控时无从下手 |
| **默认强制签名** | 区分官方 / 合作方 / 社区，支持第三方公钥 | V2+ 的事，但清单结构要留位 |

**取舍**：借它的**类型化、双版本字段、统一信封、能力门控、量化限制**；**不借**进程外 daemon（Windows 不支持）、"凭证明文下发"（那是它的弱点，不是范式）；签名与市场体系延后。

### 1.5 MCP —— 工具描述与安装安全的标准化范本

MCP 的价值在于它把"给模型用的工具"标准化了，并且**专门为"本地宿主 + 本地服务"写过安全要求**（SEP-1024）—— 这与我们的场景高度重合。我们直接核对了官方规范原文（含 `2025-06-18` 与更晚的 `2026-07-28` 修订版及 schema）：

| 机制（已核实） | 内容 | 我们的用法 |
|---|---|---|
| **生命周期与能力表达（正在演进）** | 早期版本（`2025-11-25` 及更早）用 **`initialize` 握手**协商协议版本与双方能力；**`2026-07-28` 起改为"按请求携带能力元数据"，`initialize` 退化为 legacy 兼容路径**（并为旧客户端保留回退） | 两点启发：① 能力协商是必要的；② 但把它做成**一次性有状态握手**会带来耦合，MCP 自己在往无状态走。我们本地进程内激活只协商一次是合理的，但**能力清单必须随时可查**，不能只存在于握手那一刻 |
| **三大原语** | `tools`（可执行动作）、`resources`（只读数据）、`prompts`（提示模板） | 对应我们的 Tool / Resource / Skill |
| **工具描述** | `name` + `description` + **`inputSchema`**（入参 JSON Schema）+ **`outputSchema`**（出参 Schema，可选）+ **`annotations`**（只读/破坏性等行为提示）；结构化结果走 `structuredContent` | 我们的 Tool 注册必须带 schema 与风险标注。规范明确要求**客户端必须把 annotations 当作安全提示对待** —— 与我们的 `risk` 字段同义 |
| **按需列举与错误语义** | `tools/list` 发现、`tools/call` 调用；工具自身失败通过结果里的 **`isError: true`** 表达（而不是协议错误） | 与"skill 目录 + 按需加载"同构。"业务失败"与"协议失败"要分开表达 |
| **⚠️ 本地安装必须显式同意** | **SEP-1024**（Final，Standards Track）：本地服务的"一键安装"不得静默执行命令，**必须让用户看到将要执行的完整命令并明确同意** | **直接写进我们的安全底线**：插件安装/更新绝不静默执行脚本；要展示命令与权限并取得同意 |
| **安全边界** | 官方强调用户同意、信任边界、以及工具描述可能被注入的风险 | 工具标风险等级；来自外部的描述需经用户确认 |

**取舍**：V1 不引入网络传输层（我们本地进程内），但**采用它的工具描述形态、能力协商思想与安装同意原则**；并把 MCP 定位为**将来的"进程外插件"通道**，用于接入外部工具生态。

**取舍**：V1 不引入网络传输层（我们本地进程内），但**采用它的工具描述形态、能力协商思想与安装同意原则**；并把 MCP 定位为**将来的"进程外插件"通道**，用于接入外部工具生态。

### 1.6 五个参考的综合取舍

| 参考 | 抄什么 | 不抄什么 |
|---|---|---|
| **DSH / Cordis** | 声明式依赖注入、注册即资源（disposer）、分层注册表与裁决、补丁式分层配置、"最弱机制优先"、零成本清单、Skill 调用策略 | 它的 Web/React 客户端体系、Node 生态的具体包 |
| **pluggy** | hookspec/hookimpl 契约分离、注册时签名校验、`optionalhook` 兼容开关、`set_blocked` 屏蔽、entry point 发现 | 它面向"钩子横切"的模型（我们需要的是"能力注册"，不是事件钩子） |
| **uTools** | 声明式指令索引（cmds 六种匹配、命中才拉起）、输入即用、开发态热更新、AI 工具的 schema 化 | 网页插件技术栈、**"安装即全权"的权限模型**、无版本字段 |
| **Dify** | 插件类型化、**双版本字段**（规范版本 + 最低运行时版本）、统一信封协议、能力门控、量化资源限制 | 进程外 daemon（**不支持 Windows**）、凭证明文下发、市场与签名体系 |
| **MCP** | 工具描述 schema、能力协商、风险标注、错误语义 | 网络传输层（V1 用不上） |

---

## 2. Core 职责定义

### 2.1 Core 负责什么

| # | 职责 | 说明 |
|---|---|---|
| C1 | **生命周期编排** | 发现 → 加载 → 激活 → 停用 → 卸载；含超时、失败隔离、重试 |
| C2 | **契约定义与校验** | 清单 schema、API 版本、能力名白名单、Tool 的 JSON Schema、hookspec 签名校验 |
| C3 | **能力注册与查找** | 分层注册表（内核层 / 插件层 / 用户层）、重名裁决规则 |
| C4 | **依赖注入与作用域** | 按 scope 分层；插件声明依赖，缺失时保持 inactive 而非报错 |
| C5 | **配置装配与持久化** | 三层配置合并（默认 / 宿主 / 用户）、schema 校验、原子写入 |
| C6 | **资源释放** | 注册即资源：所有注册都返回 disposer，随作用域一同释放 |
| C7 | **Skill 注册表** | 合并多来源、按需加载、调用策略、目录变更通知 |
| C8 | **Tool 注册表** | 收集内核与插件注册的工具，向 Agent 提供统一 schema 视图 |
| C9 | **宿主界面骨架** | 窗口、导航、主题令牌、槽位（slot）机制；**不含任何业务页面** |
| C10 | **公共能力实现** | files / settings / ffmpeg / ai / events / logger 等内核能力 |
| C11 | **可观测性** | 结构化日志、插件状态与失败原因、`doctor` 诊断、调用追踪 |
| C12 | **权限声明的校验与授权** | manifest 声明能力 → 执行点校验 → 危险操作**首次使用时向用户授权**；能力与用途在 UI 中可见可撤销 |

### 2.2 Core 禁止负责什么

| # | 禁止事项 | 为什么 |
|---|---|---|
| N1 | **任何具体业务逻辑** | 视频分析、内容生成、营销自动化都不属于内核。内核加业务 = 内核不再稳定 |
| N2 | **认识任何具体插件** | 内核代码里**不允许出现任何插件 id**。一旦出现，插件化就失败了 |
| N3 | **AI 的模型细节** | 不写 prompt、不选模型、不调具体 API；只提供 `ai` 能力接口与 Provider 抽象 |
| N4 | **FFmpeg 的业务参数** | 只负责"定位并执行"，抽帧参数/转码策略属于插件 |
| N5 | **插件的私有数据结构** | 内核不认识插件的表、文件格式、缓存布局 |
| N6 | **商业规则** | 授权、会员、计费、商城规则一律不在内核；内核只保留"插件包从哪来"的抽象 |
| N7 | **替代插件做策略** | 重试、缓存、降级由插件自己决定；内核只提供能力与超时保护 |
| N8 | **用户 / 租户 / 多租户概念** | V1 完全不出现。这是云端概念，混进本地内核会造成长期负担 |
| N9 | **插件之间的编排** | 内核不规定"A 插件完成后调用 B 插件"；那是 Skill/Agent 的职责 |
| N10 | **自动更新与商店协议** | V2+；内核只留"插件来源"接口 |

### 2.3 三条边界判定规则（写给未来的自己）

当不确定某个东西该放内核还是插件时，按顺序问三个问题：

1. **依赖方向规则** —— "内核需要知道这个插件的名字吗？" 需要 → 设计错了，改。
2. **能力归属规则** —— "所有插件都可能用吗？" 是 → 内核提供能力；只有一个插件用 → 插件自己做。
3. **变更频率规则** —— "它会因为产品需求频繁变吗？" 会 → 放插件；只在架构演进时变 → 放内核。

**推论**：内核的代码量应该**几乎不随插件数量增长**。如果加了 10 个插件，内核改了 10 次，说明抽象是错的。

---

## 3. Plugin 接口设计

### 3.1 插件包结构

一个插件包就是一个目录：

```
plugins/media-analyzer/
├─ plugin.json          # 清单（必须）
├─ plugin.py            # 入口代码（必须）
├─ skills/              # 本插件贡献的 skill（可选）
│  └─ shot-analysis/SKILL.md
├─ assets/              # 图标、模板等（可选）
│  └─ icon.svg
└─ README.md            # 可选
```

### 3.2 清单（plugin.json）

```jsonc
{
  "id": "media_analyzer",              // 唯一标识，小写+下划线，2-32 字符
  "name": "AI 视频助手",
  "version": "0.1.0",
  "api_version": "1.0",                // 内核 API 契约（主版本必须匹配）
  "entry": "plugin.py:MediaAnalyzerPlugin",
  "description": "镜头切分与视频理解",
  "author": "夏半仓",
  "icon": "assets/icon.svg",           // 相对路径，不得越出插件目录
  "capabilities": ["files", "ffmpeg", "ai", "settings"],  // 需要内核提供的能力
  "requires": [],                      // 依赖的其他插件服务（V1 留空）
  "tools": [                           // 本插件向 Agent 暴露的工具
    { "name": "split_shots", "description": "把视频切分成镜头", "risk": "read" },
    { "name": "analyze_shot", "description": "对单个镜头做 AI 理解", "risk": "read" }
  ],
  "skills": ["shot-analysis"],         // 本插件贡献的 skill 名
  "commands": [                        // 命令面板的声明式索引（学 uTools）
    { "code": "analyze-video", "label": "分析视频",
      "match": { "type": "files", "extensions": [".mp4", ".mov"] } },
    { "code": "recap", "label": "生成解说",
      "match": { "type": "text", "keywords": ["解说", "recap"] } }
  ],
  "config_schema": {                   // 该插件配置的 JSON Schema
    "type": "object",
    "properties": { "frame_count": { "type": "integer", "default": 3 } }
  }
}
```

**字段设计要点**：

- `capabilities` 是**白名单**：没声明的能力，访问时直接报错（已实现）。
- `tools` 在清单里**声明**（供零成本展示与权限预览），但**实际注册在代码里**（保证 schema 与实现同源）。
- `risk` 标注风险等级（`read` / `write` / `destructive`），供 UI 提示与将来授权使用（学 MCP 的 annotations）。
- `icon` 路径必须落在插件目录内，拒绝绝对路径、URL 与符号链接跳出（学 DSH）。
- `commands` 是**声明式索引**（学 uTools）：宿主只读清单就能建立命令面板索引，用户输入命中后**才激活插件**。这样装 50 个插件也不会拖慢启动。匹配类型至少支持 `text`（关键词）/ `files`（文件类型）/ `regex` / `window`（活动窗口）。
- **元数据必须在"不导入代码"的前提下可读**，否则插件管理器与命令面板会随插件增多而变慢、且一个坏插件会拖垮列表。

### 3.3 生命周期

```
                  ┌──────────────┐
                  │  DISCOVERED  │  只读了 plugin.json，未导入代码
                  └──────┬───────┘
                         │ load（校验版本 + 导入 + 实例化 + 校验配置）
                         ▼
                  ┌──────────────┐
                  │    LOADED    │  代码在内存，尚未注册任何东西
                  └──────┬───────┘
                         │ activate（执行 apply，注册资源）
                         ▼
                  ┌──────────────┐   deactivate（释放全部注册）
                  │    ACTIVE    │ ◀───────────────────────┐
                  └──────┬───────┘                          │
                         │                                  │
                         └──────────────────────────────────┘
                         │ unload（从内存移除模块）
                         ▼
                  ┌──────────────┐
                  │   UNLOADED   │
                  └──────────────┘

  任意环节失败 → FAILED（记录原因，可重试；不影响其他插件与宿主）
```

| 状态 | 允许的操作 | 说明 |
|---|---|---|
| `DISCOVERED` | 展示名称/描述/图标；激活 | 零成本，**不导入代码** |
| `LOADED` | 激活；查看配置 schema | 已校验版本与配置 |
| `ACTIVE` | 调用其 Tool / 访问其 Service / 读取其 Skill | 正常运行 |
| `INACTIVE` | 重新激活 | 代码保留（快速再激活），资源已释放 |
| `FAILED` | 查看失败原因；重试 | 必须给出**可读的原因**，不能只有栈 |
| `UNLOADED` | 重新加载 | 模块已从 `sys.modules` 移除 |

**设计要点**：

1. **`DISCOVERED` 必须能展示而不导入**（DSH 的经验）。
2. **`INACTIVE` 与 `UNLOADED` 必须区分**：前者可快速恢复（用户临时关掉），后者是真正卸载（换插件包）。
3. **每一层都要能失败而不外溢**：`load` / `activate` / `deactivate` 三个边界全部包住异常，失败只影响该插件。
4. **`deactivate` 必须是幂等的**，且不得抛异常（否则用户关不掉插件）。

### 3.4 注册方式

**两种注册并存，各管一段**：

**(a) 声明式注册（配置文件）** —— 决定"加载什么、是否启用、什么配置"

```yaml
# config/plugins.yml（宿主内置层）
- id: media-analyzer
  name: media_analyzer
  enabled: true
  config:
    frame_count: 3
```

```yaml
# 用户层补丁（%LOCALAPPDATA%/夏半仓工具箱/config/plugins.yml）
- id: media-analyzer          # 只写 id = 覆盖已有行
  config:
    frame_count: 6            # ⚠️ config 整体替换，未写的字段回落到默认层
```

**(b) 代码式注册（`apply`）** —— 做真正的逻辑

```python
class MediaAnalyzerPlugin(XbcPlugin):
    def apply(self, ctx, config):
        # 注册即资源：每个注册都返回 disposer，内核负责在 deactivate 时释放
        ctx.effect(lambda: ctx.tools.register(
            name="split_shots",
            schema={...},
            handler=self.split_shots,
        ))
        ctx.effect(lambda: ctx.skills.register(
            name="shot-analysis",
            path=self.package_dir / "skills/shot-analysis/SKILL.md",
        ))
```

**为什么两者都要**：

- 声明式能在**不加载代码**的前提下决定要不要加载（开关、排序、纯配置插件）；
- 代码式才能承载逻辑；
- 纯配置插件（如"接一个 MCP 服务器"）**一行代码都不用写**，这是生态能长大的关键。

### 3.5 分层注册表与裁决规则

注册落入哪个层，由**注册时的作用域**决定：

| 层 | 谁注册 | 优先级 |
|---|---|---|
| 内核层 | 内核自带的能力与工具 | 最低（可被覆盖） |
| 插件层 | 已激活插件注册的 Service / Tool / Skill | 中 |
| 用户层 | 用户手工注册的工具/技能、用户配置 | **最高** |

**重名裁决**：

1. 更靠近用户的层获胜（用户层 > 插件层 > 内核层）；
2. 同层内按 `rank`（显式声明的优先级）→ 注册先后顺序；
3. 被遮蔽的注册**必须记日志并可在 `doctor` 中查看**（不能静默吞掉）。

### 3.6 配置方式

**三层配置**：

| 层 | 位置 | 谁改 | 升级时 |
|---|---|---|---|
| L1 默认层 | 插件代码里的 `config_schema.default` | 插件作者 | 随笔更新 |
| L2 宿主层 | `config/plugins.yml`（随内核发布） | 内核维护者 | 被新版本替换 |
| L3 用户层 | `%LOCALAPPDATA%/.../config/plugins.yml` | 用户 | **保留，不覆盖** |

**合并语义：整对象替换，不做深合并**（学 DSH）。

理由：深合并会制造两种难以解释的状态 —— "我明明改了 A，为什么 B 还是旧值"、"升级后我的配置被悄悄改了"。整体替换语义简单、可预测，配合 UI 展示"当前有效配置"即可。

**校验**：激活前用 `config_schema` 校验；不通过 → `FAILED` + 指出**哪个字段、期望什么**。

**密钥处理**：敏感值（API Key 等）**不进插件配置文件、不进插件内存、不进日志**。

- 首选**宿主代持**：密钥存系统凭据库，插件发起需要密钥的请求时，由宿主代理出站（插件只拿到一次性的、限定用途的短期凭据）。
- 次选：`ctx.settings.get_secret(name)` 按需取用，并强制脱敏。
- **明确拒绝** Dify 那种"invoke 时把长期明文凭据下发给插件"的做法 —— 插件一旦被第三方接管或写出日志，密钥就泄漏了。

### 3.7 通信方式

**只有三种，且只要三种**：

| # | 方式 | 方向 | 用途 | 特征 |
|---|---|---|---|---|
| M1 | **能力注入** | 内核 → 插件 | 插件使用内核能力 | 通过 `ctx`，**同步**，声明白名单 |
| M2 | **服务查找** | 插件 ↔ 插件 | 插件使用其他插件的能力 | `ctx.get(service_name)`，需 `inject` 声明 |
| M3 | **事件广播** | 一对多 | 通知（"某事发生了"） | 订阅/发布，**不用于请求-响应** |

**明确不引入**：消息队列、RPC、socket、进程间通信。本地单进程桌面软件用不上，引入只会增加故障面。

**将来需要时的扩展位**：若出现 C++ 插件或不可信插件，M3 之上加一层"进程外传输"，插件接口不变（这正是 MCP 的设计目标）。**这个扩展位现在就留出来，但不实现。**

### 3.8 依赖注入与失败语义

- 插件用 `requires`（清单）与 `inject`（代码）声明依赖；
- **依赖缺失时，插件保持 `INACTIVE` 而不是抛异常**（学 DSH）—— 这样"装了就报错"变成"装了先不用"；
- 依赖在运行中消失 → 停止向该插件派发调用，状态转 `INACTIVE`，原因可查。

### 3.9 版本兼容与 API 契约

| 规则 | 内容 |
|---|---|
| **双版本字段** | 插件声明**两个**版本：`spec_version`（清单规范版本，内核不认识的字段可降级忽略）与 `api_version`（内核 API 契约，主版本必须匹配）。前者保证"新插件在老内核上至少能被读出基本信息"，后者保证"真不兼容时明确拒绝"。（学 Dify 的 `meta.version` + `minimum_dify_version`） |
| 主版本 | `api_version` 主版本号必须与内核一致，否则拒绝加载并说明原因 |
| 向后兼容 | 内核**新增**可选字段/可选钩子 → 不升主版本；插件可声明 `optionalhook` 容忍内核没有该扩展点 |
| 破坏性变更 | 删除字段、改语义、改调用协议 → 主版本 +1，并保留旧版加载路径一个周期 |
| 插件版本 | 插件自己的 `version` 与内核 API 版本**解耦**；用户看到的是插件版本，且**只能有一个真源**（不像 uTools 那样打包与发布各填一个） |
| 错误信封 | 跨边界错误统一为 `{code, message, detail}`，`code` 可分类（契约错 / 配置错 / 依赖缺失 / 运行时错），避免把栈直接抛给用户 |
| 未声明实现检查 | 插件实现了内核不认识的钩子/工具名 → 记警告（学 pluggy 的 `check_pending`） |

### 3.10 安全底线（V1 就必须有的五条）

这五条不是"以后再说"的加固，而是**架构里必须预留的执行点**。它们是三个参考的反面教训换来的：

| # | 底线 | 来源教训 |
|---|---|---|
| S1 | **能力声明 → 执行点校验**。manifest 没声明的能力，在调用点直接拒绝（已实现 `CapabilityDenied`）。 | uTools 没有任何权限清单，等于安装即全权 |
| S2 | **危险操作首次授权**。写文件、执行外部命令、访问网络等，首次使用向用户展示"谁、要做什么、影响什么"并取得同意；可撤销、可在管理页查看。 | Dify/MCP 都强调用户同意；uTools 缺这一层 |
| S3 | **安装与更新绝不静默执行命令**。要展示将要执行的完整内容（脚本、依赖安装、网络请求）并取得显式同意。 | MCP SEP-1024（Final）：一键安装的静默命令执行是重大攻击面 |
| S4 | **密钥由宿主代持**，插件不持有长期明文凭据；日志与错误信息强制脱敏。 | Dify 在 invoke 时把明文凭据下发插件，属已知弱点 |
| S5 | **资源必须限额**：激活超时、单次调用超时、产物大小上限、插件数据目录配额。 | Dify 把限额量化（600s / 50MB / 5MB / 120s）；无限制则失控时无从下手 |

**V1 的实现程度**：S1 完整实现；S4、S5 实现基础版；S2、S3 实现"展示 + 同意"的最小闭环（不做细粒度授权策略）。

---

## 4. Skill / Agent / Plugin 关系

### 4.1 先修正一处概念

需求给出的链条是：**Agent 调用 Skill → Skill 调用 Plugin → Plugin 提供 Capability**。

方向是对的，但有一处必须精确化，否则后面会一直混乱：

> **Capability 是「内核」提供给「插件」的，不是插件提供的。**
> 插件向外提供的是 **Service（给其他插件）** 和 **Tool（给 Agent）**。

### 4.2 四层模型（修正后）

```
        ┌──────────────────────────────────────────────┐
        │  Agent     目标 + 决策：读目录、选 skill、调 tool │  ← 唯一的决策者
        └───────────────┬──────────────────────────────┘
                        │ ① 加载（按名，按需）
                        ▼
        ┌──────────────────────────────────────────────┐
        │  Skill     声明式指令：做什么、按什么顺序      │  ← 不是代码，可热改
        │            （"如何把视频做成解说"）            │
        └───────────────┬──────────────────────────────┘
                        │ ② 指引（skill 告诉 agent：该调哪个 tool）
                        ▼
        ┌──────────────────────────────────────────────┐
        │  Tool      可执行动作 + JSON Schema           │  ← Agent 唯一能"执行"的东西
        │            （split_shots / analyze_shot）      │
        └───────────────▲──────────────────────────────┘
                        │ ③ 注册（插件把自己的动作注册成 tool）
        ┌───────────────┴──────────────────────────────┐
        │  Plugin    代码单元：集成与实现                 │  ← 有生命周期，可装卸
        │            （ffmpeg 调用、Ollama 调用）        │
        └───────────────▲──────────────────────────────┘
                        │ ④ 消费（插件使用内核能力）
        ┌───────────────┴──────────────────────────────┐
        │  Capability  内核提供的公共能力                │  ← 由内核拥有，插件只消费
        │            （files / ai / ffmpeg / settings）  │
        └──────────────────────────────────────────────┘
```

**一句话记忆**：

- **Capability 是内核给插件的**（能力）
- **Tool 是插件给 Agent 的**（动作）
- **Skill 是给 Agent 的说明书**（知识）
- **Agent 是唯一做决定的**（决策）

### 4.3 调用链（一个完整例子）

用户说："帮我把这个视频做成解说"。

1. **Agent** 收到目标，看会话目录，发现有个 skill 叫 `video-recap`；
2. **Agent 调用 `skill` 工具**加载 `video-recap` 的完整指令（只给目录不给正文，是为了省 context）；
3. **Skill 的正文**告诉 Agent：先 `split_shots`，再逐个 `analyze_shot`，最后 `render_recap`；
4. **Agent 依次调用这些 Tool**（每个都有 JSON Schema，参数受校验）；
5. 这些 Tool 由 **`media_analyzer` 插件**注册，插件内部使用 **Capability**（`ffmpeg` 切镜头、`ai` 理解画面）；
6. 结果回到 Agent，Agent 决定下一步或结束。

**关键**：Skill 里**没有代码**，它只是"把 Agent 引导到正确的 Tool 顺序上"。所以改流程 = 改 Markdown，不用发版、不用重启内核。

### 4.4 Skill 的调用策略矩阵（学 DSH）

每个 Skill 带两个布尔策略，决定谁能看到、谁能否加载：

| `modelInvocable` | `userInvocable` | 含义 | 例子 |
|---|---|---|---|
| ✅ | ✅ | 模型能自己发现并加载；用户也能用 `/名字` 直接调用 | 通用工作流 |
| ✅ | ❌ | 只给模型用 | 内部纠错流程，用户不该直接触发 |
| ❌ | ✅ | 只给用户用 | 需要用户明确授权的危险操作 |
| ❌ | ❌ | 都不给 | 临时停用某 skill（保留内容但不生效） |

**为什么要这个矩阵**：不是所有指令都适合模型自主决定加载。危险流程应当由用户显式触发。

### 4.5 边界纪律：什么该做成什么

| 你手上的东西是… | 做成 | 判据 |
|---|---|---|
| 一段**知识/流程**（"先做什么再做什么"） | **Skill** | 纯文本、可热改、不需代码 |
| 一项**能力/集成**（调 FFmpeg、接云端 API） | **Plugin** | 需要代码、需要生命周期、可能失败 |
| 一个**具体可执行动作**（切镜头、生成缩略图） | **Tool** | 需要 JSON Schema，Agent 才能调用 |
| 一个**目标与决策逻辑** | **Agent** | 不写死流程 |
| 一项**所有插件都要用的能力**（文件、配置、AI） | **Capability** | 由内核提供 |

**最容易犯的两个错**：

1. **把能力写成 Skill** —— 写一堆 Markdown 说"你要调用 ffmpeg 并传这些参数"，然后 Agent 每次都得自己拼命令。**这是错的**，应该做成 Tool（代码 + schema）。
2. **把流程写成 Plugin** —— 把"先切镜头再分析再拼接"硬编码进插件。**这也是错的**，流程应该放 Skill，插件只提供原子动作。**流程会变，原子能力相对稳定** —— 让会变的东西待在不用发版的地方。

### 4.6 为什么这样分层能带来演进稳定性

| 变化类型 | 需要动什么 | 需要发版吗 |
|---|---|---|
| 流程调整 | 改 Skill（Markdown） | ❌ |
| 新增一个原子能力 | 改插件 | 插件自己的版本 |
| 换 AI 模型 | 改配置 | ❌ |
| 换 AI 供应商 | 加一个 Provider 实现 | 内核（但插件不动） |
| 新增一个垂直场景 | 新增插件 + skill | ❌ 内核不动 |

**这张表就是"加插件不用改内核"的证明。**

---

## 5. 目录结构设计

### 5.1 源码结构

```
XBC/
├─ src/xbc/
│  ├─ core/                          # 内核：零第三方依赖、不含 Qt、不含业务
│  │  ├─ contract/                   # 契约层
│  │  │  ├─ manifest.py              # 清单解析与校验
│  │  │  ├─ api_version.py           # API 版本契约
│  │  │  ├─ plugin_base.py           # 插件基类（apply / hooks）
│  │  │  └─ hookspec.py              # 钩子契约（学 pluggy）
│  │  ├─ runtime/                    # 运行时
│  │  │  ├─ scope.py                 # 作用域与 effect/disposer（注册即资源）
│  │  │  ├─ registry.py              # 分层注册表 + 裁决
│  │  │  ├─ loader.py                # 发现与模块导入
│  │  │  ├─ lifecycle.py             # 状态机
│  │  │  └─ manager.py               # 插件管理器（对外接口）
│  │  ├─ capabilities/               # 内核提供给插件的能力
│  │  │  ├─ files.py  settings.py  events.py
│  │  │  ├─ ffmpeg.py                # 定位与执行，不含业务参数
│  │  │  └─ ai.py                    # Provider 抽象 + Ollama 实现
│  │  ├─ skills/                     # Skill 注册表
│  │  │  ├─ catalog.py               # 合并多来源、目录生成
│  │  │  ├─ provider_fs.py           # 文件系统 provider
│  │  │  └─ policy.py                # modelInvocable / userInvocable
│  │  ├─ tools/                      # Tool 注册表（Agent 可调用）
│  │  │  ├─ registry.py
│  │  │  └─ schema.py                # JSON Schema 校验
│  │  ├─ config/                     # 三层配置
│  │  │  ├─ layers.py  merge.py  secrets.py
│  │  ├─ diagnostics/                # doctor、状态上报
│  │  └─ paths.py  logging_setup.py  errors.py  context.py
│  ├─ shell/                         # 宿主界面（PySide6）：只消费内核
│  │  ├─ main_window.py
│  │  ├─ plugin_manager_page.py      # 插件管理器（不导入代码即可展示）
│  │  ├─ command_palette.py          # 输入即用：只读清单里的 commands 索引，命中才拉起插件
│  │  └─ theme.py                    # 主题令牌
│  └─ agent/                         # Agent 循环（V1 只留接口，不实现完整 Agent）
│     ├─ loop.py
│     └─ tool_bridge.py              # 把 Tool 注册表暴露给 Agent
├─ plugins/                          # 内置插件
│  ├─ hello_xbc/                     # 已有：插件机制验证
│  └─ media_analyzer/                # MVP：第一个真实插件
├─ config/                           # 宿主层配置（随内核发布）
│  ├─ plugins.yml
│  └─ skills.yml
├─ tools/                            # 开发工具（已有 github_daily_scan.py）
├─ tests/
└─ docs/
```

**分层原则**：`core` 不 import `shell`，`shell` 只 import `core`，`agent` 依赖 `core` 的 Tool 注册表。**依赖方向单向向下**。

### 5.2 运行期数据目录

```
%LOCALAPPDATA%\夏半仓工具箱\
├─ config/
│  ├─ plugins.yml            # 用户层配置（跨版本存活，绝不覆盖）
│  └─ secrets.json           # 敏感值（将来接系统凭据库）
├─ plugins/                  # 用户安装的插件（复制进来即安装，删掉即卸载）
├─ data/
│  └─ <plugin_id>/           # 插件私有数据（内核保证隔离，插件拿不到别人的）
├─ cache/
│  └─ <plugin_id>/
├─ skills/                   # 用户自己的 Skill
└─ logs/
   └─ xbc.log
```

`XBC_HOME` 环境变量可整体重定向（测试与便携版已依赖此机制）。

### 5.3 一个插件的标准结构

见 3.1。**关键约定**：一个目录 = 一个插件，不需要安装程序、不写注册表。这既是 uTools 式的"轻"，也是我们"复制即安装"的实现基础。

### 5.4 内置插件 vs 用户插件

| | 内置插件 | 用户插件 |
|---|---|---|
| 位置 | `<仓库>/plugins/` | `%LOCALAPPDATA%/.../plugins/` |
| 来源 | 随内核发布 | 用户/将来商城 |
| 层 | 插件层 | 插件层（**同层，但用户目录后扫描 → 重名时用户获胜**） |
| 可否卸载 | 可停用，不建议删 | 可删 |
| 信任级别 | 高（随内核发布） | V2+ 需签名 |

---

## 6. 第一阶段 MVP 范围

### 6.1 MVP 目标（一句话）

> **把"内核能装上一个真实插件，Agent 能通过 Skill 引导调用这个插件的 Tool"这条链路完整跑通。**

不是"能装插件"，而是"**插件真的能被用起来**"——这是插件运行时是否成立的唯一证明。

### 6.2 必须做（MVP 清单）

| # | 内容 | 验收要点 |
|---|---|---|
| M1 | **契约层** | 清单 schema 完整（capabilities / tools / skills / config_schema / icon）；API 版本校验；注册时签名校验 |
| M2 | **生命周期** | 六个状态齐全；`deactivate` 幂等；失败隔离；`DISCOVERED` 不导入代码 |
| M3 | **作用域与 effect** | 所有注册返回 disposer；停用后**无残留**（可用测试断言） |
| M4 | **能力层** | files / settings / ffmpeg / ai / events 五个能力可用（前四个已有基础） |
| M5 | **Tool 注册表** | JSON Schema 校验；风险等级标注；向 Agent 暴露统一视图 |
| M6 | **Skill 注册表** | 文件系统 provider；目录只给"名称 + 限长描述"；按需加载正文；调用策略生效 |
| M7 | **三层配置** | 默认 / 宿主 / 用户；整对象替换语义；schema 校验失败给出字段级原因 |
| M8 | **插件管理器 UI** | 列表（未激活也能显示名称/描述/图标）、启用/停用、配置编辑、失败原因展示 |
| M9 | **诊断** | `xbc doctor` 输出：插件状态、失败原因、重载裁决、依赖缺失、能力可用性 |
| M10 | **一个真实插件** | `media_analyzer`：镜头切分 + 抽帧 + AI 理解；贡献 1 个 Skill + 3 个 Tool |

### 6.3 明确不做（MVP 边界）

| 不做 | 原因 |
|---|---|
| 插件商城、支付、授权、会员 | 用户已明确排除；且内核里留了"来源"抽象即可 |
| 云端、账号、用户系统、多租户 | 同上 |
| 插件签名与信任分级 | 等到有第三方插件时再做，现在做是空转 |
| 进程外插件 / MCP 接入 | 留扩展位（见 3.7），但 V1 不实现 |
| 自动更新 | V2+ |
| 完整的 Agent 循环 | V1 只做到"Skill 目录 + Tool 调用"能被验证；自主 Agent 是下一步 |
| UI 插件（插件自定义界面） | V1 用固定槽位 + 插件配置页；自由 UI 插件复杂度高，收益延后 |

### 6.4 MVP 验收标准（可执行）

| # | 验收命令 / 场景 | 期望结果 |
|---|---|---|
| A1 | `xbc plugin list` | 列出 `hello_xbc` 与 `media_analyzer`；**未激活也显示名称/描述/图标** |
| A2 | `xbc plugin disable media_analyzer` → 重启 → `xbc plugin list` | 仍为禁用（用户层配置持久化） |
| A3 | 在用户层配置覆盖 `frame_count=6` | `xbc doctor` 显示有效配置为 6；内核层默认值不变 |
| A4 | 故意让某插件 `apply()` 抛异常 | 该插件 `FAILED` + 可读原因；**其他插件仍 ACTIVE；宿主正常启动** |
| A5 | 插件停用后检查注册表 | 其注册的工具/技能/服务**全部消失**，无残留 |
| A6 | 插件声明依赖不存在的服务 | 该插件保持 `INACTIVE`（**不是报错**），`doctor` 说明缺什么 |
| A7 | 列出 Skill 目录 → 按名加载 → 调用 Tool | 目录只有名称+描述；加载返回正文；Tool 调用成功 |
| A8 | 用错误参数调用 Tool | JSON Schema 校验失败，返回可读错误（不抛栈给用户） |
| A9 | `python -m unittest discover -s tests` | 全部通过（含新增的运行时测试） |
| A10 | `xbc doctor` | 无 error |

### 6.5 MVP 交付物

1. `core/contract` + `core/runtime` + `core/skills` + `core/tools` + `core/config` 五个模块；
2. 插件管理器页面（宿主 UI 第一版）；
3. `plugins/media_analyzer`（第一个真实插件）；
4. 运行时测试套件（作用域释放、失败隔离、配置合并、契约校验）；
5. 本方案的实现对照说明（哪些按设计做了、哪些改了、为什么）。

---

## 7. 风险与开放问题

| # | 风险 | 影响 | 缓解 | 状态 |
|---|---|---|---|---|
| R1 | Python 进程内插件无法真正隔离，插件崩溃可能带崩宿主 | 高 | 边界全捕获 + 关键能力（如 FFmpeg）走子进程；把"进程外插件"作为 V2 扩展位 | 接受 |
| R2 | `config` 整体替换的语义可能让用户困惑 | 中 | UI 展示"当前有效配置 + 每项来自哪一层" | 待验证 |
| R3 | Skill 与 Tool 的边界容易被写糊 | 中 | 4.5 的判据表 + Code Review 检查项 | 待验证 |
| R4 | 分层注册表的重名裁决对用户不透明 | 中 | `doctor` 必须能列出被遮蔽的注册 | 设计已覆盖 |
| R5 | 插件间 Service 的版本兼容 | 中 | `requires` 声明 + 服务名带主版本 | 待细化 |
| R6 | "零成本清单"要求元数据与代码严格分离，可能约束插件表达力 | 低 | 清单只放可静态读取的信息 | 接受 |
| R7 | MVP 范围仍然偏大（10 项） | 中 | 先做 M1–M3 + M10（能跑通即证明），M8/M9 可降级为 CLI | 待确认 |

**需要用户决策的两个开放问题**：

1. **M8 插件管理器 UI 是否必须在 MVP 内？** 若可以先用 CLI，MVP 能显著变小。
2. **media_analyzer 的 AI 部分是否必须跑通？** 若 MVP 只要求"镜头切分 + 抽帧"（纯 FFmpeg，不依赖 Ollama），验收会更稳定、更快。

---

## 8. 附：与现有 XBC 最小内核的差距

| 能力 | 现状 | 本方案要求 | 差距 |
|---|---|---|---|
| 清单与校验 | `manifest.py` 已实现（id/entry/api_version/capabilities） | 增加 tools / skills / config_schema / icon / risk | 扩展字段 |
| 能力注入 | `PluginContext` 白名单已实现 | 增加 events 能力 | 小 |
| 生命周期 | 六状态 + 错误隔离**已实现** | `apply` 语义、`deactivate` 幂等、`DISCOVERED` 不导入 | 中等 |
| 注册即资源 | 未实现 | 作用域 + disposer | **新增** |
| 分层注册表 | 未实现 | 三层 + 裁决规则 | **新增** |
| Tool 注册表 | 未实现 | schema 校验 + Agent 视图 | **新增** |
| Skill 注册表 | 未实现 | provider + 目录 + 按需加载 + 调用策略 | **新增** |
| 配置 | 单文件 JSON + 插件分区**已实现** | 三层 + 整对象替换 + schema 校验 | 中等 |
| 宿主 UI | 最小插件列表**已实现** | 插件管理器页面 + 配置编辑 + 命令面板 | 中等 |
| 诊断 | `xbc env` 环境自检**已实现** | 扩展为插件运行时诊断 | 小 |
| 真实插件 | 只有 `hello_xbc` | `media_analyzer` | **新增** |

**结论**：现有内核已经打下了正确的骨架（契约校验、能力白名单、生命周期、错误隔离、零依赖），本方案是**在骨架上补齐"注册与协作"这一层**，不需要推倒重来。

---

## 附录 A：本方案引用的调研材料

**直接读源码/原始规范（一手材料）**

- **DeepSeek Harness**：其发行包 `app.asar` 内自带的插件开发指南 —— `cordis-plugin-development` 技能（SKILL.md + `references/host-plugin.md`、`practices.md`、`mcp-bundle.md`、`ui-plugin.md`、`verification.md`、`user-actions.md` + `templates/`）、`cordis-composition-reference` 技能、`dsh-skill` 与 `dsh-tool-skill` 包文档、`dsh-agent-preset` 的 skills 目录。
- **pluggy** 源码：`src/pluggy/_manager.py`（`register` / `add_hookspecs` / `_verify_hook` / `check_pending` / `set_blocked` / `load_setuptools_entrypoints` / `subset_hook_caller`）、`_decorators.py`（`tryfirst` / `trylast` / `wrapper` / `hookwrapper` / `optionalhook` / `specname` / `firstresult` / `historic`）、`_execution.py`（`_multicall` 与 firstresult 中止语义）、`_hooks.py`、`_result.py`。本地副本：`F:\Downloads\xbc-refs\pluggy`。
- **MCP** 官方规范原文与 schema（`2025-06-18` 与 `2026-07-28` 两版，含 lifecycle / transports / tools / resources / prompts / sampling / roots / elicitation / versioning / changelog、工具与协议 schema，以及 SEP-1024 与安全最佳实践）。本地材料目录：`mcp-research/`（未入库）。

**委派调研（二手材料，已交叉核对字段）**

- **uTools**：官方开发者文档（plugin.json / 文件结构 / preload / events / window / features / db / 打包发布）与官方 `utools-api-types` 内的 `utools.schema.json`。报告全文：[docs/research/utools-plugin-mechanism.md](research/utools-plugin-mechanism.md)。其中文档与 schema 不一致处（如 `feature.platform`、`dbStorage`）已按"存疑"处理，未做推测性补全。
- **Dify Plugin**：官方文档（插件类型 / manifest 字段 / CLI）与 GitHub 仓库（`langgenius/dify` 的 `api/core/plugin/*`、`api/models/tools.py`、`api/core/helper/encrypter.py`；`dify-plugin-daemon`；`dify-plugin-sdks`）。
