"""插件版本（SemVer 子集）。

为什么自己写而不引入 `packaging` / `semver`：
**内核零第三方依赖**是硬约束，而插件版本比较需要的只是很小一块。

支持的写法：`主.次.修订`，可带预发布标签（如 `1.2.3-beta.1`）与构建元数据（`+build`）。

比较规则（本实现明确支持的范围）：

- 先比 `主 ← 次 ← 修订`，**按数值**比较（所以 `1.2.10 > 1.2.9`，不是字符串比较）；
- 有预发布标签的**小于**同号的正式版（`1.0.0-beta < 1.0.0`）；
- 两个都有预发布标签时，按点分段比较：纯数字段按数值比，数字段小于非数字段，
  其余按字典序；
- **构建元数据（`+` 之后）不参与比较**（与 SemVer 一致）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import total_ordering

_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.\-]+))?(?:\+([0-9A-Za-z.\-]+))?$")


@total_ordering
@dataclass(frozen=True)
class PluginVersion:
    """可直接比较的插件版本。"""

    major: int
    minor: int
    patch: int
    prerelease: str = ""
    build: str = ""

    @classmethod
    def parse(cls, text: str) -> "PluginVersion":
        """解析版本字符串；不合法抛 ValueError（错误信息里带原值，便于定位）。"""
        match = _VERSION_RE.match(str(text).strip())
        if not match:
            raise ValueError(
                f"非法的版本号 {text!r}：要求 主.次.修订（可带 -预发布 与 +构建元数据）"
            )
        major, minor, patch, prerelease, build = match.groups()
        return cls(int(major), int(minor), int(patch), prerelease or "", build or "")

    @classmethod
    def try_parse(cls, text: str) -> "PluginVersion | None":
        try:
            return cls.parse(text)
        except ValueError:
            return None

    @property
    def is_prerelease(self) -> bool:
        return bool(self.prerelease)

    @property
    def release(self) -> tuple[int, int, int]:
        return (self.major, self.minor, self.patch)

    def __str__(self) -> str:
        text = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            text += f"-{self.prerelease}"
        if self.build:
            text += f"+{self.build}"
        return text

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, PluginVersion):
            return NotImplemented
        # 构建元数据不参与比较
        return self.release == other.release and self.prerelease == other.prerelease

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, PluginVersion):
            return NotImplemented
        if self.release != other.release:
            return self.release < other.release
        if self.prerelease == other.prerelease:
            return False
        # 正式版 > 预发布版
        if not self.prerelease:
            return False
        if not other.prerelease:
            return True
        return _compare_prerelease(self.prerelease, other.prerelease) < 0


def _compare_prerelease(left: str, right: str) -> int:
    """预发布标签比较。返回 -1 / 0 / 1。"""
    left_parts = left.split(".")
    right_parts = right.split(".")
    for a, b in zip(left_parts, right_parts):
        if a == b:
            continue
        a_num, b_num = a.isdigit(), b.isdigit()
        if a_num and b_num:
            return -1 if int(a) < int(b) else 1
        if a_num != b_num:
            # 数字标识符优先级低于非数字
            return -1 if a_num else 1
        return -1 if a < b else 1
    if len(left_parts) == len(right_parts):
        return 0
    return -1 if len(left_parts) < len(right_parts) else 1


def compare(left: str, right: str) -> int:
    """便捷比较：返回 -1 / 0 / 1。任一方不合法抛 ValueError。"""
    a, b = PluginVersion.parse(left), PluginVersion.parse(right)
    return 0 if a == b else (-1 if a < b else 1)
