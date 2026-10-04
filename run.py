#!/usr/bin/env python
"""开发期启动脚本：无需安装即可运行。

    python run.py smoke      全流程冒烟测试
    python run.py list       列出插件
    python run.py env        环境自检
    python run.py ui         启动图形宿主（需要 PySide6）
"""

import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from xbc.__main__ import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
