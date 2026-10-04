"""日志配置。

目标：用户出问题时，我们能在 logs 目录里拿到可读的现场记录。
所有模块用 logging.getLogger(__name__) 或本模块的 get_logger 取 logger，
不自己 open 文件写日志。
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

LOGGER_NAME = "xbc"
_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_MAX_BYTES = 1_000_000
_BACKUP_COUNT = 3


def shutdown_logging() -> None:
    """关闭并移除内核 logger 的全部 handler。

    必须能被显式调用：日志文件句柄不释放，在 Windows 上会导致其所在目录
    无法删除 —— 测试会因此留下垃圾目录（这不是猜测，是实测到的现象）。
    """
    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # noqa: BLE001 - 关闭失败不应影响正常退出
            pass


def setup_logging(paths: Any, level: str = "INFO", console: bool = True) -> logging.Logger:
    """配置内核根 logger。重复调用会先清空旧 handler，避免日志重复输出。"""
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    logger.propagate = False

    shutdown_logging()

    formatter = logging.Formatter(_FORMAT)

    if console:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)

    try:
        Path(paths.logs_dir).mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            Path(paths.logs_dir) / "xbc.log",
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError as exc:
        # 日志文件不可写绝不能导致内核无法启动
        logger.warning("日志文件不可写，仅使用控制台输出: %s", exc)

    return logger


def get_logger(name: str = LOGGER_NAME) -> logging.Logger:
    return logging.getLogger(name)


def plugin_logger(plugin_id: str) -> logging.Logger:
    """插件专用 logger，日志里能直接看出是哪个插件出的问题。"""
    return logging.getLogger(f"{LOGGER_NAME}.plugin.{plugin_id}")
