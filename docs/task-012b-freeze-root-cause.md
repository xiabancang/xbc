# TASK-012b 界面卡死 —— 根因报告（A 类前置）

| 项 | 内容 |
|---|---|
| 类型 | **A 类前置：先报告，不改代码**（本文档是本次唯一产出） |
| 现象 | 素材库页正常显示 → 点某按钮 → 界面冻住（未响应）→ 只能强杀 |
| **触发按钮** | **「导入素材」** 或 **「检索」**（只有这两个走后台路径） |
| 根因 | `src/xbc/ui/bridge.py::BackgroundCall.start()` 两个 bug 叠加 |
| **你的那次运行的指纹** | `QThread: Destroyed while thread '' is still running` + `exit code 1` |
| 为什么 15 项测试全绿 | 测试调的是按钮的**同步方法**，**从没走 `background()`** |

---

## 0. 逐项回答（a — e）

### a. 素材库四个按钮，分别调用什么？

| 按钮 | 处理函数 | 调用什么 |
|---|---|---|
| **浏览…** | `on_browse` | **不调工具**。`QFileDialog.getExistingDirectory()` 开原生模态对话框，只把选中的路径填回输入框 |
| **导入素材** | `on_scan` | 工具 **`library_scan`**（参数 `directory`） |
| **刷新概况** | `on_refresh_clicked` → `show_status()` | 工具 **`library_status`** |
| **检索** | `on_search` | 工具 **`library_search_semantic`** 或 **`library_search_labels`**（按下拉框选） |

### b. 哪些走后台线程、哪些同步？

| 按钮 | 路径 | 阻塞主线程？ |
|---|---|---|
| 浏览… | **模态对话框**（嵌套事件循环） | 阻塞调用线程，但窗口仍响应 —— **这是设计行为** |
| 导入素材 | `self.background("library_scan", …)` → **后台** | ❌ 不阻塞 |
| 刷新概况 | `self.show_status()` → **同步** | ✅ 阻塞，但实测 **~40ms** |
| 检索 | `self.background(tool, …)` → **后台** | ❌ 不阻塞 |

> **反直觉但证据确凿**：卡死的是**走后台的两个**，不是同步那个。
> 同步的 `library_status` 只有 40 毫秒，**排除**。

### c. "图片嵌入不可用"时，页面加载 / 按钮点击会不会触发模型加载或等待？

**不会。三层都验证了：**

| 层 | 代码 | 结论 |
|---|---|---|
| Core 路由 | `AIService.provider(capability=…)` 只按 **`supports()`** 过滤（`service.py:145`），**不调 `available()`** | 不探测、不联网 |
| Provider | `ChineseClipProvider.supports()` 未配置时返回 `False`，只做 **3 次 `is_file()`** | 便宜，不加载模型 |
| 插件 | `_image_capability_status()` 的不可用分支**直接 return**，连 `describe()` 都不调 | 纯返回 |

**实测**：`library_status` 冷/热都是 **~40ms**；那份 `available=False` 的 UI 实例
`Responding=True`、CPU 7.9s。**这条路径不阻塞。排除。**

### d. 后台线程启动路径是否正确？

**不正确 —— 两个 bug。** 但要说清两件事：

| 问题 | 答案 |
|---|---|
| QThread 真的启动了吗？ | ✅ **启动了**（`thread.start()` 生效） |
| 线程里的任务执行了吗？ | ❌ **一次都没执行** |
| 阻塞主线程了吗？ | ❌ **没有** —— 是"页面被永久禁用"，不是主线程被占住 |

**真实点击 + 真实工具**（不是假 bridge）的实测：

```
切到素材库后: enabled=True  状态='概况已刷新'        ← 正常
点「刷新概况」  → 状态不变（同步，正常）
点「检索」      → 状态 -> '请输入检索词'            ← 前置校验，正常
点「检索」（带词）→ _CallJob.run 从没执行
                  enabled = False                   ← 永久禁用
                  状态卡在 '正在执行 library_search_semantic …'
                  表格 0 行
```

### e. 模型缺失时，界面有优雅降级吗？

**工具层有；界面层只有一半。** 分开说：

| 层 | 表现 | 评价 |
|---|---|---|
| **工具层** | TASK-010 的"不声明能力"逻辑生效：CLIP 未配置 → `supports()` 返回 `False` → 路由**不会选到它**；`library_status` 返回 `image_embedding.available=False` + 获取方式的 hint | ✅ **正确降级** |
| **界面展示** | 素材库页如实显示"图片嵌入：**不可用（图片检索会降级）**"；检索查不到东西时把工具的 `note` 原样显示 | ✅ 如实 |
| **界面控制** | `require()` 只检查"**工具是否注册**"（= 插件启没启用），**不检查"能力是否可用"** → 模型缺失时按钮**照样可点** | ⚠️ **半个降级**：点了会给一句 note，但界面上**没有提前说明"图片检索这条路不可用"** |

**结论**：降级本身是好的（与 TASK-010 的"不声明能力"一致），
**但它跟这次卡死无关** —— 卡死发生在 `background()` 那一层，与模型可用性完全不相干。

---

## 1. 根因

`src/xbc/ui/bridge.py::BackgroundCall.start()` 有**两个 bug**，必须一起修：

```python
def start(self, on_done):
    thread = QThread()
    job = _CallJob(self._bridge, self._tool, self._arguments)   # ← bug 1：局部变量
    job.moveToThread(thread)                                     #    moveToThread 不转移
    thread.started.connect(job.run)                              #    Python 所有权
    job.done.connect(on_done)                                    # ← bug 2：闭包不是槽
    job.done.connect(thread.quit)
    job.done.connect(job.deleteLater)
    thread.finished.connect(thread.deleteLater)
    self._thread = thread                                        #    只保住了 thread
    thread.start()
```

### Bug 1 —— `job` 被 Python 回收 → 任务静默不执行 → QThread 空转

`job` 是 `start()` 的**局部变量**。`job.moveToThread(thread)` 只改 **C++ 线程亲和性**，
**不增加 Python 引用**。`start()` 返回的瞬间，CPython 引用计数归零 → 立即回收 →
底层 C++ `_CallJob` 被销毁 → `thread.started → job.run` 指向已销毁对象 → **静默不执行**。

`self._thread` 保住了 thread，`self._call` 保住了 BackgroundCall ——
**没有一处保住 `job`**。

**连锁反应**（这一串正好解释你看到的一切）：

1. `Page.background()` 先调 `self.busy()` → `setEnabled(False)` **禁用整页**
2. 恢复启用的 `idle()` 在**回调里**，而回调从不执行 → **页面永久禁用**
3. `thread.quit` 也连在 `job.done` 上 → 从不触发 → **QThread 空转到进程结束**
4. 关闭窗口时 Qt 清理这个仍在跑的 QThread → **卡住**（表现为"未响应"）
5. 强杀 → **`exit code 1`**

**第 4、5 步就是你那次运行的退出信息**：

```
QThread: Destroyed while thread '' is still running
[exit code: 1]
```

### Bug 2 —— 即使 job 跑起来，回调也在**工作线程**执行

`on_done` 是 `Page.background` 里的**普通 Python 闭包**，不是 `QObject` 的槽。
PySide6 对这类连接用 **`DirectConnection`** —— **信号在哪个线程发，回调就在哪个线程跑**。

而回调干的是：

```python
def done(result):
    self.idle()        # QWidget.setEnabled(True)         ← 非主线程碰控件
    render(result)     # 往 QTableWidget / QLabel 写数据   ← 非主线程碰控件
```

`render_scan` 还会再**同步调一次 `library_status`** —— 插件代码 + sqlite 也在工作线程里跑，
与主线程共享同一份插件状态。

**Qt 规定：控件只能在它所属的线程里访问。** 违反的典型表现就是界面冻死 / 崩溃。

### 两个 bug 的实证（A/B 对照）

| 写法 | `job.run` 执行了吗 | `on_done` 在哪个线程 |
|---|---|---|
| **A：现在的写法**（job 是局部变量） | **❌ 从没执行** | — |
| **B：把 job 强引用住** | ✅ `Dummy-1` | **❌ `Dummy-1`（工作线程）** |

主线程名是 `MainThread`，明确不是它。

### 两个 bug 与你的症状的对应关系（如实说明）

| 症状 | 由谁造成 |
|---|---|
| 点了按钮没反应、页面变灰再也点不动 | **Bug 1**（回调不执行 → `idle()` 没跑） |
| 关闭时"未响应"、只能强杀、exit code 1 | **Bug 1**（QThread 空转，退出时清理卡住）← **你的指纹** |
| （若任务真的跑起来）界面冻死 / 崩溃 | **Bug 2** |

**当前 Bug 1 永远先发作**（引用计数立即归零），所以 Bug 2 是**潜伏的**。
但**只修 Bug 1 会让 Bug 2 必然发作** —— 两个必须一起修。

### 顺带发现的第三处隐患（不在本次范围）

```python
thread.finished.connect(thread.deleteLater)   # C++ QThread 被删
self._thread = thread                         # 但 Python 侧还引用着它
```

之后访问 `self._thread.isRunning()` 会碰到已销毁的 C++ 对象。

---

## 2. 为什么 TASK-012b 的测试没抓到

**因为测试根本没走被点的那个函数。**

| | 测试调的 | 按钮调的 |
|---|---|---|
| 导入素材 | `page.scan()` —— **同步** | `page.on_scan()` → `background()` |
| 检索 | `page.search()` —— **同步** | `page.on_search()` → `background()` |

```python
def scan(self):                                    # 测试走这条
    result = self.call("library_scan", **self.scan_arguments())
    self.render_scan(result)

def on_scan(self):                                 # 鼠标点的是这条
    self.background("library_scan", arguments, self.render_scan)
```

两条路**共用取参数与渲染**（我当时刻意这么设计防分叉），
**唯独 `background()` 这层没有任何测试覆盖**。

**而我在 TASK-012b 报告里自己写了**：

> "没有真的点过鼠标：验收是用页面的按钮方法（按钮的同步实现）跑的……
> 信号连接是否正确靠代码审查，不是点击测试。"

**这就是那句话的代价。** 15 项界面测试全绿，用户点第一下就卡死。

---

## 3. 修复方案（等你确认后再改）

### 改动点：只动 `src/xbc/ui/bridge.py::BackgroundCall`（约 8 行）

**修 Bug 1 —— 保住 job 的强引用：**

```python
self._thread = thread
self._job = job          # ← 新增：moveToThread 不转移 Python 所有权
```

**修 Bug 2 —— 让回调回到主线程：**

关键洞察：**`BackgroundCall` 本身就是在主线程构造的 `QObject`**。
把 `job.done` 连到它的一个**槽**上，Qt 就会自动用 `QueuedConnection`
把调用投递到主线程：

```python
class BackgroundCall(QObject):
    def start(self, on_done):
        ...
        self._on_done = on_done
        job.done.connect(self._relay)        # ← self 是 QObject 且亲和主线程
        ...                                   #    => 自动 QueuedConnection => 主线程执行

    def _relay(self, result):                # 在主线程执行
        callback, self._on_done = self._on_done, None
        if callback is not None:
            callback(result)
```

**为什么不用 `Qt.QueuedConnection` 直接连闭包**：QueuedConnection 需要**接收者**有线程亲和性，
普通闭包没有 —— 强行指定会变成"投递到没人处理的队列"。**必须有个 QObject 当接收者**，
`BackgroundCall` 正好是现成的。

**为什么不用 `QThreadPool` + `QRunnable`**：那也能修，但要重写整条路径。
按**最小改动**原则，只修这 8 行。

### 建议一并修（同一段代码，等你点头）

第三处隐患：`self._thread = thread` 与 `thread.deleteLater()` 冲突 ——
`deleteLater` 后把 `self._thread` 置 `None`，或干脆不 `deleteLater`（让 Python 管）。

### 验收标准（修完必须全过）

| # | 断言 |
|---|---|
| 1 | `QTest.mouseClick` 点「检索」→ **回调真的回来了**（状态栏不再停在"正在执行…"） |
| 2 | 点击后页面**恢复 `isEnabled() == True`** |
| 3 | 结果**真的渲染进表格**（行数 == 假 bridge 返回的条数） |
| 4 | 回调**在主线程执行**（断言 `threading.current_thread().name == "MainThread"`） |
| 5 | `job` 在任务完成前**未被回收**（weakref 断言） |
| 6 | **进程能干净退出**（不再出现 `QThread: Destroyed while thread is still running`） |
| 7 | 已有 451 项测试**不退化** |

---

## 4. 补真实点击测试（要求 3）

新增 `tests/test_ui_clicks.py`，用 `QTest.mouseClick`（**不是** `button.click()`）覆盖：

| 分组 | 点什么 |
|---|---|
| 主界面 | 左侧导航 4 项各点一遍，断言切页正确 |
| 素材库 | 「刷新概况」「检索」（含下拉切换）「导入素材」（假 bridge，不碰真模型） |
| 文案匹配 | 「开始匹配」「刷新列表」「读取」「设为首选」「上移」「下移」 |
| 插件中心 | 插件列表选中 + 「启用」「停用」 |
| **回归锁** | **每个走后台的按钮点完后断言"页面被重新启用" + "回调确实回来了"** ← 直接锁住本次 bug |

**注意**：`QTest.mouseClick` 需要控件**可见且有尺寸** —— 测试里要 `widget.show()` + `QTest.qWait()`。
offscreen 平台下实测可行（本轮的诊断脚本就是这么跑的）。

**不测「浏览…」**：它开原生模态对话框，测试里会阻塞。
改用"替换 `QFileDialog.getExistingDirectory` 为桩"的方式，只断言"点击后调了它、并把返回值填进了输入框"。

---

## 5. 规则补充（要求 3）

写进 [docs/harness-rules.md](harness-rules.md)，新增一节
**《界面任务：验收必须包含真实点击》**：

> 界面任务的验收**不许**只用"按钮背后的同步方法"。
> 必须至少有一条 `QTest.mouseClick` 的真实点击用例，覆盖**每一个会改变状态的按钮**，
> 并断言：**回调真的回来了、控件状态真的恢复了、界面上真的能看到变化**。
>
> 理由：`button.click()` / 直接调同步方法都**绕过信号连接与线程调度** ——
> 而"点了没反应""点完就卡"这类 bug 恰恰只活在那两层里。
> TASK-012b 的 15 项界面测试全绿、用户点第一下就卡死，就是没做真实点击的代价。

---

## 6. 发现但未做

按《任务边界：禁止"顺手"》如实列出：

| 发现了什么 | 为什么觉得该改 | 为什么这次不做 |
|---|---|---|
| `thread.deleteLater()` 与 `self._thread` 引用冲突 | 之后访问会碰到已销毁的 C++ 对象 | 本次没触发；**但就在同一段代码里，建议并入本次修复** —— 等你点头 |
| `require()` 只查"工具是否注册"，不查"能力是否可用" | 模型缺失时按钮照样可点，只给一句 note | 属交互设计改进，不影响卡死 |
| `library_status` 每次调 `_library()` **两次**（各一次 connect + DDL + migrate），~40ms | 纯浪费 | 不影响卡死；性能优化 |
| 长任务不可取消 | 用户体验 | TASK-012b 报告已记为局限 |
| `background()` 期间导航仍可切走，切回可见禁用页 | 交互一致性 | 不影响本 bug |
| TASK-012b 历史报告里那条局限**措辞太轻** | 实际后果是用户点第一下就卡死 | **不改历史记录**；新规则从今往后生效 |

---

## 7. 需要你确认

1. **修复范围**：只修两个 bug（约 8 行）？还是**把第三处隐患一起修**（同一段代码）？
2. **`test_ui_clicks.py` 按 §4 的四组 + 回归锁**够不够？
3. **「浏览…」用桩替换**（不弹真对话框）可以吗？
4. 确认后我再动代码，按 §3 的 **7 条**验收标准给结果。
