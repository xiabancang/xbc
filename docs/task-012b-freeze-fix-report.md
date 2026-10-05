# TASK-012b 界面卡死修复 —— 交付报告

| 项 | 内容 |
|---|---|
| 任务 | 修 TASK-012b 素材库页卡死（三个 bug 一起修） |
| 改动 | `src/xbc/ui/bridge.py::BackgroundCall`（一个类） |
| 新增测试 | `tests/test_ui_clicks.py`（**27 项**，六组按钮 + 错误路径） |
| 测试 | **478 项通过**（系统 Python + venv 双环境，exit=0） |
| 规则 | 新增《界面任务：验收必须包含真实点击》+ 补《例外二》 |
| 根因报告 | [task-012b-freeze-root-cause.md](task-012b-freeze-root-cause.md)（A 类前置，未改代码） |

---

## 0. 验收对照

| # | 标准 | 结果 |
|---|---|---|
| 1 | 点「检索」→ **回调真的回来了**（不再停在"正在执行…"） | ✅ |
| 2 | 点击后页面**恢复 `isEnabled()`** | ✅ 41ms |
| 3 | 结果**真的渲染进表格** | ✅ |
| 4 | 回调**在主线程执行** | ✅ 断言 `MainThread` |
| 5 | `job` 在任务完成前**未被回收** | ✅ weakref + `job_alive` 断言 |
| 6 | **进程能干净退出**（不再有 QThread 警告） | ✅ 子进程断言 + 双环境 exit=0 |
| 7 | 已有测试**不退化** | ✅ 451 → **478** 全过 |
| + | **错误路径**：工具 `ok=False` → 回调仍执行 → 页面 re-enable | ✅ 1 项单元 + 3 项页面级 |

---

## 1. 三个修复

全在 `src/xbc/ui/bridge.py::BackgroundCall` 一个类里。

### 修复 1 —— `self._job = job`（防 GC）

```python
self._job = job          # 新增
```

`job.moveToThread(thread)` **只改 C++ 线程亲和性，不增加 Python 引用**。
写成局部变量，`start()` 一返回引用计数就归零 → C++ 对象销毁 →
`thread.started → job.run` 指向死对象 → **任务静默不执行**；
而 `thread.quit` 也连在 `job.done` 上 → QThread 永不退出。

### 修复 2 —— `job.done.connect(self._relay)`（回调回主线程）

```python
job.done.connect(self._relay)        # 不再是 job.done.connect(on_done)
```

直接把用户的闭包连到 `job.done` 会走 **`DirectConnection`** ——
信号在哪个线程发，回调就在哪个线程跑。而回调要碰控件（`setEnabled` / 往表格写数据）。

`self` 是在**主线程构造的 `QObject`**，连到它的槽上，Qt 自动用
`QueuedConnection` 投递到主线程：

```python
def _relay(self, result):
    callback, self._on_done = self._on_done, None
    if callback is not None:
        callback(result)
```

### 修复 3 —— `thread.finished` / `self._thread` 的引用冲突

> **按要求单列说明，不写成"顺手"。**

原来是这样：

```python
thread.finished.connect(thread.deleteLater)   # C++ QThread 被删
self._thread = thread                         # Python 侧还指着它
```

改成：

```python
thread.finished.connect(self._on_thread_finished)   # 不再 deleteLater

def _on_thread_finished(self):
    self._job = None
    self._thread = None
```

#### 为什么它算范围内 —— 两条依据

**依据一：与本次修复同一类根因（例外二第一条）**

本次卡死的根因就是 **"Python 引用与 C++ 对象生命周期不一致"**：

| | 问题 | 方向 |
|---|---|---|
| Bug 1 | `job` 的 Python 引用**太短** → C++ 对象提前死 | 引用不足 |
| **第三处** | `self._thread` 的 Python 引用**太长** → C++ 对象死了 Python 还指着 | 引用过长 |

**同一个函数、同一类根因、同一个修复动作。** 不修，就是统一了 Bug 1 却留下它的镜像。

**依据二：本次修复会让它的触发路径从「走不到」变成「走得到」（例外二第二条）**

- **修复前**：QThread 因为 Bug 1 **永远不结束** → `thread.finished` **从不发出** →
  `deleteLater` 从不执行 → `self._thread` 指向的对象一直活着 → **第三处从没被走到**
- **修复后**：线程**真的会结束**了 → `finished` 发出 → `deleteLater` 删掉 C++ 对象 →
  `self._thread` 立刻变成悬空引用 → **`running` / `wait()` 一访问就撞**

而 `running` 是公开属性、`wait()` 是公开方法，`Page` 侧随时可能调用。

**它不修的话会怎么发作**：线程正常跑完之后，任何一次
`BackgroundCall.running` 或 `.wait()` 都会碰到已销毁的 C++ 对象，
轻则 `RuntimeError`，重则崩进程 —— 而且是在"看起来已经修好了"之后才出现。

**结论**：它不是我顺手看到的东西，是**这次修复的直接后果**。
不一起修，等于把 Bug 1 换成一个更隐蔽的 Bug。

---

## 2. 真机路径实测（修复前后对照）

**真 bridge + 真插件 + `QTest.mouseClick`** —— 走的就是你点鼠标那条路。

### 修复前（你那次运行的指纹）

```
点「检索」→ _CallJob.run 从没执行
             enabled = False                    ← 永久禁用
             状态卡在 '正在执行 library_search_semantic …'
             表格 0 行
关闭窗口 → QThread: Destroyed while thread '' is still running
           [exit code: 1]
```

### 修复后

```
切到素材库: enabled=True  状态='概况已刷新'

--- QTest.mouseClick 点「检索」 ---
  点击瞬间: enabled=False        ← 忙碌（正常）
  恢复启用: True   耗时 41 ms
  最终 enabled=True
  状态: '「测试查询」命中 0 个镜头（semantic）　库里还没有向量：先跑 library_scan 入库'
  状态栏还停在 '正在执行…' 吗: False

完成（无 QThread 警告）
exit=0
```

**注意状态栏那一行**：它是**真工具算出来的结果**（"库里还没有向量：先跑 library_scan 入库"），
不是卡住的占位文字 —— 这证明整条链路真的走通了。

---

## 3. 新增测试 `tests/test_ui_clicks.py`（27 项）

### 六组按钮（全部用 `QTest.mouseClick`，不是 `.click()`）

| 组 | 覆盖 | 项数 |
|---|---|---|
| 1 主界面导航 | 左侧 4 项逐项点 + **事件循环不被冻住**（定时器还在跳） | 3 |
| 2 工作台 | 「刷新」+ 三个跳转按钮 | 2 |
| 3 素材库 | 「刷新概况」「检索」「标签检索」「导入素材」「浏览…」（桩） | 7 |
| 4 文案匹配 | 「开始匹配」「刷新列表」「读取」「设为首选」「上移」「下移」 | 6 |
| 5 插件中心 | 插件选中 + 「启用」「停用」「刷新」 | 2 |
| 6 `BackgroundCall` | 线程/主线程/引用生命周期/干净退出 | 4 |

> **你点名的三组都在**：素材库的「导入素材」「刷新概况」「检索」各有独立用例
> （其中「检索」还分语义 / 标签两种模式），其余三组是额外覆盖。

### ⚠️ 「浏览…」**没有做真实点击** —— 这是本次测试的局限

`test_browse_button_fills_the_field` 用的是**桩**：

```python
QFileDialog.getExistingDirectory = staticmethod(lambda *a, **k: "D:/picked")
click(self.page.browse_button)          # 点击是真的，但对话框被替换掉了
```

**为什么必须用桩**：`QFileDialog.getExistingDirectory` 是**原生模态对话框** ——
在无人值守的测试里它没人去关，会**永久阻塞**整个测试进程。

**因此这条用例只证明了**：按钮连对了、点得动、返回值被填进了输入框。
**它没有证明**："真机上弹出原生对话框"这一段不出问题。

**这条 bug 也不在它身上**：卡死的两个按钮是「导入素材」与「检索」（走 `background()`），
而「浏览…」**不调用任何工具**，也就没有线程调度那一层。但**"没测过"和"没问题"是两回事**，
如实记在这里。

（真机上手点一次即可补上这一段 —— 留给你在实机上确认。）

### 回归锁（直接锁住本次 bug）

每个走后台的按钮点完都断言三件事：

```python
self.assertFalse(page.isEnabled(), "点击后应立刻禁用整页")
self.assertTrue(wait_for(lambda: page.isEnabled(), 4000),
                "回调没回来 —— 页面永久禁用（TASK-012b 的原症状）")
self.assertEqual(page.table.rowCount(), 2, "结果必须真的渲染进表格")
```

还有一条**反向断言**：工具调用**必须不在主线程**跑 ——

```python
self.assertNotEqual(bridge.threads[0], "MainThread",
                    "工具调用跑到主线程 = 会冻界面")
```

### 错误路径（4 项，按你要求新增）

| 层 | 用例 |
|---|---|
| 单元 | `test_failed_tool_still_reaches_the_callback` —— 工具 `ok=False` 时回调**仍然**执行 |
| 页面 | 检索失败 / 导入失败 / 匹配失败 → **页面都恢复启用**，且**工具的原文显示出来**（不吞） |

> 依据：`ToolRegistry.call` 的契约是"**任何失败都返回 ToolResult，不抛异常**"
> （`registry.py:154`）。所以回调一定能拿到结果；**"失败"绝不允许变成"卡死"**。

### 干净退出（验收 6）用一个子进程断言

```python
self.assertNotIn("QThread: Destroyed while thread", completed.stderr,
                 "线程没收干净 —— 退出时会卡")
```

---

## 4. 两条规则

### 新增《界面任务：验收必须包含真实点击》

写进 [docs/harness-rules.md](harness-rules.md)：

> 界面任务的验收**不许**只用"按钮背后的同步方法"。
> 必须至少有一条 `QTest.mouseClick`，覆盖**每一个会改变状态的按钮**，断言：
> **回调真的回来了 / 控件状态真的恢复了 / 界面上真的能看到变化**。

理由写在规则里：`button.click()` 与同步方法**都绕过**「信号连接」和「线程调度」两层，
而"点了没反应""点完就卡"这类 bug **只活在这两层里**。

**还写进了一个实测到的坑**：等待不要用 `QTest.qWait` ——
它与运行中的 `QThread` 同用会让进程**直接崩**（`0xC0000409`）。
要用 `QEventLoop` 嵌套循环；参考实现见测试里的 `pump()` / `wait_for()`。

### 《例外二：同函数内、与本次修复「同源」的已知缺陷》

补进《任务边界：禁止"顺手"》的例外条款，条件收紧到两条（满足任意一条）：

1. 与本次修复**同一类根因**；
2. **本次修复会让它的触发路径从"走不到"变成"走得到"**。

且必须在报告里单列同源关系与"不修会怎么发作"。**不满足的一律走《发现但未做》。**

---

## 5. 发现但未做

| 发现了什么 | 为什么觉得该改 | 为什么这次不做 |
|---|---|---|
| **一次未能复现的崩溃**：改测试夹具途中，`python -m unittest tests.test_ui_clicks` 出现 `exit=-1073741819`（0xC0000005 访问违例），日志 0 行。之后**连跑 3 次全绿**（exit=0），全量也稳定 | 访问违例是硬崩溃，不能因为"复现不了"就当作没发生 | 无法定位、无法写出稳定复现的用例；**本次修复范围是三个 bug**。如实记录，若再出现按新证据排查 |
| `require()` 只查"工具是否注册"，不查"能力是否可用" | 模型缺失（图片嵌入不可用）时按钮照样可点，只给一句 note | 属交互设计改进，与卡死无关。A 类报告已单列 |
| `library_status` 每次调 `_library()` **两次**（各一次 connect + `executescript(DDL)` + migrate），实测 ~40ms | 纯浪费 | 不影响本 bug，属性能优化 |
| 长任务不可取消（后台跑起来只能等） | 用户体验 | TASK-012b 报告已记为局限 |
| `background()` 期间导航仍可切走，切回可见禁用页 | 交互一致性 | 不影响本 bug |
| TASK-012b 历史报告里"没有真的点过鼠标"那条**措辞太轻** | 实际后果是用户点第一下就卡死 | **不改历史记录**；新规则从今往后生效 |
| **「浏览…」没有做真实点击**（§3 已展开） | `QFileDialog.getExistingDirectory` 是原生模态对话框，测试里会永久阻塞，只能用桩替换 | 桩只证明"按钮连对了、返回值填对了"，**没有证明"真机弹对话框"这一段**。卡死不在它身上（它不调工具），但"没测过 ≠ 没问题"。真机手点一次即可补上 |

---

## 6. 复现

```powershell
python run.py ui
# → 素材库 → 填素材目录 → 点「导入素材」或「检索」
# → 页面短暂禁用后**恢复**，状态栏显示工具结果（不再卡死）
```

```powershell
# 只跑真实点击测试
python -m unittest tests.test_ui_clicks

# 全量（需要 QT_QPA_PLATFORM=offscreen 才不弹窗）
$env:QT_QPA_PLATFORM='offscreen'; python -m unittest discover -s tests
```

---

## 7. 说明

- **UI 实例已停**：我上一轮为你启动的那个（PID 21216）**已在上一轮自行退出**
  —— 退出信息正是本次 bug 的指纹（`QThread: Destroyed while thread '' is still running`
  + `exit code 1`）。当前系统里已无 XBC 界面进程。
- **Core 零改动**，只动了 `src/xbc/ui/bridge.py` 一个类。
