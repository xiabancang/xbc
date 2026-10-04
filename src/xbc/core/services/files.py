"""文件能力。

插件通过它读写文件，而不是直接使用 open()：
便于统一处理编码、自动建目录、以及在将来加入沙箱与访问审计。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from ..errors import XbcError


class FileService:
    """最小但够用的文件读写封装。"""

    def __init__(self, logger: Any = None) -> None:
        self._log = logger

    # ---------- 目录 ----------
    def ensure_dir(self, path: Path | str) -> Path:
        target = Path(path)
        target.mkdir(parents=True, exist_ok=True)
        return target

    def exists(self, path: Path | str) -> bool:
        return Path(path).exists()

    def list_files(self, directory: Path | str, pattern: str = "*") -> list[Path]:
        folder = Path(directory)
        if not folder.is_dir():
            return []
        return sorted(p for p in folder.glob(pattern) if p.is_file())

    # ---------- 文本 ----------
    def read_text(self, path: Path | str, default: str | None = None) -> str | None:
        target = Path(path)
        try:
            return target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            if self._log:
                self._log.debug("读取文本失败 %s: %s", target, exc)
            return default

    def write_text(self, path: Path | str, text: str) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target

    # ---------- JSON ----------
    def read_json(self, path: Path | str, default: Any = None) -> Any:
        text = self.read_text(path)
        if text is None:
            return default
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            if self._log:
                self._log.warning("JSON 解析失败 %s: %s", path, exc)
            return default

    def write_json(self, path: Path | str, data: Any, indent: int = 2) -> Path:
        return self.write_text(path, json.dumps(data, ensure_ascii=False, indent=indent))

    # ---------- 其他 ----------
    def copy(self, source: Path | str, target: Path | str) -> Path:
        src, dst = Path(source), Path(target)
        if not src.exists():
            raise XbcError(f"源文件不存在: {src}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return dst

    def size(self, path: Path | str) -> int:
        target = Path(path)
        return target.stat().st_size if target.exists() else 0
