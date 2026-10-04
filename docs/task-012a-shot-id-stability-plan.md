# TASK-012a 方案报告 —— shot_id 稳定性

| 项 | 内容 |
|---|---|
| 类型 | **A 类前置：方案先报告，等确认后再改** |
| 状态 | **方案已确认（选 B），已实现 —— 见[交付报告](task-012a-shot-id-stability-report.md)** |
| 结论 | **推荐方案 B**（镜头加持久标识 `shot_key`，匹配结果存它）；方案 A 单独做**不能解决问题** |

---

## 1. 根因

### 1.1 三件事叠在一起

**① `shots.id` 是 `AUTOINCREMENT`，永不复用**

```sql
-- plugins/video_analyzer/xbc_va_library.py:79
CREATE TABLE IF NOT EXISTS shots (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ...
```

SQLite 的 `AUTOINCREMENT` 会在 `sqlite_sequence` 里维护一个**单调递增**计数器，
删除行**不会**让 id 回收 —— 下一个 id 只会更大。

**② `replace_analysis` 走的是 DELETE + INSERT**

```python
# plugins/video_analyzer/xbc_va_library.py:371-388
with self.session() as connection:
    connection.execute("DELETE FROM shots WHERE video_id = ?", (video_id,))
    for shot in shots:
        cursor = connection.execute(
            "INSERT INTO shots(...) VALUES(?,?,?,?,?,?,?)", ...)
        shot_id = int(cursor.lastrowid)          # ← 每次都是新 id
```

**同一个镜头，每次重新分析都拿到一个全新的 id。**

**③ `PRAGMA foreign_keys = ON` → 删除会级联**

```python
# 同文件 :195
connection.execute("PRAGMA foreign_keys = ON")
```

`frames` / `labels` / `vectors` 都是 `ON DELETE CASCADE`，
所以 `DELETE FROM shots` 会把它们的行**一并删掉重建** ——
`frames.id` / `labels.id` / `vectors.id` 也跟着churn。

### 1.2 实测证据（三个视频，跑了三轮）

| 操作 | shot_id | `sqlite_sequence.shots` |
|---|---|---|
| 首次入库 | 1, 2, 3 | 3 |
| `library_scan(force=True)` | **4, 5, 6** | 6 |
| 再 `force=True` | **7, 8, 9** | 9 |
| `library_rebuild`（删库重建） | 1, 2, 3（**重置，偶然相同**） | 3 |

```
首次入库 → force 重新分析（按 文件+shot_index 对齐 3 个镜头）
  shot_id 不变:   0/3   ← 会变
  时间码不变:     3/3
  file_hash 不变: 3/3

第一次 force → 第二次 force
  shot_id 不变:   0/3   ← 又变了
  时间码不变:     3/3
  file_hash 不变: 3/3
```

### 1.3 一个容易忽略的坑：`library_rebuild` 更彻底

`library_rebuild` **直接删掉库文件重建**（`plugin.py:923`），所以：

- `videos.id` 也会**从头重置**
- 上表里 rebuild 后 shot_id 恰好回到 1/2/3，是因为视频数量与扫描顺序没变 ——
  **这是巧合，不是保证**（多一个视频、扫描顺序变了就不同）

**推论：任何自增 id（`shots.id` 也好 `videos.id` 也好）都不能当持久身份。**

### 1.4 什么在变、什么不变

| 字段 | force 重分析 | rebuild | 能当身份吗 |
|---|---|---|---|
| `shots.id` | ❌ 变（单调增长） | ❌ 重置 | 否 |
| `videos.id` | ✅ 不变 | ❌ 重置 | 否（rebuild 就废） |
| `frames.id` / `labels.id` / `vectors.id` | ❌ 变（级联重建） | ❌ 重置 | 否 |
| `videos.file_hash` | ✅ 不变 | ✅ 不变 | **可以** |
| `shots.shot_index` | ✅ 不变\* | ✅ 不变\* | 部分 |
| `shots.start_seconds` / `end_seconds` | ✅ 不变 | ✅ 不变 | **可以** |

\* `shot_index` 与时间码在**检测参数没变**时稳定；如果 `scene_threshold` 改了，
镜头边界会变，两者都可能漂移。

**可用的持久身份 = `(file_hash, start_seconds, end_seconds)`** ——
内容寻址 + 时间码，不依赖任何自增 id。

---

## 2. 影响面（谁真的存了 shot_id）

| 位置 | 性质 | 受影响？ |
|---|---|---|
| `matches/<名字>.json` 的 `candidates[].shot_id`、`selected_shot_id` | **持久引用** | ✅ **是问题所在** |
| `library_audit(shot_id=)` / `library_export(shot_id=)` | 调用期参数 | ❌ 调用方现取现用 |
| `match_select(shot_id=)` / `match_reorder(shot_id=)` | 调用期参数 | ⚠️ 间接：它们定位的是**已存盘**的候选项 |
| `frames` / `labels` / `vectors` 的 `shot_id` | 库内，与 shots 同生共死 | ❌ 库内自洽 |

**所以对外持久引用只有一处：TASK-011 的匹配结果 JSON。** 改动面很窄。

---

## 3. 方案对比

### 方案 A：保留行 id（`replace_analysis` 改 UPSERT）

**做法**：对 `(video_id, shot_index)` 已存在的行做 `UPDATE`（或
`INSERT ... ON CONFLICT(video_id, shot_index) DO UPDATE`），不再先 DELETE 全部；
多出来的旧镜头（新镜位数变少）再删。

| | |
|---|---|
| ✅ 优点 | force 重分析后 `shot_id` **稳定**；`frames`/`labels`/`vectors` 不再被无谓级联删除 |
| ❌ **致命缺点** | **单独做不能解决问题**：如果检测参数变了、镜头边界变了，`shot_index` 相同**不代表同一个镜头** —— id 保留了，但它指向的**内容变了**，等于"稳定地指向错镜头" |
| ❌ 覆盖不全 | `library_rebuild` 是删库重建，UPSERT 无从谈起，id 照样重置 |
| 改动面 | `replace_analysis` 一处 + 处理镜位数变少的清理 |

**结论：A 让 id 变稳，但不让 id 变对。**

### 方案 B：镜头加持久标识 `shot_key`，匹配结果存它 ★推荐

**做法**：

1. `shots` 加列 `shot_key TEXT NOT NULL DEFAULT ''` + 唯一索引
2. key 由 **`(file_hash, start_seconds, end_seconds)`** 生成，例：
   `sha1(f"{file_hash}:{start:.3f}:{end:.3f}")[:16]`
   —— **内容寻址 + 时间码，不含任何自增 id**
3. `replace_analysis` 写入时算 key：同一个视频、同一条边界 → **同一个 key**
4. 匹配结果 `version: 1 → 2`：候选存 `shot_key`；`shot_id` 降级为
   "写入时的快照"，只用于显示与对比
5. **读取时解析**：`shot_key → 当前 shot_id`，以解析结果为准对外输出
6. 解析不到 → 该项标 `stale: true` 并给出原因（文件被替换 / 时间码对不上 / 镜头已删），
   **绝不静默指向一个错的镜头**

| | |
|---|---|
| ✅ 优点 | **force 与 rebuild 两条路径都覆盖**（rebuild 后 file_hash 与时间码不变 → key 不变）；不依赖任何自增 id；素材改名/移动也能跟上（key 里不含路径）；失效时**明确报错**而不是悄悄指错 |
| ✅ 直接满足验收 4 | "重新分析后旧匹配结果仍指向正确镜头" |
| ⚠️ 代价 | schema 迁移 3→4；结果结构 v1→v2；多一层解析 |
| ⚠️ 已知边界 | 视频**内容被替换**（重新编码）→ file_hash 变 → key 变 → 旧结果判为失效。**这是正确行为**（内容变了，镜头就不是同一个） |

### 方案 C：A + B

- **B 保证"对"**，**A 保证"稳"**（同一个镜头的 id 在 force 后也不变：日志好读、
  人眼对得上、减少无谓的级联删除）
- 两者**互相独立，可以分开做**；A 不做也不影响 B 的正确性

---

## 4. 推荐

**做 B（必需）；A 作为可选的第二步。**

理由：用户报的症状是"旧匹配结果**指向错镜头**" —— 这是**正确性**问题，不是稳定性问题。
A 只解决"id 变不变"，不解决"id 指向得对不对"。B 直接解决正确性，
且同时覆盖 force 与 rebuild 两条路径。

---

## 5. 方案 B 的具体改动清单

### 5.1 `plugins/video_analyzer/xbc_va_library.py`

| 改动 | 说明 |
|---|---|
| `SCHEMA_VERSION` 3 → 4 | |
| `shots` 加列 `shot_key` | `TEXT NOT NULL DEFAULT ''`，加 `UNIQUE INDEX idx_shots_key` |
| `_migrate()` 加 v3→v4 分支 | `ALTER TABLE shots ADD COLUMN shot_key`，然后**回填**（用 file_hash + 时间码算） |
| 新增 `shot_key(file_hash, start, end)` | 纯函数，可单测 |
| `replace_analysis` | 写入 `shot_key` |
| 新增 `shot_by_key(key)` / `shots_with_keys()` | 供解析 |
| `shot_vectors()` 等返回值带上 `shot_key` | 供匹配写入 |

### 5.2 `plugins/video_analyzer/xbc_va_match.py`

| 改动 | 说明 |
|---|---|
| 结果 `version: 1 → 2` | |
| 候选项加 `shot_key` | `shot_id` 保留为快照 |
| `selected_shot_id` → `selected_shot_key` | 选中以 key 为身份 |
| `select_shot` / `reorder_candidate` 按 key 定位 | |
| v1 结果的处理 | 读取到 v1 → 标记 `legacy`（见 §7） |

### 5.3 `plugins/video_analyzer/plugin.py`

| 改动 | 说明 |
|---|---|
| `script_match` | 候选写入 `shot_key` |
| `match_show` | **把 key 解析成当前 shot_id**并回显；顺带返回 `shot_key` |
| `match_select` / `match_reorder` | 定位改走 key（见 §6 的参数取舍） |
| 解析不到 | 该项标 `stale` + 原因，并在返回里给出 `stale_count` |

### 5.4 **不动的东西**（对应你的要求 3）

- ❌ 不改检索（`_retrieval_context` / `_score_query`）
- ❌ 不改分段（`segment_script`）
- ❌ 不改分析算法、不重切镜头、不重算向量
- ❌ 不改 Core（零改动）
- ❌ 不改导出 / 审计 / 扫描工具的签名
- ✅ 只动 `shots` 表的**加列**（不重建、不改既有列）

---

## 6. 需要你定的三个取舍

### 取舍 1：调整工具的地址参数用 `shot_id` 还是 `shot_key`？

| 选项 | 说明 |
|---|---|
| **① 保持 `shot_id`（推荐）** | 工具签名不变（"不动其他功能"）；所有输出**新增** `shot_key`；两个调整工具**新增可选** `shot_key` 参数（给了就用 key，更稳） |
| ② 改成 `shot_key` | 更明确，但要改 2 个工具签名，且检索/状态输出都得加 key 才有人能用 |

### 取舍 2：解析不到时怎么办？

| 选项 | 说明 |
|---|---|
| **① 标 `stale` 并说明原因（推荐）** | 诚实；人知道这条要重选 |
| ② 用时间重叠 ≥50% 兜底重定位 | 边界微调时体验更顺，但可能**静默选到另一个镜头** |
| ③ 两者结合 | 先精确匹配；不中再重叠兜底，但**明确标 `relocated`**，不装成精确命中 |

### 取舍 3：是否同时做方案 A？

- **不做**：改动更小，正确性已由 B 保证（**推荐**）
- **做**：id 也稳定，但需要额外处理"镜位数变少"的清理

---

## 7. 存量数据的处理

项目**尚未发布**，没有真实存量。但迁移逻辑要有，避免读到旧文件就崩：

- 读到 `version: 1`（只有 `shot_id`）：
  - 若**当前库**里那个 `shot_id` 仍在 → 反查它的 `shot_key` 并**升级写回** v2
  - 若不在（已经重新分析过，id 已经漂了）→ 标 `legacy_unresolvable`，
    提示"这份结果是旧格式且库已变化，请重新匹配"，**不猜**
- v2 之后的解析失败一律走 §6 取舍 2

---

## 8. 验证方案（对应你的要求 4）

### 8.1 自动化测试（新增）

1. `shot_key` 生成的确定性：同 `(file_hash, start, end)` → 同 key
2. 同一个视频 `force=True` 重新分析后：`shot_id` **变了**，但
   **`shot_key` 不变**（这条测试直接固定住根因）
3. 匹配结果在 `library_scan(force=True)` 之后读取：
   每个候选项解析出的 `(video_path, start, end)` 与重新分析前**完全一致**
4. `library_rebuild` 之后同样断言
5. `selected` 指向的镜头在重分析后仍是**同一个镜头**（按 key 判定）
6. 素材文件被替换（hash 变）→ 该项标 `stale`，且**不指向任何错镜头**
7. v1 旧结果 → 按 §7 行为

### 8.2 端到端实证（报告里给对照表）

```
入库 → script_match → match_show（记录身份） → library_scan(force=True)
     → match_show（再记录） → 对照：shot_id 可以不同，身份必须相同
```

---

## 9. 风险

| 风险 | 说明 | 应对 |
|---|---|---|
| `shot_key` 唯一索引与**同视频同边界的重复镜头**冲突 | 理论上同一个视频不会有两个完全相同的时间段 | 建索引前先扫一遍；有冲突就加 `shot_index` 进 key 的生成 |
| 时间码浮点比较 | 两次分析的时间码来自同一段 FFmpeg 文本输出，实测 3/3 一致 | key 生成时统一 `round(...,3)` |
| 检测参数改变导致边界漂移 | key 对不上 → 判失效 | 这是**诚实**行为；若你要更顺的体验，用取舍 2 的选项 ③ |
| schema 迁移 | v3 → v4 只加列，不重建表 | 迁移走既有 `_migrate()`，并加回归测试 |

---

## 10. 你要确认的

1. **方案选 B（推荐）还是 C（B + A）？**
2. **取舍 1**：调整工具的地址参数保持 `shot_id`（推荐）还是改 `shot_key`？
3. **取舍 2**：解析不到时标 `stale`（推荐）还是时间重叠兜底？
4. **确认后我再动代码**，改完给出要求 4 的对照实证。
