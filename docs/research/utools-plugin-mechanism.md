# uTools 插件机制技术事实报告

依据：uTools 官方开发者文档（u-tools.cn/docs/developer/**）、帮助中心、官方 npm 包 utools-api-types 的 `utools.schema.json`。凡是文档未明确的内容均标注「未确认」。

## 0. 总体模型

uTools 宿主是 Electron 应用，插件应用 = 本地 HTML/CSS/JS 界面 + 可选 `preload.js`（Node 本地能力）。官方定义："Node.js 本地原生能力 + Web 前端网页"。宿主把 `utools` API 注入渲染进程。

## 1. 插件包结构（plugin.json）

必需：`plugin.json`、`logo`，且 `main` 与 `preload` 至少存在其一（官方 JSONSchema 原文）。

| 字段 | 类型/必填 | 含义 |
|---|---|---|
| `main` | string，与 preload 至少其一 | 入口，相对路径，必须是 `.html`；为空即"模板插件应用" |
| `preload` | string，可选 | 窗口加载前执行的 `.js`，可用 Node.js 原生能力与 Electron 渲染进程 API |
| `logo` | string，必填 | 相对路径图标（png/jpg） |
| `features[]` | 必填，≥1 | 指令集合 |
| `feature.code` | 必填、唯一 | 进入插件时回传给插件的功能编码 |
| `feature.explain` / `icon` | 可选 | 描述 / 功能图标（png/jpg/svg，相对路径） |
| `feature.mainPush` | bool | 是否允许向搜索框推送内容 |
| `feature.mainHide` | bool | 触发时不弹出主搜索框 |
| `feature.cmds` | 必填，≥1 | 字符串=功能指令（自动支持拼音/首字母）；对象=匹配指令 `type: regex\|over\|img\|files\|window`，附 `label`、`match`、`minLength`、`maxLength`、`fileType`、`extensions` |
| `pluginSetting.single` | bool，默认 true | 是否单例；多开=分离后再创建 |
| `pluginSetting.height` | number，默认 544 | 初始高度，可用 `utools.setExpendHeight` 动态改 |
| `development.main` | object | 开发态入口，可指向 `http://127.0.0.1:5173/index.html` 以支持 HMR |
| `tools` | object | 暴露给 AI Agent 的工具：键为 snake_case，值含 `description`/`inputSchema`/`outputSchema`；必须在运行时 `utools.registerTool` 注册；纯 AI 无 UI 插件最小配置 = `logo`+`preload`+`tools` |

- **`version` 字段**：官方 JSONSchema 中不存在；打包与发布时各自填写版本号，两处版本号互不关联，遵守 semver（[offline-plugin](https://www.u-tools.cn/docs/developer/basic/offline-plugin.html)、[publish-plugin](https://www.u-tools.cn/docs/developer/basic/publish-plugin.html)）。
- `feature.platform`：动态指令的 `Feature` 类型含 `platform`，文档注明"可参考 plugin.json 中 feature.platform"，但官方 JSONSchema 未列出该字段（文档与 schema 不一致，**存疑**）。
- 目录结构：框架代码编译到 `dist` 后再打包；Node 依赖必须与 `preload.js` 同级且不编译；`preload.js` 禁止打包/压缩/混淆，须逐行可读（反滥用规范）。
- 模板插件应用：删 `main` + 在 preload 中挂 `window.exports = { code: { mode: "none"|"list"|"doc", args: { enter, search, select, placeholder, indexes } } }`。

来源：[plugin.json 核心配置](https://www.u-tools.cn/docs/developer/information/plugin-json.html)、[目录结构](https://www.u-tools.cn/docs/developer/information/file-structure.html)、[模板插件应用](https://www.u-tools.cn/docs/developer/information/window-exports.html)、[schema](https://cdn.jsdelivr.net/npm/utools-api-types/resource/utools.schema.json)

## 2. 发现与加载

- **开发模式（dev）**：在「uTools 开发者工具」插件里新建项目（名称/描述/运行平台/开发者/团队），选择工程目录下的 `plugin.json` 后点击"接入开发"；宿主按 plugin.json 相对路径解析 `logo`/`main`/`preload`。`development.main` 可指向本地 URL 配合 Vite/Webpack HMR；`utools.isDev()` 可判定。`preload.js` 改动不热更新，需开启"退出到后台立即结束运行"。
- **已安装插件**：通过插件应用市场安装，或从 UPXS 离线安装包安装（安装时弹安全提示）。加载的是打包后的静态资源。
- **用户侧开关**（帮助中心）：已安装列表、退出到后台 / 结束运行、Ctrl+D 分离窗口、自动分离为独立窗口、跟随主程序同时启动运行、允许推送内容到搜索面板。逐 feature 的启用/禁用、插件磁盘安装目录官方未公开 → **未确认**。
- 启用/运行状态：Esc 退出后插件仍在后台运行（列表带红色图标可结束运行）。

来源：[调试插件](https://www.u-tools.cn/docs/developer/basic/debug-plugin.html)、[第一个插件](https://www.u-tools.cn/docs/developer/basic/first-plugin.html)、[插件应用界面](https://www.u-tools.cn/docs/guide/plugin-interface.html)、[插件应用市场](https://www.u-tools.cn/docs/guide/plugin-store.html)

## 3. 进程模型

- 插件页面运行在 Electron 渲染进程（主窗口 BrowserWindow、分离窗口 detach、`utools.createBrowserWindow` 创建的 browser 窗口），`utools.getWindowType()` 返回 `main|detach|browser`。
- `preload.js` 在窗口加载前执行，遵循 CommonJS，可 `require` Node 原生模块（示例：`node:fs`、`node:path`、`node:os`、`node:child_process`）、自写模块、第三方 npm 模块（`node_modules` 需与 preload.js 同级），也可 `require("electron")` 使用渲染进程 API（示例：`clipboard`、`nativeImage`、`ipcRenderer`）。
- 运行时版本：preload 文档写 **Node.js 16.x**；FAQ 写 **Chromium 91 + Node.js 14**，两处不一致 → 以实际安装版本为准（**未确认**）。
- 生命周期："结束运行"= 进程结束（`onPluginOut(isKill=true)`），隐藏到后台 ≠ 结束，据此推断各插件运行于独立渲染进程（官方未直述进程隔离细节，**部分未确认**）。主进程 API（`app`/`ipcMain`）未对插件文档化 → **未确认**。
- 官方 preload 示例把对象直接挂到 `window`（`window.customApis`/`window.services`）供页面调用，说明文档假定 preload 与页面共享 window；具体 `webPreferences`（contextIsolation/nodeIntegration）未公开 → **未确认**。

## 4. 宿主 API 注入

宿主把 API 注入为渲染进程全局对象，文档中写作 `window.utools` 或直接 `utools`，preload 与页面代码均可用。API 分类（官方参考目录）：事件、窗口、复制、输入、系统、屏幕、用户、数据存储、动态指令、模拟按键、用户付费、ubrowser、MCP 工具、AI、Sharp、FFmpeg。

代表 API（均为文档原文名称）：
- 事件：`onPluginEnter`、`onPluginOut`、`onMainPush`、`onPluginDetach`、`onDbPull`
- 窗口：`hideMainWindow`、`showMainWindow`、`setExpendHeight`、`setSubInput`/`removeSubInput`/`setSubInputValue`/`subInputFocus`、`outPlugin`、`redirect`、`showOpenDialog`、`showSaveDialog`、`createBrowserWindow`、`sendToParent`、`startDrag`、`findInPage`、`isDarkColors`
- 复制/输入：`copyText`、`copyFile`、`copyImage`、`getCopyedFiles`；`hideMainWindowPasteText`、`…PasteImage`、`…PasteFile`、`hideMainWindowTypeString`
- 系统：`showNotification`、`shellOpenPath`、`shellTrashItem`、`shellShowItemInFolder`、`shellOpenExternal`、`shellBeep`、`getPath`、`getFileIcon`、`getNativeId`、`getAppVersion`、`readCurrentFolderPath`、`readCurrentBrowserUrl`、`isDev/isWindows/isMacOS/isLinux`
- 屏幕：`screenCapture`、`screenColorPick`、`getAllDisplays`、`getPrimaryDisplay`、`getCursorScreenPoint`、`desktopCaptureSources`、DIP↔物理坐标转换
- 模拟按键：`simulateKeyboardTap`、`simulateMouseMove/Click/DoubleClick/RightClick`
- 用户：`getUser`、`fetchUserServerTemporaryToken`
- 数据：`utools.db.{put,get,remove,bulkDocs,allDocs,postAttachment}`（含 `db.promises.*` 异步版；文档 ≤1M、附件 ≤10M、`_id`/`_rev` 版本字段）；另在官方示例中出现 `utools.dbStorage.setItem`，但数据库页正文未给出定义 → **部分未确认**
- 动态指令：`getFeatures`、`setFeature`、`removeFeature`、`redirectHotKeySetting`、`redirectAiModelsSetting`；AI 工具注册 `registerTool`

## 5. 能力与权限模型

- **没有声明式权限清单，也没有运行时授权弹窗**（文档中未见此类机制）。限制手段是：市场人工审核 + preload 代码必须可读不可混淆 + 发布资源清洁要求 + 离线安装时的安全提示。即"安装即全权"。
- preload 能力：任意 Node 原生模块（文件、`child_process`、网络）、任意第三方 npm、Electron 渲染 API、跨域不受限；FAQ 明确插件通常不受跨域影响。
- 网络限制：开发阶段允许访问 `http(s)` 远程资源；发布时禁止直接请求远程 css/js/图片，禁止动态加载运行外部 js（依赖规范与审核，非沙箱强制）。
- 宿主封装能力：文件对话框、shell 打开/回收站/定位、拖出文件、读当前资源管理器路径与浏览器 URL、剪贴板读写与文件列表、截图/取色/录屏源、模拟键鼠、活动窗口信息（`MatchWindow`：`id/class/title/x/y/width/height/appPath/pid/app`）、本地优先 DB（会员可跨设备秒级同步，且禁止写高频/临时数据，违规下架）、内置 ubrowser/AI/Sharp/FFmpeg。
- 平台维度：项目创建时选"运行平台"，动态 feature 有 `platform`，`utools.isWindows()/isMacOS()` 可运行时判定。

来源：[preload](https://www.u-tools.cn/docs/developer/information/preload.html)、[FAQ](https://www.u-tools.cn/docs/developer/faq.html)、[系统](https://www.u-tools.cn/docs/developer/utools-api/system.html)、[屏幕](https://www.u-tools.cn/docs/developer/utools-api/screen.html)、[db](https://www.u-tools.cn/docs/developer/utools-api/db.html)

## 6. 通信方式

- **宿主→插件（回调注册）**：`onPluginEnter(cb)` 收到 `PluginEnterAction{ code, type: "text"|"img"|"file"|"regex"|"over"|"window", payload, from: "main"|"panel"|"hotkey"|"reirect", option }`；`onPluginOut(isKill)`；`onPluginDetach()`；`onDbPull(docs)`；`onMainPush(callback, onSelect)`（需 `mainPush:true`，`onSelect` 返回 `true` 才进 UI，否则静默执行）；`setSubInput(onChange)`、`screenCapture(callback)` 等回调式 API。
- **插件→宿主**：直接调用同步/异步 API（多数返回 boolean/string）；db 提供 `promises` 变体。
- **指令匹配**：宿主在主输入框/超级面板/全局快捷键/重定向触发时按 `cmds` 匹配（文本、正则、任意文本、图片、文件、活动窗口），命中后带 `payload` 调用 `onPluginEnter`。注意文档中 `feature.cmd.type` 写作 `files`，而 `PluginEnterAction.type` 为 `file`（单复数不一致，按原文引用）。
- **窗口间 IPC**：独立窗口→主窗口只能用 `utools.sendToParent(channel, ...args)`；主窗口→独立窗口只能用 `win.webContents.send`；接收统一用 `require("electron").ipcRenderer.on`。
- **插件间跳转**：`utools.redirect(label | [插件名, 指令名], payload)`，目标未安装则跳插件应用市场。
- **运行时能力变更**：`setFeature`/`removeFeature` 动态增删指令，`registerTool` 注册 AI 工具。

来源：[事件](https://www.u-tools.cn/docs/developer/utools-api/events.html)、[窗口](https://www.u-tools.cn/docs/developer/utools-api/window.html)、[动态指令](https://www.u-tools.cn/docs/developer/utools-api/features.html)

## 7. 分发与更新

- **离线安装包**：开发者工具"打包"按钮 → 填版本信息 → 生成 **UPXS** 文件；安装时 uTools 弹安全提示需用户确认；官方定位为测试/内部分享，而非发布渠道。
- **市场发布**：开发者工具"发布" → 确认版本号与发布目录 → 填版本说明/介绍/截图 → 提交审核 → 通过后进入市场（最新上架/最近更新），可在"发布历史"查看审核结果。
- **版本**：打包版本号与发布版本号互不关联；遵守 semver 部分规范；plugin.json 内无 version 字段。
- **更新/卸载**：市场详情页有"版本记录"；帮助中心只说明默认插件"可选择保留或卸载"，自动更新实现与卸载后的数据清理策略官方未描述 → **未确认**。
- **数据**：本地 NoSQL 离线优先，会员可开启同步；同步 DB 只应存用户数据，禁止缓存/日志/剪贴板历史/监听结果等高频临时数据。

来源：[offline-plugin](https://www.u-tools.cn/docs/developer/basic/offline-plugin.html)、[publish-plugin](https://www.u-tools.cn/docs/developer/basic/publish-plugin.html)、[plugin-data](https://www.u-tools.cn/docs/guide/plugin-data.html)

## 对本项目的启示（Windows 本地插件工具箱）

1. **学"声明式指令索引 + 按需拉起"**：把插件能力写成 manifest 里的 `features/cmds`（关键词、正则、文件、窗口匹配），宿主只维护索引，命中才启动插件进程，避免所有插件常驻。
2. **学 preload 桥 + 注入式 API 对象**（`window.utools`）：插件作者只写 Web 代码，能力由宿主统一封装（文件、剪贴板、shell、截图、模拟输入、DB），生态门槛最低。
3. **避免 uTools 的"无权限层"**：它靠 preload 必须可读 + 人工审核 + 安装提示兜底，等于安装即全权；我们应默认 `contextIsolation: true` + 白名单 IPC，对 shell/文件写入/模拟输入/截图做按能力的声明与首次授权，并给出插件级资源配额与超时回收。
4. **显式区分"隐藏到后台"与"结束运行"**：单例/多开、跟随主程序启动、空闲回收、进程残留清理要在设计初期定好，这是效率工具手感的关键，也是我们最该复用 uTools 的部分。
5. **分发链路分层**：开发态走本地目录 + 热更新（`development.main` 指 URL），发布态走签名包与审核；版本号在打包与发布两处解耦；同步存储只存用户数据，避免高频写入拖垮同步与性能。

---

## 调研合规记录

按《调研结果的使用规则》（见 [README](README.md) 与 [TASKS.md](../../TASKS.md)）补齐四栏：

| 项 | 内容 |
|---|---|
| 来源 | uTools 官方开发者文档（u-tools.cn/docs/developer/**）、帮助中心、官方 npm 包 `utools-api-types` 内的 `utools.schema.json` |
| **许可证** | **闭源商业软件** —— uTools 本身不是开源项目；本报告只读了公开文档与官方类型定义，**不存在可采用的代码** |
| 可商业使用 | 不适用（未采用任何代码） |
| **使用方式** | **参考** —— 只参考设计思路，代码自己写 |
| **架构冲突** | **有，且是方向性的**：uTools 宿主是 Electron（Node/Web），插件是本地 HTML/JS + `preload.js`；其 **preload 全权模型**（安装即全权，靠人工审核 + 安装提示兜底）与夏半仓"**能力白名单 + 内核强制风险 + 作用域释放**"的模型**相反**。**未强行整合。** |
| 代码去向 | **未取任何代码** |
| **时效性** | 本报告基于 **2026-10** 抓取的官方文档与 `utools-api-types` 的 schema。uTools 是**闭源商业产品**，无法从仓库判断维护状态；但**本项目未采用任何代码**，因此它的维护状态不影响夏半仓。<br>**未发现需要标注"已过时 / 已放弃"的项。** 一个**未完成**项单独说明：报告中 `features/cmds` 的借鉴点对应清单的 `commands` 字段，当前**只做了声明与校验，匹配逻辑未实现**（排期在界面二期）—— 这是**未完成**，不是放弃。 |

**借鉴点与自写程度**：

| 借鉴点 | 夏半仓落地位置 | 自写程度 |
|---|---|---|
| 声明式指令索引（关键词 / 正则 / 文件 / 窗口匹配），宿主只维护索引、命中才拉起 | 清单 `commands` 字段（当前只做声明与校验，匹配未实现） | 全部自写 |
| 显式区分"隐藏到后台"与"结束运行"、单例 / 多开、空闲回收 | 插件状态机 `DISCOVERED → LOADED → ACTIVE → INACTIVE → UNLOADED` | 全部自写 |
| "避免无权限层"这个教训 | `PluginManifest.capabilities` 白名单 + 能力未声明时抛 `CapabilityDenied` | 全部自写 |

**明确不借鉴**：preload 全权模型、Electron 进程模型、uTools 的 dbStorage 同步存储设计。
