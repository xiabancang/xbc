"""GitHub 每日项目扫描。

用途：每天找出"最近有更新、且与夏半仓工具箱相关"的开源项目，产出一份候选清单，
再由 AI 研发工程师（或人）判断哪些值得跟进。

设计要点：
- 只用标准库（与本项目"内核零第三方依赖"的原则一致）
- 只调用 GitHub Search API：搜索配额是独立的（未认证 10 次/分钟），
  而搜索结果已携带 stars / 许可证 / 更新时间 / 语言，无需再打 core API
- 维护 seen.json，让每日报告能标出"本次新增"，避免每天重复同一批项目
- 任何网络/配额异常都不抛栈，而是记进报告，保证定时任务不会静默失败

用法：
    python tools/github_daily_scan.py                # 扫描并写出当日报告
    python tools/github_daily_scan.py --days 60      # 放宽更新时间窗口
    python tools/github_daily_scan.py --deep         # 每个方向跑全部关键词（更慢）
    python tools/github_daily_scan.py --dry-run      # 只打印，不写文件
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT_DIR = REPO_ROOT / "reports" / "github"

API = "https://api.github.com/search/repositories"
USER_AGENT = "xbc-daily-scan/0.1"

# 与夏半仓工具箱路线图对应的关注方向。
#
# **范围由《Harness 工作规则》的「每日 GitHub 扫描（广度兜底）」定死：只扫这五个方向，
# 无关领域不扫。** 加方向前先改规则，别在这里顺手加。
#
# 本列表是**纯数据**：扫描逻辑不认方向名，只按 `queries` 打 GitHub Search。
#
# 每条 = 一个方向，queries 里第一个是主查询（默认只跑主查询，`--deep` 才跑全部）。
TOPICS: list[dict] = [
    {
        "id": "video_processing",
        "label": "视频处理",
        "why": "镜头切分 / 视频结构 / 视频理解 / 多模态 —— 素材库与匹配的上游能力",
        "queries": [
            "scene detection video",
            "shot boundary detection",
            "video scene segmentation",
            "video understanding multimodal",
            "video captioning",
        ],
    },
    {
        "id": "material_management",
        "label": "素材管理",
        "why": "素材库、标签、检索、向量索引、媒体资产组织",
        "queries": [
            "media asset management",
            "video asset library",
            "media search embedding",
        ],
    },
    {
        "id": "ai_capability",
        "label": "AI 能力层",
        "why": "本地模型（Ollama 生态）、模型路由、能力层抽象 —— 本地优先的 AI 接入",
        "queries": [
            "ollama client",
            "local llm desktop",
            "llm model router",
        ],
    },
    {
        "id": "voice_cloning",
        "label": "声音克隆",
        "why": "TTS、音色克隆、配音 —— 文案匹配之后的成片环节",
        "queries": [
            "voice cloning tts",
            "text to speech open source",
            "voice conversion",
        ],
    },
    {
        "id": "script_matching",
        "label": "文案匹配",
        "why": "文案 / 脚本与画面的匹配（**收窄到「文案」，不再泛扫营销自动化**）",
        "queries": [
            "video moment retrieval",
            "text video matching",
            "script to video",
        ],
    },
]

# 已经知道/已在用的项目，报告中标注出来而不是当成新发现
KNOWN = {
    "breakthrough/pyscenedetect": "已在参考清单",
    "pytest-dev/pluggy": "已在参考清单",
    "ollama/ollama-python": "已在参考清单",
    "zhiyiyo/pyqt-fluent-widgets": "已在参考清单",
    "ollama/ollama": "已在参考清单",
}


# 相关度打分用的关键词。目标不是"精确分类"，而是把明显对口的东西顶到前面，
# 让每天的报告第一屏就有用（原始清单仍然完整保留，不做删除）。
HIGH_SIGNAL = (
    "video", "scene", "shot", "multimodal", "vision", "vlm", "ollama", "llm",
    "plugin", "pyside", "pyqt", "qt", "ffmpeg", "desktop", "rag",
    "knowledge base", "caption", "transcri", "subtitle",
)
NOISE = (
    "minecraft", "steam", "game server", "vlmcsd", "volume license",
    "crack", "cheat", "asset management", "vulnerability",
)


def relevance(item: dict) -> int:
    """粗略相关度：热度 + 关键词命中 + 语言 + 新鲜度 - 明显无关。"""
    text = f"{item['full_name']} {item['description']}".lower()
    score = min(item["stars"], 3000) // 100          # 热度，最高 30
    score += sum(6 for kw in HIGH_SIGNAL if kw in text)
    if item["language"] == "Python":
        score += 10                                   # 我们的技术栈
    score -= sum(25 for kw in NOISE if kw in text)
    try:
        days = (date.today() - date.fromisoformat(item["pushed_at"])).days
        score += max(0, 14 - days) // 2               # 越新越加分
    except (ValueError, TypeError):
        pass
    return score


_last_request_at = 0.0


def _throttle(min_interval: float) -> None:
    """全局请求限速。

    GitHub 未认证的 search 配额是 **10 次/分钟**，所以间隔必须加在每一次请求之间，
    而不是只在"同一方向内的多个关键词"之间 —— 后者会让多个方向连续打出去直接 403。
    """
    global _last_request_at
    if min_interval <= 0:
        return
    elapsed = time.time() - _last_request_at
    if elapsed < min_interval:
        time.sleep(min_interval - elapsed)
    _last_request_at = time.time()


def _request(url: str, min_interval: float = 0.0, timeout: int = 30) -> tuple[dict | None, str]:
    """返回 (数据, 错误信息)。错误不抛异常，交给调用方记进报告。"""
    _throttle(min_interval)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8")), ""
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            return None, "触发 GitHub 配额限制（403），请稍后重试或减少方向数"
        return None, f"HTTP {exc.code}: {exc.reason}"
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return None, f"{type(exc).__name__}: {exc}"


def build_query(keyword: str, since: date, min_stars: int) -> str:
    return (
        f"{keyword} "
        f"pushed:>{since.isoformat()} "
        f"stars:>={min_stars} "
        f"archived:false"
    )


def scan_topic(topic: dict, args: argparse.Namespace, since: date) -> tuple[list[dict], list[str]]:
    """扫描一个方向，返回 (条目列表, 错误列表)。"""
    keywords = topic["queries"] if args.deep else topic["queries"][:1]
    results: list[dict] = []
    errors: list[str] = []

    for keyword in keywords:
        params = urllib.parse.urlencode(
            {
                "q": build_query(keyword, since, args.min_stars),
                "sort": "stars",
                "order": "desc",
                "per_page": args.per_topic,
            }
        )
        data, error = _request(f"{API}?{params}", args.sleep)
        if error:
            errors.append(f"{topic['label']} · `{keyword}` → {error}")
            continue
        if not data:
            continue
        for item in data.get("items", []):
            license_info = item.get("license") or {}
            results.append(
                {
                    "full_name": item.get("full_name", ""),
                    "html_url": item.get("html_url", ""),
                    "description": (item.get("description") or "").strip(),
                    "stars": item.get("stargazers_count", 0),
                    "language": item.get("language") or "",
                    "pushed_at": (item.get("pushed_at") or "")[:10],
                    "created_at": (item.get("created_at") or "")[:10],
                    "license": license_info.get("spdx_id") or "无",
                    "topics": item.get("topics", []),
                    "topic_id": topic["id"],
                    "topic_label": topic["label"],
                    "topic_why": topic["why"],
                    "matched_keyword": keyword,
                }
            )
    return results, errors


def dedupe(items: list[dict]) -> list[dict]:
    """同一仓库可能命中多个方向，只保留 stars 最高的那条记录。"""
    best: dict[str, dict] = {}
    for item in items:
        key = item["full_name"].lower()
        if key not in best or item["stars"] > best[key]["stars"]:
            best[key] = item
    return sorted(best.values(), key=lambda x: -x["stars"])


def load_seen(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    try:
        return set(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return set()


def save_seen(path: Path, names: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(names), ensure_ascii=False, indent=2), encoding="utf-8")


def render_markdown(items: list[dict], errors: list[str], args: argparse.Namespace, since: date) -> str:
    today = date.today().isoformat()
    lines: list[str] = [
        f"# GitHub 每日扫描 · {today}",
        "",
        f"- 过滤条件：最近更新 `> {since.isoformat()}`（{args.days} 天内）、`stars >= {args.min_stars}`、未归档",
        f"- 扫描方向：{len(TOPICS)} 个；命中并去重后 **{len(items)}** 个项目",
        "",
    ]

    known = [i for i in items if i["full_name"].lower() in KNOWN]
    fresh = [i for i in items if i["full_name"].lower() not in KNOWN]
    new_since_last = [i for i in fresh if i.get("is_new")]

    lines.append(f"- 其中已在参考清单中：{len(known)} 个；本次新出现：**{len(new_since_last)}** 个")
    lines.append("")

    if items:
        lines.append("## 最值得关注（按相关度排序）")
        lines.append("")
        lines.append("> 打分 = 热度 + 关键词命中 + Python 技术栈 + 新鲜度 − 明显无关项。只是排序手段，不代表最终结论。")
        lines.append("")
        for item in sorted(items, key=lambda x: -x["score"])[:8]:
            lines.append(_item_block(item))
            lines.append("")

    if new_since_last:
        lines.append("## 本次新增（重点看这部分）")
        lines.append("")
        for item in new_since_last:
            lines.append(_item_block(item))
    else:
        lines.append("## 本次新增")
        lines.append("")
        lines.append("_本次没有新出现的项目（与上次扫描相比）。_")
        lines.append("")

    lines.append("## 全部命中（按方向分组）")
    lines.append("")
    for topic in TOPICS:
        group = sorted(
            (i for i in fresh if i["topic_id"] == topic["id"]), key=lambda x: -x["score"]
        )
        if not group:
            continue
        lines.append(f"### {topic['label']}")
        lines.append("")
        lines.append(f"> 与我们的关系：{topic['why']}")
        lines.append("")
        for item in group:
            lines.append(_item_block(item, compact=True))
    lines.append("")

    if known:
        lines.append("## 已在参考清单中（无需重复调研）")
        lines.append("")
        for item in known:
            lines.append(f"- `{item['full_name']}` — {KNOWN[item['full_name'].lower()]}")
        lines.append("")

    if errors:
        lines.append("## 本次扫描的问题")
        lines.append("")
        for error in errors:
            lines.append(f"- {error}")
        lines.append("")

    return "\n".join(lines)


def _item_block(item: dict, compact: bool = False) -> str:
    mark = " 🆕" if item.get("is_new") else ""
    stars = f"{item['stars']:,}"
    head = f"**[{item['full_name']}]({item['html_url']})**{mark} — ⭐ {stars} · {item['language'] or '未知'} · 更新 {item['pushed_at']} · `{item['license']}`"
    desc = item["description"] or "_（无描述）_"
    if len(desc) > 160:
        desc = desc[:157] + "..."
    lines = [head]
    if not compact:
        lines.append(f"  - 方向：{item['topic_label']}（{item['topic_why']}）")
    lines.append(f"  - {desc}")
    return "\n".join(lines) + "\n"


def main() -> int:
    # 保证中文在任意 Windows 控制台编码下都能输出（否则 GBK 控制台会乱码）
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    parser = argparse.ArgumentParser(description="扫描 GitHub 上与我们相关的开源项目")
    parser.add_argument("--days", type=int, default=30, help="只考虑最近 N 天有更新的项目")
    parser.add_argument("--min-stars", type=int, default=30, help="最低 star 数")
    parser.add_argument("--per-topic", type=int, default=8, help="每个查询取前 N 条")
    parser.add_argument("--sleep", type=float, default=9.0, help="每次请求之间的间隔秒数（配额 10 次/分钟）")
    parser.add_argument("--deep", action="store_true", help="每个方向跑全部关键词")
    parser.add_argument("--dry-run", action="store_true", help="只打印结果，不写文件")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help="报告输出目录")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    seen_path = out_dir / "seen.json"
    seen = load_seen(seen_path)

    since = date.today() - timedelta(days=args.days)
    collected: list[dict] = []
    errors: list[str] = []

    started = datetime.now(timezone.utc)
    for topic in TOPICS:
        found, topic_errors = scan_topic(topic, args, since)
        collected.extend(found)
        errors.extend(topic_errors)
        print(f"  [{topic['label']}] 命中 {len(found)}")

    items = dedupe(collected)
    for item in items:
        item["is_new"] = item["full_name"].lower() not in seen
        item["score"] = relevance(item)

    markdown = render_markdown(items, errors, args, since)
    new_count = sum(1 for i in items if i["is_new"])

    print(f"\n扫描完成：{len(items)} 个项目，其中新增 {new_count} 个，用了 {(datetime.now(timezone.utc) - started).seconds} 秒")
    if errors:
        print(f"有 {len(errors)} 个查询失败（已记入报告）")

    if args.dry_run:
        print("\n--- 报告预览 ---")
        print(markdown)
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / f"{date.today().isoformat()}.md"
    report_path.write_text(markdown, encoding="utf-8")
    (out_dir / "latest.json").write_text(
        json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    save_seen(seen_path, seen | {i["full_name"].lower() for i in items})

    print(f"报告已写入: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
