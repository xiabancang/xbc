"""主界面的四个页面。

| 页面 | 干什么 | 依赖 |
|---|---|---|
| 工作台 | 看当前状态、去别的页 | Runtime + `library_status` |
| 素材库 | 导入素材、检索 | `library_*` 工具 |
| 文案匹配 | 输入文案、看候选、人工调整 | `script_match` / `match_*` 工具 |
| 插件中心 | 启用 / 停用插件 | `PluginManager`（与 CLI 同一方法） |

**页面里没有业务逻辑**：入库、切镜头、算相似度、排序全在工具里。
页面只做三件事：收参数、调工具、铺结果。
"""

from .base import Page
from .library import LibraryPage
from .match import MatchPage
from .plugins import PluginsPage
from .workbench import WorkbenchPage

__all__ = [
    "LibraryPage",
    "MatchPage",
    "Page",
    "PluginsPage",
    "WorkbenchPage",
]
