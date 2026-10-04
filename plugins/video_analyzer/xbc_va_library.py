"""素材库存储层（SQLite）—— 只做存取，不碰 FFmpeg、不碰模型。

## 为什么是 SQLite

- 单文件、标准库自带（`sqlite3`）、支持事务与索引
- 库文件放在**插件自己的数据目录**里，与其他插件和用户文件隔离
- 库损坏/丢失时可以整体删掉，由视频文件重建（任务书要求"分析结果不是唯一副本"）

## 为什么不做通用存储抽象

任务书明确禁止（禁止项：通用存储抽象 / Core 存储能力）。
只有一个业务插件时，多一层抽象只是把复杂度从"能看见的地方"挪到"看不见的地方"。

## 表结构

| 表 | 内容 |
|---|---|
| `videos` | 路径、**内容哈希**、大小、时长、分辨率、状态、错误、分析时间 |
| `shots` | 所属视频、镜头序号、起止秒、AI 来源（provider/model） |
| `frames` | 所属镜头、时间码、图片路径 |
| `labels` | 所属镜头、标签文本、来源 |
| `vectors` | 镜头级与帧级 embedding（float32 打包成 BLOB），**并记录被嵌入的原始文本、provider/model、产生时间** |

**增量判定用内容哈希，不用路径或修改时间** —— 文件被改名、被复制、
时间戳被同步工具改动，都不应该导致重复分析；而内容真的变了就必须重分析。

## TASK-010 修的存储缺陷（v2）

v1 只存了 `labels`，但向量其实是由 `answer + labels` 算出来的 ——
**库里看不到真正被嵌入的文本，检索行为无法审计**。v2 在 `vectors` 上补三列：

| 列 | 作用 |
|---|---|
| `embed_input` | **真正被嵌入的输入**（可审计）：文本向量存原文，图片向量存图片路径 |
| `kind` | `frame`（单帧）/ `shot_centroid`（多帧质心），避免把质心当成单帧文本 |
| `created_at` | 向量产生时间（可追溯） |

配合既有的 `vectors.provider` / `vectors.model` / `videos.file_hash` / `videos.analyzed_at`，
一次检索结果可以被完整解释。**

旧库（v1）升级时这三列会被补上，但 `embed_input` 只能是空字符串 ——
**已经算过的向量无法反推原文**，所以旧库需要重新入库才能获得可审计性。
"""

from __future__ import annotations

import array
import math
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 3

DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS videos (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    path             TEXT    NOT NULL UNIQUE,
    file_hash        TEXT    NOT NULL DEFAULT '',
    size_bytes       INTEGER NOT NULL DEFAULT 0,
    duration_seconds REAL,
    width            INTEGER,
    height           INTEGER,
    fps              REAL,
    status           TEXT    NOT NULL DEFAULT 'pending',
    error            TEXT    NOT NULL DEFAULT '',
    analyzed_at      TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_videos_hash ON videos(file_hash);
CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status);

CREATE TABLE IF NOT EXISTS shots (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id         INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    shot_index       INTEGER NOT NULL,
    start_seconds    REAL    NOT NULL,
    end_seconds      REAL    NOT NULL,
    duration_seconds REAL    NOT NULL,
    ai_provider      TEXT    NOT NULL DEFAULT '',
    ai_model         TEXT    NOT NULL DEFAULT '',
    UNIQUE(video_id, shot_index)
);
CREATE INDEX IF NOT EXISTS idx_shots_video ON shots(video_id);

CREATE TABLE IF NOT EXISTS frames (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    shot_id      INTEGER NOT NULL REFERENCES shots(id) ON DELETE CASCADE,
    time_seconds REAL    NOT NULL,
    file_path    TEXT    NOT NULL,
    UNIQUE(shot_id, file_path)
);
CREATE INDEX IF NOT EXISTS idx_frames_shot ON frames(shot_id);

CREATE TABLE IF NOT EXISTS labels (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    shot_id  INTEGER NOT NULL REFERENCES shots(id) ON DELETE CASCADE,
    frame_id INTEGER NOT NULL DEFAULT 0,
    text     TEXT    NOT NULL,
    source   TEXT    NOT NULL DEFAULT 'ai',
    UNIQUE(shot_id, text, source)
);
CREATE INDEX IF NOT EXISTS idx_labels_text ON labels(text);
CREATE INDEX IF NOT EXISTS idx_labels_shot ON labels(shot_id);

"""

#: `vectors` 的建表语句单独拆出来 —— 迁移时要**重建**这张表
#: （SQLite 改不了已有约束），所以需要能单独执行它。
VECTORS_DDL = """
CREATE TABLE IF NOT EXISTS vectors (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    shot_id     INTEGER NOT NULL REFERENCES shots(id) ON DELETE CASCADE,
    frame_id    INTEGER NOT NULL DEFAULT 0,
    -- 'frame' = 单帧向量；'shot_centroid' = 该镜头多帧向量的质心
    kind        TEXT    NOT NULL DEFAULT 'frame',
    provider    TEXT    NOT NULL,
    model       TEXT    NOT NULL,
    dim         INTEGER NOT NULL,
    vector      BLOB    NOT NULL,
    -- **真正被嵌入的输入**：文本向量存原文，图片向量存图片路径。
    -- TASK-009 只存了 labels，而向量其实是由 answer + labels 算出来的 ——
    -- 导致检索行为无法审计。这一列是 TASK-010 的核心修复。
    embed_input TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL DEFAULT '',
    -- 一个帧可以同时有**多个向量空间的向量**（中文 CLIP 图片向量 + Caption 文本向量）。
    -- 它们维度可能相同、但语义空间不同，绝不可互相比较 ——
    -- 所以唯一键必须带上 provider + model，而不是只按 (shot_id, frame_id)。
    UNIQUE(shot_id, frame_id, provider, model)
);
CREATE INDEX IF NOT EXISTS idx_vectors_shot ON vectors(shot_id);
CREATE INDEX IF NOT EXISTS idx_vectors_space ON vectors(provider, model);
"""

DDL = DDL + VECTORS_DDL

#: `vectors` 表的目标列集合（迁移用它判断要不要重建表）
VECTOR_COLUMNS = (
    "id", "shot_id", "frame_id", "kind", "provider", "model",
    "dim", "vector", "embed_input", "created_at",
)

#: 目标唯一键的列顺序
VECTOR_UNIQUE_COLUMNS = ["shot_id", "frame_id", "provider", "model"]


class LibraryError(RuntimeError):
    """素材库的存取失败。"""


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def pack_vector(values: list[float]) -> bytes:
    """把向量打包成 float32 字节串 —— 768 维只占 3KB，比 JSON 省一半以上。"""
    return array.array("f", [float(v) for v in values]).tobytes()


def unpack_vector(blob: bytes) -> list[float]:
    values = array.array("f")
    values.frombytes(blob)
    return list(values)


def cosine(left: list[float], right: list[float]) -> float:
    """余弦相似度。维度不一致返回 0.0（而不是抛错 —— 检索时不该因为一条脏数据整体失败）。"""
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    norm_left = math.sqrt(sum(a * a for a in left))
    norm_right = math.sqrt(sum(b * b for b in right))
    if not norm_left or not norm_right:
        return 0.0
    return dot / (norm_left * norm_right)


class Library:
    """素材库。一个实例对应一个库文件。"""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    # ---------------- 连接 ----------------
    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.path))
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def session(self) -> Iterator[sqlite3.Connection]:
        """一次数据库会话：**负责关闭连接**。

        为什么不能直接 `with self.connect() as connection:`：
        `sqlite3.Connection` 的上下文管理器**只管理事务，不关闭连接**。
        连接一直开着，在 Windows 上库文件就被锁住 ——
        `drop()`（整库重建）会直接 `WinError 32 另一个程序正在使用此文件`。
        这是实测踩到的坑，不是理论问题。
        """
        connection = self.connect()
        try:
            with connection:  # 事务：正常提交、异常回滚
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        """建表 + 迁移。库文件损坏时抛**可操作**的 `LibraryError`。

        刻意**不自动重建**：自动删库等于静默丢数据。正确的恢复路径是显式调用
        `library_rebuild` —— 它先删文件、不依赖打开成功。
        """
        try:
            with self.session() as connection:
                connection.executescript(DDL)
                self._migrate(connection)
                connection.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
        except sqlite3.DatabaseError as exc:
            raise LibraryError(
                f"素材库文件不是有效的数据库（可能已损坏）：{self.path}\n"
                f"原始错误：{exc}\n"
                "恢复方式：调用 library_rebuild（会丢弃库文件并从视频重新分析）。"
            ) from exc

    @staticmethod
    def _has_space_unique(connection: sqlite3.Connection) -> bool:
        """`vectors` 的唯一键是否已经是 `(shot_id, frame_id, provider, model)`。"""
        for index in connection.execute("PRAGMA index_list(vectors)").fetchall():
            if not index["unique"]:
                continue
            columns = [
                row["name"]
                for row in connection.execute(f"PRAGMA index_info({index['name']})")
            ]
            if columns == VECTOR_UNIQUE_COLUMNS:
                return True
        return False

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> list[str]:
        """把旧库补到当前 schema。返回实际做了哪些迁移动作。

        `CREATE TABLE IF NOT EXISTS` **既不会加列、也不会改约束**，所以老库必须走这里。

        v1 / v2 的 `vectors` 有两个问题：

        1. 缺少审计列（`kind` / `embed_input` / `created_at`）
        2. `UNIQUE(shot_id, frame_id)` 只允许每帧**一条**向量 —— 而 TASK-010 要让
           **中文 CLIP 图片向量**与 **Caption 文本向量**（两个不同向量空间）共存

        SQLite 改不了已有约束，所以走**重建表 + 搬数据**。
        `embed_input` 搬过来是空串 —— **已算过的向量无法反推输入**，
        旧库要重新入库才有可审计性（`stats()['note']` 会如实说明）。
        """
        existing = {
            row["name"] for row in connection.execute("PRAGMA table_info(vectors)")
        }
        if not existing:
            return []  # executescript 刚建好的就是目标结构

        if set(VECTOR_COLUMNS) <= existing and Library._has_space_unique(connection):
            return []

        actions: list[str] = []
        # v2 的中间版本把这一列叫 embed_text（那时只考虑文本向量），统一搬到 embed_input
        source = (
            "embed_text"
            if "embed_text" in existing and "embed_input" not in existing
            else "embed_input"
        )
        if source != "embed_input":
            actions.append("rename:embed_text->embed_input")

        kind_expr = "kind" if "kind" in existing else "'frame'"
        input_expr = source if source in existing else "''"
        created_expr = "created_at" if "created_at" in existing else "''"

        connection.execute("ALTER TABLE vectors RENAME TO vectors_pre_t10")
        connection.executescript(VECTORS_DDL)
        connection.execute(
            "INSERT INTO vectors (id, shot_id, frame_id, kind, provider, model,"
            " dim, vector, embed_input, created_at)"
            f" SELECT id, shot_id, frame_id, {kind_expr}, provider, model,"
            f" dim, vector, {input_expr}, {created_expr} FROM vectors_pre_t10"
        )
        connection.execute("DROP TABLE vectors_pre_t10")
        actions.append("rebuild:vectors(unique=shot_id,frame_id,provider,model)")
        return actions

    def exists(self) -> bool:
        return self.path.is_file()

    def drop(self) -> None:
        """整库删除。库不是唯一副本 —— 视频文件还在，可以重建。"""
        if self.path.is_file():
            self.path.unlink()

    # ---------------- 写入 ----------------
    def upsert_video(
        self,
        *,
        path: str,
        file_hash: str,
        size_bytes: int,
        duration_seconds: float | None = None,
        width: int | None = None,
        height: int | None = None,
        fps: float | None = None,
        status: str = "pending",
    ) -> int:
        """登记或更新一个视频，返回 video id。

        **按内容哈希决定是否重置分析结果**：哈希没变就保留原有镜头/标签，
        哈希变了就把旧的级联删掉（重新分析）。
        """
        with self.session() as connection:
            row = connection.execute(
                "SELECT id, file_hash FROM videos WHERE path = ?", (path,)
            ).fetchone()

            if row is None:
                cursor = connection.execute(
                    "INSERT INTO videos(path, file_hash, size_bytes, duration_seconds,"
                    " width, height, fps, status) VALUES(?,?,?,?,?,?,?,?)",
                    (path, file_hash, size_bytes, duration_seconds, width, height, fps, status),
                )
                return int(cursor.lastrowid)

            video_id = int(row["id"])
            if row["file_hash"] != file_hash:
                # 文件内容变了：旧分析结果作废
                connection.execute("DELETE FROM shots WHERE video_id = ?", (video_id,))
            connection.execute(
                "UPDATE videos SET file_hash=?, size_bytes=?, duration_seconds=?,"
                " width=?, height=?, fps=? WHERE id=?",
                (file_hash, size_bytes, duration_seconds, width, height, fps, video_id),
            )
            return video_id

    def set_status(self, video_id: int, status: str, error: str = "") -> None:
        with self.session() as connection:
            connection.execute(
                "UPDATE videos SET status=?, error=?, analyzed_at=? WHERE id=?",
                (status, error[:2000], now() if status != "pending" else "", video_id),
            )

    def replace_analysis(
        self,
        video_id: int,
        *,
        shots: list[dict[str, Any]],
        ai_provider: str = "",
        ai_model: str = "",
    ) -> int:
        """用一个视频的完整分析结果替换旧结果（单事务）。

        `shots` 里每项形如：
        `{index, start, end, duration, frames: [{time, file}], labels: [str], vectors: [...]}`
        """
        with self.session() as connection:
            connection.execute("DELETE FROM shots WHERE video_id = ?", (video_id,))
            count = 0
            for shot in shots:
                cursor = connection.execute(
                    "INSERT INTO shots(video_id, shot_index, start_seconds, end_seconds,"
                    " duration_seconds, ai_provider, ai_model) VALUES(?,?,?,?,?,?,?)",
                    (
                        video_id,
                        int(shot["index"]),
                        float(shot["start"]),
                        float(shot["end"]),
                        float(shot["duration"]),
                        ai_provider,
                        ai_model,
                    ),
                )
                shot_id = int(cursor.lastrowid)
                count += 1

                frame_ids: list[int] = []
                for frame in shot.get("frames", []):
                    frame_cursor = connection.execute(
                        "INSERT INTO frames(shot_id, time_seconds, file_path) VALUES(?,?,?)",
                        (shot_id, float(frame["time"]), str(frame["file"])),
                    )
                    frame_ids.append(int(frame_cursor.lastrowid))

                for text in shot.get("labels", []):
                    connection.execute(
                        "INSERT OR IGNORE INTO labels(shot_id, frame_id, text, source)"
                        " VALUES(?,?,?,'ai')",
                        (shot_id, 0, str(text)),
                    )

                def _put(vector: dict[str, Any], frame_id: int, kind: str) -> None:
                    """写一条向量。**唯一键含 provider+model**，所以不同向量空间可共存。"""
                    connection.execute(
                        "INSERT OR REPLACE INTO vectors(shot_id, frame_id, kind, provider,"
                        " model, dim, vector, embed_input, created_at)"
                        " VALUES(?,?,?,?,?,?,?,?,?)",
                        (
                            shot_id,
                            frame_id,
                            kind,
                            str(vector["provider"]),
                            str(vector["model"]),
                            int(vector["dim"]),
                            pack_vector(vector["values"]),
                            str(vector.get("embed_input", "")),
                            str(vector.get("created_at") or now()),
                        ),
                    )

                # 每个向量空间各写一组：Caption 文本向量（如 768 维）与
                # 中文 CLIP 图片向量（1024 维）。它们维度可能相同、但空间不同，
                # 靠 (provider, model) 区分 —— 检索时也按这个过滤，不会混算。
                for key, kind in (("frame_vectors", "frame"),
                                  ("clip_frame_vectors", "frame")):
                    for vector in shot.get(key, []):
                        index = int(vector.get("frame_index", -1))
                        frame_id = frame_ids[index] if 0 <= index < len(frame_ids) else 0
                        _put(vector, frame_id, kind)

                for key in ("vector", "clip_vector"):
                    shot_vector = shot.get(key)
                    if shot_vector:
                        _put(shot_vector, 0, "shot_centroid")
            return count

    # ---------------- 查询 ----------------
    def get_video_by_path(self, path: str) -> sqlite3.Row | None:
        with self.session() as connection:
            return connection.execute(
                "SELECT * FROM videos WHERE path = ?", (path,)
            ).fetchone()

    def videos(self, status: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM videos"
        args: tuple = ()
        if status:
            sql += " WHERE status = ?"
            args = (status,)
        with self.session() as connection:
            return list(connection.execute(sql + " ORDER BY path", args))

    def stats(self) -> dict[str, Any]:
        try:
            return self._stats()
        except sqlite3.DatabaseError as exc:
            raise LibraryError(
                f"读不出库统计（库文件可能损坏）：{self.path}\n"
                f"原始错误：{exc}\n恢复方式：library_rebuild。"
            ) from exc

    def _stats(self) -> dict[str, Any]:
        with self.session() as connection:
            video_rows = connection.execute(
                "SELECT status, COUNT(*) AS n FROM videos GROUP BY status"
            ).fetchall()
            total_shots = connection.execute("SELECT COUNT(*) FROM shots").fetchone()[0]
            total_frames = connection.execute("SELECT COUNT(*) FROM frames").fetchone()[0]
            total_labels = connection.execute("SELECT COUNT(*) FROM labels").fetchone()[0]
            distinct_labels = connection.execute(
                "SELECT COUNT(DISTINCT text) FROM labels"
            ).fetchone()[0]
            total_vectors = connection.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]
            dim = connection.execute(
                "SELECT dim FROM vectors WHERE frame_id = 0 LIMIT 1"
            ).fetchone()
            auditable = connection.execute(
                "SELECT COUNT(*) FROM vectors WHERE embed_input <> ''"
            ).fetchone()[0]
            kinds = {
                row["kind"]: row["n"]
                for row in connection.execute(
                    "SELECT kind, COUNT(*) AS n FROM vectors GROUP BY kind"
                ).fetchall()
            }
        note = ""
        if total_vectors and not auditable:
            note = (
                "库里所有向量都没有 embed_input（v1 旧库）—— "
                "已经算过的向量无法反推原文，需要重新入库（library_rebuild）才有可审计性。"
            )
        elif 0 < auditable < total_vectors:
            note = f"有 {total_vectors - auditable} 条向量缺少 embed_input（v1 遗留），建议重新入库。"
        return {
            "library_path": str(self.path),
            "library_exists": self.exists(),
            "videos_by_status": {row["status"]: row["n"] for row in video_rows},
            "videos_total": sum(row["n"] for row in video_rows),
            "shots": total_shots,
            "frames": total_frames,
            "labels": total_labels,
            "distinct_labels": distinct_labels,
            "vectors": total_vectors,
            "vectors_by_kind": kinds,
            "vectors_with_embed_input": auditable,
            "auditable": bool(total_vectors) and auditable == total_vectors,
            "spaces": self.spaces(),
            "schema_version": SCHEMA_VERSION,
            "vector_dim": dim["dim"] if dim else None,
            "note": note,
        }

    # ---------------- 检索：标签 ----------------
    def search_labels(
        self, query: str, *, limit: int = 10, match: str = "fuzzy"
    ) -> list[dict[str, Any]]:
        """按标签找镜头。

        `match="exact"` 精确相等；`match="fuzzy"` 子串包含。
        **两者都只是字符串匹配** —— 标签是自由文本，别说对了"同义但不同字"的情况
        （这正是必须同时提供语义检索的原因）。

        **一个镜头只出一行**：一个镜头可能同时命中多个标签（例如既命中"测试画面"
        又命中"电视测试画面"），早期实现按 `(镜头, 命中标签)` 分组，导致同一个镜头
        在结果里重复出现 —— 调用方要的是镜头列表，不是"镜头×标签"列表。
        命中到的标签全部放进 `matched_labels`。
        """
        text = (query or "").strip()
        if not text:
            return []
        if match == "exact":
            where, args = "l.text = ?", (text,)
        else:
            where, args = "l.text LIKE ?", (f"%{text}%",)

        sql = f"""
            SELECT v.id AS video_id, v.path AS video_path,
                   s.id AS shot_id, s.shot_index, s.start_seconds, s.end_seconds,
                   s.duration_seconds,
                   group_concat(DISTINCT l.text) AS matched_labels,
                   (SELECT group_concat(text) FROM labels WHERE shot_id = s.id) AS shot_labels
              FROM labels l
              JOIN shots  s ON s.id = l.shot_id
              JOIN videos v ON v.id = s.video_id
             WHERE {where}
             GROUP BY s.id
             ORDER BY v.path, s.shot_index
             LIMIT ?
        """
        with self.session() as connection:
            rows = connection.execute(sql, (*args, int(limit))).fetchall()
        return [self._shot_row(row) for row in rows]

    # ---------------- 检索：语义 ----------------
    def spaces(self) -> list[dict[str, Any]]:
        """库里一共有哪些**向量空间**（按 provider+model 分组）。

        一个帧可以同时有多个空间的向量（Caption 文本 / 中文 CLIP 图片）。
        **它们不可互相比较** —— 所以检索必须先选定一个空间，再在该空间内排序。
        """
        sql = """
            SELECT ve.provider, ve.model, ve.dim, ve.kind, COUNT(*) AS n
              FROM vectors ve
             GROUP BY ve.provider, ve.model, ve.dim, ve.kind
             ORDER BY ve.provider, ve.model, ve.kind
        """
        with self.session() as connection:
            rows = connection.execute(sql).fetchall()
        grouped: dict[tuple, dict[str, Any]] = {}
        for row in rows:
            key = (row["provider"], row["model"])
            entry = grouped.setdefault(key, {
                "provider": row["provider"], "model": row["model"],
                "dim": row["dim"], "vectors": 0, "frames": 0, "shot_centroids": 0,
            })
            entry["vectors"] += row["n"]
            if row["kind"] == "shot_centroid":
                entry["shot_centroids"] += row["n"]
            else:
                entry["frames"] += row["n"]
        return list(grouped.values())

    def shot_vectors(
        self, *, provider: str | None = None, model: str | None = None
    ) -> list[dict[str, Any]]:
        """镜头级向量（`frame_id = 0`），**带可审计的溯源信息**。

        `provider` / `model` 用于**选定向量空间** —— 传了就只返回该空间的向量。
        """
        clauses = ["ve.frame_id = 0"]
        args: list[Any] = []
        if provider is not None:
            clauses.append("ve.provider = ?")
            args.append(provider)
        if model is not None:
            clauses.append("ve.model = ?")
            args.append(model)
        sql = f"""
            SELECT ve.shot_id, ve.provider, ve.model, ve.dim, ve.vector,
                   ve.kind, ve.embed_input, ve.created_at,
                   s.shot_index, s.start_seconds, s.end_seconds, s.duration_seconds,
                   v.id AS video_id, v.path AS video_path
              FROM vectors ve
              JOIN shots  s ON s.id = ve.shot_id
              JOIN videos v ON v.id = s.video_id
             WHERE {' AND '.join(clauses)}
        """
        with self.session() as connection:
            rows = connection.execute(sql, tuple(args)).fetchall()
        return [
            {
                "shot_id": row["shot_id"],
                "video_id": row["video_id"],
                "video_path": row["video_path"],
                "shot_index": row["shot_index"],
                "start": row["start_seconds"],
                "end": row["end_seconds"],
                "duration": row["duration_seconds"],
                "provider": row["provider"],
                "model": row["model"],
                "dim": row["dim"],
                "kind": row["kind"],
                "embed_input": row["embed_input"],
                "created_at": row["created_at"],
                "values": unpack_vector(row["vector"]),
            }
            for row in rows
        ]

    # ---------------- 审计：这条向量到底是用什么文本算出来的 ----------------
    def vector_audit(self, shot_id: int | None = None) -> list[dict[str, Any]]:
        """返回向量的完整溯源：被嵌入的文本、provider/model、维度、产生时间、来源素材哈希。

        这是 TASK-010 第一部分的核心 —— v1 只存 labels，看不到真正被嵌入的文本，
        检索行为无法解释。现在每一条向量都能回答"它是从哪个字符串算出来的"。
        """
        sql = """
            SELECT ve.id AS vector_id, ve.shot_id, ve.frame_id, ve.kind,
                   ve.provider, ve.model, ve.dim, ve.embed_input, ve.created_at,
                   ve.vector,
                   s.shot_index, s.start_seconds, s.end_seconds,
                   v.path AS video_path, v.file_hash, v.analyzed_at
              FROM vectors ve
              JOIN shots  s ON s.id = ve.shot_id
              JOIN videos v ON v.id = s.video_id
        """
        args: tuple = ()
        if shot_id is not None:
            sql += " WHERE ve.shot_id = ?"
            args = (int(shot_id),)
        sql += " ORDER BY ve.shot_id, ve.frame_id"

        with self.session() as connection:
            rows = connection.execute(sql, args).fetchall()

        return [
            {
                "vector_id": row["vector_id"],
                "shot_id": row["shot_id"],
                "frame_id": row["frame_id"],
                "kind": row["kind"],
                "provider": row["provider"],
                "model": row["model"],
                "dim": row["dim"],
                "embed_input": row["embed_input"],
                "embed_input_chars": len(row["embed_input"] or ""),
                "has_embed_input": bool(row["embed_input"]),
                "created_at": row["created_at"],
                "video_path": row["video_path"],
                "video_hash": (row["file_hash"] or "")[:16],
                "video_analyzed_at": row["analyzed_at"],
                "shot_index": row["shot_index"],
                "start": round(row["start_seconds"], 3),
                "end": round(row["end_seconds"], 3),
                "vector_bytes": len(row["vector"]),
            }
            for row in rows
        ]

    def shot_labels(self, shot_ids: list[int]) -> dict[int, list[str]]:
        if not shot_ids:
            return {}
        marks = ",".join("?" for _ in shot_ids)
        with self.session() as connection:
            rows = connection.execute(
                f"SELECT shot_id, text FROM labels WHERE shot_id IN ({marks})"
                " ORDER BY id",
                tuple(shot_ids),
            ).fetchall()
        result: dict[int, list[str]] = {}
        for row in rows:
            result.setdefault(row["shot_id"], []).append(row["text"])
        return result

    # ---------------- 导出用 ----------------
    def shot(self, shot_id: int) -> dict[str, Any] | None:
        sql = """
            SELECT s.*, v.path AS video_path, v.duration_seconds AS video_duration,
                   v.file_hash AS video_hash
              FROM shots s JOIN videos v ON v.id = s.video_id
             WHERE s.id = ?
        """
        with self.session() as connection:
            row = connection.execute(sql, (int(shot_id),)).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["labels"] = self.shot_labels([int(shot_id)]).get(int(shot_id), [])
        return data

    @staticmethod
    def _shot_row(row: sqlite3.Row) -> dict[str, Any]:
        matched = row["matched_labels"] or ""
        labels = row["shot_labels"] or ""
        return {
            "video_id": row["video_id"],
            "video_path": row["video_path"],
            "shot_id": row["shot_id"],
            "shot_index": row["shot_index"],
            "start": round(row["start_seconds"], 3),
            "end": round(row["end_seconds"], 3),
            "duration": round(row["duration_seconds"], 3),
            "matched_labels": [item for item in matched.split(",") if item],
            "labels": [item for item in labels.split(",") if item],
        }
