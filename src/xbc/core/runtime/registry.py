"""分层注册表。

方案 3.5 定义的裁决规则：

| 层 | 谁注册 | 优先级 |
|---|---|---|
| 内核层 | 内核自带的能力与工具 | 最低 |
| 插件层 | 已激活插件注册的东西 | 中 |
| 用户层 | 用户手工注册的内容 | 最高 |

裁决顺序：

1. **更靠近用户的层获胜**；
2. 同层内先比 `rank`（显式声明的优先级）；
3. 仍然相同则**先到先得**（先注册的获胜）—— 与 DeepSeek Harness 的
   "同层重名先到先得"一致，语义可预测，不依赖加载顺序的偶然性；
4. **被遮蔽的注册必须保留可查**，不能静默吞掉（`doctor` 会展示）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Callable, Iterator

Disposer = Callable[[], Any]


class Layer(IntEnum):
    """注册层级。数值越大越靠近用户，优先级越高。"""

    KERNEL = 0
    PLUGIN = 1
    USER = 2


@dataclass
class Registration:
    """一次注册的完整记录。"""

    name: str
    value: Any
    layer: int
    rank: int
    order: int
    owner: str = ""
    shadowed_by: str | None = None

    @property
    def is_effective(self) -> bool:
        return self.shadowed_by is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "layer": Layer(self.layer).name.lower(),
            "rank": self.rank,
            "owner": self.owner,
            "effective": self.is_effective,
            "shadowed_by": self.shadowed_by,
        }


class LayeredRegistry:
    """按层裁决的注册表。工具、技能、服务都复用它。"""

    def __init__(self, what: str, logger: Any = None) -> None:
        self.what = what
        self._log = logger or logging.getLogger("xbc")
        self._entries: list[Registration] = []
        self._counter = 0

    # ---------- 注册 ----------
    def register(
        self,
        name: str,
        value: Any,
        *,
        layer: int = Layer.PLUGIN,
        rank: int = 0,
        owner: str = "",
    ) -> Disposer:
        """注册一个条目，返回 disposer。"""
        self._counter += 1
        new = Registration(name=name, value=value, layer=int(layer), rank=int(rank), order=self._counter, owner=owner)
        current = self._resolve(name)

        if current is not None and not self._beats(new, current):
            # 落败：登记为被遮蔽项，但不生效
            new.shadowed_by = current.owner or f"{Layer(current.layer).name.lower()}层"
            self._entries.append(new)
            self._log.info(
                "%s 名称冲突：%s 的 %r 被 %s 遮蔽（先到先得）",
                self.what, owner or "?", name, new.shadowed_by,
            )
        else:
            if current is not None:
                current.shadowed_by = owner or f"{Layer(new.layer).name.lower()}层"
            self._entries.append(new)

        return lambda: self._remove(new)

    @staticmethod
    def _beats(new: Registration, current: Registration) -> bool:
        if new.layer != current.layer:
            return new.layer > current.layer
        return new.rank > current.rank  # 同层同 rank → 先到先得

    def _remove(self, target: Registration) -> None:
        if target not in self._entries:
            return
        was_winner = target.shadowed_by is None
        self._entries.remove(target)
        if was_winner:
            # 获胜者被移除后，必须从被遮蔽的候选里重新选出获胜者，
            # 否则这个名称会凭空消失（曾经的一个真实 bug）。
            self._resettle(target.name)

    def _resettle(self, name: str) -> None:
        candidates = [e for e in self._entries if e.name == name and e.shadowed_by is not None]
        if not candidates:
            return
        best = max(candidates, key=lambda e: (e.layer, e.rank, -e.order))
        best.shadowed_by = None
        self._log.info("%s %r 重新生效：%s", self.what, name, best.owner)

    # ---------- 查询 ----------
    def _resolve(self, name: str) -> Registration | None:
        winners = [e for e in self._entries if e.name == name and e.shadowed_by is None]
        return max(winners, key=lambda e: (e.layer, e.rank, -e.order)) if winners else None

    def resolve(self, name: str) -> Registration | None:
        return self._resolve(name)

    def get(self, name: str, default: Any = None) -> Any:
        entry = self._resolve(name)
        return entry.value if entry is not None else default

    def owner_of(self, name: str) -> str:
        entry = self._resolve(name)
        return entry.owner if entry is not None else ""

    def names(self) -> list[str]:
        return sorted({e.name for e in self._entries if e.shadowed_by is None})

    def items(self) -> list[tuple[str, Any]]:
        return [(name, self.get(name)) for name in self.names()]

    def entries(self) -> list[Registration]:
        """全部注册记录（含被遮蔽项）。"""
        return list(self._entries)

    def shadowed(self) -> list[Registration]:
        """被遮蔽的注册，供 doctor 展示。"""
        return [e for e in self._entries if e.shadowed_by is not None]

    def __contains__(self, name: object) -> bool:
        return self._resolve(str(name)) is not None

    def __len__(self) -> int:
        return len(self.names())

    def __iter__(self) -> Iterator[tuple[str, Any]]:
        return iter(self.items())

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<LayeredRegistry {self.what} effective={len(self)} shadowed={len(self.shadowed())}>"
