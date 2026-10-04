"""插件包格式（TASK-006 第 1 项）。

## 格式定义

插件包就是一个 **ZIP 归档**，扩展名 `.xbcplugin`（也接受 `.zip`）：

```
video_analyzer-0.1.0.xbcplugin
├─ plugin.json          清单（必需，且必须位于归档根目录）
├─ plugin.py            入口
├─ skills/...           可选
└─ assets/...           可选
```

**没有引入新的清单文件**：包里就是插件目录本身，`plugin.json` 既是运行期清单、
也是包的元数据源。少一个概念，少一处可以不同步的地方。

## 安全

解包是本模块风险最高的操作，因此做了三件事：

1. **拒绝路径穿越（zip-slip）**：绝对路径、盘符、`..`、反斜杠伪装全部拒绝；
2. **拒绝符号链接**：ZIP 里可以塞符号链接指向归档外；
3. **拒绝异常大小**：单个文件与总解压体积都有上限，防止 zip bomb。

解包一律走"先解到临时目录、再原子替换"，任何一步失败都不会留下半个插件。
"""

from __future__ import annotations

import os
import re
import zipfile
from pathlib import Path, PurePosixPath
from typing import Iterable

from ..errors import XbcError

PACKAGE_SUFFIX = ".xbcplugin"
ACCEPTED_SUFFIXES = (PACKAGE_SUFFIX, ".zip")
MANIFEST_NAME = "plugin.json"

#: 单个文件解压上限
MAX_MEMBER_BYTES = 64 * 1024 * 1024
#: 整包解压上限
MAX_TOTAL_BYTES = 256 * 1024 * 1024
#: 归档条目数上限
MAX_MEMBERS = 2000

_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class PackageError(XbcError):
    """插件包不合法（格式错、越界路径、超限等）。"""


def is_package(path: Path | str) -> bool:
    candidate = Path(path)
    return candidate.is_file() and candidate.suffix.lower() in ACCEPTED_SUFFIXES


def _safe_member_path(name: str) -> PurePosixPath:
    """把归档里的条目名规范化，越界一律拒绝。"""
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or _DRIVE_RE.match(normalized):
        raise PackageError(f"包内包含绝对路径，已拒绝：{name!r}")

    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise PackageError(f"包内包含越界路径，已拒绝：{name!r}")
    if not parts:
        raise PackageError(f"包内包含空路径：{name!r}")
    return PurePosixPath(*parts)


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    # ZIP 用外部属性高 16 位存 Unix 模式；0xA000 表示符号链接
    return (info.external_attr >> 16) & 0xF000 == 0xA000


def build_package(plugin_dir: Path | str, output: Path | str | None = None) -> Path:
    """把一个插件目录打成 `.xbcplugin` 包。

    打包时**校验清单与版本**，让不合规的插件在发布环节就被拦住，
    而不是等用户安装时才失败。
    """
    from ..contract.manifest import PluginManifest, load_manifest

    folder = Path(plugin_dir).resolve()
    if not (folder / MANIFEST_NAME).is_file():
        raise PackageError(f"不是插件目录（缺少 {MANIFEST_NAME}）：{folder}")

    manifest: PluginManifest = load_manifest(folder)
    from .version import PluginVersion

    try:
        PluginVersion.parse(manifest.version)
    except ValueError as exc:
        raise PackageError(f"{folder.name}: {exc}") from exc

    target = Path(output) if output else folder.parent / f"{manifest.id}-{manifest.version}{PACKAGE_SUFFIX}"
    if target.suffix.lower() not in ACCEPTED_SUFFIXES:
        target = target.with_suffix(PACKAGE_SUFFIX)
    target.parent.mkdir(parents=True, exist_ok=True)

    # 入口文件必须存在，且版本号必须可比较（产品化的两个最低要求）
    entry_file = entry_file_of(manifest)
    if not (folder / entry_file).is_file():
        raise PackageError(
            f"{folder.name}: 清单声明的入口文件不存在：{entry_file!r}"
        )

    files = sorted(
        p for p in folder.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
    )
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.relative_to(folder).as_posix())
    return target


def entry_file_of(manifest: Any) -> str:
    """从 `entry`（如 `plugin.py:Demo` / `mypkg/main:Demo`）推出入口文件相对路径。"""
    module = str(manifest.entry).split(":", 1)[0]
    return module if module.endswith(".py") else f"{module}.py"


def read_package_manifest(package: Path | str):
    """只读包里的 `plugin.json`，**不解包**。

    用于安装前校验（id、版本、入口文件、API 兼容性），避免先落盘再发现不合规。
    """
    from ..contract.manifest import PluginManifest, parse_manifest

    source = Path(package)
    if not source.is_file():
        raise PackageError(f"插件包不存在：{source}")

    try:
        with zipfile.ZipFile(source) as archive:
            names = set(archive.namelist())
            if MANIFEST_NAME not in names:
                raise PackageError(f"插件包根目录缺少 {MANIFEST_NAME}：{source}")
            raw = archive.read(MANIFEST_NAME)
    except zipfile.BadZipFile as exc:
        raise PackageError(f"不是合法的 ZIP 归档：{source}（{exc}）") from exc

    import json
    from pathlib import Path as _Path

    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PackageError(f"包内 {MANIFEST_NAME} 不是合法 JSON：{exc}") from exc

    manifest: PluginManifest = parse_manifest(data, _Path(f"<package:{source.name}>"))

    # 入口文件必须真的在包里 —— 否则装上去只会在加载时失败，
    # 而用户会以为是工具箱坏了。在安装前拦住更友好。
    entry_file = entry_file_of(manifest)
    if entry_file not in names:
        raise PackageError(
            f"包内缺少清单声明的入口文件 {entry_file!r}（插件 {manifest.id}）"
        )
    return manifest


def extract_package(package: Path | str, target_dir: Path | str) -> list[str]:
    """安全解包到 `target_dir`（该目录必须为空或不存在）。

    返回解压出来的相对路径列表。
    """
    source = Path(package)
    destination = Path(target_dir)
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()

    extracted: list[str] = []
    total = 0

    try:
        archive = zipfile.ZipFile(source)
    except zipfile.BadZipFile as exc:
        raise PackageError(f"不是合法的 ZIP 归档：{source}（{exc}）") from exc

    with archive:
        members = archive.infolist()
        if len(members) > MAX_MEMBERS:
            raise PackageError(f"包内条目过多（{len(members)} > {MAX_MEMBERS}）")

        for info in members:
            relative = _safe_member_path(info.filename)
            if _is_symlink(info):
                raise PackageError(f"包内包含符号链接，已拒绝：{info.filename!r}")

            out_path = (root / Path(*relative.parts)).resolve()
            if not out_path.is_relative_to(root):
                raise PackageError(f"包内路径越出目标目录，已拒绝：{info.filename!r}")

            if info.is_dir():
                out_path.mkdir(parents=True, exist_ok=True)
                continue

            if info.file_size > MAX_MEMBER_BYTES:
                raise PackageError(
                    f"包内单个文件过大：{info.filename!r}（{info.file_size} 字节）"
                )
            total += info.file_size
            if total > MAX_TOTAL_BYTES:
                raise PackageError(f"整包解压体积超过上限（{MAX_TOTAL_BYTES} 字节）")

            out_path.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as src, open(out_path, "wb") as dst:
                while True:
                    chunk = src.read(1024 * 256)
                    if not chunk:
                        break
                    dst.write(chunk)
            extracted.append(relative.as_posix())

    if MANIFEST_NAME not in extracted:
        raise PackageError(f"包内根目录缺少 {MANIFEST_NAME}（实际条目：{extracted[:10]}）")
    return extracted


def list_package(package: Path | str) -> Iterable[str]:
    """列出包内条目（只读，不解压）。"""
    with zipfile.ZipFile(Path(package)) as archive:
        return archive.namelist()


def atomic_replace_directory(staging: Path, target: Path, backup_root: Path | None = None) -> Path | None:
    """把 `staging` 原子地变成 `target`，返回备份目录（没有则为 None）。

    失败时回滚，绝不留下半个插件。
    """
    staging = Path(staging)
    target = Path(target)
    backup: Path | None = None

    try:
        if target.exists():
            backup_root = backup_root or target.parent
            backup = Path(backup_root) / f".backup-{target.name}-{os.getpid()}-{abs(hash(str(target))) % 100000}"
            if backup.exists():
                import shutil

                shutil.rmtree(backup, ignore_errors=True)
            os.replace(target, backup)
        os.replace(staging, target)
    except BaseException:
        # 回滚：把备份放回去
        if backup is not None and backup.exists() and not target.exists():
            try:
                os.replace(backup, target)
            except OSError:
                pass
        raise
    return backup
