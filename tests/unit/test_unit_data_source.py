"""data_source 模块单测（设计一：data_source 三模式 + auto 优先级，契约 v1.4）。

覆盖：
- ``parse_data_source``：auto/local/live 合法；非法 → MissingKeyError（→ 400）；
- ``local_has_data``：文件存在 / 不存在 / 鸭子类型 reader 无 day_path → True；
- ``fetch_live_frame``：列格式与 ``bars_to_frame`` 一致；日期区间过滤；
  返回空 → DataIntegrityError；上游异常统一包装 → DataIntegrityError（D1.5）；
- ``_live_years_for``：同年 → 2 年，跨年 → N+1（D1.4）。

全部假 fetcher / 假 reader，**不发真网络**。
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from Kuantix.adapters.factor_bridge import bars_to_frame
from Kuantix.backtest.data_source import (
    DATA_SOURCE_VALUES,
    _live_years_for,
    fetch_live_frame,
    local_has_data,
    parse_data_source,
)
from Kuantix.core.contracts import Bar
from Kuantix.core.fail_loud import DataIntegrityError, MissingKeyError
from Kuantix.core.market import get_market_profile


class _FakeFetcher:
    """假拉取器：返回确定 Bar 列表（不发网络）。"""

    def __init__(self, bars: list[Bar] | None = None, error: Exception | None = None) -> None:
        self._bars = bars if bars is not None else []
        self._error = error
        self.calls: list[dict[str, Any]] = []

    def fetch_kline(
        self,
        market: str,
        code: str,
        years: int = 10,
        *,
        exchange: str | None = None,
        count: int | None = None,
        adjust: Any = None,
    ) -> list[Bar]:
        self.calls.append(
            {"market": market, "code": code, "years": years, "exchange": exchange}
        )
        if self._error is not None:
            raise self._error
        return list(self._bars)


class _DuckReader:
    """鸭子类型 reader：无 day_path 属性（测试注入场景）。"""

    def read_daily_frame(self, exchange: str, code: str) -> pd.DataFrame:
        raise AssertionError("不应被调用")


def _make_bars(n: int = 5, start: dt.date = dt.date(2024, 1, 2)) -> list[Bar]:
    return [
        Bar(
            date=start + dt.timedelta(days=i),
            open=10.0 + i,
            high=11.0 + i,
            low=9.0 + i,
            close=10.5 + i,
            vol=1000.0 + i,
            amount=10500.0 + i * 100,
        )
        for i in range(n)
    ]


def _profile():
    return get_market_profile("CN")


# ---------------------------------------------------------------------------
# parse_data_source
# ---------------------------------------------------------------------------


def test_parse_data_source_valid_values() -> None:
    assert parse_data_source("auto") == "auto"
    assert parse_data_source("LOCAL") == "local"
    assert parse_data_source(" Live ") == "live"


def test_parse_data_source_invalid_raises_missing_key() -> None:
    with pytest.raises(MissingKeyError) as excinfo:
        parse_data_source("bogus")
    assert "data_source" in str(excinfo.value)
    assert "auto" in str(excinfo.value)


def test_data_source_values_contract() -> None:
    assert DATA_SOURCE_VALUES == ("auto", "local", "live")


# ---------------------------------------------------------------------------
# local_has_data
# ---------------------------------------------------------------------------


def test_local_has_data_file_exists(tmp_path: Path) -> None:
    lday = tmp_path / "sh" / "lday"
    lday.mkdir(parents=True)
    (lday / "sh600519.day").write_bytes(b"\x00")

    class _Reader:
        def __init__(self, root: Path) -> None:
            self._root = root

        def day_path(self, exchange: str, code: str) -> Path:
            return self._root / exchange / "lday" / f"{exchange}{code}.day"

    assert local_has_data(_Reader(tmp_path), _profile(), "600519") is True


def test_local_has_data_file_missing(tmp_path: Path) -> None:
    class _Reader:
        def __init__(self, root: Path) -> None:
            self._root = root

        def day_path(self, exchange: str, code: str) -> Path:
            return self._root / exchange / "lday" / f"{exchange}{code}.day"

    assert local_has_data(_Reader(tmp_path), _profile(), "600519") is False


def test_local_has_data_duck_reader_without_day_path() -> None:
    """鸭子类型 reader 无 day_path → 保守视为本地有数据（兼容既有测试语义）。"""
    assert local_has_data(_DuckReader(), _profile(), "600519") is True


# ---------------------------------------------------------------------------
# fetch_live_frame
# ---------------------------------------------------------------------------


def test_fetch_live_frame_columns_match_bars_to_frame() -> None:
    bars = _make_bars()
    fetcher = _FakeFetcher(bars)
    frame = fetch_live_frame(
        fetcher, _profile(), "600519", dt.date(2024, 1, 1), dt.date(2024, 12, 31)
    )
    expected = bars_to_frame(bars)
    assert list(frame.columns) == list(expected.columns)
    assert list(frame.columns) == [
        "datetime", "open", "high", "low", "close", "vol", "amount",
    ]
    assert len(frame) == len(bars)
    # 拉取参数：经 profile 推断交易所、years = 跨年份数 + 1
    call = fetcher.calls[0]
    assert call["market"] == "CN"
    assert call["exchange"] == "sh"
    assert call["years"] == _live_years_for(dt.date(2024, 1, 1), dt.date(2024, 12, 31))


def test_fetch_live_frame_filters_date_range() -> None:
    bars = _make_bars(n=10, start=dt.date(2023, 6, 1))  # 2023-06-01 ~ 2023-06-10
    fetcher = _FakeFetcher(bars)
    frame = fetch_live_frame(
        fetcher, _profile(), "600519", dt.date(2023, 6, 5), dt.date(2023, 6, 8)
    )
    assert len(frame) == 4
    dates = pd.to_datetime(frame["datetime"]).dt.date.tolist()
    assert dates[0] == dt.date(2023, 6, 5)
    assert dates[-1] == dt.date(2023, 6, 8)


def test_fetch_live_frame_empty_raises() -> None:
    fetcher = _FakeFetcher([])
    with pytest.raises(DataIntegrityError) as excinfo:
        fetch_live_frame(
            fetcher, _profile(), "600519", dt.date(2024, 1, 1), dt.date(2024, 12, 31)
        )
    assert "返回空" in str(excinfo.value)
    assert "600519" in str(excinfo.value)


def test_fetch_live_frame_exception_wrapped() -> None:
    fetcher = _FakeFetcher(error=RuntimeError("socket timeout"))
    with pytest.raises(DataIntegrityError) as excinfo:
        fetch_live_frame(
            fetcher, _profile(), "600519", dt.date(2024, 1, 1), dt.date(2024, 12, 31)
        )
    message = str(excinfo.value)
    assert "实时拉取失败" in message
    assert "600519" in message
    assert "socket timeout" in message


def test_fetch_live_frame_data_integrity_passthrough() -> None:
    """拉取器自身抛 DataIntegrityError → 原样传播（不二次包装）。"""

    class _RaisingFetcher:
        def fetch_kline(self, *args: Any, **kwargs: Any) -> list[Bar]:
            raise DataIntegrityError("[fail-loud/NF-26] 上游数据自相矛盾")

    with pytest.raises(DataIntegrityError) as excinfo:
        fetch_live_frame(
            _RaisingFetcher(), _profile(), "600519",
            dt.date(2024, 1, 1), dt.date(2024, 12, 31),
        )
    assert "上游数据自相矛盾" in str(excinfo.value)


# ---------------------------------------------------------------------------
# _live_years_for
# ---------------------------------------------------------------------------


def test_live_years_for_same_year() -> None:
    assert _live_years_for(dt.date(2024, 1, 1), dt.date(2024, 12, 31)) == 2


def test_live_years_for_cross_year() -> None:
    assert _live_years_for(dt.date(2024, 1, 1), dt.date(2025, 12, 31)) == 3
    assert _live_years_for(dt.date(2020, 1, 1), dt.date(2025, 12, 31)) == 7


def test_live_years_for_invalid_range() -> None:
    with pytest.raises(DataIntegrityError):
        _live_years_for(dt.date(2025, 12, 31), dt.date(2024, 1, 1))
