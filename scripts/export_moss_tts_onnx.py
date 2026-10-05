r"""获取 `moss-tts-nano` 的 ONNX 模型到 Core 的共享模型目录（**一次性步骤**）。

## 这个脚本为什么叫 "export" 但干的是 "获取"

任务书给的脚本名是 `export_moss_tts_onnx.py`（参照 TASK-010 那个导出脚本）。
但 **MOSS 官方已经把 ONNX 产物发布出来了**，不需要我们自己从 PyTorch 导出：

| 仓库 | 大小 |
|---|---|
| `OpenMOSS-Team/MOSS-TTS-Nano-100M-ONNX` | 641.5 MB |
| `OpenMOSS-Team/MOSS-Audio-Tokenizer-Nano-ONNX` | 86.4 MB |
| **合计** | **727.9 MB** |

自己导出反而更差：要装 PyTorch（+1183 MB）、跑一遍导出，而且**产物未必和官方发布的一致**。
所以本脚本的职能是**获取 + 校验**，名字保持任务书给的那个以免口径漂移。
（这一点作为 B 类自主决定记在交付报告里。）

## 只下载不下 PyTorch

脚本**只用标准库** `urllib` 拉文件，不依赖 `huggingface_hub` ——
少一个依赖，也避免"为了下载而装一堆东西"。

## 产物放哪

放 **Core 的共享模型目录**：

```
<数据根>/models/moss-tts-nano/
├── MOSS-TTS-Nano-100M-ONNX/
└── MOSS-Audio-Tokenizer-Nano-ONNX/
```

模型**不随插件包分发**，也不放插件数据目录 —— 它是 Core 级共享资源。
**插件不知道这个路径**（由注册机制通过 `bind_models` 注入），
所以这里放好之后不需要改任何插件配置。

```powershell
# Windows 默认数据根
python scripts/export_moss_tts_onnx.py --out "$env:LOCALAPPDATA\夏半仓工具箱\models\moss-tts-nano"

# 或者不给 --out，脚本自己按 Core 的路径规则解析
python scripts/export_moss_tts_onnx.py
```

## 校验什么

1. 两个仓库的文件**逐个字节数比对** HF 元数据 —— 不一致就重下 / 报错
2. 本 Provider 运行时要用的 4 个关键文件必须存在
3. 退出码非 0 表示没成功
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

#: 两个官方 ONNX 仓库（许可证都是 Apache-2.0，已核对：模型卡 front-matter + cardData）
REPOS = (
    ("OpenMOSS-Team/MOSS-TTS-Nano-100M-ONNX", "MOSS-TTS-Nano-100M-ONNX"),
    ("OpenMOSS-Team/MOSS-Audio-Tokenizer-Nano-ONNX", "MOSS-Audio-Tokenizer-Nano-ONNX"),
)

#: 运行时必需的 4 个文件（相对 model_dir）—— 与 Provider 的检查保持一致
REQUIRED = (
    "MOSS-TTS-Nano-100M-ONNX/browser_poc_manifest.json",
    "MOSS-TTS-Nano-100M-ONNX/tts_browser_onnx_meta.json",
    "MOSS-TTS-Nano-100M-ONNX/tokenizer.model",
    "MOSS-Audio-Tokenizer-Nano-ONNX/codec_browser_onnx_meta.json",
)

USER_AGENT = {"User-Agent": "xbc-moss-tts-fetch/0.1"}


def default_root() -> Path:
    """按 Core 的路径规则解析数据根（与 `AppPaths.app_root` 保持一致）。"""
    import os

    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "夏半仓工具箱"
    return Path.home() / ".xbc"


def api(url: str) -> dict:
    request = urllib.request.Request(url, headers=USER_AGENT)
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def list_repo_files(repo_id: str) -> list[dict]:
    """列出仓库里的文件与字节数（跳过 .cache 之类的元数据目录）。"""
    tree = api(f"https://huggingface.co/api/models/{repo_id}/tree/main?recursive=true")
    return [
        node for node in tree
        if node.get("type") == "file" and not node["path"].startswith(".")
    ]


def download(repo_id: str, relative_path: str, destination: Path) -> int:
    """下载单个文件；已存在且字节数一致就跳过。"""
    url = f"https://huggingface.co/{repo_id}/resolve/main/{relative_path}"
    request = urllib.request.Request(url, headers=USER_AGENT)
    tmp = destination.with_suffix(destination.suffix + ".part")
    with urllib.request.urlopen(request, timeout=600) as response:
        total = int(response.headers.get("Content-Length") or 0)
        with tmp.open("wb") as handle:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
    size = tmp.stat().st_size
    if total and size != total:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(
            f"{relative_path} 下载不完整：{size} 字节，期望 {total} 字节"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp.replace(destination)
    return size


def human(size: float) -> str:
    if size >= 1073741824:
        return f"{size / 1073741824:.2f} GB"
    return f"{size / 1048576:.1f} MB"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--out", default=None,
        help="模型输出目录（默认按 Core 路径规则：<数据根>/models/moss-tts-nano）",
    )
    parser.add_argument(
        "--check-only", action="store_true",
        help="只校验已有文件，不下载",
    )
    args = parser.parse_args(argv)

    model_dir = Path(args.out).expanduser() if args.out else (
        default_root() / "models" / "moss-tts-nano"
    )
    print(f"模型目录：{model_dir}")
    print()

    problems: list[str] = []
    grand_total = 0

    for repo_id, subdir in REPOS:
        print(f"=== {repo_id} ===")
        try:
            files = list_repo_files(repo_id)
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            problems.append(f"{repo_id}: 取文件列表失败（{exc}）")
            print(f"  ✗ 取文件列表失败：{exc}")
            continue

        expected_total = sum(node.get("size") or 0 for node in files)
        print(f"  远端 {len(files)} 个文件，合计 {human(expected_total)}")

        for node in files:
            relative = node["path"]
            expected = node.get("size") or 0
            target = model_dir / subdir / relative
            if target.is_file() and target.stat().st_size == expected:
                print(f"  = {relative}（已存在，{human(expected)}）")
                grand_total += expected
                continue
            if args.check_only:
                problems.append(f"{subdir}/{relative}: 缺失或字节数不符")
                print(f"  ✗ {relative} 缺失或字节数不符（期望 {expected}）")
                continue
            print(f"  ↓ {relative}（{human(expected)}）…", end="", flush=True)
            try:
                size = download(repo_id, relative, target)
            except Exception as exc:  # noqa: BLE001 - 直接报出来，不吞
                problems.append(f"{subdir}/{relative}: 下载失败（{exc}）")
                print(f" 失败：{exc}")
                continue
            if expected and size != expected:
                problems.append(
                    f"{subdir}/{relative}: 字节数不符（{size} != {expected}）"
                )
                print(f" 字节数不符：{size} != {expected}")
                continue
            grand_total += size
            print(" 完成")
        print()

    print("=== 运行时必需文件 ===")
    for relative in REQUIRED:
        target = model_dir / relative
        ok = target.is_file()
        if not ok:
            problems.append(f"缺少必需文件：{relative}")
        print(f"  {'✓' if ok else '✗'} {relative}")

    print()
    print(f"已就位合计：{human(grand_total)}")

    if problems:
        print()
        print(f"✗ 有 {len(problems)} 个问题：")
        for item in problems:
            print(f"  · {item}")
        return 1

    print()
    print("✓ 模型就位。")
    print("  下一步：在工作台/插件中心确认 voice_tts 插件已启用，")
    print("  然后跑 voice_selftest 或 voice_clone 验证。")
    print("  （放好之后不需要改任何插件配置 —— 路径由 Core 注入）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
