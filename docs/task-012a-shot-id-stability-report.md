# TASK-012a 交付报告 —— shot_id 稳定性

| 项 | 内容 |
|---|---|
| 任务 | TASK-012a 修 shot_id 稳定性（A 类前置，方案已确认） |
| 采用的方案 | **B：镜头加持久标识 `shot_key`**（见[方案报告](task-012a-shot-id-stability-plan.md)） |
| 确认的取舍 | 地址参数保持 `shot_id`；解析不到时标 `stale` |
| Core 改动 | **零改动**（纯插件内改动） |
| 测试 | **436 项通过**（系统 Python + venv 双环境），新增 13 项 |
| schema | v3 → **v4**（只加列，不重建表） |
| 工具 | **17 个，签名未变**（本次不新增、不改名） |
| **验收结果** | **force 重分析与整库重建后，旧匹配结果 3/3 仍指向正确镜头** |

---

## 0. 结论

| 你的要求 | 结果 |
|---|---|
| 1 先报告根因 | ✅ [方案报告](task-012a-shot-id-stability-plan.md) §1（含实证） |
| 2 给出方案 | ✅ 方案 A / B / C 对比，推荐 B |
| 3 不动其他功能 | ✅ 检索、分段、分析算法、导出、审计、扫描全部未改 |
| 4 重新分析后旧匹配结果仍指向正确镜头 | ✅ **force 3/3、rebuild 3/3** |

---

## 1. 根因（一句话）

`shots.id` 是 `AUTOINCREMENT`（SQLite 永不复用），而重新分析走
`DELETE FROM shots` + `INSERT` —— **同一个镜头每次都拿到全新的 id**。

实测：首次入库 `1,2,3` → force 重分析 `4,5,6` → 再 force `7,8,9`。
`PRAGMA foreign_keys = ON` 让删除还级联到 `frames / labels / vectors`。
`library_rebuild` 更彻底 —— 连 `videos.id` 都重置。

> 完整证据与代码位置见[方案报告](task-012a-shot-id-stability-plan.md) §1。

**为什么没选方案 A（保留行 id）**：它只让 id"变稳"，不让 id"变对" ——
检测参数变了、边界变了，`shot_index` 相同不代表同一镜头，
等于**稳定地指向错镜头**；而且 `library_rebuild` 路径它覆盖不到。

---

## 2. 实现

### 2.1 持久标识 `shot_key`

```python
def shot_key(file_hash: str, start: float, end: float) -> str:
    """镜头的持久标识：内容哈希 + 时间码。"""
    raw = f"{file_hash}:{round(start, 3):.3f}:{round(end, 3):.3f}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
```

**内容寻址 + 时间码，不含任何自增 id。**

| 情形 | key 变不变 | 对不对 |
|---|---|---|
| 重新分析同一个视频 | **不变** | ✅ 同一个镜头 |
| 素材改名 / 移动 | **不变**（key 里不含路径） | ✅ 同一个镜头 |
| 视频内容被替换（重新编码） | 变 | ✅ 引用**明确失效**（内容变了就不是同一个镜头） |
| 改了 `scene_threshold` 导致边界漂移 | 变 | ✅ 引用失效，需重选 |

### 2.2 存的是 key，给出的是现算的 id

```
写入：script_match → 候选存 shot_key（shot_id 只作快照）
读取：match_show   → 用 shot_key 查当前库 → 现算 shot_id 给你
```

所以**你在界面上看到的 id 永远是当前库里的正确值**。

### 2.3 解析不到时标 `stale`，不猜

```json
{"shot_key": "fbd4e1134f1fbb55", "shot_id": null,
 "stale": true,
 "stale_reason": "素材库里已找不到这个镜头（视频内容被替换或该镜头已不存在）"}
```

**失效项绝不保留一个可能错的 `shot_id`** —— 宁可告诉你"要重选"。

### 2.4 一个刻意的设计：`shot_key` 是**普通索引**，不是唯一索引

同一份视频内容存在两个路径时（重复拷贝、被两个扫描根收录），
`(file_hash, start, end)` 会算出**同一个 key** —— 那确实是同一个镜头内容，
不该当冲突。解析时用记录里的 `video_path` 做**排序偏好**消歧
（文件移动过也不怕：路径只是偏好，不是匹配条件）。

---

## 3. 验收 4 的实证（真语料 + 真模型）

3 个画面（mandelbrot / life / red）+ Ollama + 中文 CLIP，
文案 3 段 → 匹配 → force 重分析 → library_rebuild：

### 匹配完成后

| 段 | 文案 | 选中 shot_id | shot_key | 视频 | 起-止 |
|---|---|---|---|---|---|
| 0 | 屏幕上显示出绚丽的彩色分形图案。 | 1 | `fbd4e1134f1fbb55` | s01_mandelbrot.mp4 | 0.00-3.00 |
| 1 | 夜色里散布着点点星光。 | 2 | `90159f5658551cab` | s03_life.mp4 | 0.00-3.00 |
| 2 | 大面积的纯红色铺满画面。 | 3 | `d616a74378b4cb26` | s09_red.mp4 | 0.00-3.00 |

### force 重新分析素材库后

| 段 | 文案 | 选中 shot_id | shot_key | 视频 | 起-止 |
|---|---|---|---|---|---|
| 0 | 屏幕上显示出绚丽的彩色分形图案。 | **4** | `fbd4e1134f1fbb55` | s01_mandelbrot.mp4 | 0.00-3.00 |
| 1 | 夜色里散布着点点星光。 | **5** | `90159f5658551cab` | s03_life.mp4 | 0.00-3.00 |
| 2 | 大面积的纯红色铺满画面。 | **6** | `d616a74378b4cb26` | s09_red.mp4 | 0.00-3.00 |

### library_rebuild（删库重建）后

| 段 | 选中 shot_id | shot_key | 视频 | 起-止 |
|---|---|---|---|---|
| 0 | 1 | `fbd4e1134f1fbb55` | s01_mandelbrot.mp4 | 0.00-3.00 |
| 1 | 2 | `90159f5658551cab` | s03_life.mp4 | 0.00-3.00 |
| 2 | 3 | `d616a74378b4cb26` | s09_red.mp4 | 0.00-3.00 |

### 结论表

```
force 重新分析前后（3 段）:
  shot_id 相同:   0/3   ← 会变，这正是问题
  shot_key 相同:  3/3
  指向的镜头相同: 3/3   ← **用户看到的结果**

library_rebuild 前后（3 段）:
  shot_key 相同:  3/3
  指向的镜头相同: 3/3

失效候选数: force 后 0   rebuild 后 0   （0 = 全部成功重新定位）
```

**`shot_id` 照样会变（0/3），但旧匹配结果 3/3 指向同一个镜头。**

---

## 4. 自动化测试（新增 13 项，防回归）

`tests/test_script_match.py::ShotKeyStabilityTests`

| 测试 | 断言 |
|---|---|
| `test_shot_key_is_deterministic_and_content_addressed` | 同输入同 key；时间码/哈希变了 key 要变；毫秒以下差异不产生新 key |
| `test_force_reanalysis_changes_shot_id_but_keeps_shot_key` | **shot_id 变了，shot_key 完全不变**（直接钉住根因） |
| `test_match_result_still_points_to_the_same_shot_after_force_reanalysis` | 每个候选解析出的 `(路径, 起, 止)` 与之前**完全一致**；`stale = 0` |
| `test_match_result_survives_a_full_library_rebuild` | 整库重建后同样成立 |
| `test_replaced_video_makes_the_reference_stale_not_wrong` | 换掉素材内容 → 标 `stale`，**且不保留可能错的 id** |
| `test_manual_selection_keeps_working_after_reanalysis` | 手动改过的选择重分析后仍在且仍对 |
| `test_v1_result_is_readable_and_flagged` | 老格式不炸，标 `v1 旧格式` |
| `SnapshotRefreshTests`（4 项） | 解析、失效标记、选中项跟随 key 而非快照、v1 标记 |
| `EditingTests`（9 项，改） | 编辑层身份改为 `shot_key` |

`tests/test_video_library.py` 新增 2 项：v3→v4 回填正确、`shot_key` 索引非唯一。

---

## 5. 改动清单

| 文件 | 改动 |
|---|---|
| `xbc_va_library.py` | +141 −5：`SCHEMA_VERSION` 3→4；`shots` 加 `shot_key` 列；`shot_key()` 纯函数；`_migrate()` 拆成 `_migrate_vectors` + `_migrate_shot_key`；`replace_analysis` 写 key；新增 `shots_by_key` / `shot_by_key`；`shot_vectors` / `library_search_labels` 带出 key |
| `xbc_va_match.py` | +91 −14：`RESULT_VERSION = 2`；候选项带 `shot_key`；`selected_shot_key`；编辑按 key 定位；新增 `refresh_snapshots()`（读取侧解析 + 失效标记）；新增 `segment_at()` |
| `plugin.py` | +89 −27：`_score_query` 结果带 `shot_key`；`script_match` 写 v2；`match_show` 读取时解析；新增 `_match_resolver` / `_resolve_current`；`match_select` / `match_reorder` 把当前 id 换成 key 再存 |
| `tests/test_script_match.py` | +265 −24 |
| `tests/test_video_library.py` | +43 −2 |

**`_migrate()` 拆开的原因**：原来是"vectors 没问题就 return"，
那会让 `shots` 的迁移**永远跑不到**。现在两张表各管各的。

### 不动的东西（对应要求 3）

- ❌ 检索（`_retrieval_context` / `_score_query` 的打分逻辑）
- ❌ 分段（`segment_script`）
- ❌ 分析算法、镜头切分、向量计算
- ❌ 导出 / 审计 / 扫描 / 标签检索工具
- ❌ Core（**零改动**）
- ✅ 只给 `shots` **加了一列**（不重建表、不改既有列）

---

## 6. 兼容与迁移

| 情形 | 行为 |
|---|---|
| v3 旧库（有镜头、无 `shot_key`） | 迁移时 `ALTER TABLE` 加列 + 用 `(file_hash, 起, 止)` **回填**（实测：`test_v1_library_gets_shot_key_backfilled`） |
| v1/v2 旧库 | 既有迁移链照旧走到 v4 |
| v1 匹配结果（只有 `shot_id`） | 可读；候选项标 `stale` + `v1 旧格式`，提示重新匹配 |
| v2 匹配结果 | 正常解析 |

项目未发布，没有真实存量；但迁移与降级路径都有测试守着。

---

## 7. 局限

1. **改了 `scene_threshold` 之类的检测参数后，旧匹配结果会判失效**（key 对不上）。
   这是**按你选的取舍刻意为之**（诚实优先，不自动猜）。若以后觉得太硬，
   可以加"时间重叠兜底 + 标 `relocated`"——本次没做。
2. **`shot_key` 只是"内容 + 时间码"**。若同一个视频里出现两段**完全相同**的时间区间
   （实际不会），key 会撞；`shots_by_key` 会返回多条，解析取第一条。
3. **匹配结果与库文件不在同一个生命周期**：`library_rebuild` 只删 `library.db`，
   `matches/` 保留 —— 这正是我们要的，但也意味着**匹配结果会一直留着**，
   素材删掉后会变成一堆 `stale` 引用（不会指错，但也不会自动清理）。
4. 本次**没有**做方案 A（保留行 id），所以 `shot_id` 仍会随重分析增长。

---

## 8. 复现

```powershell
# 匹配 → 重新分析 → 再看结果（身份必须不变）
python run.py tool call script_match --kwargs "{`"name`": `"demo`", `"script`": `"夜幕降临，城市华灯初上。`"}"
python run.py tool call match_show   --kwargs "{`"name`": `"demo`"}"
python run.py tool call library_scan --kwargs "{`"directory`": `"D:/materials`", `"force`": true}"
python run.py tool call match_show   --kwargs "{`"name`": `"demo`"}"   # 指向同一个镜头

# 测试
python -m unittest discover -s tests
```
