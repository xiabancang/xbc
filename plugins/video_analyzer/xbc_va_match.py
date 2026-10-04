"""文案 → 镜头匹配的**纯逻辑层**：分段 + 匹配结果的存取。

## 这里没有检索逻辑

检索（向量空间解析、查询编码、余弦排序）只有一份实现，在插件的
`_retrieval_context` / `_score_query` 里（TASK-010 交付的）。
本模块只做两件事：

1. **把一段文案切成可匹配的段**（`segment_script`）
2. **匹配结果的读写**（`MatchStore`）

这样"匹配"就不是第二套检索，而是"对每段文案各调一次同一个检索"。

## 为什么分段粒度是 32 字（B 类自主决定）

依据是 **TASK-010 的模型约束 + TASK-009 的实测教训**：

- 中文 CLIP 的文本塔 `context_length = 52`，去掉 `[CLS]`/`[SEP]` 只剩 **50 个 token**；
  汉字多数 1 token，但生僻字会拆成多个 —— 取 32 字留出足够余量，避免静默截断
- TASK-009 的教训是"**长文本稀释语义**"：当时向量由整段 `answer` + 标签算出，
  长回答压过了标签的区分度，检索 top-1 只有 2/10。
  文案同理 —— 一段里塞三个意思，一个向量表达不了

所以：**句末标点为一级切分（语义完整），过长再按次级标点切（不硬切），
过短并入邻段（"然后""的"这类没有画面语义）**。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

#: 一级切分：句末标点。切完标点跟随前句。
_SENTENCE_END = "。！？!?；;…"

#: 二级切分：句内停顿。句子过长时先按这些切。
_CLAUSE = "，,、：:"

#: 段落默认上限与下限（字数）
DEFAULT_MAX_CHARS = 32
DEFAULT_MIN_CHARS = 6

#: 匹配结果的文件名允许字符：字母数字、下划线、连字符、汉字
_NAME_RE = re.compile(r"^[0-9A-Za-z_\-\u4e00-\u9fff]{1,64}$")

MATCH_DIR_NAME = "matches"


class MatchError(ValueError):
    """分段的输入不合法，或匹配结果不可用。"""


# ---------------------------------------------------------------- 分段
def _split_sentences(text: str) -> list[str]:
    sentences: list[str] = []
    buffer = ""
    for char in text:
        if char == "\n":
            if buffer.strip():
                sentences.append(buffer.strip())
            buffer = ""
            continue
        buffer += char
        if char in _SENTENCE_END:
            if buffer.strip():
                sentences.append(buffer.strip())
            buffer = ""
    if buffer.strip():
        sentences.append(buffer.strip())
    return sentences


def _split_clauses(sentence: str) -> list[str]:
    chunks: list[str] = []
    buffer = ""
    for char in sentence:
        buffer += char
        if char in _CLAUSE:
            if buffer.strip():
                chunks.append(buffer.strip())
            buffer = ""
    if buffer.strip():
        chunks.append(buffer.strip())
    return chunks


def _merge_short(pieces: list[str], min_chars: int) -> list[str]:
    """把过短的**碎片**并入上一片。

    只对**我们自己切出来的碎片**用（超长句子的二级切分、硬切兜底）——
    作者亲手用标点/换行断开的句子**一律保留**：
    `夜幕降临。` 只有 5 个字，但它是作者写的一个节拍，不是噪声。
    我们自己切出来的 `然后，` 才是噪声。
    """
    merged: list[str] = []
    for piece in pieces:
        if merged and len(piece) < min_chars:
            merged[-1] = merged[-1] + piece
        else:
            merged.append(piece)
    if len(merged) > 1 and len(merged[0]) < min_chars:
        merged[1] = merged[0] + merged[1]
        merged.pop(0)
    return merged


def segment_script(
    script: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    min_chars: int = DEFAULT_MIN_CHARS,
) -> list[str]:
    """把一段文案切成可匹配的段。

    - 一级：**作者写的句子**（句末标点 / 换行）—— `max_chars` 以内**原样保留**，
      再短也不合并：那是作者的节拍，不是噪声
    - 二级：句子超过 `max_chars` 时按句内标点再切
    - 三级：仍超过则硬切（兜底）
    - 收尾：**只有我们自己切出来的碎片**才按 `min_chars` 并入邻片

    `max_chars` 是**软目标**：合并短碎片会让个别片段略微超过它。
    真正的硬约束是模型的 50 token（超过会被静默截断），`max_chars` 留了足够余量。
    """
    if max_chars < 1:
        raise MatchError(f"max_chars 必须为正整数，得到 {max_chars}")
    if min_chars < 0:
        raise MatchError(f"min_chars 不能为负，得到 {min_chars}")

    text = (script or "").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        return []

    pieces: list[str] = []
    for sentence in _split_sentences(text):
        if len(sentence) <= max_chars:
            pieces.append(sentence)          # 作者写的句子：原样保留
            continue
        fragments: list[str] = []
        for chunk in _split_clauses(sentence):
            while len(chunk) > max_chars:
                fragments.append(chunk[:max_chars])
                chunk = chunk[max_chars:]
            if chunk:
                fragments.append(chunk)
        pieces.extend(_merge_short(fragments, min_chars))
    return [piece for piece in pieces if piece.strip()]


# ---------------------------------------------------------------- 结果存取
def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class MatchStore:
    """匹配结果的落盘与读取。一个名字一份 JSON，放在插件数据目录下。

    为什么是 JSON 而不是塞进素材库的 SQLite：
    匹配结果是**人工调整过的编辑决策**，不是从素材算出来的派生物 ——
    它有独立生命周期（可改名、可复制、可 diff、可给人看），
    也不该被 `library_rebuild` 清掉。库是"素材的派生数据"，匹配结果是"人的产物"。
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.dir = self.root / MATCH_DIR_NAME

    @staticmethod
    def validate_name(name: str) -> str:
        text = str(name or "").strip()
        if not _NAME_RE.match(text):
            raise MatchError(
                "匹配结果名只允许字母、数字、下划线、连字符与汉字，长度 1–64："
                f"{name!r}"
            )
        return text

    def path(self, name: str) -> Path:
        return self.dir / f"{self.validate_name(name)}.json"

    def exists(self, name: str) -> bool:
        return self.path(name).is_file()

    def save(self, payload: dict[str, Any]) -> Path:
        name = self.validate_name(str(payload.get("name", "")))
        payload = dict(payload)
        payload["name"] = name
        payload["updated_at"] = _now()
        payload.setdefault("created_at", payload["updated_at"])
        target = self.path(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return target

    def load(self, name: str) -> dict[str, Any]:
        target = self.path(name)
        if not target.is_file():
            raise MatchError(f"没有这份匹配结果：{name}（{target}）")
        try:
            return json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise MatchError(f"匹配结果不是合法 JSON：{target}\n{exc}") from exc

    def listing(self) -> list[dict[str, Any]]:
        if not self.dir.is_dir():
            return []
        items: list[dict[str, Any]] = []
        for path in sorted(self.dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                items.append({"name": path.stem, "broken": True})
                continue
            items.append({
                "name": data.get("name", path.stem),
                "segments": len(data.get("segments", [])),
                "created_at": data.get("created_at", ""),
                "updated_at": data.get("updated_at", ""),
            })
        return items


#: 匹配结果的格式版本。
#:
#: - `1`：候选只存 `shot_id` —— **重新分析素材库后会指向错镜头**（TASK-012a 修的就是它）
#: - `2`：候选存 `shot_key`（内容哈希 + 时间码，见 `xbc_va_library.shot_key`）；
#:   `shot_id` 降级为"写入时的快照"，只用于显示与对比，**不再当身份用**
RESULT_VERSION = 2


# ---------------------------------------------------------------- 结果编辑
def _segments(payload: dict[str, Any]) -> list[dict[str, Any]]:
    segments = payload.get("segments")
    if not isinstance(segments, list):
        raise MatchError("匹配结果里没有 segments 列表")
    return segments


def _segment_at(payload: dict[str, Any], index: int) -> dict[str, Any]:
    segments = _segments(payload)
    if not 0 <= index < len(segments):
        raise MatchError(f"段序号越界：{index}（共 {len(segments)} 段）")
    return segments[index]


def segment_at(payload: dict[str, Any], index: int) -> dict[str, Any]:
    """取某一提段（越界报错）。"""
    return _segment_at(payload, index)


def find_candidate(segment: dict[str, Any], shot_key: str) -> int:
    """按**持久标识**找候选下标。`shot_key` 是身份，不是快照 id。"""
    for position, candidate in enumerate(segment.get("candidates", [])):
        if str(candidate.get("shot_key", "")) == str(shot_key):
            return position
    return -1


def select_shot(
    payload: dict[str, Any], *, segment_index: int, shot_key: str,
    shot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把某段**选中的镜头**设为 `shot_key`（"替换/手动指定"都走这里）。

    - 该镜头已在候选里 → 直接选中
    - 不在候选里 → **插到候选首位**，标 `manual: true`、`score: null`

    为什么手动指定的候选**不给分数**：分数是"检索排名"的产物，
    人指定它不是检索的结果。硬塞一个 cosine 进去会让"分数"这个词
    在同一个列表里有两个含义。

    **身份是 `shot_key`，不是 `shot_id`** —— 重新分析后 id 会变，key 不会。
    """
    segment = _segment_at(payload, segment_index)
    candidates = segment.setdefault("candidates", [])
    position = find_candidate(segment, shot_key)

    if position < 0:
        if shot is None:
            raise MatchError(
                f"镜头 {shot_key} 不在第 {segment_index} 段的候选里，"
                "且没有提供它的镜头信息（无法插入）"
            )
        candidates.insert(0, {
            "shot_key": str(shot_key),
            "shot_id": int(shot.get("id", 0)),
            "video_path": str(shot.get("video_path", "")),
            "shot_index": int(shot.get("shot_index", 0)),
            "start": round(float(shot.get("start_seconds", 0.0)), 3),
            "end": round(float(shot.get("end_seconds", 0.0)), 3),
            "duration": round(float(shot.get("duration_seconds", 0.0)), 3),
            "score": None,
            "manual": True,
            "labels": list(shot.get("labels", [])),
        })
    else:
        # 从库里刷一遍快照 id —— 人可能隔了很久才来指定
        if shot is not None:
            candidates[position]["shot_id"] = int(shot.get("id", 0))
    segment["selected_shot_key"] = str(shot_key)
    if shot is not None:
        segment["selected_shot_id"] = int(shot.get("id", 0))
    return segment


def reorder_candidate(
    payload: dict[str, Any], *, segment_index: int, shot_key: str, direction: str
) -> dict[str, Any]:
    """把某段里的一个候选上移或下移一位。按 `shot_key` 定位。"""
    if direction not in ("up", "down"):
        raise MatchError(f"direction 只能是 up / down，得到 {direction!r}")
    segment = _segment_at(payload, segment_index)
    candidates = segment.setdefault("candidates", [])
    position = find_candidate(segment, shot_key)
    if position < 0:
        raise MatchError(f"镜头 {shot_key} 不在第 {segment_index} 段的候选里")
    target = position - 1 if direction == "up" else position + 1
    if not 0 <= target < len(candidates):
        raise MatchError(
            f"镜头 {shot_key} 已经在{'最前' if direction == 'up' else '最后'}，无法再"
            f"{'上' if direction == 'up' else '下'}移"
        )
    candidates[position], candidates[target] = candidates[target], candidates[position]
    return segment


def refresh_snapshots(
    payload: dict[str, Any], resolve: Callable[[str, str], dict[str, Any] | None]
) -> dict[str, Any]:
    """把所有候选的 **`shot_id` 快照刷新成当前库里的值**，并标出失效项。

    `resolve(shot_key, video_path)` 返回当前库里的镜头（查不到返回 `None`）。

    这是 TASK-012a 的**读取侧落点**：文件里存的是 `shot_key`，
    对外给出的 `shot_id` 一律**现算** —— 所以重新分析后依然指向正确的镜头。

    解析不到时标 `stale: true` 并给出原因，**绝不静默指向一个错的镜头**。
    """
    for segment in _segments(payload):
        for candidate in segment.get("candidates", []):
            key = str(candidate.get("shot_key", ""))
            if not key:
                candidate["stale"] = True
                candidate["stale_reason"] = (
                    "这条候选是 v1 旧格式，没有 shot_key，无法在库变化后重新定位"
                )
                continue
            shot = resolve(key, str(candidate.get("video_path", "")))
            if shot is None:
                candidate["stale"] = True
                candidate["stale_reason"] = (
                    "素材库里已找不到这个镜头（视频内容被替换或该镜头已不存在）"
                )
                candidate["shot_id"] = None
                continue
            candidate["stale"] = False
            candidate.pop("stale_reason", None)
            candidate["shot_id"] = int(shot["shot_id"])
            candidate["video_path"] = str(shot["video_path"])
            candidate["shot_index"] = int(shot["shot_index"])
            candidate["start"] = round(float(shot["start_seconds"]), 3)
            candidate["end"] = round(float(shot["end_seconds"]), 3)
            candidate["duration"] = round(float(shot["duration_seconds"]), 3)

        selected = segment.get("selected_shot_key")
        if not selected:
            continue
        chosen = next(
            (c for c in segment.get("candidates", [])
             if str(c.get("shot_key", "")) == str(selected) and not c.get("stale")),
            None,
        )
        segment["selected_shot_id"] = int(chosen["shot_id"]) if chosen else None
        segment["selected_stale"] = chosen is None
    return payload


__all__ = [
    "DEFAULT_MAX_CHARS",
    "DEFAULT_MIN_CHARS",
    "MATCH_DIR_NAME",
    "RESULT_VERSION",
    "MatchError",
    "MatchStore",
    "find_candidate",
    "refresh_snapshots",
    "reorder_candidate",
    "segment_at",
    "segment_script",
    "select_shot",
]
