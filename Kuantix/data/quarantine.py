"""隔离区持久化（NF-26/NF-27）。

任何被显式拒绝的数据（UNKNOWN 证券类型、uint32 越界、回读不一致、NaN 字段…）
都落成一条 :class:`~Kuantix.core.contracts.QuarantineEntry`，**不允许静默丢弃**。

本模块用 SQLite 持久化隔离区，默认落在 ``~/.Kuantix/db/quarantine.db``
（NF-15/18：与 ``~/.easy_tdx/`` 完全隔离）。

fail-loud 要点
--------------
- ``add`` 是**幂等 upsert**：同一 ``(code, market)`` 再次出现时累加 ``attempts``、
  刷新 ``last_try``，而不是覆盖成新条目 —— 便于观察反复失败；
- ``remove`` 只删除精确匹配的条目，返回删除条数；不存在时返回 0（合法结果，
  不是静默吞错）。
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import threading
from pathlib import Path

from Kuantix.core.contracts import QuarantineEntry
from Kuantix.core.db import connect_sqlite
from Kuantix.core.fail_loud import require_non_empty

__all__ = ["QuarantineStore"]

#: 隔离区表结构
_SCHEMA = """
CREATE TABLE IF NOT EXISTS quarantine (
    code TEXT NOT NULL,
    market TEXT NOT NULL,
    reason TEXT NOT NULL,
    detail TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    last_try TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (code, market)
)
"""


def _iso(ts: dt.datetime) -> str:
    return ts.isoformat(timespec="seconds")


def _parse_iso(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text)


class QuarantineStore:
    """SQLite 持久化隔离区（线程安全）。

    Args:
        db_dir: 数据库目录（如 ``~/.Kuantix/db``）。
        db_name: 数据库文件名，默认 ``quarantine.db``。

    Examples:
        >>> store = QuarantineStore(tmp_path)  # doctest: +SKIP
        >>> entry = QuarantineEntry(code="430047", market="CN", reason="UNKNOWN_SECURITY_TYPE",
        ...                         detail="bj 前缀", occurred_at=now, last_try=now)
        >>> store.add(entry)  # doctest: +SKIP
        >>> len(store.list("CN"))  # doctest: +SKIP
        1
    """

    def __init__(self, db_dir: Path | str, *, db_name: str = "quarantine.db") -> None:
        self._dir = Path(db_dir).expanduser()
        self._dir.mkdir(parents=True, exist_ok=True)
        self._db_path = self._dir / db_name
        self._lock = threading.RLock()
        with self._lock:
            with self._connect() as conn:
                conn.execute(_SCHEMA)

    # ------------------------------------------------------------------ #
    # 公开接口
    # ------------------------------------------------------------------ #

    @property
    def db_path(self) -> Path:
        """数据库文件路径。"""
        return self._db_path

    def add(self, entry: QuarantineEntry) -> None:
        """写入/更新一条隔离记录（幂等 upsert）。

        Args:
            entry: 隔离条目。
        """
        require_non_empty(entry.code, "隔离区 code")
        require_non_empty(entry.market, "隔离区 market")
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO quarantine
                        (code, market, reason, detail, occurred_at, last_try, attempts)
                    VALUES (?, ?, ?, ?, ?, ?, 1)
                    ON CONFLICT(code, market) DO UPDATE SET
                        reason=excluded.reason,
                        detail=excluded.detail,
                        last_try=excluded.last_try,
                        attempts=quarantine.attempts + 1
                    """,
                    (
                        entry.code,
                        entry.market,
                        entry.reason,
                        entry.detail,
                        _iso(entry.occurred_at),
                        _iso(entry.last_try),
                    ),
                )

    def list(
        self,
        market: str | None = None,
        *,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[QuarantineEntry]:
        """列出隔离区条目（P1-2：支持 LIMIT/OFFSET DB 分页）。

        Args:
            market: 市场过滤；``None`` 返回全部。
            limit: 最大返回条数（SQLite LIMIT）；``None`` 不限制。
            offset: 跳过条数（SQLite OFFSET）；``None`` 不跳过。

        Returns:
            按 ``occurred_at`` 降序的条目列表（最近发生在前）。
        """
        sql_base = (
            "SELECT code, market, reason, detail, occurred_at, last_try, attempts "
            "FROM quarantine"
        )
        where, params = ("", []) if market is None else (" WHERE market = ?", [str(market)])
        order = " ORDER BY occurred_at DESC"
        paginate = ""
        if limit is not None:
            paginate += " LIMIT ?"
            params.append(int(limit))
        if offset is not None:
            paginate += " OFFSET ?"
            params.append(int(offset))
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(sql_base + where + order + paginate, params).fetchall()
        return [self._row_to_entry(row) for row in rows]

    def count(self, market: str | None = None) -> int:
        """匹配条件的隔离区条目总数（P1-2：配合 :meth:`list` 的 DB 分页用）。"""
        with self._lock:
            with self._connect() as conn:
                if market is None:
                    row = conn.execute("SELECT COUNT(*) FROM quarantine").fetchone()
                else:
                    row = conn.execute(
                        "SELECT COUNT(*) FROM quarantine WHERE market = ?",
                        (str(market),),
                    ).fetchone()
        return int(row[0]) if row is not None else 0

    def remove(self, code: str, market: str | None = None) -> int:
        """删除隔离条目。

        Args:
            code: 标的代码。
            market: 市场过滤；``None`` 时只按 code 删除。

        Returns:
            实际删除的条数（0 表示不存在，合法结果）。
        """
        with self._lock:
            with self._connect() as conn:
                if market is None:
                    cur = conn.execute("DELETE FROM quarantine WHERE code = ?", (code,))
                else:
                    cur = conn.execute(
                        "DELETE FROM quarantine WHERE code = ? AND market = ?",
                        (code, str(market)),
                    )
                return int(cur.rowcount)

    def clear(self) -> int:
        """清空全部隔离条目（供测试/运维重置）。"""
        with self._lock:
            with self._connect() as conn:
                cur = conn.execute("DELETE FROM quarantine")
                return int(cur.rowcount)

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def _connect(self) -> sqlite3.Connection:
        """打开数据库连接（Row 工厂 + P1-1 WAL/busy_timeout/NORMAL 并发基线）。"""
        return connect_sqlite(self._db_path)

    @staticmethod
    def _row_to_entry(row: sqlite3.Row) -> QuarantineEntry:
        return QuarantineEntry(
            code=str(row["code"]),
            market=str(row["market"]),
            reason=str(row["reason"]),
            detail=str(row["detail"]),
            occurred_at=_parse_iso(str(row["occurred_at"])),
            last_try=_parse_iso(str(row["last_try"])),
            attempts=int(row["attempts"]),
        )
