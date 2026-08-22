"""监控层 SQLite 持久化（NF-15 / NF-26）。

规则 / 持仓 / 自选 / 告警历史统一落在 ``~/.Kuantix/db/monitor.db``
（路径来自 ``config.paths.db``，测试可注入临时路径）。

fail-loud 要点（NF-26）
----------------------
- 表结构在首次连接时自动建表（幂等），任何 DDL 失败直接抛，不静默；
- 读不到行返回 ``None`` / 空列表是**合法状态**（不是兜底），调用方显式处理；
- 全库禁止 ``dict.get(k, 默认值)``（R4-B）与 ``try/except: pass``（R4-A）。
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from Kuantix.core.contracts import Alert, Position
from Kuantix.core.db import apply_sqlite_pragmas, connect_sqlite
from Kuantix.core.fail_loud import MissingConfigError, require_key, require_known

__all__ = [
    "MonitorStore",
    "DEFAULT_DB_FILENAME",
    "WatchlistItem",
]

#: 数据库文件名（位于 ``config.paths.db`` 下）
DEFAULT_DB_FILENAME = "monitor.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rules (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    market TEXT NOT NULL,
    codes TEXT NOT NULL,            -- JSON 数组或 ["*"]
    criterion_type TEXT NOT NULL,
    params TEXT NOT NULL,           -- JSON 对象
    level TEXT NOT NULL,
    cooldown_seconds REAL NOT NULL,
    enabled INTEGER NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual',  -- manual 用户自建 / preset 预设注入
    preset_key TEXT,                -- 预设规则 key（仅 source=preset 时非空）
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_triggered_at TEXT
);
CREATE TABLE IF NOT EXISTS positions (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    market TEXT NOT NULL,
    shares REAL NOT NULL,
    cost_price REAL NOT NULL,
    opened_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS watchlist (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    market TEXT NOT NULL,
    source TEXT NOT NULL,
    added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
    id TEXT PRIMARY KEY,
    code TEXT NOT NULL,
    market TEXT NOT NULL,
    rule TEXT NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    ts TEXT NOT NULL,
    payload TEXT NOT NULL           -- JSON 对象
);
CREATE INDEX IF NOT EXISTS idx_alerts_market_level ON alerts(market, level);
CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts);
"""


class WatchlistItem:
    """一条自选股记录（契约 §3.5 WatchlistItem 的持久化形态）。"""

    __slots__ = ("code", "name", "market", "source", "added_at")

    def __init__(
        self,
        code: str,
        name: str = "",
        market: str = "CN",
        source: str = "manual",
        added_at: dt.datetime | None = None,
    ) -> None:
        self.code = str(code).strip()
        self.name = str(name)
        self.market = str(market).strip().upper()
        self.source = str(source)
        self.added_at = added_at if added_at is not None else dt.datetime.now().astimezone()

    def to_dict(self) -> dict[str, Any]:
        """转为 JSON 安全字典（契约 §3.5 WatchlistItem）。"""
        return {
            "code": self.code,
            "name": self.name,
            "market": self.market,
            "added_at": self.added_at.isoformat(timespec="seconds"),
            "source": self.source,
        }

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<WatchlistItem {self.market}:{self.code}>"


def _iso(value: dt.datetime | None) -> str | None:
    """datetime → ISO-8601 秒级字符串（含时区偏移）。"""
    if value is None:
        return None
    return value.isoformat(timespec="seconds")


def _parse_iso(value: str | None) -> dt.datetime | None:
    """ISO-8601 字符串 → datetime（带时区）。"""
    if value is None or not value:
        return None
    return dt.datetime.fromisoformat(value)


class MonitorStore:
    """监控层 SQLite 存储（线程安全，单连接 + RLock）。

    Args:
        db_path: 数据库文件路径；``None`` 时取 ``config.paths.db / monitor.db``。
    """

    def __init__(self, db_path: Path | str | None = None) -> None:
        if db_path is None:
            from Kuantix.config import get_config

            config = get_config()
            self._path = Path(config.paths.db) / DEFAULT_DB_FILENAME
        else:
            self._path = Path(db_path).expanduser()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # P1-1：connect_sqlite / apply_sqlite_pragmas 统一 WAL + busy_timeout
        # + synchronous=NORMAL（原代码已手动 PRAGMA，切换后参数统一管理）。
        self._conn = connect_sqlite(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        apply_sqlite_pragmas(self._conn)
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
            self._migrate_rules_columns()

    @property
    def path(self) -> Path:
        """数据库文件路径。"""
        return self._path

    def close(self) -> None:
        """关闭连接（幂等）。"""
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self._lock:
            cursor = self._conn.execute(sql, params)
            self._conn.commit()
            return cursor

    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            cursor = self._conn.execute(sql, params)
            return list(cursor.fetchall())

    def _migrate_rules_columns(self) -> None:
        """为已存在的 rules 表补齐新增列（source / preset_key），兼容旧库。"""
        with self._lock:
            cols = {
                r["name"] for r in self._conn.execute("PRAGMA table_info(rules)")
            }
        if "source" not in cols:
            self._execute(
                "ALTER TABLE rules ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'"
            )
        if "preset_key" not in cols:
            self._execute("ALTER TABLE rules ADD COLUMN preset_key TEXT")

    # ------------------------------------------------------------------ #
    # 规则 CRUD（Rule 对象由 rules.py 负责 <-> dict 互转，本层只存 JSON）
    # ------------------------------------------------------------------ #

    def find_rule_by_preset_key(self, preset_key: str) -> dict[str, Any] | None:
        """按预设 key 查询已注入的预设规则（不存在返回 ``None``，合法态）。"""
        rows = self._query(
            "SELECT * FROM rules WHERE preset_key = ? LIMIT 1", (preset_key,)
        )
        return self._rule_row_to_dict(rows[0]) if rows else None

    def add_rule(self, rule: dict[str, Any]) -> None:
        """新增规则。

        Args:
            rule: 规则字典（字段与契约 §3.5 Rule 一致，含 id）。

        Raises:
            MissingConfigError: 缺少必填键。
        """
        require_key(rule, "id", "monitor.store.add_rule")
        require_key(rule, "name", "monitor.store.add_rule")
        require_key(rule, "market", "monitor.store.add_rule")
        require_key(rule, "criterion_type", "monitor.store.add_rule")
        require_key(rule, "params", "monitor.store.add_rule")
        require_key(rule, "level", "monitor.store.add_rule")
        codes = require_key(rule, "codes", "monitor.store.add_rule")
        self._execute(
            """
            INSERT OR REPLACE INTO rules (
                id, name, market, codes, criterion_type, params, level,
                cooldown_seconds, enabled, source, preset_key,
                created_at, updated_at, last_triggered_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(rule["id"]),
                str(rule["name"]),
                str(rule["market"]).upper(),
                json.dumps(list(codes), ensure_ascii=False),
                str(rule["criterion_type"]),
                json.dumps(rule["params"], ensure_ascii=False),
                str(rule["level"]),
                float(rule["cooldown_seconds"]),
                1 if bool(rule["enabled"]) else 0,
                str(rule["source"]) if rule.get("source") else "manual",
                str(rule["preset_key"]) if rule.get("preset_key") else None,
                _iso(rule["created_at"]) or _iso(dt.datetime.now().astimezone()),
                _iso(rule["updated_at"]) or _iso(dt.datetime.now().astimezone()),
                _iso(rule.get("last_triggered_at")),
            ),
        )

    def update_rule(self, rule: dict[str, Any]) -> None:
        """整体更新规则（必须携带完整字段，R4-B 禁止 .get 兜底）。"""
        require_key(rule, "id", "monitor.store.update_rule")
        require_key(rule, "name", "monitor.store.update_rule")
        require_key(rule, "market", "monitor.store.update_rule")
        require_key(rule, "criterion_type", "monitor.store.update_rule")
        require_key(rule, "params", "monitor.store.update_rule")
        require_key(rule, "level", "monitor.store.update_rule")
        codes = require_key(rule, "codes", "monitor.store.update_rule")
        self._execute(
            """
            UPDATE rules SET
                name = ?, market = ?, codes = ?, criterion_type = ?, params = ?,
                level = ?, cooldown_seconds = ?, enabled = ?, updated_at = ?,
                last_triggered_at = ?
            WHERE id = ?
            """,
            (
                str(rule["name"]),
                str(rule["market"]).upper(),
                json.dumps(list(codes), ensure_ascii=False),
                str(rule["criterion_type"]),
                json.dumps(rule["params"], ensure_ascii=False),
                str(rule["level"]),
                float(rule["cooldown_seconds"]),
                1 if bool(rule["enabled"]) else 0,
                _iso(rule["updated_at"]) or _iso(dt.datetime.now().astimezone()),
                _iso(rule.get("last_triggered_at")),
                str(rule["id"]),
            ),
        )

    def delete_rule(self, rule_id: str) -> bool:
        """删除规则；返回是否确实删除了。"""
        cursor = self._execute("DELETE FROM rules WHERE id = ?", (str(rule_id),))
        return cursor.rowcount > 0

    def get_rule(self, rule_id: str) -> dict[str, Any] | None:
        """按 id 读取规则（字典形态，供 rules.py 还原为 Rule）。"""
        rows = self._query("SELECT * FROM rules WHERE id = ?", (str(rule_id),))
        if not rows:
            return None
        return self._rule_row_to_dict(rows[0])

    def list_rules(
        self,
        market: str | None = None,
        enabled_only: bool = False,
        *,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[dict[str, Any]]:
        """列出规则（P1-2：支持 LIMIT/OFFSET DB 级分页）。

        Args:
            market: 市场过滤；``None`` 表示全部。
            enabled_only: 只返回启用规则。
            limit: 最大返回条数；``None`` 不限。
            offset: 跳过条数；``None`` 不跳过。
        """
        clauses: list[str] = []
        params: list[Any] = []
        if market is not None:
            clauses.append("market = ?")
            params.append(str(market).upper())
        if enabled_only:
            clauses.append("enabled = 1")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM rules{where} ORDER BY created_at"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        if offset is not None:
            sql += " OFFSET ?"
            params.append(int(offset))
        rows = self._query(sql, tuple(params))
        return [self._rule_row_to_dict(row) for row in rows]

    def count_rules(self, market: str | None = None, enabled_only: bool = False) -> int:
        """P1-2：规则匹配条目总数。"""
        clauses: list[str] = []
        params: list[Any] = []
        if market is not None:
            clauses.append("market = ?")
            params.append(str(market).upper())
        if enabled_only:
            clauses.append("enabled = 1")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._query(f"SELECT COUNT(*) AS c FROM rules{where}", tuple(params))
        return int(rows[0]["c"]) if rows else 0

    def set_last_triggered(self, rule_id: str, ts: dt.datetime) -> None:
        """更新规则最近触发时刻（冷却去重的持久化依据）。"""
        self._execute(
            "UPDATE rules SET last_triggered_at = ? WHERE id = ?",
            (_iso(ts), str(rule_id)),
        )

    @staticmethod
    def _rule_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": str(row["id"]),
            "name": str(row["name"]),
            "market": str(row["market"]),
            "codes": json.loads(row["codes"]),
            "criterion_type": str(row["criterion_type"]),
            "params": json.loads(row["params"]),
            "level": str(row["level"]),
            "cooldown_seconds": float(row["cooldown_seconds"]),
            "enabled": bool(row["enabled"]),
            "source": str(row["source"]) if row["source"] else "manual",
            "preset_key": str(row["preset_key"]) if row["preset_key"] else None,
            "created_at": _parse_iso(row["created_at"]),
            "updated_at": _parse_iso(row["updated_at"]),
            "last_triggered_at": _parse_iso(row["last_triggered_at"]),
        }

    # ------------------------------------------------------------------ #
    # 持仓 CRUD
    # ------------------------------------------------------------------ #

    def add_position(self, position: Position, *, name: str = "") -> None:
        """新增持仓。"""
        self._execute(
            """
            INSERT OR REPLACE INTO positions (code, name, market, shares, cost_price, opened_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                position.code,
                str(name),
                str(position.market).upper(),
                float(position.shares),
                float(position.cost_price),
                position.opened_at.isoformat(),
                _iso(dt.datetime.now().astimezone()),
            ),
        )

    def update_position(
        self,
        code: str,
        *,
        shares: float | None = None,
        cost_price: float | None = None,
        opened_at: dt.date | None = None,
        name: str | None = None,
    ) -> bool:
        """更新持仓；只更新非 None 字段，返回是否命中。"""
        current = self.get_position(code)
        if current is None:
            return False
        new_shares = current["shares"] if shares is None else float(shares)
        new_cost = current["cost_price"] if cost_price is None else float(cost_price)
        new_opened = current["opened_at"] if opened_at is None else opened_at
        new_name = current["name"] if name is None else str(name)
        self._execute(
            """
            UPDATE positions SET shares = ?, cost_price = ?, opened_at = ?, name = ?, updated_at = ?
            WHERE code = ?
            """,
            (
                new_shares,
                new_cost,
                new_opened.isoformat(),
                new_name,
                _iso(dt.datetime.now().astimezone()),
                str(code),
            ),
        )
        return True

    def delete_position(self, code: str) -> bool:
        """删除持仓；返回是否确实删除了。"""
        cursor = self._execute("DELETE FROM positions WHERE code = ?", (str(code),))
        return cursor.rowcount > 0

    def get_position(self, code: str) -> dict[str, Any] | None:
        """按代码读取持仓记录。"""
        rows = self._query("SELECT * FROM positions WHERE code = ?", (str(code),))
        if not rows:
            return None
        return self._position_row_to_dict(rows[0])

    def list_positions(
        self,
        market: str | None = None,
        *,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[dict[str, Any]]:
        """列出全部持仓记录（P1-2：支持 DB 级 LIMIT/OFFSET）。"""
        params: list[Any] = []
        if market is not None:
            sql = "SELECT * FROM positions WHERE market = ? ORDER BY code"
            params.append(str(market).upper())
        else:
            sql = "SELECT * FROM positions ORDER BY code"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        if offset is not None:
            sql += " OFFSET ?"
            params.append(int(offset))
        rows = self._query(sql, tuple(params))
        return [self._position_row_to_dict(row) for row in rows]

    def count_positions(self, market: str | None = None) -> int:
        """P1-2：持仓匹配条目总数。"""
        if market is not None:
            rows = self._query(
                "SELECT COUNT(*) AS c FROM positions WHERE market = ?",
                (str(market).upper(),),
            )
        else:
            rows = self._query("SELECT COUNT(*) AS c FROM positions")
        return int(rows[0]["c"]) if rows else 0

    @staticmethod
    def _position_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "code": str(row["code"]),
            "name": str(row["name"]),
            "market": str(row["market"]),
            "shares": float(row["shares"]),
            "cost_price": float(row["cost_price"]),
            "opened_at": dt.date.fromisoformat(row["opened_at"]),
        }

    # ------------------------------------------------------------------ #
    # 自选 CRUD
    # ------------------------------------------------------------------ #

    def add_watch(self, item: WatchlistItem) -> None:
        """新增/覆盖自选。"""
        self._execute(
            """
            INSERT OR REPLACE INTO watchlist (code, name, market, source, added_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                item.code,
                item.name,
                str(item.market).upper(),
                item.source,
                _iso(item.added_at),
            ),
        )

    def delete_watch(self, code: str) -> bool:
        """删除自选；返回是否确实删除了。"""
        cursor = self._execute("DELETE FROM watchlist WHERE code = ?", (str(code),))
        return cursor.rowcount > 0

    def get_watch(self, code: str) -> WatchlistItem | None:
        """按代码读取自选。"""
        rows = self._query("SELECT * FROM watchlist WHERE code = ?", (str(code),))
        if not rows:
            return None
        row = rows[0]
        return WatchlistItem(
            code=str(row["code"]),
            name=str(row["name"]),
            market=str(row["market"]),
            source=str(row["source"]),
            added_at=_parse_iso(row["added_at"]) or dt.datetime.now().astimezone(),
        )

    def list_watch(
        self,
        market: str | None = None,
        *,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[WatchlistItem]:
        """列出自选（P1-2：支持 DB 级 LIMIT/OFFSET）。"""
        params: list[Any] = []
        if market is not None:
            sql = "SELECT * FROM watchlist WHERE market = ? ORDER BY code"
            params.append(str(market).upper())
        else:
            sql = "SELECT * FROM watchlist ORDER BY code"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        if offset is not None:
            sql += " OFFSET ?"
            params.append(int(offset))
        rows = self._query(sql, tuple(params))
        return [
            WatchlistItem(
                code=str(row["code"]),
                name=str(row["name"]),
                market=str(row["market"]),
                source=str(row["source"]),
                added_at=_parse_iso(row["added_at"]) or dt.datetime.now().astimezone(),
            )
            for row in rows
        ]

    def count_watch(self, market: str | None = None) -> int:
        """P1-2：自选匹配条目总数。"""
        if market is not None:
            rows = self._query(
                "SELECT COUNT(*) AS c FROM watchlist WHERE market = ?",
                (str(market).upper(),),
            )
        else:
            rows = self._query("SELECT COUNT(*) AS c FROM watchlist")
        return int(rows[0]["c"]) if rows else 0

    def watch_codes(self, market: str | None = None) -> list[str]:
        """返回自选代码列表（升序）。"""
        items = self.list_watch(market)
        return [item.code for item in items]

    # ------------------------------------------------------------------ #
    # 告警历史
    # ------------------------------------------------------------------ #

    def add_alert(self, alert: Alert) -> None:
        """持久化一条告警（M15 历史查询的数据源）。"""
        self._execute(
            """
            INSERT OR REPLACE INTO alerts (id, code, market, rule, level, message, ts, payload)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                alert.id,
                alert.code,
                str(alert.market).upper(),
                alert.rule,
                alert.level.value,
                alert.message,
                _iso(alert.ts),
                json.dumps(alert.payload, ensure_ascii=False),
            ),
        )

    def list_alerts(
        self,
        *,
        market: str | None = None,
        level: str | None = None,
        limit: int = 100,
        offset: int | None = None,
    ) -> list[Alert]:
        """列出告警（按时间倒序）。P1-2：新增 ``offset`` 参数，配合 limit 做 DB 分页。

        Args:
            market: 市场过滤。
            level: 级别过滤（``info``/``warning``/``critical``）。
            limit: 最大条数（默认 100）。
            offset: 跳过条数（SQLite OFFSET）。
        """
        clauses: list[str] = []
        params: list[Any] = []
        if market is not None:
            clauses.append("market = ?")
            params.append(str(market).upper())
        if level is not None:
            clauses.append("level = ?")
            params.append(str(level).strip().lower())
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(int(limit))
        sql = f"SELECT * FROM alerts{where} ORDER BY ts DESC LIMIT ?"
        if offset is not None:
            sql += " OFFSET ?"
            params.append(int(offset))
        rows = self._query(sql, tuple(params))
        return [self._alert_row_to_alert(row) for row in rows]

    def count_alerts(
        self,
        *,
        market: str | None = None,
        level: str | None = None,
    ) -> int:
        """P1-2：告警匹配条目总数。"""
        clauses: list[str] = []
        params: list[Any] = []
        if market is not None:
            clauses.append("market = ?")
            params.append(str(market).upper())
        if level is not None:
            clauses.append("level = ?")
            params.append(str(level).strip().lower())
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._query(f"SELECT COUNT(*) AS c FROM alerts{where}", tuple(params))
        return int(rows[0]["c"]) if rows else 0

    @staticmethod
    def _alert_row_to_alert(row: sqlite3.Row) -> Alert:
        from Kuantix.core.contracts import AlertLevel

        level_value = require_known(
            str(row["level"]),
            "monitor.store.alert.level",
            allowed={lv.value for lv in AlertLevel},
        )
        return Alert(
            id=str(row["id"]),
            code=str(row["code"]),
            market=str(row["market"]),
            rule=str(row["rule"]),
            level=AlertLevel(level_value),
            message=str(row["message"]),
            ts=_parse_iso(row["ts"]) or dt.datetime.now().astimezone(),
            payload=json.loads(row["payload"]) if row["payload"] else {},
        )
