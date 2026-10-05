# TASK-013c 向量空间可见性改进 —— 交付报告

| 项 | 内容 |
|---|---|
| 类型 | **B 类：直接做** |
| 背景 | TASK-013a 查证发现：`auto` 静默降级 + 界面完全不显示向量空间 |
| 改动 | 插件 1 个文件 + 界面 2 个页面 + 页面基类；**零删除、零数据改动** |
| 新增测试 | `tests/test_space_visibility.py`（**23 项**） |
| 测试 | **501 项通过**（系统 Python + venv 双环境，exit=0） |
| 真数据验收 | **两条验收标准都用真实素材库副本跑通**，见 §3 |
| 根因报告 | [task-013a-match-vector-space-report.md](task-013a-match-vector-space-report.md) |

---

## 0. 验收对照

| # | 标准 | 结果 |
|---|---|---|
| 1 | **有图片空间时，界面显示 chinese_clip 1024 维** | ✅ 真数据实测：`向量空间：chinese_clip / chinese-clip-rn50 · 1024 维 · 图片`，**无警告** |
| 2 | **无图片空间时，界面明确提示"降级到文本空间"** | ✅ 真数据实测：`⚠ 向量空间：ollama / nomic-embed-text · 768 维 · 文本　——　当前使用文本空间（Caption），质量弱于图片空间…` |
| 3 | **匹配逻辑不变**（`auto` 优先图片） | ✅ 5 项回归锁；`_pick_space` 一行未改 |
| 4 | **向量数据不动**（ollama 文本向量保留） | ✅ 2 项锁 + 真数据核对：两套向量都还在 |

---

## 1. 改了什么

### 1.1 插件：降级出声（`plugins/video_analyzer/plugin.py`）

新增两个**纯文案**方法（不含任何检索逻辑）：

```python
SPACE_DOWNGRADE_NOTE = (
    "当前使用文本空间（Caption），质量弱于图片空间"
    "：装好图片嵌入模型并重新扫描素材后会自动切到图片空间（见工作台「图片嵌入」状态）"
)

@classmethod
def _space_note(cls, want, chosen):
    """只在 auto 自动挑、且挑不到图片空间时出声。"""
    if want != "auto" or chosen.get("modality") == "image":
        return ""
    return cls.SPACE_DOWNGRADE_NOTE

@classmethod
def _stored_space_note(cls, space):
    """已存结果用的：这份结果当初是用哪个空间算的。"""
    ...
```

**三条设计取舍**，都写进了注释：

| 取舍 | 理由 |
|---|---|
| **显式 `space="text"` 不报警** | 那是主动选择（用于对比 / 复现），不是降级 —— 报警会变成噪声 |
| **显式 `space="image"` 但没有 → 报错不报降级** | 那是"做不到"，不是"降级"；套降级话术会误导 |
| 已存结果用**落盘快照里的 space** 判断 | 说的是"这份结果的质量档位"，与当前机器装没装模型无关 |

**三个出口都接上了**：

| 工具 | 加的东西 |
|---|---|
| `library_search_semantic` | `note`（降级提示）+ `embedding.modality`（新字段，界面靠它区分图片/文本） |
| `script_match` | `note` |
| `match_show` | `note`（按落盘 space 判断） |

### 1.2 界面：显示空间

**页面基类新增两个纯展示函数**（`src/xbc/ui/pages/base.py`）：

```python
def describe_space(space) -> str:
    """向量空间：chinese_clip / chinese-clip-rn50 · 1024 维 · 图片"""

def space_line(space, note="") -> str:
    """有降级提示就前面加 ⚠ 并接上原因"""
```

> **界面不判断该不该降级** —— 提示文案来自工具返回的 `note`，界面只负责显示。
> 这守住了 TASK-012b 定的规矩：**界面里没有业务逻辑**。
> 模态的中文名（`image`→图片、`text`→文本）是**展示映射**，不是判断。

**两个页面各加一个常驻标签**：

| 页面 | 标签 | 填充时机 |
|---|---|---|
| 素材库 | `self.space_label` | `render_search`（用返回的 `embedding`） |
| 文案匹配 | `self.space_label` | `render_open`（用返回的 `space`）—— 「开始匹配」也走它，两个入口显示一致 |

**为什么用常驻标签而不是只写状态栏**：状态栏会被下一条消息覆盖（TASK-012b 就踩过：
导入结果被 `show_status()` 冲掉）。空间信息得一直看得见。

**同时保留了状态栏里的提示** —— 状态栏和标签各说一遍，用户先看哪个都不会漏。

---

## 2. 测试（23 项）

`tests/test_space_visibility.py`，按验收标准分组：

| 组 | 项 | 覆盖 |
|---|---|---|
| `PluginSpaceNoteTests` | 3 | **有图片空间不报警**（检索 / 匹配）＋ 显式 text 不算降级 |
| `PluginDowngradeNoteTests` | 4 | **只有文本空间时报降级**（检索 / 匹配 / `match_show`）＋ 显式 image 做不到时报错不报降级 |
| `SpacePickerRegressionTests` | 5 | **`auto` 优先图片**（含"文本排前面也照样选图片"）、显式模态生效、`_space_note` 只在 auto 降级时出声 |
| `VectorDataUntouchedTests` | 2 | **检索前后两个空间都在**、两种模态都保留 |
| `LibraryPageSpaceTests` | 2 | 界面显示 1024 维 / 界面出 ⚠ 降级提示 |
| `MatchPageSpaceTests` | 3 | 读取与「开始匹配」两个入口都填标签 |
| `SpaceLineHelperTests` | 4 | 纯展示：四字段都渲染、未知模态如实显示、空空间说"未知"、note 变 ⚠ 前缀 |

**插件层用真插件 + 假 Provider 跑**（`WITH_IMAGE = True/False` 两种库），
**界面层用与插件输出同形状的假 bridge**（形状由插件层测试锁定）。

---

## 3. 真数据验收（两条标准都实测）

用**真实素材库的一份副本**跑，跑完删除副本，**真实数据根未改动**：

### A. 原样（你的库，有 chinese_clip）

```
library_search_semantic  ok=True
  modality = 'image'   provider = 'chinese_clip'   dim = 1024
  note     = ''
script_match             ok=True
  space    = {'provider': 'chinese_clip', 'model': 'chinese-clip-rn50', 'dim': 1024, 'modality': 'image'}
  note     = ''
素材库页 space_label 会显示：
  向量空间：chinese_clip / chinese-clip-rn50 · 1024 维 · 图片
```

### B. 副本里删掉 chinese_clip 向量（模拟"还没装图片模型"）

```
library_search_semantic  ok=True
  modality = 'text'   provider = 'ollama'   dim = 768
  note     = '当前使用文本空间（Caption），质量弱于图片空间：装好图片嵌入模型并重新扫描素材后会自动切到图片空间（见工作台「图片嵌入」状态）'
script_match             ok=True
  space    = {'provider': 'ollama', 'model': 'nomic-embed-text', 'dim': 768, 'modality': 'text'}
  note     = '当前使用文本空间（Caption），质量弱于图片空间：…'
素材库页 space_label 会显示：
  ⚠ 向量空间：ollama / nomic-embed-text · 768 维 · 文本　——　当前使用文本空间（Caption），质量弱于图片空间：…
```

### 跑完后核对真实数据根

```
chinese_clip/chinese-clip-rn50  1024 维  2 条
ollama/nomic-embed-text          768 维  2 条
```

**两套都在** —— 验收标准 4 成立。

---

## 4. 明确**没动**的东西

| 项 | 状态 | 怎么证明 |
|---|---|---|
| `_pick_space` 的 `auto` 逻辑 | **一行未改** | `SpacePickerRegressionTests` 5 项锁住（含"文本空间排在前面也照样选图片"） |
| 检索 / 匹配的打分实现（`_score_query`） | **一行未改** | 501 项全过，其中 56 项 `test_script_match` 原样通过 |
| 库里的 ollama 文本向量 | **一条没删** | `VectorDataUntouchedTests` 2 项 + 真数据核对 |
| 检索结果排序 / 条数 | **没动** | `test_space_visibility` 只加 `note` 与 `modality` 两个新键 |

> `embedding` 里**新增了 `modality` 字段** —— 是**加**，不是改。
> 由于 `_score_query` 的返回值是全量 `dict`，加键不会影响任何既有读取方。

---

## 5. 发现但未做

| 发现了什么 | 为什么觉得该改 | 为什么这次不做 |
|---|---|---|
| **降级后如果文本 Provider 也跑不起来，用户看到的是普通报错，不是降级提示** | 两个信息本该同时给 | 报错比提示更紧急，先报错不算错；且这条路径要真跑通才行验证，属另一个场景 |
| **工作台的「图片嵌入」状态**只在一个标签里，没跟着 TASK-013c 一起做成常驻可见 | 与本次是同一类问题（信息不可见） | 任务书写的是"匹配页 + 素材库检索页"，**工作台不在范围内** |
| 匹配页的 `space_label` 在「还没读取结果」时是占位文字 | 体验小瑕疵 | 不是验收项 |
| `describe_space` 对**多模态混装**（同一 provider 既有图片又有文本空间）只显示一个 | 现实里不会出现 | 上库结构下不可能发生 |

---

## 6. 复现

```powershell
$env:QT_QPA_PLATFORM='offscreen'
python -m unittest tests.test_space_visibility     # 23 项
python -m unittest discover -s tests               # 501 项
```

界面上手看：

```powershell
F:\Downloads\XBC\.venv\Scripts\python.exe F:\Downloads\XBC\run.py ui
# 素材库 → 输入检索词 → 点「检索」
#   有图片模型：标签显示「向量空间：chinese_clip / chinese-clip-rn50 · 1024 维 · 图片」
#   没有图片模型：标签带 ⚠ 并写明「当前使用文本空间…质量弱于图片空间」
# 文案匹配 → 开始匹配 / 读取 → 同一个标签同样显示
```

---

## 7. 说明

- **改动文件**：`plugins/video_analyzer/plugin.py`、`src/xbc/ui/pages/base.py`、
  `src/xbc/ui/pages/library.py`、`src/xbc/ui/pages/match.py`、新增 `tests/test_space_visibility.py`
- **Core 零改动**
- 已清理本轮验收产生的两个临时数据副本（真实数据根未改动）
