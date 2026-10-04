# 夏半仓工具箱（XBC）

一个**可插件化**的 Windows 本地 AI 工具平台内核。

当前阶段只做一件事：**最小可运行的工具箱内核 + 一个极小的验证插件**，
先把"插件能装、能加载、能启动、能调用公共能力、能卸载"这条链路跑通。

![最小图形宿主：插件列表 + 动作 + 内核日志](docs/shell-preview.png)

---

## 设计要点

| 决策 | 原因 |
|---|---|
| **内核零第三方依赖**（只用标准库） | 内核永远能跑起来，插件机制也能脱离界面被测试 |
| **内核不含 Qt** | 图形界面只是内核的一个消费者；界面写错了也不会影响插件机制 |
| **插件 = 一个文件夹 + `plugin.json`** | 复制进来就是安装，删掉就是卸载，将来插件商城不需要改内核 |
| **能力需要声明** | `plugin.json` 里没写的能力，插件访问时直接报错（最小权限） |
| **插件数据按 id 隔离** | 插件拿不到 `AppPaths`，无法越界写别的插件或内核目录 |
| **单个插件失败被隔离** | 一个坏插件不能拖垮宿主，也不能影响其他插件 |
| **API 版本契约** | `api_version` 主版本号必须与内核一致，内核演进不会静默破坏插件 |

---

## 目录结构

```
XBC/
├─ run.py                    开发期启动脚本（免安装）
├─ pyproject.toml            打包与依赖声明（GUI 依赖为可选）
├─ src/xbc/
│  ├─ version.py             版本号与插件 API 契约
│  ├─ __main__.py            命令行入口（env / list / start / stop / invoke / smoke / ui）
│  ├─ core/                  内核：不依赖任何 GUI 库
│  │  ├─ paths.py            运行期目录的唯一来源（支持 XBC_HOME 重定向）
│  │  ├─ config.py           JSON 配置 + 原子写入 + 插件配置命名空间
│  │  ├─ logging_setup.py    控制台 + 轮转文件日志
│  │  ├─ errors.py           错误类型（含 CapabilityDenied）
│  │  ├─ context.py          AppContext（内核）与 PluginContext（插件受限视图）
│  │  ├─ services/           公共能力：files / ffmpeg / ai
│  │  └─ plugins/            插件机制：manifest / base / manager
│  └─ ui/shell.py            最小图形宿主（PySide6，可选）
├─ plugins/hello_xbc/        最小验证插件
└─ tests/                    内核与插件机制测试（不依赖 PySide6）
```

---

## 快速开始

```bash
# 全流程冒烟测试（推荐先跑这个）
python run.py smoke

# 环境自检：ffmpeg、本地模型、数据目录
python run.py env

# 列出插件
python run.py list

# 调用插件动作
python run.py invoke hello_xbc hello --kwargs "{\"name\": \"夏半仓\"}"

# 图形界面（需要 PySide6）
python run.py ui
```

跑测试：

```bash
python -m unittest discover -s tests -v
```

> `smoke` 的输出里会出现一条 `fail_on_purpose` 的失败记录，那是**故意**的：
> 它用来证明"单个插件抛异常不会影响宿主和其他插件"。

运行期数据默认位于 `%LOCALAPPDATA%\夏半仓工具箱\`：

```
config.json      内核与各插件配置
logs/xbc.log     轮转日志
data/<插件id>/   插件私有数据（互相隔离）
plugins/         用户安装的插件（将来插件商城的落地位置）
```

用环境变量 `XBC_HOME` 可以把整个数据目录搬走（测试与便携版都靠它）。

---

## 开发环境

### 一键健康检查

```bash
python run.py smoke   # 先确认功能链路正常
python run.py env     # 再确认环境是否就绪
```

`env` 输出 `"status": "ok"` 即环境就绪；不可用时返回退出码 1。检查分三级：

| 级别 | 缺失后果 | 检查项 |
|---|---|---|
| `required` | 环境不可用（`status: broken`，退出码 1） | Python ≥ 3.10、数据目录可写、插件目录存在 |
| `recommended` | 降级但可运行（`status: degraded`） | PySide6、git、FFmpeg |
| `optional` | 不影响环境健康 | 本地模型服务（Ollama） |

### 隔离环境（推荐）

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[gui]"
```

之后用 `.venv\Scripts\python run.py smoke` 运行，不依赖系统 Python。
内核本身零第三方依赖，只有图形界面需要 PySide6。

> **工作区的完整性标签（重要，重建 .venv 后必读）**
>
> `F:\Downloads\XBC` 根目录带有 `Mandatory Label\Low Mandatory Level:(OI)(CI)(NW)`，
> 由 DSH 沙箱写入，并且会被**在其中新建的所有文件继承**。后果是：放在工作区里的
> 任何可执行文件都会以 **Low 完整性**运行（实测 `S-1-16-4096`，而系统 Python 是
> `S-1-16-12288`），于是被拒绝写入 `%TEMP%`、用户目录、`C:\Windows\Temp`。
> 表现很隐蔽：`tempfile.gettempdir()` 会静默退化成当前目录，pip / 构建工具
> 则随机报"拒绝访问"。
>
> 因此**重建虚拟环境后需要执行一次**（只作用于 `.venv`，不动工作区本身）：
>
> ```powershell
> icacls .venv /setintegritylevel '(OI)(CI)M' /T /C
> ```
>
> 执行后 `.venv` 内的 Python 恢复 Medium 完整性。**不要**去重置工作区根目录的标签：
> 沙箱在受限模式下依赖它。

---

## 插件怎么写

一个插件就是一个文件夹，最少两个文件：

**`plugins/我的插件/plugin.json`**

```json
{
  "id": "my_plugin",
  "name": "我的插件",
  "version": "0.1.0",
  "api_version": "1.0",
  "entry": "plugin.py:MyPlugin",
  "capabilities": ["files", "ai"]
}
```

**`plugins/我的插件/plugin.py`**

```python
from xbc.core.plugins.base import XbcPlugin, action


class MyPlugin(XbcPlugin):
    def on_load(self):  ...
    def on_start(self): ...
    def on_stop(self):  ...
    def on_unload(self): ...

    @action
    def do_something(self) -> dict:
        # 通过公共能力做事，不要直接 open() / subprocess / requests
        target = self.ctx.files.write_text(self.ctx.data_dir / "out.txt", "内容")
        return {"file": str(target)}
```

可用能力（必须在 `capabilities` 中声明）：

| 能力 | 插件拿到的东西 | 用途 |
|---|---|---|
| `files` | `ctx.files` | 文件读写（统一编码、自动建目录，将来可加审计/沙箱） |
| `ffmpeg` | `ctx.ffmpeg` | 定位并调用 FFmpeg / ffprobe（不含业务参数） |
| `ai` | `ctx.ai` | 调用本地或云端模型（Provider 可替换） |
| `settings` | `ctx.settings` | 插件配置持久化（`config.json` 的 `plugins.<id>`） |

另外始终可用：`ctx.logger`、`ctx.data_dir`、`ctx.cache_dir`、`ctx.core_version`。

---

## 下一步

见 [TASKS.md](TASKS.md) 的任务台账（TASK-002 起）。
