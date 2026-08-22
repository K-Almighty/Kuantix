"""盘前 / 盘后：数据持久化（News/Fundamental/LimitUpDown 三库）。

- 三库文件均位于 ``config.paths.db / analysis_<name>.db``；
- 所有写入走 :class:`NewsStore` / :class:`FundamentalStore` /
  :class:`LimitUpDownStore`，外部仅通过契约 DTO 交互（NF-3 / NF-9）；
- 所有列表接口下推 ``LIMIT/OFFSET``（P1-2 分页下推），配套独立 COUNT。
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from Kuantix.core import contracts as C
from Kuantix.core.db import apply_sqlite_pragmas, connect_sqlite
from Kuantix.core.fail_loud import DataIntegrityError, require_key

__all__ = ["NewsStore", "FundamentalStore", "LimitUpDownStore"]

# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

NEWS_DDL = """
CREATE TABLE IF NOT EXISTS news (
    market       TEXT    NOT NULL,
    date         TEXT    NOT NULL,       -- YYYY-MM-DD（publish_ts 所属交易日）
    id           TEXT    NOT NULL,
    source       TEXT    NOT NULL,
    category     TEXT    NOT NULL,       -- news / announcement / policy
    title        TEXT    NOT NULL,
    url          TEXT    NOT NULL DEFAULT '',
    publish_ts   TEXT    NOT NULL,       -- ISO datetime
    codes        TEXT    NOT NULL DEFAULT '[]',    -- JSON string list
    importance   INTEGER NOT NULL DEFAULT 0,
    keywords     TEXT    NOT NULL DEFAULT '[]',    -- JSON string list
    summary      TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (market, date, id)
);
CREATE INDEX IF NOT EXISTS idx_news_publish ON news (market, date, publish_ts DESC);
CREATE INDEX IF NOT EXISTS idx_news_category ON news (market, date, category);
"""

FUNDAMENTAL_DDL = """
CREATE TABLE IF NOT EXISTS fundamental (
    market            TEXT    NOT NULL,
    date              TEXT    NOT NULL,   -- 画像基准日
    code              TEXT    NOT NULL,
    name              TEXT    NOT NULL,
    sector            TEXT    NOT NULL,
    industry          TEXT    NOT NULL,
    market_cap        REAL    NOT NULL,
    pe                REAL,
    pb                REAL,
    roe               REAL,
    revenue_growth    REAL,
    net_profit_growth REAL,
    debt_ratio        REAL,
    dividend_yield    REAL,
    announcements     TEXT    NOT NULL DEFAULT '[]',
    grade             TEXT    NOT NULL,   -- A / B / C / D
    summary_lines     TEXT    NOT NULL DEFAULT '[]',
    PRIMARY KEY (market, date, code)
);
CREATE INDEX IF NOT EXISTS idx_fundamental_grade ON fundamental (market, date, grade);
"""

LIMIT_ENTRIES_DDL = """
CREATE TABLE IF NOT EXISTS limit_entries (
    market          TEXT    NOT NULL,
    date            TEXT    NOT NULL,
    code            TEXT    NOT NULL,
    name            TEXT    NOT NULL,
    sector          TEXT    NOT NULL,
    limit_type      TEXT    NOT NULL,   -- 业绩驱动 / 概念炒作 / 技术突破 / ...
    close           REAL    NOT NULL,
    change_pct      REAL    NOT NULL,
    volume_ratio    REAL,
    continuous_days INTEGER NOT NULL DEFAULT 1,
    reasons         TEXT    NOT NULL DEFAULT '[]',
    PRIMARY KEY (market, date, code)
);
CREATE INDEX IF NOT EXISTS idx_limit_type ON limit_entries (market, date, limit_type);
CREATE INDEX IF NOT EXISTS idx_limit_sector ON limit_entries (market, date, sector);

CREATE TABLE IF NOT EXISTS limit_summaries (
    market          TEXT    NOT NULL,
    date            TEXT    NOT NULL,
    up_count        INTEGER NOT NULL,
    down_count      INTEGER NOT NULL,
    flat_count      INTEGER NOT NULL,
    total_count     INTEGER NOT NULL,
    up_ratio        REAL    NOT NULL,
    down_ratio      REAL    NOT NULL,
    by_sector_json  TEXT    NOT NULL DEFAULT '[]',
    by_type_json    TEXT    NOT NULL DEFAULT '[]',
    generated_at    TEXT    NOT NULL,
    PRIMARY KEY (market, date)
);
"""


def _connect(db_path: Path) -> sqlite3.Connection:
    """打开分析类 SQLite 连接并应用 PRAGMA。"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect_sqlite(db_path, check_same_thread=False)
    return conn


def _safe_json_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if isinstance(value, str):
        if not value.strip():
            return []
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise DataIntegrityError(
                f"[fail-loud/NF-26] SQLite JSON 字段非法: {value!r} ({exc})"
            ) from exc
        return list(parsed) if isinstance(parsed, list) else []
    return list(value)


# ---------------------------------------------------------------------------
# NewsStore
# ---------------------------------------------------------------------------


class NewsStore:
    """消息面条目存储（按 market/date 分区，分页下推 SQLite）。"""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.conn: sqlite3.Connection = _connect(self.db_path)
        with self.conn:
            self.conn.executescript(NEWS_DDL)

    # -- 写 ---------------------------------------------------------------- #

    def upsert(self, market: str, date: dt.date, items: Iterable[C.NewsItem]) -> int:
        rows: list[tuple[Any, ...]] = []
        for item in items:
            if not isinstance(item, C.NewsItem):
                raise DataIntegrityError(
                    "[fail-loud/NF-26] NewsStore.upsert 仅接受 NewsItem 合约对象"
                )
            rows.append((
                str(market).strip().upper(),
                date.isoformat(),
                item.id,
                item.source,
                item.category.value,
                item.title,
                item.url,
                item.publish_ts.isoformat(timespec="seconds"),
                json.dumps(list(item.codes), ensure_ascii=False),
                int(item.importance),
                json.dumps(list(item.matched_keywords), ensure_ascii=False),
                item.summary,
            ))
        if not rows:
            return 0
        with self.conn:
            self.conn.executemany(
                "INSERT INTO news (market,date,id,source,category,title,url,"
                "publish_ts,codes,importance,keywords,summary) VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(market,date,id) DO UPDATE SET "
                "source=excluded.source,category=excluded.category,"
                "title=excluded.title,url=excluded.url,"
                "publish_ts=excluded.publish_ts,codes=excluded.codes,"
                "importance=excluded.importance,keywords=excluded.keywords,"
                "summary=excluded.summary",
                rows,
            )
        return len(rows)

    # -- 读（分页） -------------------------------------------------------- #

    def count(
        self,
        market: str,
        date: dt.date,
        *,
        category: str | None = None,
        keywords: Iterable[str] | None = None,
    ) -> int:
        sql = "SELECT COUNT(*) FROM news WHERE market=? AND date=?"
        params: list[Any] = [str(market).strip().upper(), date.isoformat()]
        if category:
            sql += " AND category=?"
            params.append(str(category).strip())
        if keywords:
            kw_list = [str(k).strip() for k in keywords if str(k).strip()]
            if kw_list:
                likes = " OR ".join(["instr(keywords, ?) > 0"] * len(kw_list))
                sql += f" AND ({likes})"
                for kw in kw_list:
                    params.append(json.dumps(kw, ensure_ascii=False))
        cur = self.conn.execute(sql, params)
        return int(cur.fetchone()[0])

    def list(
        self,
        market: str,
        date: dt.date,
        *,
        category: str | None = None,
        keywords: Iterable[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[C.NewsItem]:
        sql = (
            "SELECT market,date,id,source,category,title,url,publish_ts,"
            "codes,importance,keywords,summary FROM news "
            "WHERE market=? AND date=?"
        )
        params: list[Any] = [str(market).strip().upper(), date.isoformat()]
        if category:
            sql += " AND category=?"
            params.append(str(category).strip())
        if keywords:
            kw_list = [str(k).strip() for k in keywords if str(k).strip()]
            if kw_list:
                likes = " OR ".join(["instr(keywords, ?) > 0"] * len(kw_list))
                sql += f" AND ({likes})"
                for kw in kw_list:
                    params.append(json.dumps(kw, ensure_ascii=False))
        sql += " ORDER BY importance DESC, publish_ts DESC LIMIT ? OFFSET ?"
        params.extend([int(limit), int(offset)])
        cur = self.conn.execute(sql, params)
        result: list[C.NewsItem] = []
        for row in cur.fetchall():
            result.append(C.NewsItem(
                id=row["id"],
                source=row["source"],
                category=row["category"],
                title=row["title"],
                url=row["url"],
                publish_ts=dt.datetime.fromisoformat(row["publish_ts"]),
                codes=tuple(_safe_json_list(row["codes"])),
                importance=int(row["importance"]),
                matched_keywords=tuple(_safe_json_list(row["keywords"])),
                summary=row["summary"],
            ))
        return result


# ---------------------------------------------------------------------------
# FundamentalStore
# ---------------------------------------------------------------------------


class FundamentalStore:
    """公司基本面画像存储。"""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.conn: sqlite3.Connection = _connect(self.db_path)
        with self.conn:
            self.conn.executescript(FUNDAMENTAL_DDL)

    def upsert(self, market: str, date: dt.date, profiles: Iterable[C.FundamentalProfile]) -> int:
        rows: list[tuple[Any, ...]] = []
        for p in profiles:
            if not isinstance(p, C.FundamentalProfile):
                raise DataIntegrityError(
                    "[fail-loud/NF-26] FundamentalStore.upsert 仅接受 FundamentalProfile"
                )
            rows.append((
                str(market).strip().upper(),
                date.isoformat(),
                p.code,
                p.name,
                p.sector,
                p.industry,
                float(p.market_cap),
                None if p.pe is None else float(p.pe),
                None if p.pb is None else float(p.pb),
                None if p.roe is None else float(p.roe),
                None if p.revenue_growth is None else float(p.revenue_growth),
                None if p.net_profit_growth is None else float(p.net_profit_growth),
                None if p.debt_ratio is None else float(p.debt_ratio),
                None if p.dividend_yield is None else float(p.dividend_yield),
                json.dumps(list(p.latest_announcements), ensure_ascii=False),
                p.grade.value,
                json.dumps(list(p.summary_lines), ensure_ascii=False),
            ))
        if not rows:
            return 0
        with self.conn:
            self.conn.executemany(
                "INSERT INTO fundamental (market,date,code,name,sector,industry,market_cap,"
                "pe,pb,roe,revenue_growth,net_profit_growth,debt_ratio,dividend_yield,"
                "announcements,grade,summary_lines) VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(market,date,code) DO UPDATE SET "
                "name=excluded.name,sector=excluded.sector,industry=excluded.industry,"
                "market_cap=excluded.market_cap,pe=excluded.pe,pb=excluded.pb,"
                "roe=excluded.roe,revenue_growth=excluded.revenue_growth,"
                "net_profit_growth=excluded.net_profit_growth,"
                "debt_ratio=excluded.debt_ratio,dividend_yield=excluded.dividend_yield,"
                "announcements=excluded.announcements,grade=excluded.grade,"
                "summary_lines=excluded.summary_lines",
                rows,
            )
        return len(rows)

    def count(
        self,
        market: str,
        date: dt.date,
        *,
        codes: Iterable[str] | None = None,
        grade: str | None = None,
    ) -> int:
        sql = "SELECT COUNT(*) FROM fundamental WHERE market=? AND date=?"
        params: list[Any] = [str(market).strip().upper(), date.isoformat()]
        codes_list = [str(c).strip() for c in (codes or []) if str(c).strip()]
        if codes_list:
            placeholders = ",".join("?" * len(codes_list))
            sql += f" AND code IN ({placeholders})"
            params.extend(codes_list)
        if grade:
            sql += " AND grade=?"
            params.append(str(grade).strip())
        return int(self.conn.execute(sql, params).fetchone()[0])

    def list(
        self,
        market: str,
        date: dt.date,
        *,
        codes: Iterable[str] | None = None,
        grade: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[C.FundamentalProfile]:
        sql = (
            "SELECT * FROM fundamental WHERE market=? AND date=?"
        )
        params: list[Any] = [str(market).strip().upper(), date.isoformat()]
        codes_list = [str(c).strip() for c in (codes or []) if str(c).strip()]
        if codes_list:
            placeholders = ",".join("?" * len(codes_list))
            sql += f" AND code IN ({placeholders})"
            params.extend(codes_list)
        if grade:
            sql += " AND grade=?"
            params.append(str(grade).strip())
        sql += " ORDER BY grade ASC, market_cap DESC LIMIT ? OFFSET ?"
        params.extend([int(limit), int(offset)])
        result: list[C.FundamentalProfile] = []
        for row in self.conn.execute(sql, params).fetchall():
            result.append(C.FundamentalProfile(
                code=row["code"],
                name=row["name"],
                market=row["market"],
                sector=row["sector"],
                industry=row["industry"],
                market_cap=float(row["market_cap"]),
                pe=row["pe"],
                pb=row["pb"],
                roe=row["roe"],
                revenue_growth=row["revenue_growth"],
                net_profit_growth=row["net_profit_growth"],
                debt_ratio=row["debt_ratio"],
                dividend_yield=row["dividend_yield"],
                latest_announcements=tuple(_safe_json_list(row["announcements"])),
                grade=row["grade"],
                summary_lines=tuple(_safe_json_list(row["summary_lines"])),
            ))
        return result

    def get(self, market: str, date: dt.date, code: str) -> C.FundamentalProfile | None:
        codes = str(code).strip()
        rows = self.list(market, date, codes=[codes], limit=1, offset=0)
        return rows[0] if rows else None


# ---------------------------------------------------------------------------
# LimitUpDownStore
# ---------------------------------------------------------------------------


class LimitUpDownStore:
    """涨跌停条目 + 汇总存储。"""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.conn: sqlite3.Connection = _connect(self.db_path)
        with self.conn:
            self.conn.executescript(LIMIT_ENTRIES_DDL)

    def upsert(
        self,
        market: str,
        date: dt.date,
        entries: Iterable[C.LimitEntry],
        summary: C.LimitUpDownSummary | None = None,
        by_sector: Iterable[dict[str, Any]] | None = None,
        by_type: Iterable[dict[str, Any]] | None = None,
    ) -> int:
        rows: list[tuple[Any, ...]] = []
        for e in entries:
            if not isinstance(e, C.LimitEntry):
                raise DataIntegrityError(
                    "[fail-loud/NF-26] LimitUpDownStore.upsert 仅接受 LimitEntry"
                )
            rows.append((
                str(market).strip().upper(),
                date.isoformat(),
                e.code,
                e.name,
                e.sector,
                e.limit_type.value,
                float(e.close),
                float(e.change_pct),
                None if e.volume_ratio is None else float(e.volume_ratio),
                int(e.continuous_days),
                json.dumps(list(e.reasons), ensure_ascii=False),
            ))
        with self.conn:
            if rows:
                self.conn.executemany(
                    "INSERT INTO limit_entries (market,date,code,name,sector,limit_type,"
                    "close,change_pct,volume_ratio,continuous_days,reasons) VALUES "
                    "(?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(market,date,code) DO UPDATE SET "
                    "name=excluded.name,sector=excluded.sector,limit_type=excluded.limit_type,"
                    "close=excluded.close,change_pct=excluded.change_pct,"
                    "volume_ratio=excluded.volume_ratio,"
                    "continuous_days=excluded.continuous_days,reasons=excluded.reasons",
                    rows,
                )
            if summary is not None:
                if not isinstance(summary, C.LimitUpDownSummary):
                    raise DataIntegrityError(
                        "[fail-loud/NF-26] summary 必须是 LimitUpDownSummary"
                    )
                sector_list = list(by_sector) if by_sector is not None else list(summary.by_sector)
                type_list = list(by_type) if by_type is not None else list(summary.by_type)
                self.conn.execute(
                    "INSERT INTO limit_summaries (market,date,up_count,down_count,flat_count,"
                    "total_count,up_ratio,down_ratio,by_sector_json,by_type_json,generated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(market,date) DO UPDATE SET "
                    "up_count=excluded.up_count,down_count=excluded.down_count,"
                    "flat_count=excluded.flat_count,total_count=excluded.total_count,"
                    "up_ratio=excluded.up_ratio,down_ratio=excluded.down_ratio,"
                    "by_sector_json=excluded.by_sector_json,by_type_json=excluded.by_type_json,"
                    "generated_at=excluded.generated_at",
                    (
                        str(market).strip().upper(),
                        date.isoformat(),
                        int(summary.up_count),
                        int(summary.down_count),
                        int(summary.flat_count),
                        int(summary.total_count),
                        float(summary.up_ratio),
                        float(summary.down_ratio),
                        json.dumps(sector_list, ensure_ascii=False),
                        json.dumps(type_list, ensure_ascii=False),
                        summary.generated_at.isoformat(timespec="seconds"),
                    ),
                )
        return len(rows)

    def count(
        self,
        market: str,
        date: dt.date,
        *,
        limit_type: str | None = None,
        sector: str | None = None,
        only_up: bool | None = None,
    ) -> int:
        sql = "SELECT COUNT(*) FROM limit_entries WHERE market=? AND date=?"
        params: list[Any] = [str(market).strip().upper(), date.isoformat()]
        if limit_type:
            sql += " AND limit_type=?"
            params.append(str(limit_type).strip())
        if sector:
            sql += " AND sector=?"
            params.append(str(sector).strip())
        if only_up is True:
            sql += " AND change_pct >= 0"
        elif only_up is False:
            sql += " AND change_pct < 0"
        return int(self.conn.execute(sql, params).fetchone()[0])

    def list(
        self,
        market: str,
        date: dt.date,
        *,
        limit_type: str | None = None,
        sector: str | None = None,
        only_up: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[C.LimitEntry]:
        sql = "SELECT * FROM limit_entries WHERE market=? AND date=?"
        params: list[Any] = [str(market).strip().upper(), date.isoformat()]
        if limit_type:
            sql += " AND limit_type=?"
            params.append(str(limit_type).strip())
        if sector:
            sql += " AND sector=?"
            params.append(str(sector).strip())
        if only_up is True:
            sql += " AND change_pct >= 0"
        elif only_up is False:
            sql += " AND change_pct < 0"
        sql += " ORDER BY ABS(change_pct) DESC, change_pct DESC, continuous_days DESC LIMIT ? OFFSET ?"
        params.extend([int(limit), int(offset)])
        result: list[C.LimitEntry] = []
        for row in self.conn.execute(sql, params).fetchall():
            result.append(C.LimitEntry(
                code=row["code"],
                name=row["name"],
                sector=row["sector"],
                limit_type=row["limit_type"],
                close=float(row["close"]),
                change_pct=float(row["change_pct"]),
                volume_ratio=row["volume_ratio"],
                continuous_days=int(row["continuous_days"]),
                reasons=tuple(_safe_json_list(row["reasons"])),
            ))
        return result

    def get_summary(self, market: str, date: dt.date) -> C.LimitUpDownSummary | None:
        row = self.conn.execute(
            "SELECT * FROM limit_summaries WHERE market=? AND date=?",
            (str(market).strip().upper(), date.isoformat()),
        ).fetchone()
        if row is None:
            return None
        return C.LimitUpDownSummary(
            date=dt.date.fromisoformat(row["date"]),
            market=row["market"],
            up_count=int(row["up_count"]),
            down_count=int(row["down_count"]),
            flat_count=int(row["flat_count"]),
            total_count=int(row["total_count"]),
            up_ratio=float(row["up_ratio"]),
            down_ratio=float(row["down_ratio"]),
            by_sector=tuple(dict(x) for x in _safe_json_list(row["by_sector_json"])),
            by_type=tuple(dict(x) for x in _safe_json_list(row["by_type_json"])),
            generated_at=dt.datetime.fromisoformat(row["generated_at"]),
        )
