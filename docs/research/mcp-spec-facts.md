# MCP（Model Context Protocol）技术事实报告

**关键前提**：官方当前规范是 **2026-07-28**（[Versioning](https://modelcontextprotocol.io/specification/versioning)），这是一次破坏性重构。提问中假设的 `initialize` 握手与版本协商属于 **2025-11-25 及更早**的 legacy 行为，必须分开陈述。

## 1. 协议基础

JSON-RPC 2.0：请求 `{jsonrpc,id,method,params?}`、结果 `{jsonrpc,id,result}`、错误 `{jsonrpc,id,error:{code,message,data?}}`、通知无 `id`。MCP 追加两条约束：`id` 不得为 `null`；`result` 必须含 `resultType`（`complete` / `input_required`，缺省按 `complete`）。错误码用标准 `-32700`、`-32600`~`-32603`，另定义 `-32020` HeaderMismatch、`-32021` MissingRequiredClientCapability、`-32022` UnsupportedProtocolVersion（[Basic](https://modelcontextprotocol.io/specification/2026-07-28/basic)）。

传输：标准传输只有 **stdio** 与 **Streamable HTTP**，可自定义。stdio 由宿主拉起子进程，stdin/stdout 逐行 JSON-RPC，stdout 不得混入非 MCP 消息，stderr 可记日志。Streamable HTTP 用单一 MCP endpoint 收发 POST/GET，POST 需 `Accept: application/json, text/event-stream`，服务端可回单个 JSON 或 SSE 流。2026-07-28 删除了 session（`Mcp-Session-Id`）、GET 流、SSE 断点续传（`Last-Event-ID`）与 `resources/subscribe`，改为 `subscriptions/listen` 长连接 POST 流，并要求 `Mcp-Method`/`Mcp-Name` 头与 `Origin` 校验（[stdio](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/stdio)、[Streamable HTTP](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http)）。历史：2024-11-05 的 HTTP+SSE 自 2025-03-26 起弃用并由 Streamable HTTP 取代（[旧版 Transports](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports)）。

生命周期：2026-07-28 起**无握手、无连接状态**，每个请求在 `_meta` 携带 `io.modelcontextprotocol/protocolVersion` 与 `clientCapabilities`（必填）、`clientInfo`（SHOULD），版本不符返回 `-32022`；强制 RPC `server/discover` 返回 `supportedVersions`、`capabilities`（[server/discover](https://modelcontextprotocol.io/specification/2026-07-28/server/discover)）。legacy（≤2025-11-25）流程为 `initialize` → result → `notifications/initialized` → 操作 → 关闭；协商规则是客户端报其支持的最新版本，服务端可回自己支持的版本，客户端无法支持则断开（[legacy Lifecycle](https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle)）。

## 2. 服务端原语

- **tools**：`tools/list`（`cursor` 分页，`ttlMs`/`cacheScope` 缓存字段）与 `tools/call{name,arguments}`；返回 `content[]`（text / image / audio / resource_link / resource）+ 可选 `structuredContent` + `isError`。错误二分：找不到工具、请求不合 schema → JSON-RPC 协议错误；工具自身失败（API 失败、输入校验失败、业务错误）→ 结果内 `isError:true`，便于模型自纠；2026-07-28 还可返回 `resultType:"input_required"`（MRTR）。
- **resources**：`resources/list`、`resources/templates/list`、`resources/read`；以 URI 标识（`https://`、`file://`、`git://` 或自定义），内容为 `text` 或 base64 `blob`；不存在返回 `-32602`（旧版 `-32002`）。
- **prompts**：`prompts/list`、`prompts/get`；由用户显式触发（典型为斜杠命令），参数以 `{name,description,required}` 声明。

## 3. 客户端原语

`elicitation` 仍活跃：`elicitation/create` 向用户补信息，form 模式用受限的扁平 `requestedSchema`，url 模式让敏感交互不经 MCP 客户端；规范禁止用 form 模式索取密码、API key、支付凭证。`roots`（告知相关目录/文件，仅为提示、非访问控制）与 `sampling`（服务端借客户端 LLM 生成）在 2026-07-28 **已弃用**。三者现统一走 MRTR：服务端返回 `InputRequiredResult.inputRequests`，客户端以 `inputResponses` 重试原请求，重试的 `id` 必须不同。

## 4. 能力协商

legacy 在 `initialize` 交换双方 capabilities；modern 由每个请求声明客户端能力、由 `server/discover` 公布服务端能力。依赖关系：声明 `tools`/`resources`/`prompts` 才能被对应 list；`listChanged` 决定是否发变更通知；`resources.subscribe` 决定资源级订阅；客户端声明 `roots`/`sampling`/`elicitation` 后服务端才可发起相应请求，`elicitation` 还需细分 form/url。服务端不得依赖客户端未声明的能力，否则返回 `-32021`。扩展经 `capabilities.extensions` 协商。

## 5. 工具描述

`name`（建议 1–128 字符、仅 `A-Za-z0-9_.-`、服务器内唯一；跨服务器同名由客户端加前缀消歧）、`title`、`description`、`icons`、`inputSchema`（根必须 `type:"object"`，默认 JSON Schema 2020-12，可用 `$ref` 与组合关键字；无参数推荐 `{type:"object",additionalProperties:false}`）、`outputSchema`、`annotations`、`_meta`。`annotations` 全为 **hint**：`readOnlyHint`（默认 false）、`destructiveHint`（默认 true）、`idempotentHint`（默认 false）、`openWorldHint`（默认 true）、`title`；用于审批分级与展示，官方明确来自不可信服务器的 annotations 不得作为工具使用决策依据。

## 6. 安全模型

Host 必须获得用户明确同意才能外传数据、才能调用任何工具；工具即任意代码执行。官方最佳实践覆盖：confused deputy、token passthrough（禁收非本服务签发的 token）、SSRF（OAuth 发现 URL 须挡私网与云元数据地址）、state handle hijacking、本地 MCP 服务器被攻陷（一键安装须不截断地展示待执行命令并取得显式同意，SEP-1024）、授权 URL 仅允许 http(s) 且禁止用 shell 打开、stdio 代理提权、mix-up、localhost 重定向冒充。工具描述注入：规范仅要求把工具描述与 annotations 视为不可信，2026-07-28 的 Security Best Practices 未设专章，是否存在独立的规范级章节**未确认**（[Security Best Practices](https://modelcontextprotocol.io/docs/2026-07-28/tutorials/security/security_best_practices)、[SEP-1024](https://modelcontextprotocol.io/seps/1024-mcp-client-security-requirements-for-local-server-)）。

## 7. 版本与生态

修订序列：2024-11-05 → 2025-03-26 → 2025-06-18 → 2025-11-25 → **2026-07-28（current）**，另有 draft；日期版本号仅在破坏性变更时递增。特性可标记 Deprecated（至少 12 个月窗口），当前 Roots/Sampling/Logging/HTTP+SSE/动态客户端注册已弃用，尚无 Removed。官方 SDK 共 10 个（TS/Python/C#/Go/Rust/Ruby 为 Tier 1，Java Tier 2，Swift/PHP/Kotlin Tier 3）；官方 Registry 仍为 preview，我用其公开 API 分页实测 ≥20,000 条版本记录未跑完（下界），服务端实现量级 10^4；客户端总数官方无公开计数（**未确认**），官方文档点名 Claude、ChatGPT、VS Code、Cursor、MCPJam（[SDKs](https://modelcontextprotocol.io/docs/2026-07-28/sdk)、[Registry](https://modelcontextprotocol.io/registry/about)）。

## 对本项目的启示

我们做的是本地桌面插件工具箱，插件是本地 Python 包，不是网络服务。

1. **借鉴工具契约**：每个插件用 JSON Schema 声明参数与结构化输出，并配 annotations 做审批分级；但 annotations 只作 UI 提示，绝不作安全判定。
2. **借鉴能力协商，降级为加载时清单声明**：插件声明网络、文件根、用户输入、耗时等需求，宿主不支持即拒绝并给结构化错误码；不需要 per-request `_meta` 那套无状态协商。
3. **借鉴错误二分与结果契约**：把"发现/参数错误"与"执行失败"分开（后者类 `isError`），让模型能自纠；输出用 schema 校验。
4. **借鉴 SEP-1024 与最小权限**：安装本地包等同代码执行，必须展示包名/版本/入口/权限并显式同意；运行时按 `destructiveHint` 分级确认，并沙箱化、限制文件与网络访问。
5. **明确不适用**：stdio/Streamable HTTP/SSE、session、OAuth、Origin 校验、DNS rebinding 防护等传输与网络层全部不需要；roots/sampling 官方已弃用，宿主自带 LLM 即可，无需自建反向上游通道。

## 参考来源

- 规范总览与版本：https://modelcontextprotocol.io/specification/2026-07-28 、https://modelcontextprotocol.io/specification/versioning
- 关键变更（2026-07-28）：https://modelcontextprotocol.io/specification/2026-07-28/changelog
- 基础协议与 `_meta`：https://modelcontextprotocol.io/specification/2026-07-28/basic
- 传输：https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/stdio 、…/streamable-http ；旧版：https://modelcontextprotocol.io/specification/2025-06-18/basic/transports
- 生命周期（legacy 握手与版本协商）：https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle
- 服务端原语：https://modelcontextprotocol.io/specification/2026-07-28/server/tools 、…/resources 、…/prompts
- 客户端原语：https://modelcontextprotocol.io/specification/2026-07-28/client/elicitation 、…/sampling 、…/roots
- 发现：https://modelcontextprotocol.io/specification/2026-07-28/server/discover
- 安全：https://modelcontextprotocol.io/docs/2026-07-28/tutorials/security/security_best_practices 、https://modelcontextprotocol.io/seps/1024-mcp-client-security-requirements-for-local-server-
- 权威 schema（TypeScript 为准）：https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/schema/2026-07-28/schema.ts
- 生态：https://modelcontextprotocol.io/docs/2026-07-28/sdk 、https://modelcontextprotocol.io/registry/about （API：https://registry.modelcontextprotocol.io/v0/servers ）

---

## 调研合规记录

按《调研结果的使用规则》（见 [README](README.md) 与 [TASKS.md](../../TASKS.md)）补齐四栏：

| 项 | 内容 |
|---|---|
| 来源 | MCP 官方规范原文（`modelcontextprotocol.io/specification/**`，覆盖 `2024-11-05` 至 `2026-07-28`）、权威 schema `schema/2026-07-28/schema.ts`、SEP-1024 与安全最佳实践 |
| **许可证** | **NOASSERTION** —— 规范仓库 `modelcontextprotocol/modelcontextprotocol` 被 GitHub 归类为非标准许可（`license.spdx_id = "NOASSERTION"`）。本报告**只读了公开规范文本，未取任何代码**；将来若要采用其代码，**必须先逐条读 `LICENSE`** |
| 可商业使用 | 不适用（本报告不涉及采用）；**采用前须另行判定** |
| **使用方式** | **参考** —— 只读公开规范，代码自己写 |
| **架构冲突** | **有，且是结构性的**：MCP 是**客户端-服务端进程协议** —— 宿主拉起 stdio 子进程、`server/discover` 握手、独立的传输层与会话生命周期；夏半仓是**同进程插件 + 作用域隔离**。信任模型（跨进程边界 vs 同进程能力白名单）与生命周期（独立进程 vs 作用域释放）**都不同**。**未强行整合。** |
| 代码去向 | **未取任何代码** |

**借鉴点与自写程度**：

| 借鉴点 | 夏半仓落地位置 | 自写程度 |
|---|---|---|
| 工具的 `inputSchema` 形态；`annotations` 只作提示 | `src/xbc/core/tools/registry.py` 的 `ToolSpec` | 全部自写 |
| `elicitation` —— 需要用户补信息时向用户提问 | 工具授权回调 `approver(tool_name, risk, arguments)` | 全部自写 |
| **安全教训**：`annotations` 是提示，**不得**用于安全决策 | 风险由内核按 `risk` 字段强制，不信插件自报 | 全部自写 |
| SEP-1024：本地安装需用户知情同意、命令原文不得截断 | 插件安装流程与 `doctor` 的输出 | 全部自写 |

**明确不整合**：跨进程 stdio 传输、`server/discover` 握手、以及 MCP 的会话式生命周期
（夏半仓的插件生命周期是自己的状态机，不是 MCP 的会话模型）。
