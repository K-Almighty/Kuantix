"""StockDetailService 单元测试。

重点覆盖：
- ``_aggregate_minute`` 15/60 分钟桶聚合（回归：旧实现按小时整除分桶，
  导致 9:30~14:59 全部聚成一根K线）；
- ``_resample_daily`` 周/月/年重采样（回归：旧实现 resample("W") 按
  W-SUN 对齐，周五交易的周K横轴显示成周日）；
- 日K读取走 SQL 反向取尾（tail），上市日期走 O(1) MIN(date) 查询。
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from Kuantix.analysis.stock_detail import (
    StockDetailService,
    _resample_daily,
)
from Kuantix.core.contracts import Bar
from Kuantix.data.market_store import MinuteBar


# --------------------------------------------------------------------- #
# 分钟桶聚合
# --------------------------------------------------------------------- #
def _mb(time: int, vol: float = 1.0, date: int = 20260821) -> MinuteBar:
    return MinuteBar(
        market="CN",
        code="600000",
        date=date,
        time=time,
        open=1.0,
        high=2.0,
        low=0.5,
        close=1.5,
        vol=vol,
        amount=10.0,
    )


class TestAggregateMinute:
    def test_15min_buckets_align_to_quarter_hour(self):
        """9:31/9:32/9:40 → 9:30 桶；9:45 → 9:45 桶；不与小时取整混桶。"""
        rows = [_mb(931), _mb(932), _mb(940), _mb(945), _mb(1301), _mb(1315)]
        out = StockDetailService._aggregate_minute(rows, "15min")
        assert [b.time for b in out] == [930, 945, 1300, 1315]
        # 桶内求和：930 桶含 931/932/940 三根
        assert out[0].vol == pytest.approx(3.0)
        assert out[1].vol == pytest.approx(1.0)
        assert out[2].vol == pytest.approx(1.0)
        assert out[3].vol == pytest.approx(1.0)

    def test_15min_full_trading_day(self):
        """完整交易日（上午 9:30-11:29 + 下午 13:00-14:59 共 240 分钟）
        → 16 个 15 分钟桶，回归旧实现按小时取整只出 2 根。"""
        times = (
            [9 * 100 + m for m in range(30, 60)]
            + [10 * 100 + m for m in range(60)]
            + [11 * 100 + m for m in range(30)]
            + [13 * 100 + m for m in range(60)]
            + [14 * 100 + m for m in range(60)]
        )
        rows = [_mb(t) for t in times]
        out = StockDetailService._aggregate_minute(rows, "15min")
        assert len(out) == 16
        assert out[0].time == 930
        assert out[7].time == 1115
        assert out[8].time == 1300
        assert out[-1].time == 1445
        assert out[0].vol == pytest.approx(15.0)

    def test_60min_buckets_split_morning_afternoon(self):
        """60 分钟桶：上午 9:30/10:30 两桶 + 下午 13:00/14:00 两桶。"""
        rows = [_mb(t) for t in (931, 950, 1000, 1050, 1301, 1320, 1400, 1450)]
        out = StockDetailService._aggregate_minute(rows, "60min")
        assert [b.time for b in out] == [900, 1000, 1300, 1400]
        assert out[0].vol == pytest.approx(2.0)
        assert out[1].vol == pytest.approx(2.0)
        assert out[2].vol == pytest.approx(2.0)
        assert out[3].vol == pytest.approx(2.0)

    def test_unknown_bucket_returns_rows_unchanged(self):
        rows = [_mb(931), _mb(945)]
        out = StockDetailService._aggregate_minute(rows, "5min")
        assert out is rows

    def test_empty_rows(self):
        assert StockDetailService._aggregate_minute([], "15min") == []


# --------------------------------------------------------------------- #
# 日线重采样（周/月/年）
# --------------------------------------------------------------------- #
def _daily_frame(dates: list[str]) -> pd.DataFrame:
    n = len(dates)
    return pd.DataFrame(
        {
            "datetime": pd.to_datetime(dates),
            "open": [1.0] * n,
            "high": [2.0] * n,
            "low": [0.5] * n,
            "close": [1.5] * n,
            "vol": [100.0] * n,
            "amount": [1000.0] * n,
        }
    )


class TestResampleDaily:
    def test_week_label_is_last_trading_day_not_sunday(self):
        """周一~周五同一周 → 单根周K，标签为周五（非周日）。"""
        frame = _daily_frame(
            ["2026-08-17", "2026-08-18", "2026-08-19", "2026-08-20", "2026-08-21"]
        )
        rs = _resample_daily(frame, "W-FRI", 10)
        assert len(rs) == 1
        assert rs.iloc[0]["datetime"] == pd.Timestamp("2026-08-21")

    def test_week_label_uses_last_actual_trading_day(self):
        """周五停牌（无数据）→ 该周标签为周四（桶内最后交易日）。"""
        frame = _daily_frame(
            ["2026-08-10", "2026-08-11", "2026-08-17", "2026-08-18", "2026-08-19", "2026-08-20"]
        )
        rs = _resample_daily(frame, "W-FRI", 10)
        assert len(rs) == 2
        assert rs.iloc[0]["datetime"] == pd.Timestamp("2026-08-11")
        assert rs.iloc[1]["datetime"] == pd.Timestamp("2026-08-20")

    def test_week_aggregation_values(self):
        frame = _daily_frame(
            ["2026-08-17", "2026-08-18", "2026-08-19", "2026-08-20", "2026-08-21"]
        )
        rs = _resample_daily(frame, "W-FRI", 10)
        assert rs.iloc[0]["vol"] == pytest.approx(500.0)
        assert rs.iloc[0]["amount"] == pytest.approx(5000.0)

    def test_month_and_year(self):
        """月/年桶标签取桶内最后交易日；乱序输入按时间升序输出。"""
        frame = _daily_frame(
            [
                "2026-01-05", "2026-01-20", "2026-02-02", "2026-02-27",
                "2025-03-10", "2025-06-15",
            ]
        )
        rs_m = _resample_daily(frame, "ME", 10)
        assert len(rs_m) == 4
        assert list(rs_m["datetime"]) == [
            pd.Timestamp("2025-03-10"),
            pd.Timestamp("2025-06-15"),
            pd.Timestamp("2026-01-20"),
            pd.Timestamp("2026-02-27"),
        ]
        rs_y = _resample_daily(frame, "YE", 10)
        assert len(rs_y) == 2
        assert list(rs_y["datetime"]) == [
            pd.Timestamp("2025-06-15"),
            pd.Timestamp("2026-02-27"),
        ]

    def test_limit_truncates_tail(self):
        dates = [f"2026-01-{d:02d}" for d in range(1, 29)]
        frame = _daily_frame(dates)
        rs = _resample_daily(frame, "ME", 10)
        assert len(rs) <= 10

    def test_unsupported_rule_raises(self):
        frame = _daily_frame(["2026-08-17"])
        with pytest.raises(ValueError):
            _resample_daily(frame, "W", 10)


# --------------------------------------------------------------------- #
# get_detail 集成（FakeStore）
# --------------------------------------------------------------------- #
class FakeStore:
    """最小市场存储桩：记录 read_daily_bars 的 tail 调用形态。"""

    def __init__(self, bars: list[Bar]) -> None:
        self.bars = bars
        self.read_calls: list[tuple[int | None]] = []
        self.first_date_calls = 0

    def read_daily_bars(self, market: str, code: str, *, tail: int | None = None):
        self.read_calls.append((tail,))
        if tail is not None:
            return self.bars[-tail:]
        return self.bars

    def security_name(self, code: str, market: str = "CN") -> str:
        return "测试股份"

    def first_daily_date(self, market: str, code: str) -> int | None:
        self.first_date_calls += 1
        return int(self.bars[0].date.strftime("%Y%m%d")) if self.bars else None


def _make_bars(n: int) -> list[Bar]:
    start = dt.date(2020, 1, 1)
    return [
        Bar(
            date=start + dt.timedelta(days=i),
            open=10.0 + i * 0.01,
            high=11.0 + i * 0.01,
            low=9.0 + i * 0.01,
            close=10.5 + i * 0.01,
            vol=1000.0 + i,
            amount=10000.0 + i * 10,
        )
        for i in range(n)
    ]


class TestGetDetailDaily:
    def test_day_period_reads_tail_not_full_history(self):
        """日K周期：SQL 反向取尾（limit+80 预热），不读全历史。"""
        store = FakeStore(_make_bars(3000))
        svc = StockDetailService(store=store, config=None)
        payload = svc.get_detail("600000", period="day", limit=600)
        assert store.read_calls == [(680,)]
        assert payload["available"] is True
        assert payload["data_source"] == "lake"
        assert len(payload["bars"]) == 600

    def test_listing_date_uses_lightweight_query(self):
        """上市日期走 first_daily_date（O(1)），不再全量读库。"""
        store = FakeStore(_make_bars(500))
        svc = StockDetailService(store=store, config=None)
        payload = svc.get_detail("600000", period="day", limit=100)
        assert store.first_date_calls >= 1
        assert payload["listing_date"] == "2020-01-01"

    def test_week_period_reads_full_history(self):
        """周K需要全历史重采样：read_daily_bars 不传 tail。"""
        store = FakeStore(_make_bars(300))
        svc = StockDetailService(store=store, config=None)
        payload = svc.get_detail("600000", period="week", limit=50)
        assert store.read_calls[0] == (None,)
        assert len(payload["bars"]) > 0

    def test_minute_period_without_data_marks_unavailable(self):
        """本地无分钟数据：available=False + 提示，不编造数据。"""
        store = FakeStore(_make_bars(10))
        store.read_minute_bars = lambda *a, **k: []  # type: ignore[method-assign]
        svc = StockDetailService(store=store, config=None)
        payload = svc.get_detail("600000", period="min15", limit=100)
        assert payload["available"] is False
        assert payload["bars"] == []
        assert "分钟" in payload["message"]
