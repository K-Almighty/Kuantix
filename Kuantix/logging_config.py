"""P1-3：统一日志配置（CLI / REST / 调度器 / worker 共用）。

设计原则
--------
* **零意外**：不传 ``config`` 就退化为 Python 默认（不覆盖用户自己配置的 logging）。
* **幂等**：同进程多次调用只生效第一次，不重复加 Handler。
* **双输出**：stdout（容器/K8s 友好）+ ``paths.logs/Kuantix.log``（按大小滚动 5 × 50MB）。
* **结构化字段**：`%(asctime)s [%(levelname)s] %(name)s: %(message)s`
  （项目里没有引入 structlog，就用人类可读 + 易 grep 的格式）。
* **uvicorn 对齐**：uvicorn 的 ``uvicorn`` / ``uvicorn.access`` logger 也挂到
  同一套 Handler，格式一致。

典型调用位置
------------
* :func:`Kuantix.main.create_app`（REST 入口，首个业务请求前）
* :func:`Kuantix.cli._state`（CLI 首次取全局配置后）
"""

from __future__ import annotations

import logging
import logging.handlers
import threading
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - 避免循环 import
    from Kuantix.config import Config

__all__ = ["configure_logging", "LOG_FORMAT"]

#: 统一日志格式（asctime [LEVEL] logger_name: message）
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

#: 按大小滚动的日志文件参数
_LOG_FILE_MAX_BYTES = 50 * 1024 * 1024  # 50 MB
_LOG_FILE_BACKUP_COUNT = 5  # 保留最近 5 份

#: 幂等锁（保证全局只配置一次）
_configure_lock = threading.Lock()
_configured_once: bool = False

#: 需要与根 logger 对齐格式的第三方 / 框架 logger
_UICORN_LOGGER_NAMES: tuple[str, ...] = (
    "uvicorn",
    "uvicorn.access",
    "uvicorn.error",
)


def configure_logging(config: "Config | None" = None) -> None:
    """P1-3：按配置初始化 Python logging（幂等）。

    * ``config is None`` → 直接返回（不覆盖第三方或用户的 logging 设置）。
    * 同进程多次调用只有第一次生效，防止 Handler 重复叠加导致日志翻倍。

    Args:
        config: Kuantix 配置；必须包含 ``app.log_level``（INFO/DEBUG/...）与
            ``paths.logs``（滚动日志目录）。
    """
    global _configured_once
    if config is None:
        return
    # 快速路径：已配置过直接返回
    if _configured_once:
        return
    with _configure_lock:
        if _configured_once:
            return
        _do_configure(config)
        _configured_once = True


def _do_configure(config: "Config") -> None:
    """实际执行日志配置（已被外层幂等锁保护）。"""
    level_str = str(getattr(config.app, "log_level", "INFO")).upper()
    level: int = getattr(logging, level_str, logging.INFO)

    # 1. 根 logger（所有后代默认继承）
    root = logging.getLogger()
    root.setLevel(level)

    # 清除 root 已有 Handler（某些库/uvicorn 会在 import 时预埋 Handler）
    for h in list(root.handlers):
        root.removeHandler(h)

    formatter = logging.Formatter(LOG_FORMAT)

    # 2. stdout（容器日志；Always on）
    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(level)
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)

    # 3. 文件滚动（落到 paths.logs，运维排查历史问题）
    logs_dir = Path(config.paths.logs)
    try:
        logs_dir.mkdir(parents=True, exist_ok=True)
        log_file = logs_dir / "Kuantix.log"
        file_handler = logging.handlers.RotatingFileHandler(
            filename=str(log_file),
            maxBytes=_LOG_FILE_MAX_BYTES,
            backupCount=_LOG_FILE_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError:  # noqa: BLE001 - 日志目录不可写是运维问题，不阻断服务
        import warnings

        warnings.warn(
            f"[logging_config] 无法创建/写入日志文件 {logs_dir}，继续只用 stdout；"
            f"请检查磁盘权限与剩余空间。",
            stacklevel=3,
        )

    # 4. uvicorn logger：沿用同一套 format + handler，去掉独立 handler 避免重复
    for name in _UICORN_LOGGER_NAMES:
        logger = logging.getLogger(name)
        logger.setLevel(level)
        # 移除 uvicorn 默认预埋的 StreamHandler（否则每一条出现两次）
        for h in list(logger.handlers):
            logger.removeHandler(h)
        # 不重复挂根 handler；默认 propagate=True，消息会传给根的 stdout + file
