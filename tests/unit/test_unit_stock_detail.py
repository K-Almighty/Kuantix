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
        """本地无分钟数据且 tdx 回退不可用：available=False + 提示，不编造数据。"""
        store = FakeStore(_make_bars(10))
        store.read_minute_bars = lambda *a, **k: []  # type: ignore[method-assign]
        svc = StockDetailService(store=store, config=None)
        payload = svc.get_detail("600000", period="min15", limit=100)
        assert payload["available"] is False
        assert payload["bars"] == []
        assert "分钟" in payload["message"]


# --------------------------------------------------------------------- #
# 分钟周期 tdx 实时回退（本地 db/minute 为空时的数据来源）
# --------------------------------------------------------------------- #
def _tdx_minute_rows(n: int, *, period_minutes: int) -> list[MinuteBar]:
    """构造 tdx 分钟线替身：每天 240/16/4 根（按周期档），连续自然日。"""
    per_day = 240 // period_minutes
    days = (n + per_day - 1) // per_day
    base = dt.date(2026, 6, 1)
    rows: list[MinuteBar] = []
    for d in range(days):
        date = int((base + dt.timedelta(days=d)).strftime("%Y%m%d"))
        for k in range(per_day):
            minute_of_day = 9 * 60 + 30 + k * period_minutes
            hh, mm = divmod(minute_of_day, 60)
            rows.append(
                MinuteBar(
                    market="CN",
                    code="600000",
                    date=date,
                    time=hh * 100 + mm,
                    open=10.0,
                    high=11.0,
                    low=9.0,
                    close=10.5,
                    vol=100.0,
                    amount=1000.0,
                )
            )
    return rows


class FakeTdxMinuteFetcher:
    """tdx 分钟拉取替身：记录调用参数，返回预置分钟线。"""

    def __init__(self, rows: list[MinuteBar]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, int, int]] = []

    def fetch_minute_kline(self, market, code, *, period_minutes, count):
        self.calls.append((market, period_minutes, count))
        return self.rows


class TestMinuteTdxFallback:
    def _svc(self, fetcher: FakeTdxMinuteFetcher) -> StockDetailService:
        store = FakeStore(_make_bars(10))
        store.read_minute_bars = lambda *a, **k: []  # type: ignore[method-assign]
        svc = StockDetailService(store=store, config=None)
        svc._tdx_fetcher = fetcher  # type: ignore[assignment]  # 注入替身跳过真实网络
        return svc

    def test_min15_falls_back_to_tdx(self):
        """本地无 15 分钟数据 → tdx 拉 15 分钟档（960 根），标注 tdx_realtime。"""
        fetcher = FakeTdxMinuteFetcher(_tdx_minute_rows(960, period_minutes=15))
        payload = self._svc(fetcher).get_detail("600000", period="min15", limit=500)
        assert fetcher.calls == [("CN", 15, 960)]
        assert payload["available"] is True
        assert payload["data_source"] == "tdx_realtime"
        assert len(payload["bars"]) == 500  # limit 截尾
        # 分钟线 datetime 带时间部分（YYYY-MM-DD HH:MM）
        assert " " in payload["bars"][0]["datetime"]
        assert payload["bars"][0]["datetime"].endswith("12:30")  # 960-500=460 → 第29天第12根

    def test_min60_falls_back_to_tdx(self):
        """本地无 60 分钟数据 → tdx 拉 60 分钟档（240 根）。"""
        fetcher = FakeTdxMinuteFetcher(_tdx_minute_rows(240, period_minutes=60))
        payload = self._svc(fetcher).get_detail("600000", period="min60", limit=500)
        assert fetcher.calls == [("CN", 60, 240)]
        assert payload["available"] is True
        assert payload["data_source"] == "tdx_realtime"
        assert len(payload["bars"]) == 240  # 不足 limit 不补

    def test_min5_falls_back_to_tdx_one_minute(self):
        """分时（min5）→ tdx 拉 1 分钟档（1200 根 = 5 个交易日）。"""
        fetcher = FakeTdxMinuteFetcher(_tdx_minute_rows(1200, period_minutes=1))
        payload = self._svc(fetcher).get_detail("600000", period="min5", limit=1500)
        assert fetcher.calls == [("CN", 1, 1200)]
        assert payload["available"] is True
        assert len(payload["bars"]) == 1200

    def test_tdx_empty_return_marks_unavailable(self):
        """tdx 也无返回（如无效代码）→ available=False，不编造。"""
        fetcher = FakeTdxMinuteFetcher([])
        payload = self._svc(fetcher).get_detail("600000", period="min15", limit=100)
        assert payload["available"] is False
        assert payload["bars"] == []
        assert "tdx 实时亦无返回" in payload["message"]

    def test_tdx_failure_marks_unavailable_with_reason(self):
        """tdx 拉取异常 → available=False，message 含失败原因。"""
        class _Boom:
            def fetch_minute_kline(self, *a, **k):
                raise RuntimeError("connection refused")

        store = FakeStore(_make_bars(10))
        store.read_minute_bars = lambda *a, **k: []  # type: ignore[method-assign]
        svc = StockDetailService(store=store, config=None)
        svc._tdx_fetcher = _Boom()  # type: ignore[assignment]
        payload = svc.get_detail("600000", period="min15", limit=100)
        assert payload["available"] is False
        assert "connection refused" in payload["message"]

    def test_local_minute_data_wins_over_tdx(self):
        """本地有分钟数据时优先本地（lake），不触发 tdx 回退。"""
        fetcher = FakeTdxMinuteFetcher(_tdx_minute_rows(960, period_minutes=15))
        store = FakeStore(_make_bars(10))
        local = _tdx_minute_rows(320, period_minutes=15)
        store.read_minute_bars = lambda *a, **k: local  # type: ignore[method-assign]
        svc = StockDetailService(store=store, config=None)
        svc._tdx_fetcher = fetcher  # type: ignore[assignment]
        payload = svc.get_detail("600000", period="min15", limit=500)
        assert fetcher.calls == []
        assert payload["data_source"] == "lake"


# --------------------------------------------------------------------- #
# 日线级别 tdx 回退（本地 lake 未全量同步、历史残缺时的数据来源）
# --------------------------------------------------------------------- #
class FakeTdxDailyFetcher:
    """tdx 日线拉取替身：记录调用参数，返回预置日线。"""

    def __init__(self, bars: list[Bar]) -> None:
        self.bars = bars
        self.calls: list[tuple[str, int | None]] = []

    def fetch_kline(self, market, code, years=10, *, count=None, **kw):
        self.calls.append((market, count))
        return self.bars


class TestDailyTdxFallback:
    def _svc(self, fetcher: FakeTdxDailyFetcher, local_n: int = 2) -> StockDetailService:
        # 本地仅 local_n 根日线（market.db 未同步的残缺状态）
        store = FakeStore(_make_bars(local_n))
        svc = StockDetailService(store=store, config=None)
        svc._tdx_fetcher = fetcher  # type: ignore[assignment]  # 注入替身跳过真实网络
        return svc

    def test_year_falls_back_when_lake_insufficient(self):
        """本地仅 2 根日线 → 年K回退 tdx 深历史（5000 根档），重采样出多年。"""
        fetcher = FakeTdxDailyFetcher(_make_bars(900))
        payload = self._svc(fetcher).get_detail("600000", period="year", limit=50)
        assert fetcher.calls == [("CN", 5000)]
        assert payload["available"] is True
        assert payload["data_source"] == "tdx_realtime"
        # 900 根日线跨 2020-2022 三个自然年 → 3 根年K（回归：旧逻辑只出 1 根）
        assert len(payload["bars"]) == 3
        # 上市日期取 tdx 数据首根，而非本地残缺元信息
        assert payload["listing_date"] == "2020-01-01"

    def test_day_falls_back_when_lake_insufficient(self):
        """本地 2 根 < limit+80 预热需求 → day 周期同样回退 tdx。"""
        fetcher = FakeTdxDailyFetcher(_make_bars(900))
        payload = self._svc(fetcher).get_detail("600000", period="day", limit=600)
        assert payload["data_source"] == "tdx_realtime"
        assert len(payload["bars"]) == 600

    def test_tdx_not_better_keeps_lake(self):
        """tdx 返回不比本地多（如新股）→ 沿用本地数据，来源仍 lake。"""
        fetcher = FakeTdxDailyFetcher(_make_bars(1))
        payload = self._svc(fetcher).get_detail("600000", period="year", limit=50)
        assert payload["data_source"] == "lake"
        assert len(payload["bars"]) == 1

    def test_sufficient_lake_skips_tdx(self):
        """本地数据充足（≥120 根）→ 不触发 tdx 回退。"""
        fetcher = FakeTdxDailyFetcher(_make_bars(900))
        payload = self._svc(fetcher, local_n=300).get_detail(
            "600000", period="year", limit=50
        )
        assert fetcher.calls == []
        assert payload["data_source"] == "lake"

    def test_tdx_failure_with_partial_lake_keeps_lake(self):
        """本地有残缺数据 + tdx 失败 → 容忍回退失败沿用本地（少好于无）。"""
        class _Boom:
            def fetch_kline(self, *a, **k):
                raise RuntimeError("connection refused")

        svc = StockDetailService(store=FakeStore(_make_bars(2)), config=None)
        svc._tdx_fetcher = _Boom()  # type: ignore[assignment]
        payload = svc.get_detail("600000", period="year", limit=50)
        assert payload["data_source"] == "lake"
        assert len(payload["bars"]) == 1


# --------------------------------------------------------------------- #
# quote：最新交易日行情快照（与周期无关，回归「年K显示单日 -27%」）
# --------------------------------------------------------------------- #
class TestDailyQuote:
    def test_year_quote_is_daily_not_yearly(self):
        """回归：年K顶部行情必须取日线最后两根，而非年K最后两根。

        旧实现前端用 bars[-1]/bars[-2] 展示，年K下「昨收」= 上年收盘、
        涨跌 = 跨年对比（如 -27%），形似数据错误；quote 固定日口径。
        """
        store = FakeStore(_make_bars(900))  # 2020-01-01 起连续 900 天
        svc = StockDetailService(store=store, config=None)
        payload = svc.get_detail("600000", period="year", limit=50)
        q = payload["quote"]
        assert q is not None
        # 日线最后两根（日口径）：close=10.5+899*0.01 / prev=10.5+898*0.01
        assert q["close"] == pytest.approx(10.5 + 899 * 0.01)
        assert q["prev_close"] == pytest.approx(10.5 + 898 * 0.01)
        assert q["change"] == pytest.approx(0.01)
        assert q["date"] == (dt.date(2020, 1, 1) + dt.timedelta(days=899)).isoformat()
        # 关键区分：年K倒数第二根是「去年收盘」，与 quote.prev_close 不同
        yearly_prev = payload["bars"][-2]["close"]
        assert q["prev_close"] != pytest.approx(yearly_prev)

    def test_day_period_quote_matches_last_bars(self):
        """日K周期：quote 与 bars 末两根同口径（一致性）。"""
        store = FakeStore(_make_bars(300))
        svc = StockDetailService(store=store, config=None)
        payload = svc.get_detail("600000", period="day", limit=100)
        q = payload["quote"]
        assert q is not None
        assert q["close"] == payload["bars"][-1]["close"]
        assert q["prev_close"] == payload["bars"][-2]["close"]

    def test_minute_period_quote_comes_from_daily(self):
        """分钟周期：quote 仍取日线口径（本地分钟+日线均有数据）。"""
        store = FakeStore(_make_bars(300))
        rows = _tdx_minute_rows(240, period_minutes=1)
        store.read_minute_bars = lambda *a, **k: rows  # type: ignore[method-assign]
        svc = StockDetailService(store=store, config=None)
        payload = svc.get_detail("600000", period="min5", limit=100)
        q = payload["quote"]
        assert q is not None
        assert q["close"] == pytest.approx(10.5 + 299 * 0.01)

    def test_unavailable_minute_still_has_daily_quote(self):
        """分钟无数据（unavailable）时 quote 仍可用（本地日线存在）。"""
        store = FakeStore(_make_bars(300))
        store.read_minute_bars = lambda *a, **k: []  # type: ignore[method-assign]
        svc = StockDetailService(store=store, config=None)  # config=None → tdx 回退不可用
        payload = svc.get_detail("600000", period="min15", limit=100)
        assert payload["available"] is False
        q = payload["quote"]
        assert q is not None
        assert q["close"] == pytest.approx(10.5 + 299 * 0.01)

    def test_tdx_fallback_quote_from_tdx_daily(self):
        """本地残缺 + tdx 回退：quote 取 tdx 日线（比本地更全）。"""
        fetcher = FakeTdxDailyFetcher(_make_bars(900))
        store = FakeStore(_make_bars(2))
        svc = StockDetailService(store=store, config=None)
        svc._tdx_fetcher = fetcher  # type: ignore[assignment]
        payload = svc.get_detail("600000", period="year", limit=50)
        q = payload["quote"]
        assert q is not None
        # tdx 900 根的末两根，而非本地 2 根
        assert q["close"] == pytest.approx(10.5 + 899 * 0.01)
        assert q["prev_close"] == pytest.approx(10.5 + 898 * 0.01)

    def test_minute_quote_skips_stale_local_daily(self):
        """回归：分钟周期本地日线残缺（测试残留假数据）→ quote 走 tdx。

        场景：本地 market.db 仅 2 根假日线（价格 10.x），分钟线来自 tdx
        回退；quote 若读本地会显示假价格，必须回退 tdx 真实日线。
        """
        daily_fetcher = FakeTdxDailyFetcher(_make_bars(900))

        class _ComboFetcher(FakeTdxDailyFetcher):
            def fetch_minute_kline(self, market, code, *, period_minutes, count):
                return _tdx_minute_rows(960, period_minutes=period_minutes)

        combo = _ComboFetcher(daily_fetcher.bars)
        store = FakeStore(_make_bars(2))  # 本地残缺
        store.read_minute_bars = lambda *a, **k: []  # type: ignore[method-assign]
        svc = StockDetailService(store=store, config=None)
        svc._tdx_fetcher = combo  # type: ignore[assignment]
        payload = svc.get_detail("600000", period="min15", limit=100)
        # 分钟线走 tdx 回退成功（fetcher 也能拉日线）
        assert payload["data_source"] == "tdx_realtime"
        q = payload["quote"]
        assert q is not None
        # tdx 900 根末两根，而非本地 2 根假日线（10.5x）
        assert q["close"] == pytest.approx(10.5 + 899 * 0.01)
        assert q["prev_close"] == pytest.approx(10.5 + 898 * 0.01)
