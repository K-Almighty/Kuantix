"""个股详情端点单测（GET /api/v1/stock/detail/{code}）。

覆盖：
- 日K：信封契约 + bars 截断 + 指标键 + 上市日期；
- 周K：日线重采样（120 根日线 → 十几根周K，vol 为桶内求和）；
- 分钟周期无本地数据：``available=False`` + 提示（不编造数据）；
- 非法周期：fail-loud → 400（MissingKeyError → CODE_INVALID_ARGUMENT）。
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from envelope_validator import assert_envelope

from Kuantix.analysis.stock_detail import StockDetailService
from Kuantix.api.deps import ServiceContainer
from Kuantix.core.contracts import Bar
from tests.unit.api.conftest import (
    FakeFactorService,
    FakeLake,
    FakeScreenService,
    make_config,
)


class _FakeStore:
    """MarketStore 替身：只提供详情页所需的最小读取面。"""

    def __init__(self, bars: list[Bar]) -> None:
        self._bars = bars

    def read_daily_bars(self, market: str, code: str, *, tail: int | None = None):
        return self._bars[-tail:] if tail else self._bars

    def read_minute_bars(self, market: str, code: str, *, start_date: int | None = None):
        return []

    def security_name(self, code: str, market: str = "CN") -> str:
        return "测试股份"

    def first_daily_date(self, market: str, code: str) -> int | None:
        return int(self._bars[0].date.strftime("%Y%m%d")) if self._bars else None


def _bars(n: int) -> list[Bar]:
    start = dt.date(2025, 6, 2)
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


@pytest.fixture()
def stock_client(tmp_path: Path, jobs):
    """带 stock_detail_service 的组合根（假 store，不发网络）。"""
    config = make_config(tmp_path)
    container = ServiceContainer(
        config=config,
        lake=FakeLake(),
        factor_service=FakeFactorService(),
        screen_service=FakeScreenService(),
        jobs=jobs,
        stock_detail_service=StockDetailService(
            store=_FakeStore(_bars(120)), config=None
        ),
    )
    from fastapi.testclient import TestClient

    from Kuantix.api.server import create_app

    app = create_app(config=config, services=container)
    with TestClient(app) as client:
        yield client


def test_detail_day_envelope_and_bars(stock_client):
    resp = stock_client.get(
        "/api/v1/stock/detail/600000", params={"period": "day", "limit": 50}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert_envelope(body)
    data = body["data"]
    assert data["code"] == "600000"
    assert data["name"] == "测试股份"
    assert data["period"] == "day"
    assert data["available"] is True
    assert data["data_source"] == "lake"
    assert data["listing_date"] == "2025-06-02"
    assert len(data["bars"]) == 50
    assert set(data["indicators"]) >= {"ma5", "ma10", "macd", "kdj", "rsi"}
    assert set(data["indicators"]["rsi"]) >= {"rsi6", "rsi12", "rsi24"}


def test_detail_week_resamples_daily(stock_client):
    resp = stock_client.get(
        "/api/v1/stock/detail/600000", params={"period": "week", "limit": 50}
    )
    assert resp.status_code == 200
    bars = resp.json()["data"]["bars"]
    # 120 个自然日 ≈ 17 个周桶；周K根数远少于日K
    assert 10 <= len(bars) <= 25
    # 首周（周一起始）5 根日线的 vol 求和 ≈ 5010，远大于单根日线
    assert bars[0]["vol"] >= 4000.0


def test_detail_minute_without_data_unavailable(stock_client):
    resp = stock_client.get("/api/v1/stock/detail/600000", params={"period": "min15"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["available"] is False
    assert data["bars"] == []
    assert "分钟" in data["message"]


def test_detail_invalid_period_400(stock_client):
    resp = stock_client.get("/api/v1/stock/detail/600000", params={"period": "hour"})
    assert resp.status_code == 400
