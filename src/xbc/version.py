"""版本号与内核 API 契约。

CORE_API_VERSION 是插件兼容性的唯一依据：
插件 manifest 中的 api_version 必须与本文件的主版本号一致，否则内核拒绝加载，
并在日志中说明原因。这样内核将来演进时，不会静默地破坏已有插件。
"""

APP_NAME = "夏半仓工具箱"
APP_ID = "xbc"

# 工具箱内核自身版本
CORE_VERSION = "0.1.0"

# 插件 API 契约版本（主版本号变更 = 不兼容变更）
CORE_API_VERSION = "1.0"


def api_major(version: str) -> str:
    """取出 "1.0" / "1.2.3" 的主版本号，便于做兼容判断。"""
    return str(version).strip().split(".", 1)[0]
