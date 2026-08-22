"""行情主存储（SQLite market.db，设计文档 08 的表 1.2）。

本模块是 **SQLite 行情湖的单一落点**：证券清单 / 日线 / 同步元数据 /
断点续传四张表全部集中在这里，业务层（搜索 / 回测 / 因子 / 迁移 /
同步）只经 :class:`MarketStore` 访问，不再各自摸文件系统。

设计要点
--------
- **WAL + busy_timeout + RLock**：WAL 允许读写并发；``busy_timeout`` 让
  多 worker 写并发时等待而不是立刻 ``database is locked``；:class:`RLock`
  保证单进程内线程安全（写侧 SyncEngine 多 worker 共用一个 store）。
- **单事务批量写**：``write_daily_bars`` 用 ``executemany`` 一次事务提交；
  迁移期可经 :meth:`bulk` 临时 ``PRAGMA synchronous=OFF`` 提速（T05）。
- **daily_bars 单表 + ``(market, code, date)`` 主键 WITHOUT ROWID**（D5）：
  查询/批量读远优于逐文件 IO，11M 行单表可承受。
- **断点表 O(1)**：``sync_checkpoint`` 以 ``(market, code)`` 为主键，
  大池（17798 只）续传按单只查询，不再整文件重写（D6）。
- **fail-loud**：损坏 / 参数非法显式抛
  :class:`~Kuantix.core.fail_loud.DataIntegrityError`，不静默兜底（R4）。

红线遵循
--------
- **R2**：本模块只用标准库 ``sqlite3``，不 import easy_tdx（上游调用收敛在
  adapters 层）。
- **R6**：市场/交易所取值一律小写（``cn``/``sh``/``sz``…），不写死 A 股
  常量；日期统一 ``YYYYMMDD`` 整数。
"""

from __future__ import annotations

from dataclasses import dataclass

import datetime as dt
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from Kuantix.core.contracts import Bar, Security
from Kuantix.core.db import (
    SQLITE_BUSY_TIMEOUT_MS,
    SQLITE_SYNCHRONOUS_NORMAL,
    SQLITE_SYNCHRONOUS_OFF,
    apply_sqlite_pragmas,
    connect_sqlite,
    set_synchronous,
)
from Kuantix.core.fail_loud import (
    DataIntegrityError,
    MissingConfigError,
    MissingKeyError,
    require_non_empty,
)

__all__ = [
    "MARKET_DB_FILENAME",
    "CHECKPOINT_STATUS_COMPLETED",
    "CHECKPOINT_STATUS_QUARANTINED",
    "CHECKPOINT_STATUS_FAILED",
    "MarketStore",
    "MinuteBar",
]

#: 行情主库默认文件名（位于 ``[paths].db`` 下）。
MARKET_DB_FILENAME = "market.db"

#: 断点状态取值（sync_checkpoint.status）。
CHECKPOINT_STATUS_COMPLETED = "completed"
CHECKPOINT_STATUS_QUARANTINED = "quarantined"
CHECKPOINT_STATUS_FAILED = "failed"

#: 连接等待锁的毫秒数（多 worker 并发写时避免立刻 database is locked）。
#: P1-1：统一移到 :mod:`Kuantix.core.db` 的 ``SQLITE_BUSY_TIMEOUT_MS``；
#: 保留本常量以兼容同文件内引用（与 ``connect(timeout=)`` 秒级参数同步）。
_BUSY_TIMEOUT_MS = SQLITE_BUSY_TIMEOUT_MS


@dataclass
class MinuteBar:
    """分钟线 bar（存储层形状；``date``=YYYYMMDD 整数，``time``=HHMM 整数）。"""

    market: str
    code: str
    date: int
    time: int
    open: float
    high: float
    low: float
    close: float
    vol: float
    amount: float

#: daily_bars 去重代码列表等派生统计的缓存 TTL（秒）。这些统计只在
#: ``data sync``/``migrate`` 写入后变化，搜索等场景被反复触发时命中缓存，
#: 避免对 1338 万行反复 ``SELECT DISTINCT`` 全表扫描。
_STATS_TTL_SECONDS = 30.0


def _date_int(date: dt.date) -> int:
    """``datetime.date`` → ``YYYYMMDD`` 整数（与 vipdoc 文件一致）。"""
    return date.year * 10000 + date.month * 100 + date.day


def _date_from_int(value: int) -> dt.date:
    """``YYYYMMDD`` 整数 → ``datetime.date``。"""
    return dt.date(int(value) // 10000, (int(value) // 100) % 100, int(value) % 100)


class MarketStore:
    """SQLite 行情主存储（四表 CRUD / 批量读写 / 断点续传）。

    Args:
        db_path: market.db 完整路径（``None`` 用 ``[paths].db / market.db``
            的默认名，由工厂 :meth:`from_config` 构造）。

    Examples:
        >>> store = MarketStore(tmp_path / "db" / "market.db")  # doctest: +SKIP
        >>> store.write_daily_bars("CN", "600000", bars)        # doctest: +SKIP
        2400
    """

    def __init__(self, db_path: Path | str | None = None) -> None:
        from Kuantix.config import get_config

        if db_path is None:
            config = get_config()
            db_path = config.paths.db / config.storage.market_db
        self._db_path = Path(db_path).expanduser()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._lock:
            with self._connect() as conn:
                self._create_schema(conn)

    # ------------------------------------------------------------------ #
    # 公开属性
    # ------------------------------------------------------------------ #

    @property
    def db_path(self) -> Path:
        """market.db 文件路径。"""
        return self._db_path

    # ------------------------------------------------------------------ #
    # 连接管理
    # ------------------------------------------------------------------ #

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """打开一条新连接（每操作一条，避免跨线程共享连接）。

        P1-1：通过 :func:`Kuantix.core.db.apply_sqlite_pragmas` 统一应用
        ``WAL + busy_timeout + synchronous=NORMAL``，替代手散写 PRAGMA。
        处于 :meth:`bulk` 窗口内时每条连接额外切到 ``synchronous=OFF``
        （迁移期提速，退出窗口下一条连接自动恢复 ``NORMAL``）。
        """
        conn = sqlite3.connect(str(self._db_path), timeout=_BUSY_TIMEOUT_MS / 1000.0)
        conn.row_factory = sqlite3.Row
        try:
            if getattr(self, "_bulk_sync_off", False):
                apply_sqlite_pragmas(conn, synchronous=SQLITE_SYNCHRONOUS_OFF)
            else:
                apply_sqlite_pragmas(conn, synchronous=SQLITE_SYNCHRONOUS_NORMAL)
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _create_schema(conn: sqlite3.Connection) -> None:
        """建四张表 + 索引（幂等）。WAL 在这里一次性启用并持久化。"""
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS securities (
                market        TEXT NOT NULL,
                code          TEXT NOT NULL,
                exchange      TEXT NOT NULL,
                security_type TEXT NOT NULL,
                name          TEXT NOT NULL DEFAULT '',
                updated_at    TEXT NOT NULL,
                PRIMARY KEY (market, code)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_securities_market_name
            ON securities(market, name COLLATE NOCASE)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_bars (
                market TEXT NOT NULL,
                code   TEXT NOT NULL,
                date   INTEGER NOT NULL,
                open   REAL NOT NULL,
                high   REAL NOT NULL,
                low    REAL NOT NULL,
                close  REAL NOT NULL,
                vol    REAL NOT NULL,
                amount REAL NOT NULL,
                PRIMARY KEY (market, code, date)
            ) WITHOUT ROWID
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_daily_bars_market_code
            ON daily_bars(market, code)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sync_meta (
                market                TEXT PRIMARY KEY,
                last_full_sync        TEXT,
                last_incremental_sync TEXT,
                total_securities      INTEGER NOT NULL DEFAULT 0,
                total_bars            INTEGER NOT NULL DEFAULT 0,
                updated_at            TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sync_checkpoint (
                market     TEXT NOT NULL,
                code       TEXT NOT NULL,
                status     TEXT NOT NULL,
                detail     TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                PRIMARY KEY (market, code)
            ) WITHOUT ROWID
            """
        )

    @contextmanager
    def bulk(self) -> Iterator[None]:
        """迁移/建湖期的批量写上下文：临时 ``PRAGMA synchronous=OFF``。

        SQLite 默认 ``synchronous=FULL``（每事务 fsync），迁移 508M 时是
        主要瓶颈；``OFF`` 把写入交给操作系统缓存，回读校验由迁移工具
        ``--verify`` 兜底（设计 3.3：导入 20min → 4-8min）。窗口内
        :meth:`_connect` 打开的每条写连接都走 ``synchronous=OFF``，退出后
        恢复默认（FULL），保证日常写盘仍是持久化语义。

        Raises:
            DataIntegrityError: 嵌套使用（不合法）。
        """
        if getattr(self, "_bulk_depth", 0) > 0:
            raise DataIntegrityError(
                "[fail-loud/NF-26] MarketStore.bulk 不支持嵌套使用"
            )
        self._bulk_depth = getattr(self, "_bulk_depth", 0) + 1
        self._bulk_sync_off = True
        try:
            yield
        finally:
            self._bulk_sync_off = False
            self._bulk_depth = max(0, getattr(self, "_bulk_depth", 1) - 1)

    # ------------------------------------------------------------------ #
    # 证券清单
    # ------------------------------------------------------------------ #

    def upsert_securities(self, securities: Sequence[Security]) -> int:
        """批量 upsert 证券清单（枚举结果落表，幂等）。

        Args:
            securities: 待写入证券列表。

        Returns:
            实际写入（含更新）的行数。

        Raises:
            MissingConfigError: 输入为空。
            DataIntegrityError: 单条 Security 含非法值（Security 构造已校验，
                这里仅防御性兜底）。
        """
        require_non_empty(list(securities), "upsert_securities.securities")
        now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        rows = [
            (
                sec.market,
                sec.code,
                sec.exchange,
                sec.security_type,
                sec.name,
                now,
            )
            for sec in securities
        ]
        with self._lock:
            with self._connect() as conn:
                conn.executemany(
                    """
                    INSERT INTO securities
                        (market, code, exchange, security_type, name, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(market, code) DO UPDATE SET
                        exchange=excluded.exchange,
                        security_type=excluded.security_type,
                        name=excluded.name,
                        updated_at=excluded.updated_at
                    """,
                    rows,
                )
                return len(rows)

    def list_securities(
        self,
        market: str | None = None,
        *,
        security_types: Sequence[str] | None = None,
    ) -> list[Security]:
        """列出证券清单（可过滤市场 / 证券类型）。

        ``security_types`` 把类型过滤下推到 SQL：全表 17634 条中 A 股仅
        5119 条（29%），下推后既少扫 2/3 的行，也少构造 2/3 的
        :class:`Security` 对象（实测 SQL 15.1ms → 5.1ms）。

        Args:
            market: 市场过滤；``None`` 返回全部。
            security_types: 证券类型白名单；``None`` 不过滤。

        Returns:
            :class:`Security` 列表（按 market/code 升序）。
        """
        clauses: list[str] = []
        params: list[Any] = []
        if market is not None:
            clauses.append("market = ?")
            params.append(str(market).upper())
        if security_types is not None:
            types = [str(t) for t in security_types]
            if not types:
                return []
            clauses.append("security_type IN (%s)" % ",".join("?" for _ in types))
            params.extend(types)
        sql = "SELECT market, code, exchange, security_type, name FROM securities"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY market, code"
        # 读路径不加锁（R2）：WAL 多读并发由 SQLite 保证，锁只保护写路径。
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_security(row) for row in rows]

    def search_securities(
        self, q: str, market: str | None = None, *, limit: int = 20
    ) -> list[Security]:
        """按代码/名称搜索证券清单（SQL 索引 + NOCASE）。

        Args:
            q: 搜索关键词（代码或名称）。
            market: 市场过滤；``None`` 不限制。
            limit: 返回条数上限。

        Returns:
            匹配列表（精确代码优先，其次名称前缀）。
        """
        query = str(q).strip()
        if not query:
            raise MissingKeyError("[fail-loud/NF-26] search q 不能为空")
        limit = max(1, min(int(limit), 500))
        clauses: list[str] = []
        params: list[Any] = []
        if market is not None:
            clauses.append("market = ?")
            params.append(str(market).upper())
        pattern = f"{query}%"
        clauses.append("(code LIKE ? OR name LIKE ? COLLATE NOCASE)")
        params.extend([pattern, pattern])
        sql = (
            "SELECT market, code, exchange, security_type, name FROM securities"
            " WHERE "
            + " AND ".join(clauses)
            + " ORDER BY CASE WHEN code = ? THEN 0 ELSE 1 END, code LIMIT ?"
        )
        order_params = [str(query)]
        # 读路径不加锁（R2）
        with self._connect() as conn:
            rows = conn.execute(sql, params + order_params + [limit]).fetchall()
        return [self._row_to_security(row) for row in rows]

    def security_name(self, code: str, market: str = "CN") -> str | None:
        """按代码查证券名称（主键索引 O(1)；未收录返回 ``None``）。

        供监控自选补全名称使用：添加/列出自选时若 name 为空，从证券清单
        （``securities`` 表）按代码查真实名称填入，解决自选表格名称列空白。

        Args:
            code: 证券代码。
            market: 市场码。

        Returns:
            证券名称（未找到返回 ``None``）。
        """
        # 读路径不加锁（R2）
        with self._connect() as conn:
            row = conn.execute(
                "SELECT name FROM securities WHERE market = ? AND code = ?",
                (str(market).upper(), str(code)),
            ).fetchone()
        if row is None:
            return None
        name = str(row["name"])
        return name or None

    def securities_count(self, market: str | None = None) -> int:
        """证券清单条数（状态展示用）。"""
        # 读路径不加锁（R2）
        with self._connect() as conn:
            if market is None:
                row = conn.execute("SELECT COUNT(*) AS n FROM securities").fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM securities WHERE market = ?",
                    (str(market).upper(),),
                ).fetchone()
        return int(row["n"])

    def securities_updated_at(self, market: str) -> str | None:
        """证券清单最近更新时间（ISO8601）；无数据返回 ``None``。"""
        # 读路径不加锁（R2）
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MAX(updated_at) AS u FROM securities WHERE market = ?",
                (str(market).upper(),),
            ).fetchone()
        value = row["u"]
        return str(value) if value is not None else None

    @staticmethod
    def _row_to_security(row: sqlite3.Row) -> Security:
        """SQLite 行 → :class:`Security`（Security 构造校验 UNKNOWN 等）。"""
        return Security(
            code=str(row["code"]),
            exchange=str(row["exchange"]),
            market=str(row["market"]),
            security_type=str(row["security_type"]),
            name=str(row["name"]),
        )

    # ------------------------------------------------------------------ #
    # 日线
    # ------------------------------------------------------------------ #

    def write_daily_bars(
        self, market: str, code: str, bars: Sequence[Bar]
    ) -> int:
        """单事务批量写日线（upsert 语义，幂等）。

        Args:
            market: 市场码（``CN``/``HK``/``US``）。
            code: 证券代码。
            bars: 待写 K 线（``vol`` 单位=手，RD-8）。

        Returns:
            写入条数。

        Raises:
            MissingKeyError: market/code 为空。
            DataIntegrityError: 单条 Bar 非法（Bar 构造已校验，防御性兜底）。
        """
        require_non_empty(str(market), "write_daily_bars.market")
        require_non_empty(str(code), "write_daily_bars.code")
        rows = [
            (
                str(market),
                str(code),
                _date_int(bar.date),
                float(bar.open),
                float(bar.high),
                float(bar.low),
                float(bar.close),
                float(bar.vol),
                float(bar.amount),
            )
            for bar in bars
        ]
        with self._lock:
            with self._connect() as conn:
                if rows:
                    conn.executemany(
                        """
                        INSERT INTO daily_bars
                            (market, code, date, open, high, low, close, vol, amount)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(market, code, date) DO UPDATE SET
                            open=excluded.open,
                            high=excluded.high,
                            low=excluded.low,
                            close=excluded.close,
                            vol=excluded.vol,
                            amount=excluded.amount
                        """,
                        rows,
                    )
                return len(rows)

    def read_daily_bars(
        self, market: str, code: str, *, tail: int | None = None
    ) -> list[Bar]:
        """读取一只标的日线（升序）。

        Args:
            market: 市场码。
            code: 证券代码。
            tail: 只取最后 ``tail`` 根（``None`` 取全部）。写后回读校验
                （NF-27）只比对末尾 N 条，传 ``tail`` 可让 SQL 走
                ``ORDER BY date DESC LIMIT n`` 反向取尾，避免为校验 20 条
                而把整只标的全历史（数千根）读进内存。

        Returns:
            升序 :class:`Bar` 列表；无数据返回空列表（合法态，调用方
            按业务语义处理）。
        """
        if tail is not None and int(tail) <= 0:
            return []
        # 读路径不加锁（R2）
        with self._connect() as conn:
            if tail is None:
                rows = conn.execute(
                    """
                    SELECT date, open, high, low, close, vol, amount
                    FROM daily_bars WHERE market = ? AND code = ?
                    ORDER BY date
                    """,
                    (str(market), str(code)),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT date, open, high, low, close, vol, amount
                    FROM daily_bars WHERE market = ? AND code = ?
                    ORDER BY date DESC LIMIT ?
                    """,
                    (str(market), str(code), int(tail)),
                ).fetchall()
                rows = rows[::-1]
        return [self._row_to_bar(row) for row in rows]

    # ------------------------------------------------------------------ #
    # 分钟线存储（按分区文件，避免单文件过大，F1 存储侧）
    # ------------------------------------------------------------------ #

    def _minute_partition_key(self, date_int: int, partition: str) -> str:
        """把 ``YYYYMMDD`` 整型日期映射为分钟线分区键。

        Args:
            date_int: 形如 ``20260802`` 的整型日期。
            partition: ``"month"``（默认）→ ``YYYY-MM``；``"week"`` → ``YYYY-Www``。

        Returns:
            分区键字符串。
        """
        s = str(int(date_int))
        y, m, d = int(s[:4]), int(s[4:6]), int(s[6:8])
        if partition == "week":
            wk = dt.date(y, m, d).isocalendar()[1]
            return f"{y}-W{wk:02d}"
        return f"{y}-{m:02d}"

    def _minute_db_path(self, market: str, code: str, key: str) -> Path:
        """分钟线分区库路径 ``<db父目录>/minute/<market>_<code>_<key>.db``。"""
        base = self._db_path.parent / "minute"
        base.mkdir(parents=True, exist_ok=True)
        return base / f"{str(market).strip()}_{str(code).strip()}_{key}.db"

    def write_minute_bars(
        self,
        market: str,
        code: str,
        bars: Sequence,
        *,
        partition: str = "month",
    ) -> int:
        """写分钟线（按分区文件落库，避免单文件过大）。

        将 ``bars`` 按日期分组到不同分区库（按月/周），每库一张 ``minute_bars``
        表，upsert 幂等。分钟线 bar 需含 ``date``(YYYYMMDD 整数) / ``time``(HHMM
        整数) / ``open`` / ``high`` / ``low`` / ``close`` / ``vol`` / ``amount``。

        Args:
            market: 市场码（``CN``/``HK``/``US``）。
            code: 证券代码。
            bars: 分钟线 bar 序列。
            partition: ``"month"`` 或 ``"week"``。

        Returns:
            写入（upsert）的总条数。
        """
        if not bars:
            return 0
        require_non_empty(str(market), "write_minute_bars.market")
        require_non_empty(str(code), "write_minute_bars.code")
        groups: dict[str, list] = {}
        for b in bars:
            key = self._minute_partition_key(int(b.date), partition)
            groups.setdefault(key, []).append(b)
        total = 0
        for key, grp in groups.items():
            total += self._write_minute_partition(str(market), str(code), grp)
        return total

    def _write_minute_partition(
        self, market: str, code: str, bars: Sequence, partition: str = "month"
    ) -> int:
        """把一组同分区分钟线写入单个分区库（单事务 upsert）。"""
        rows = [
            (
                market,
                code,
                int(b.date),
                int(b.time),
                float(b.open),
                float(b.high),
                float(b.low),
                float(b.close),
                float(b.vol),
                float(getattr(b, "amount", 0.0)),
            )
            for b in bars
        ]
        path = self._minute_db_path(market, code, self._minute_partition_key(int(bars[0].date), partition))
        with self._lock:
            # P1-1：connect_sqlite 一次性设置 WAL + busy_timeout + synchronous=NORMAL
            with connect_sqlite(path) as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS minute_bars (
                        market TEXT NOT NULL,
                        code   TEXT NOT NULL,
                        date   INTEGER NOT NULL,
                        time   INTEGER NOT NULL,
                        open   REAL NOT NULL,
                        high   REAL NOT NULL,
                        low    REAL NOT NULL,
                        close  REAL NOT NULL,
                        vol    REAL NOT NULL,
                        amount REAL NOT NULL,
                        PRIMARY KEY (market, code, date, time)
                    )
                    """
                )
                conn.executemany(
                    """
                    INSERT INTO minute_bars
                        (market, code, date, time, open, high, low, close, vol, amount)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(market, code, date, time) DO UPDATE SET
                        open=excluded.open, high=excluded.high, low=excluded.low,
                        close=excluded.close, vol=excluded.vol, amount=excluded.amount
                    """,
                    rows,
                )
                conn.commit()
        return len(rows)

    def read_minute_bars(
        self,
        market: str,
        code: str,
        *,
        start_date: int | None = None,
        end_date: int | None = None,
        partition: str | None = None,
    ) -> list:
        """读取分钟线（可指定分区或跨全部分区合并）。

        Args:
            market: 市场码。
            code: 证券代码。
            start_date: 起始 ``YYYYMMDD``（含），``None`` 不限。
            end_date: 结束 ``YYYYMMDD``（含），``None`` 不限。
            partition: 限定分区键；``None`` 合并该标的所有分区文件。

        Returns:
            按 ``(date, time)`` 升序的分钟线 bar 列表（dataclass ``MinuteBar``）。
        """
        import glob

        market = str(market).strip()
        code = str(code).strip()
        if partition is not None:
            paths = [self._minute_db_path(market, code, partition)]
        else:
            pattern = str(self._db_path.parent / "minute" / f"{market}_{code}_*.db")
            paths = sorted(glob.glob(pattern))
        out: list = []
        for path in paths:
            if not Path(path).is_file():
                continue
            # 读路径不加锁（R2）：分钟线分区库同样 WAL 多读
            with connect_sqlite(path) as conn:
                sql = (
                    "SELECT market, code, date, time, open, high, low, close, vol, amount "
                    "FROM minute_bars WHERE market = ? AND code = ?"
                )
                params: list = [market, code]
                if start_date is not None:
                    sql += " AND date >= ?"
                    params.append(int(start_date))
                if end_date is not None:
                    sql += " AND date <= ?"
                    params.append(int(end_date))
                sql += " ORDER BY date, time"
                rows = conn.execute(sql, params).fetchall()
            out.extend(MinuteBar(*row) for row in rows)
        return out

    def read_all_daily_bars(
        self, market: str,
    ) -> dict[str, list[Bar]]:
        """单连接流式读取该市场全部日线，按 code 分组（校验/统计用）。

        与逐标的 :meth:`read_daily_bars` 相比，**只建立一次 sqlite 连接**，
        避免 9000+ 标的时每次新开连接的开销（1338 万行下尤为显著）。行按
        ``(code, date)`` 升序返回，分组后每只标的自然升序，供完整性校验复用。

        Args:
            market: 市场码。

        Returns:
            ``{code: [Bar, ...]}``（每只标的按日期升序）。
        """
        grouped: dict[str, list[Bar]] = {}
        # 读路径不加锁（R2）
        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT code, date, open, high, low, close, vol, amount
                FROM daily_bars WHERE market = ? ORDER BY code, date
                """,
                (str(market),),
            )
            while True:
                batch = cursor.fetchmany(10000)
                if not batch:
                    break
                for row in batch:
                    grouped.setdefault(str(row["code"]), []).append(
                        self._row_to_bar(row)
                    )
        return grouped

    def _fetch_tail_rows(
        self,
        cols: str,
        codes: Sequence[str],
        market: str,
        range_sql: str,
        range_params: Sequence[Any],
        tail: int,
    ) -> list[tuple[Any, ...]]:
        """逐码取每只标的最后 ``tail`` 行，返回按 (code, date) 升序的扁平行集。

        主键 ``(market, code, date)`` + WITHOUT ROWID 使得
        ``code = ? ORDER BY date DESC LIMIT N`` 退化为索引倒序扫描，
        代价只与 ``N`` 相关而与该标的历史长度无关。相比 ROW_NUMBER
        窗口函数（需扫全历史后排序）在全市场规模下快 1~2 个数量级。

        单条连接内复用同一 SQL（SQLite 会命中语句缓存），
        与批量 IN 查询相比多出的只是每码一次 C 层调用（~15µs）。
        """
        sql = (
            "SELECT %s FROM daily_bars WHERE market = ? AND code = ?%s"
            " ORDER BY date DESC LIMIT ?" % (cols, range_sql)
        )
        rows: list[tuple[Any, ...]] = []
        # 读路径不加锁（R2）：WAL 多读并发由 SQLite 保证，锁只保护写路径。
        with self._connect() as conn:
            for code in sorted(codes):  # 保持与批量查询一致的 code 升序
                chunk = conn.execute(
                    sql, (str(market), str(code), *range_params, tail)
                ).fetchall()
                if chunk:
                    rows.extend(reversed(chunk))  # DESC → 还原为日期升序
        return rows

    def read_daily_frames(
        self,
        codes: Sequence[str],
        market: str,
        *,
        start_date: int | None = None,
        end_date: int | None = None,
        tail: int | None = None,
    ) -> dict[str, pd.DataFrame]:
        """批量读取多只标的日线（问题 3 B4：因子喂数据向量化）。

        一次 ``WHERE market=? AND code IN (...)?`` 取回，按 code 分组建
        DataFrame，替代逐标的 ``read_daily_frame`` 循环。

        **按码取尾**：``tail=N`` 时用 ``ROW_NUMBER() OVER (PARTITION BY
        code ORDER BY date DESC)`` 精确取每只标的最后 N 根 —— 选股技术
        过滤只需要 ``rolling(20)`` 级别的窗口，无需整段历史。与按日期
        截断不同，窗口函数对长期停牌标的同样能取足 N 根，不改变过滤语义。

        **区间过滤（数据量大优化）**：传入 ``start_date``/``end_date``（
        ``YYYYMMDD`` 整数）时，SQL 层用 ``date BETWEEN ? AND ?`` 限定区间，
        走 ``(market, code, date)`` 主键索引，只取目标区间 —— 因子计算只
        需要 ``[start, end]``（含前视缓冲）样本时，避免把全部历史
        （1338 万行）读进内存构造 DataFrame。

        Args:
            codes: 代码列表。
            market: 市场码。
            start_date: 起始日期（含，``YYYYMMDD``）；``None`` 不限。
            end_date: 结束日期（含，``YYYYMMDD``）；``None`` 不限。
            tail: 每只标的只取最后 N 根；``None`` 取区间内全部。

        Returns:
            ``{code: DataFrame}``（带 ``datetime`` 列，升序）。
        """
        require_non_empty(list(codes), "read_daily_frames.codes")
        code_list = [str(c) for c in codes]
        placeholders = ",".join("?" for _ in code_list)
        range_sql = ""
        range_params: list[Any] = []
        if start_date is not None or end_date is not None:
            if start_date is not None and end_date is not None:
                range_sql = " AND date BETWEEN ? AND ?"
                range_params.extend([int(start_date), int(end_date)])
            elif start_date is not None:
                range_sql = " AND date >= ?"
                range_params.append(int(start_date))
            else:
                range_sql = " AND date <= ?"
                range_params.append(int(end_date))
        params: list[Any] = [str(market), *code_list, *range_params]
        cols = "code, date, open, high, low, close, vol, amount"
        # 列式读取：绕开逐行 Bar 构造（13M 次对象创建 + 8000 万次标量
        # 校验，实测占全市场选股 64% 耗时），改为一次性取回后按 code
        # 切片建 DataFrame，OHLC 值域校验由 _validate_ohlc_vectorized
        # 向量化完成，fail-loud 语义不变（NF-26）。
        if tail is not None and int(tail) > 0:
            # 取尾走**逐码索引区间扫描**而非 ROW_NUMBER 窗口函数。
            # 表为 WITHOUT ROWID + 主键 (market, code, date)，
            # `code = ? ORDER BY date DESC LIMIT N` 是纯索引倒序扫描，
            # 只触碰 N 行；窗口函数则必须扫完这些 code 的全部历史再排序。
            # 实测（500 只 / tail=20）：窗口函数 898ms → 逐码取尾 15ms（59×），
            # 且两者返回行完全一致（已逐行比对验证）。
            raw = self._fetch_tail_rows(
                cols, code_list, market, range_sql, range_params, int(tail)
            )
        else:
            sql = (
                "SELECT %s"
                " FROM daily_bars WHERE market = ? AND code IN (%s)%s ORDER BY code, date"
                % (cols, placeholders, range_sql)
            )
            # 读路径不加锁（R2）
            with self._connect() as conn:
                raw = conn.execute(sql, params).fetchall()
        if not raw:
            return {}
        n_rows = len(raw)
        arr_code = np.array([r[0] for r in raw], dtype=object)
        arr_date = np.fromiter((r[1] for r in raw), dtype=np.int64, count=n_rows)
        nums = np.empty((n_rows, 6), dtype=np.float64)
        for j in range(6):
            nums[:, j] = np.fromiter(
                (r[2 + j] for r in raw), dtype=np.float64, count=n_rows
            )
        # 原始行已全部转入 numpy，立即释放：批量读 200 只 × 全历史时
        # raw 约 50 万个 8 元组（~70MB），若保留到 DataFrame 构造阶段
        # 会与结果集同时驻留，抬高峰值内存。提前 del 让 GC 立刻回收。
        del raw
        # SQL 已 ORDER BY code, date → 同 code 连续，用边界切片替代分组
        boundaries = np.flatnonzero(arr_code[1:] != arr_code[:-1]) + 1
        starts = np.concatenate(([0], boundaries))
        ends = np.concatenate((boundaries, [n_rows]))

        dt_all = pd.to_datetime(arr_date.astype("U8"), format="%Y%m%d")
        result: dict[str, pd.DataFrame] = {}
        for s, e in zip(starts.tolist(), ends.tolist()):
            code = str(arr_code[s])
            frame = pd.DataFrame(
                {
                    "datetime": dt_all[s:e],
                    "open": nums[s:e, 0],
                    "high": nums[s:e, 1],
                    "low": nums[s:e, 2],
                    "close": nums[s:e, 3],
                    "vol": nums[s:e, 4],
                    "amount": nums[s:e, 5],
                }
            )
            self._validate_ohlc_vectorized(frame, context=f"{market}/{code}")
            result[code] = frame
        return result

    def latest_closes(
        self, market: str, codes: Sequence[str] | None = None
    ) -> dict[str, tuple[int, float]]:
        """批量取每只标的的**最新一根**日线日期与收盘价（选股快路径）。

        选股在**无技术/缠论过滤条件**时只需要最新收盘价用于展示，原实现
        为此逐只读取全历史（7582 次查询 / 1334 万行）。本方法用一条
        ``GROUP BY code`` 聚合查询一次取回，避免全历史读放大。

        Args:
            market: 市场码。
            codes: 代码白名单；``None`` 表示该市场全部。

        Returns:
            ``{code: (date_int, close)}``；无数据返回空字典。
        """
        market_s = str(market)
        code_list: list[str] | None = None
        if codes is not None:
            code_list = [str(c) for c in codes]
            if not code_list:
                return {}
        # 实现选型（实测 @ 13.4M 行 / 9175 只）：
        #   A. `GROUP BY code` + bare-column MAX(date) ............ 1274ms
        #   B. `DISTINCT code` + 逐码 `ORDER BY date DESC LIMIT 1` .. 56ms  ← 采用
        # 表为 WITHOUT ROWID + 主键 (market, code, date)：
        #   - `SELECT DISTINCT code` 走主键跳跃扫描（skip-scan），只碰
        #     每个 code 的首行，17.6ms 取回全部 9175 只；
        #   - `code = ? ORDER BY date DESC LIMIT 1` 是纯索引倒序定位，
        #     单次 ~0.004ms，9175 次合计 38.6ms。
        # 而 GROUP BY 写法必须把该 market 的 1340 万行全部读出再聚合。
        # 两种写法返回的字典已逐项比对，完全相同（9175/9175）。
        # 锁策略（R2）：**读路径不加锁**。SQLite WAL 模式天然支持多读单写，
        # 且每条连接独立（_connect 每操作新建），并发读由 SQLite 内部保证；
        # 锁只保护写路径（upsert/checkpoint 等）。实测分段加锁反而不行
        # （9175 次建连 42ms→3.4s，p99 恶化 2223×）——WAL 并发读的正确打开
        # 方式是读完全并行，而不是缩小锁窗口。
        seek_sql = (
            "SELECT date, close FROM daily_bars"
            " WHERE market = ? AND code = ? ORDER BY date DESC LIMIT 1"
        )
        out: dict[str, tuple[int, float]] = {}
        with self._connect() as conn:
            if code_list is None:
                code_list = [
                    str(r[0])
                    for r in conn.execute(
                        "SELECT DISTINCT code FROM daily_bars WHERE market = ?",
                        (market_s,),
                    ).fetchall()
                ]
            for code in code_list:
                row = conn.execute(seek_sql, (market_s, code)).fetchone()
                if row is not None:
                    out[code] = (int(row[0]), float(row[1]))
        return out

    def has_data(self, market: str, code: str) -> bool:
        """是否存在该标的数据（auto 数据源分支的本地判定，O(1)）。"""
        # 读路径不加锁（R2）
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM daily_bars WHERE market = ? AND code = ? LIMIT 1",
                (str(market), str(code)),
            ).fetchone()
        return row is not None

    def last_bar_date(self, market: str, code: str) -> int | None:
        """最后交易日 ``YYYYMMDD``；无数据返回 ``None``（增量同步用）。"""
        # 读路径不加锁（R2）
        with self._connect() as conn:
            row = conn.execute(
                    """
                    SELECT MAX(date) AS last_date FROM daily_bars
                    WHERE market = ? AND code = ?
                    """,
                    (str(market), str(code)),
                ).fetchone()
        value = row["last_date"]
        return int(value) if value is not None else None

    def daily_bar_count(self, market: str | None = None) -> int:
        """日线总条数（状态展示/校验用）。"""
        # 读路径不加锁（R2）
        with self._connect() as conn:
                if market is None:
                    row = conn.execute("SELECT COUNT(*) AS n FROM daily_bars").fetchone()
                else:
                    row = conn.execute(
                        "SELECT COUNT(*) AS n FROM daily_bars WHERE market = ?",
                        (str(market),),
                    ).fetchone()
        return int(row["n"])

    def daily_bar_stats(self, market: str) -> dict[str, int | None]:
        """一次聚合返回该市场 daily_bars 的覆盖统计（O(1) 单表扫描）。

        与多次独立 ``COUNT``/``COUNT(DISTINCT)``/``MIN``/``MAX`` 相比，
        单条 SQL 只扫一遍表（1338 万行下省 2-3 次全表扫描），供 D1 状态页
        构造 coverage。返回 ``{bars, distinct_codes, first_date, last_date}``
        （日期为 ``YYYYMMDD`` 整数；空表时首末日为 ``None``）。

        Args:
            market: 市场码（``CN``/``HK``/``US``）。

        Returns:
            覆盖统计字典（无数据时 ``bars=0``、``distinct_codes=0``、
            ``first_date=None``、``last_date=None``）。
        """
        # 读路径不加锁（R2）
        with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT COUNT(*)              AS bars,
                           COUNT(DISTINCT code)  AS codes,
                           MIN(date)             AS first_date,
                           MAX(date)             AS last_date
                    FROM daily_bars WHERE market = ?
                    """,
                    (str(market),),
                ).fetchone()
        bars = int(row["bars"])
        codes = int(row["codes"])
        first_value = row["first_date"]
        last_value = row["last_date"]
        return {
            "bars": bars,
            "distinct_codes": codes,
            "first_date": int(first_value) if first_value is not None else None,
            "last_date": int(last_value) if last_value is not None else None,
        }

    def date_range(self, market: str) -> tuple[int, int] | None:
        """该市场 daily_bars 的 ``(MIN(date), MAX(date))``（YYYYMMDD 整数）。

        用于 D1 空湖判定的补充统计：未迁移完成时 securities 表可能为空（行
        只写进 daily_bars），``verify_market_store`` 遍历 securities 表会漏数，
        这里直接从 daily_bars 取首末日补齐 coverage（O(1) 聚合，不走证券表）。

        Args:
            market: 市场码（``CN``/``HK``/``US``）。

        Returns:
            ``(first_date, last_date)``；该市场无日线返回 ``None``。
        """
        # 读路径不加锁（R2）
        with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT MIN(date) AS first_date, MAX(date) AS last_date
                    FROM daily_bars WHERE market = ?
                    """,
                    (str(market),),
                ).fetchone()
        first_value = row["first_date"]
        last_value = row["last_date"]
        if first_value is None or last_value is None:
            return None
        return (int(first_value), int(last_value))

    def distinct_codes(self, market: str) -> int:
        """该市场 daily_bars 中的去重代码数（O(1) 聚合，走 (market, code) 索引）。

        securities 表可能为空（未迁移完成时行只写进 daily_bars），
        ``verify_market_store`` 遍历 securities 表会漏数；这里直接从
        daily_bars 取去重代码数，供 D1 coverage 合并统计使用。

        Args:
            market: 市场码。

        Returns:
            去重代码数。
        """
        # 读路径不加锁（R2）
        with self._connect() as conn:
                row = conn.execute(
                    "SELECT COUNT(DISTINCT code) AS n FROM daily_bars WHERE market = ?",
                    (str(market),),
                ).fetchone()
        return int(row["n"])

    def list_daily_bar_codes(self, market: str) -> list[str]:
        """该市场 daily_bars 的去重代码列表（securities 表为空时搜索兜底源）。

        迁移只导入了 ``daily_bars`` 时（未提供 ``--catalog``）``securities``
        表可能为空，证券搜索本应 422；这里从 daily_bars 提取去重代码作为
        兜底清单，保证**代码**搜索可用（名称搜索此时无名称可匹配，返回空为
        合法态）。结果带进程级 TTL 缓存，避免频繁全表去重扫描。

        Args:
            market: 市场码。

        Returns:
            升序去重代码列表（空表返回 ``[]``）。
        """
        # 读路径不加锁（R2）——注意：缓存未命中时才建连查询
        import time as _time

        now = _time.monotonic()
        cache = getattr(self, "_daily_bar_codes_cache", None)
        if cache is None:
            cache = self._daily_bar_codes_cache = {}
        entry = cache.get(str(market).upper())
        if entry is not None and now - entry["ts"] < _STATS_TTL_SECONDS:
            return entry["codes"]
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT DISTINCT code FROM daily_bars WHERE market = ? "
                    "ORDER BY code",
                    (str(market),),
                ).fetchall()
        codes = [str(row["code"]) for row in rows]
        cache[str(market).upper()] = {"ts": now, "codes": codes}
        return codes

    @staticmethod
    def _row_to_bar(row: sqlite3.Row) -> Bar:
        """SQLite 行 → :class:`Bar`（Bar 构造校验价格/区间）。"""
        return Bar(
            date=_date_from_int(int(row["date"])),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            vol=float(row["vol"]),
            amount=float(row["amount"]),
        )

    @staticmethod
    def _bars_to_frame(bars: list[Bar]) -> pd.DataFrame:
        """Bar 列表 → 带 ``datetime`` 列的 DataFrame（与 L1Reader 同构）。

        **列式构造**（性能）：原实现按行造 ``dict`` 再交给
        ``pd.DataFrame(list_of_dict)``，pandas 需逐行推断类型
        （``nested_data_to_arrays`` → ``objects_to_datetime64``），
        7582 只标的的选股链路上该路径实测占 32s。改为直接按列建
        ``np.ndarray``，类型显式给定，零逐行推断。
        """
        if not bars:
            return pd.DataFrame(
                columns=["datetime", "open", "high", "low", "close", "vol", "amount"]
            )
        n = len(bars)
        dates = np.empty(n, dtype="datetime64[ns]")
        opens = np.empty(n, dtype=np.float64)
        highs = np.empty(n, dtype=np.float64)
        lows = np.empty(n, dtype=np.float64)
        closes = np.empty(n, dtype=np.float64)
        vols = np.empty(n, dtype=np.float64)
        amounts = np.empty(n, dtype=np.float64)
        for i, bar in enumerate(bars):
            dates[i] = np.datetime64(bar.date, "ns")
            opens[i] = bar.open
            highs[i] = bar.high
            lows[i] = bar.low
            closes[i] = bar.close
            vols[i] = bar.vol
            amounts[i] = bar.amount
        return pd.DataFrame(
            {
                "datetime": dates,
                "open": opens,
                "high": highs,
                "low": lows,
                "close": closes,
                "vol": vols,
                "amount": amounts,
            }
        )

    @staticmethod
    def _validate_ohlc_vectorized(
        frame: pd.DataFrame, *, context: str
    ) -> None:
        """向量化 OHLC 值域校验（等价 :class:`Bar` 的 ``__post_init__``）。

        :class:`Bar` 逐根构造时做 fail-loud 校验（NF-26），代价是每行
        ~6 次 ``require_finite`` 标量调用 —— 全市场选股链路上实测产生
        8010 万次调用 / 26.5s。本函数用 numpy 一次性做**完全等价**的
        检查，保留 fail-loud 语义但不再逐行进 Python。

        校验项（与 ``Bar.__post_init__`` 一一对应）：
          - open/high/low/close 有限且 > 0；
          - vol/amount 有限且 >= 0；
          - high >= low；low <= open <= high；low <= close <= high。

        Args:
            frame: 含 open/high/low/close/vol/amount 列的 DataFrame。
            context: 报错上下文（如 ``"CN/600000"``）。

        Raises:
            DataIntegrityError: 任一校验失败（报第一条违规行的具体值）。
        """
        if frame.empty:
            return
        o = frame["open"].to_numpy(dtype=np.float64, copy=False)
        h = frame["high"].to_numpy(dtype=np.float64, copy=False)
        low_ = frame["low"].to_numpy(dtype=np.float64, copy=False)
        c = frame["close"].to_numpy(dtype=np.float64, copy=False)
        v = frame["vol"].to_numpy(dtype=np.float64, copy=False)
        a = frame["amount"].to_numpy(dtype=np.float64, copy=False)

        def _first_bad(mask: np.ndarray) -> int:
            idx = np.flatnonzero(mask)
            return int(idx[0]) if idx.size else -1

        for name, arr in (("open", o), ("high", h), ("low", low_), ("close", c)):
            bad = ~np.isfinite(arr) | (arr <= 0)
            i = _first_bad(bad)
            if i >= 0:
                raise DataIntegrityError(
                    f"[fail-loud/NF-26] {context} 第 {i} 行 {name}={arr[i]!r} "
                    f"非有限正数，拒绝入 L1"
                )
        for name, arr in (("vol", v), ("amount", a)):
            bad = ~np.isfinite(arr) | (arr < 0)
            i = _first_bad(bad)
            if i >= 0:
                raise DataIntegrityError(
                    f"[fail-loud/NF-26] {context} 第 {i} 行 {name}={arr[i]!r} "
                    f"非有限非负数，拒绝入 L1"
                )
        i = _first_bad(h < low_)
        if i >= 0:
            raise DataIntegrityError(
                f"[fail-loud/NF-26] {context} 第 {i} 行 high={h[i]} < low={low_[i]}，"
                f"K 线自相矛盾"
            )
        i = _first_bad((o < low_) | (o > h))
        if i >= 0:
            raise DataIntegrityError(
                f"[fail-loud/NF-26] {context} 第 {i} 行 open={o[i]} 不在 "
                f"[low={low_[i]}, high={h[i]}] 区间内"
            )
        i = _first_bad((c < low_) | (c > h))
        if i >= 0:
            raise DataIntegrityError(
                f"[fail-loud/NF-26] {context} 第 {i} 行 close={c[i]} 不在 "
                f"[low={low_[i]}, high={h[i]}] 区间内"
            )

    # ------------------------------------------------------------------ #
    # 同步元数据
    # ------------------------------------------------------------------ #

    def save_sync_meta(
        self,
        market: str,
        *,
        last_full_sync: str | None = None,
        last_incremental_sync: str | None = None,
        total_securities: int = 0,
        total_bars: int = 0,
    ) -> None:
        """写入/更新一条同步元数据（取代 sync_state.json 的行情部分）。"""
        require_non_empty(str(market), "save_sync_meta.market")
        now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO sync_meta
                        (market, last_full_sync, last_incremental_sync,
                         total_securities, total_bars, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(market) DO UPDATE SET
                        last_full_sync=COALESCE(excluded.last_full_sync, sync_meta.last_full_sync),
                        last_incremental_sync=COALESCE(excluded.last_incremental_sync, sync_meta.last_incremental_sync),
                        total_securities=excluded.total_securities,
                        total_bars=excluded.total_bars,
                        updated_at=excluded.updated_at
                    """,
                    (
                        str(market),
                        last_full_sync,
                        last_incremental_sync,
                        int(total_securities),
                        int(total_bars),
                        now,
                    ),
                )

    def sync_meta_view(self, market: str) -> dict[str, Any] | None:
        """读取一条同步元数据；不存在返回 ``None``。"""
        # 读路径不加锁（R2）
        with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT market, last_full_sync, last_incremental_sync,
                           total_securities, total_bars, updated_at
                    FROM sync_meta WHERE market = ?
                    """,
                    (str(market),),
                ).fetchone()
        if row is None:
            return None
        return {
            "market": str(row["market"]),
            "last_full_sync": row["last_full_sync"],
            "last_incremental_sync": row["last_incremental_sync"],
            "total_securities": int(row["total_securities"]),
            "total_bars": int(row["total_bars"]),
            "updated_at": str(row["updated_at"]),
        }

    # ------------------------------------------------------------------ #
    # 断点续传（D6：JSON → sync_checkpoint 表）
    # ------------------------------------------------------------------ #

    def save_checkpoint(
        self,
        market: str,
        completed: set[str],
        quarantined: set[str],
        failed: set[str],
    ) -> int:
        """批量写断点（upsert，幂等）。

        Args:
            market: 市场码。
            completed: 已完成代码集合。
            quarantined: 已隔离代码集合。
            failed: 已失败代码集合。

        Returns:
            写入行数。
        """
        now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        rows: list[tuple[str, str, str, str]] = []
        for code in completed:
            rows.append((str(market), str(code), CHECKPOINT_STATUS_COMPLETED, "", now))
        for code in quarantined:
            rows.append((str(market), str(code), CHECKPOINT_STATUS_QUARANTINED, "", now))
        for code in failed:
            rows.append((str(market), str(code), CHECKPOINT_STATUS_FAILED, "", now))
        if not rows:
            return 0
        with self._lock:
            with self._connect() as conn:
                conn.executemany(
                    """
                    INSERT INTO sync_checkpoint (market, code, status, detail, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(market, code) DO UPDATE SET
                        status=excluded.status,
                        detail=excluded.detail,
                        updated_at=excluded.updated_at
                    """,
                    rows,
                )
                return len(rows)

    def upsert_checkpoint_row(
        self,
        market: str,
        code: str,
        status: str,
        detail: str = "",
    ) -> None:
        """O(1) 单行断点 upsert（SyncEngine 每完成一只调用一次，D6 性能）。

        与 :meth:`save_checkpoint` 的区别：只更新一只标的，不整表重写 ——
        大池（17798 只）续传时避免 O(N²) 全量写放大。

        Args:
            market: 市场码。
            code: 证券代码。
            status: ``completed`` / ``quarantined`` / ``failed``。
            detail: 附加说明（默认空）。
        """
        if status not in (
            CHECKPOINT_STATUS_COMPLETED,
            CHECKPOINT_STATUS_QUARANTINED,
            CHECKPOINT_STATUS_FAILED,
        ):
            raise DataIntegrityError(
                f"[fail-loud/NF-26] sync_checkpoint 未知状态 {status!r}"
            )
        now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO sync_checkpoint (market, code, status, detail, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(market, code) DO UPDATE SET
                        status=excluded.status,
                        detail=excluded.detail,
                        updated_at=excluded.updated_at
                    """,
                    (str(market), str(code), status, str(detail), now),
                )

    def load_checkpoint(self, market: str) -> dict[str, set[str]]:
        """读取断点：``{completed, quarantined, failed}``（O(1) 单表查询）。

        Returns:
            三个集合；无数据时均为空集（合法态）。
        """
        # 读路径不加锁（R2）
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT code, status FROM sync_checkpoint WHERE market = ?
                """,
                (str(market),),
            ).fetchall()
        completed: set[str] = set()
        quarantined: set[str] = set()
        failed: set[str] = set()
        for row in rows:
            code = str(row["code"])
            status = str(row["status"])
            if status == CHECKPOINT_STATUS_COMPLETED:
                completed.add(code)
            elif status == CHECKPOINT_STATUS_QUARANTINED:
                quarantined.add(code)
            elif status == CHECKPOINT_STATUS_FAILED:
                failed.add(code)
            else:
                raise DataIntegrityError(
                    f"[fail-loud/NF-26] sync_checkpoint 未知状态 {status!r}（{market}:{code}）"
                )
        return {
            "completed": completed,
            "quarantined": quarantined,
            "failed": failed,
        }

    def checkpoint_count(self, market: str | None = None) -> int:
        """断点行数（状态展示用）。"""
        # 读路径不加锁（R2）
        with self._connect() as conn:
            if market is None:
                row = conn.execute("SELECT COUNT(*) AS n FROM sync_checkpoint").fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM sync_checkpoint WHERE market = ?",
                    (str(market),),
                ).fetchone()
        return int(row["n"])

    # ------------------------------------------------------------------ #
    # 统计 / 健康
    # ------------------------------------------------------------------ #

    def counts(self) -> dict[str, int]:
        """各表行数摘要（状态展示/验收用）。"""
        return {
            "securities": self.securities_count(),
            "daily_bars": self.daily_bar_count(),
            "sync_checkpoint": self.checkpoint_count(),
            "sync_meta": self._sync_meta_count(),
        }

    def _sync_meta_count(self) -> int:
        with self._lock:
            with self._connect() as conn:
                row = conn.execute("SELECT COUNT(*) AS n FROM sync_meta").fetchone()
        return int(row["n"])

    def summary(self) -> dict[str, Any]:
        """JSON 安全摘要（CLI ``data status`` 的 storage 字段）。"""
        return {
            "db_path": str(self._db_path),
            "backend": "sqlite",
            **self.counts(),
        }
