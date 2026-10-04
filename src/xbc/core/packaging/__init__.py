"""插件产品化基础：包格式、版本、安装/卸载/升级、用户数据隔离。

- `version.py`   插件版本（SemVer 子集）与比较
- `archive.py`   包格式定义、安全解包（防 zip-slip / 符号链接 / zip bomb）、原子替换
- `installer.py` 安装 / 卸载 / 升级 + 安装台账 + 数据目录分离

设计原则：**安装是宿主的能力，不是插件的能力** —— 插件拿不到它，
插件也不能安装别的插件。这样才不会出现"插件自带安装器"这种失控结构。
"""

from .archive import (
    ACCEPTED_SUFFIXES,
    PACKAGE_SUFFIX,
    PackageError,
    atomic_replace_directory,
    build_package,
    extract_package,
    is_package,
    list_package,
    read_package_manifest,
)
from .installer import (
    InstallError,
    InstallResult,
    InstalledPlugin,
    PluginInstaller,
)
from .version import PluginVersion, compare

__all__ = [
    "ACCEPTED_SUFFIXES",
    "PACKAGE_SUFFIX",
    "InstallError",
    "InstallResult",
    "InstalledPlugin",
    "PackageError",
    "PluginInstaller",
    "PluginVersion",
    "atomic_replace_directory",
    "build_package",
    "compare",
    "extract_package",
    "is_package",
    "list_package",
    "read_package_manifest",
]
