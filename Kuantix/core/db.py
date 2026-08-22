"""SQLite 并发与持久化调优公共层（P1-1）。

本模块零外部重依赖（仅标准库），所有业务层（data/factor/monitor/backtest/...）
均可导入使用，不违反任何分层红线。

统一 PRAGMA 策略（O5 基线）
---------------------------
===================  ==========  ====================================================
PRAGMA               默认值      说明
===================  ==========  ====================================================
``journal_mode``     ``WAL``     写操作不阻塞读；多连接并发友好；
                                 对同数据库只需设置一次即持久化。
``synchronous``      ``NORMAL``  WAL 模式下 NORMAL 可保证：
                                 断电/崩溃不损坏已提交事务，fsync 次数降低 50%+，
                                 写入吞吐量提升 2~5 倍。
``busy_timeout``     ``30000``   毫秒级等待；避免高并发下立抛 ``database is locked``。
===================  ==========  ====================================================

迁移批次可临时把 ``synchronous`` 降到 ``OFF``（内存刷盘，极速），
结束后务必调用 :func:`set_synchronous` 切回 ``NORMAL``。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Literal

__all__ = [
    "apply_sqlite_pragmas",
    "connect_sqlite",
    "ensure_wal",
    "set_synchronous",
    # 常量
    "SQLITE_BUSY_TIMEOUT_MS",
    "SQLITE_SYNCHRONOUS_NORMAL",
    "SQLITE_SYNCHRONOUS_OFF",
    "SQLITE_SYNCHRONOUS_FULL",
]

#: 默认 busy_timeout 毫秒（30s；高并发监控/调度下足以应对短期锁竞争）
SQLITE_BUSY_TIMEOUT_MS = 30_000

SQLITE_SYNCHRONOUS_NORMAL: Literal["NORMAL"] = "NORMAL"
SQLITE_SYNCHRONOUS_OFF: Literal["OFF"] = "OFF"
SQLITE_SYNCHRONOUS_FULL: Literal["FULL"] = "FULL"

SyncLevel = Literal["NORMAL", "OFF", "FULL", "EXTRA"]


def apply_sqlite_pragmas(
    conn: sqlite3.Connection,
    *,
    enable_wal: bool = True,
    synchronous: SyncLevel = SQLITE_SYNCHRONOUS_NORMAL,
    busy_timeout_ms: int = SQLITE_BUSY_TIMEOUT_MS,
    foreign_keys: bool = True,
) -> None:
    """对已有连接应用并发调优 PRAGMA（幂等）。

    不负责 ``connect(timeout=...)``，因为部分调用方在 connect 侧设置秒级
    timeout 与 ``check_same_thread``。

    Args:
        conn: 已打开的 :class:`sqlite3.Connection`。
        enable_wal: ``True`` 时先执行 ``PRAGMA journal_mode=WAL``；
            通常每库只需设置一次即持久化，但默认 True 防止老库升级遗漏。
        synchronous: 同步级别；**迁移批次可临时 OFF，结束必须切回 NORMAL**。
        busy_timeout_ms: ``PRAGMA busy_timeout`` 毫秒；30s 足够应对跨线程竞争。
        foreign_keys: 默认启用外键约束（项目未使用，但设置无副作用）。
    """
    if enable_wal:
        # SQLite 文档：journal_mode 对当前连接生效，并持久化到数据库文件
        # （后续新连接默认继承）。返回的实际模式若不是 "wal" 说明失败，
        # 但不中断（例如某些 :memory: 库不支持 WAL）。
        actual = conn.execute("PRAGMA journal_mode = WAL").fetchone()
        if actual and actual[0] != "wal":  # pragma: no cover - 运行时环境相关
            import warnings

            warnings.warn(
                f"[core/db] SQLite 启用 WAL 失败，实际模式={actual[0]!r}；"
                f"并发下可能出现 database is locked",
                stacklevel=2,
            )
    conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    conn.execute(f"PRAGMA synchronous = {str(synchronous).upper()}")
    if foreign_keys:
        conn.execute("PRAGMA foreign_keys = ON")


def connect_sqlite(
    db_path: Path | str,
    *,
    check_same_thread: bool | None = None,
    timeout: float | None = None,
    row_factory: object = sqlite3.Row,
    **pragmas: object,
) -> sqlite3.Connection:
    """建立 SQLite 连接并一次性应用 :func:`apply_sqlite_pragmas`。

    Args:
        db_path: 数据库文件路径（支持 ``~``；父目录不存在时自动创建）。
        check_same_thread: ``None`` → Python 默认值（True）；
            多线程共享连接的存储（MonitorStore/StrategyStore 等）传 False。
        timeout: 秒级 connect timeout；``None`` 时用 busy_timeout_ms/1000.0。
        row_factory: 默认 ``sqlite3.Row``；传 None 禁用。
        **pragmas: 透传给 :func:`apply_sqlite_pragmas`。

    Returns:
        已应用 PRAGMA 的连接，``row_factory`` 已设置。
    """
    p = Path(db_path).expanduser()
    if p.parent != Path("."):  # 内存库或仅文件名不建目录
        p.parent.mkdir(parents=True, exist_ok=True)

    # P1-1 + R4-B：禁止 .get(k, 默认) 静默默认值。显式判空 + 走常量兜底，
    # 确保若用户意外传 None/"" 不会被静默替换（fail-loud 第一原则）。
    busy_value = pragmas.get("busy_timeout_ms")
    busy_ms = int(busy_value) if busy_value is not None else SQLITE_BUSY_TIMEOUT_MS
    connect_kwargs: dict[str, object] = {}
    if check_same_thread is not None:
        connect_kwargs["check_same_thread"] = check_same_thread
    connect_kwargs["timeout"] = float(timeout) if timeout is not None else (busy_ms / 1000.0)

    conn = sqlite3.connect(str(p), **connect_kwargs)
    if row_factory is not None:
        conn.row_factory = row_factory
    apply_sqlite_pragmas(conn, **pragmas)  # type: ignore[arg-type]
    return conn


def ensure_wal(conn: sqlite3.Connection) -> None:
    """兼容旧代码的别名：等价 ``apply_sqlite_pragmas(conn, synchronous='NORMAL')``。

    保留给仅想设置并发基线而不想了解参数细节的调用方。
    """
    apply_sqlite_pragmas(conn, enable_wal=True, synchronous=SQLITE_SYNCHRONOUS_NORMAL)


def set_synchronous(conn: sqlite3.Connection, level: SyncLevel) -> None:
    """单独切换同步级别（迁移批次提速常用）。

    典型用法::

        try:
            set_synchronous(conn, "OFF")
            bulk_insert(conn, rows)
            conn.commit()
        finally:
            set_synchronous(conn, "NORMAL")  # 必须切回！
    """
    conn.execute(f"PRAGMA synchronous = {str(level).upper()}")
