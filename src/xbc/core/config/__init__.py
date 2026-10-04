"""配置存储与三层装配。

- `store.py`   配置文件的读写（原子写入、损坏兜底）与插件配置命名空间
- `layers.py`  插件配置的三层装配（默认 / 宿主 / 用户）与整对象替换语义
"""

from .layers import (
    PluginRow,
    config_defaults,
    effective_config,
    load_rows,
    merge_rows,
    resolve_row,
    write_row,
)
from .store import DEFAULTS, Config, PluginSettings

__all__ = [
    "DEFAULTS",
    "Config",
    "PluginRow",
    "PluginSettings",
    "config_defaults",
    "effective_config",
    "load_rows",
    "merge_rows",
    "resolve_row",
    "write_row",
]
