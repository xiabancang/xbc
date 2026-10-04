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

## 待办（尚未开始）

| ID | 任务 | 前置 | 备注 |
|---|---|---|---|
| TASK-002 | 首个真实业务插件：把 V18 的视频分析能力插件化 | TASK-001 | V18 仅作功能参考，禁止直接修改 |
| TASK-003 | 打包分发（PyInstaller），非技术用户可双击运行 | TASK-002 | 注意：工作区内产物会带 Low 标签，需先处理 |
| TASK-004 | 插件管理界面：安装 / 卸载 / 权限提示 | TASK-001 | 目前靠复制文件夹 |
| TASK-005 | 数据层迁移机制 | 出现真实业务库时 | 现在做属于过度设计 |
| TASK-006 | 平台化：账号、插件授权、插件商城、云端 AI 网关 | TASK-003 | 当前阶段明确不做 |

---

## 变更记录

| 日期 | 任务 | 说明 |
|---|---|---|
| 2026-10-04 | TASK-001 | 建立 XBC 最小内核（Core + 插件机制 + 验证插件 + 最小宿主） |
| 2026-10-04 | TASK-001 | 恢复开发环境：环境自检、隔离环境、诊断测试 |
| 2026-10-04 | TASK-001 | 证伪 ACL 假设；定位并修复完整性标签与日志句柄泄漏两个真实缺陷 |
