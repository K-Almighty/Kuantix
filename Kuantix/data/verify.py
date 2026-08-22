"""数据湖完整性校验（NF-27）。

职责
----
- **完整性**：每标的回读条数 / 日期连续性（严格递增）；
- **缺失交易日**：对照 :class:`~Kuantix.core.market.MarketProfile` 的交易日历，
  找出区间内缺失的日期；
- **隔离区报告**：把当前隔离区条目并入 :class:`VerifyReport`。

fail-loud 要点
--------------
- 读取失败的文件计入 ``corrupt``（不静默跳过）；
- 日期非严格递增计入 ``corrupt``；
- 交易日历未覆盖的年份**不**强行近似（NF-26：拒绝用"非周末即交易日"兜底），
  该文件跳过缺失日核对并给出说明。
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from typing import Any

from Kuantix.adapters.factor_bridge import L1Reader
from Kuantix.core.contracts import VerifyReport
from Kuantix.core.market import CalendarCoverageError, MarketProfile

logger = logging.getLogger(__name__)

__all__ = ["VerifyContext", "verify_vipdoc", "verify_market_store"]


class VerifyContext:
    """校验上下文：收集覆盖统计 / 缺失日 / 损坏文件。"""

    def __init__(self, market: str) -> None:
        self.market = market
        self.securities = 0
        self.files = 0
        self.total_bars = 0
        self.disk_bytes = 0
        self.first_date: dt.date | None = None
        self.last_date: dt.date | None = None
        self.missing_days: set[dt.date] = set()
        self.corrupt: list[str] = []

    def observe_range(self, first: dt.date, last: dt.date) -> None:
        """吸收一只标的的（首日, 末日），维护全局数据湖日期范围。"""
        if self.first_date is None or first < self.first_date:
            self.first_date = first
        if self.last_date is None or last > self.last_date:
            self.last_date = last

    @property
    def coverage(self) -> dict[str, Any]:
        """覆盖统计（含数据湖首/末日，供 D1/D5 的 coverage 使用）。"""
        return {
            "market": self.market,
            "securities": self.securities,
            "files": self.files,
            "total_bars": self.total_bars,
            "disk_bytes": self.disk_bytes,
            "first_date": self.first_date.isoformat() if self.first_date else None,
            "last_date": self.last_date.isoformat() if self.last_date else None,
        }


def verify_vipdoc(
    vipdoc_root: Path | str,
    market: str,
    profile: MarketProfile,
    quarantine: Any,
    *,
    exchanges: tuple[str, ...] = ("sh", "sz"),
) -> VerifyReport:
    """校验整个 vipdoc 数据湖。

    Args:
        vipdoc_root: vipdoc 根目录。
        market: 市场码。
        profile: 市场档案（交易日历/涨跌幅等，NF-5）。
        quarantine: :class:`QuarantineStore`。
        exchanges: 要扫描的交易所目录（A 股 ``sh``/``sz``）。

    Returns:
        :class:`VerifyReport`。
    """
    reader = L1Reader(Path(vipdoc_root))
    ctx = VerifyContext(market)
    generated_at = dt.datetime.now()

    for exchange in exchanges:
        lday_dir = Path(vipdoc_root) / exchange / "lday"
        if not lday_dir.is_dir():
            continue
        for path in sorted(lday_dir.glob("*.day")):
            ctx.files += 1
            ctx.disk_bytes += path.stat().st_size
            code = path.name.lower()[2:8]
            _verify_one_file(ctx, reader, profile, exchange, code, path)

    return VerifyReport(
        market=market,
        coverage=ctx.coverage,
        missing_days=sorted(ctx.missing_days),
        corrupt=sorted(ctx.corrupt),
        quarantined=quarantine.list(market),
        generated_at=generated_at,
    )


def _verify_one_file(
    ctx: VerifyContext,
    reader: L1Reader,
    profile: MarketProfile,
    exchange: str,
    code: str,
    path: Path,
) -> None:
    """校验单个 .day 文件。"""
    ctx.securities += 1
    try:
        bars = reader.read_daily_bars(exchange, code)
    except Exception as exc:  # noqa: BLE001 - 读取失败计入 corrupt，不静默
        ctx.corrupt.append(f"{path.name}: read-failed {type(exc).__name__}")
        return

    ctx.total_bars += len(bars)
    if not bars:
        ctx.corrupt.append(f"{path.name}: empty")
        return

    # bars 升序（vipdoc 写盘保证），首/末条即该标的日期范围
    ctx.observe_range(bars[0].date, bars[-1].date)

    dates = [bar.date for bar in bars]
    # 严格递增（重复日期 = 损坏）
    for prev, curr in zip(dates, dates[1:]):
        if curr <= prev:
            ctx.corrupt.append(f"{path.name}: non-increasing date {curr} after {prev}")
            return

    # 缺失交易日：只核对日历覆盖年份内的区间
    first, last = dates[0], dates[-1]
    try:
        expected = profile.trading_days_between(first, last)
    except CalendarCoverageError:
        logger.warning(
            "%s 区间 %s~%s 超出日历覆盖，跳过缺失日核对（NF-26 不近似）",
            path.name,
            first,
            last,
        )
        return
    present = set(dates)
    missing = [d for d in expected if d not in present]
    if missing:
        ctx.missing_days.update(missing)
        # 超过 20 天缺失的记为损坏（严重断档）
        if len(missing) > 20:
            ctx.corrupt.append(f"{path.name}: gap {len(missing)} days")


def verify_market_store(
    store: Any,
    market: str,
    profile: MarketProfile,
    quarantine: Any,
) -> VerifyReport:
    """校验 SQLite 行情主存储（设计文档 08：status/verify 读 SQLite）。

    与 :func:`verify_vipdoc` 同口径：逐标的读回条数 / 日期严格递增 /
    缺失交易日对照日历 / 隔离区并入报告。

    Args:
        store: :class:`~Kuantix.data.market_store.MarketStore`。
        market: 市场码。
        profile: 市场档案（交易日历，NF-5）。
        quarantine: :class:`QuarantineStore`。

    Returns:
        :class:`VerifyReport`。
    """
    ctx = VerifyContext(market)
    generated_at = dt.datetime.now()
    # 数据量大优化：单连接流式读取全部日线（按 code 分组），替代逐标的
    # ``read_daily_bars`` —— 避免 9000+ 标的时每次新开 sqlite 连接的开销。
    # 校验对象是 **daily_bars 的实际数据**（securities 表在未迁移完成时可能
    # 为空，与 :meth:`MarketStore.daily_bar_stats` 同口径）。
    try:
        grouped = store.read_all_daily_bars(market)
    except Exception as exc:  # noqa: BLE001 - 读取失败计入 corrupt，不静默
        ctx.corrupt.append(f"read-all: {type(exc).__name__}: {exc}")
        grouped = {}

    for code in sorted(grouped):
        bars = grouped[code]
        ctx.securities += 1
        ctx.total_bars += len(bars)
        ctx.files += 1
        if not bars:
            ctx.corrupt.append(f"{code}: empty")
            continue
        ctx.observe_range(bars[0].date, bars[-1].date)
        dates = [bar.date for bar in bars]
        for prev, curr in zip(dates, dates[1:]):
            if curr <= prev:
                ctx.corrupt.append(f"{code}: non-increasing date {curr} after {prev}")
                break
        else:
            first, last = dates[0], dates[-1]
            try:
                expected = profile.trading_days_between(first, last)
            except CalendarCoverageError:
                continue
            present = set(dates)
            missing = [d for d in expected if d not in present]
            if missing:
                ctx.missing_days.update(missing)
                if len(missing) > 20:
                    ctx.corrupt.append(f"{code}: gap {len(missing)} days")

    return VerifyReport(
        market=market,
        coverage=ctx.coverage,
        missing_days=sorted(ctx.missing_days),
        corrupt=sorted(ctx.corrupt),
        quarantined=quarantine.list(market),
        generated_at=generated_at,
    )
