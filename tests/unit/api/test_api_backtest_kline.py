"""backtest kline 路由单测（B5，v1.3 增量 P1：单标的 K 线 + 买卖点标注）。

- 假 L1Reader 注入（不发网络）+ 真 BacktestBridge（调上游引擎算买卖点）；
- code 非法 → 400；无数据 → 404（显式）；market=HK → 501；
- 买卖点是**信号标注**数组（数据结构，非下单动作，R5）。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from envelope_validator import assert_envelope

from Kuantix.api.deps import ServiceContainer
from Kuantix.backtest.service import BacktestService
from Kuantix.backtest.store import BacktestResultStore
from tests.unit.api.conftest import (
    FakeFactorService,
    FakeLake,
    FakeScreenService,
    make_config,
)


class _FakeReader:
    """假 L1Reader：返回确定日线（上升趋势，含 datetime 列）。"""

    def __init__(self, n: int = 120) -> None:
        self._n = n

    def read_daily_frame(self, exchange: str, code: str) -> pd.DataFrame:
        dates = pd.bdate_range("2024-01-01", periods=self._n)
        close = np.linspace(10, 20, self._n) + np.sin(np.arange(self._n) / 10) * 0.5
        return pd.DataFrame(
            {
                "datetime": dates,
                "open": close * 0.99,
                "high": close * 1.02,
                "low": close * 0.98,
                "close": close,
                "vol": np.full(self._n, 10000.0),
                "amount": np.full(self._n, 1e7),
            }
        )


class _EmptyReader:
    """假 L1Reader：读不到数据（测无数据 404）。"""

    def read_daily_frame(self, exchange: str, code: str) -> pd.DataFrame:
        return pd.DataFrame(
            columns=["datetime", "open", "high", "low", "close", "vol", "amount"]
        )


def _make_client(tmp_path: Path, reader) -> pytest.FixtureRequest:
    from fastapi.testclient import TestClient

    from Kuantix.api.server import create_app

    config = make_config(tmp_path)
    store = BacktestResultStore(tmp_path / "db" / "backtest_results.db")
    backtest_service = BacktestService(config, reader=reader, store=store)
    container = ServiceContainer(
        config=config,
        lake=FakeLake(),
        factor_service=FakeFactorService(),
        screen_service=FakeScreenService(),
        jobs=None,  # kline 用例不发 Job
        backtest_service=backtest_service,
    )
    # jobs 由 conftest 的 jobs fixture 提供，这里避免 None
    return container, config


@pytest.fixture()
def kl_client(tmp_path: Path, jobs):
    """带真 BacktestService + 假 reader 的组合根。"""
    from fastapi.testclient import TestClient

    from Kuantix.api.server import create_app

    config = make_config(tmp_path)
    store = BacktestResultStore(tmp_path / "db" / "backtest_results.db")
    backtest_service = BacktestService(config, reader=_FakeReader(), store=store)
    container = ServiceContainer(
        config=config,
        lake=FakeLake(),
        factor_service=FakeFactorService(),
        screen_service=FakeScreenService(),
        jobs=jobs,
        backtest_service=backtest_service,
    )
    app = create_app(config=config, services=container)
    with TestClient(app) as client:
        yield client


@pytest.fixture()
def kl_client_empty(tmp_path: Path, jobs):
    """假 reader 读不到数据 → 404（显式，fail-loud）。"""
    from fastapi.testclient import TestClient

    from Kuantix.api.server import create_app

    config = make_config(tmp_path)
    store = BacktestResultStore(tmp_path / "db" / "backtest_results.db")
    backtest_service = BacktestService(config, reader=_EmptyReader(), store=store)
    container = ServiceContainer(
        config=config,
        lake=FakeLake(),
        factor_service=FakeFactorService(),
        screen_service=FakeScreenService(),
        jobs=jobs,
        backtest_service=backtest_service,
    )
    app = create_app(config=config, services=container)
    with TestClient(app) as client:
        yield client


# ---------------------------------------------------------------------------
# B5 K 线 + 买卖点
# ---------------------------------------------------------------------------


def test_b5_kline_with_signals(kl_client) -> None:
    response = kl_client.get("/api/v1/backtest/kline/600000")
    assert response.status_code == 200
    payload = response.json()
    assert_envelope(payload)
    data = payload["data"]
    assert data["code"] == "600000"
    assert data["market"] == "CN"
    assert data["strategy"] == "ma_cross"
    assert len(data["kline"]) > 0
    bar = data["kline"][0]
    for key in ("date", "open", "high", "low", "close", "vol", "amount"):
        assert key in bar
    assert isinstance(bar["date"], str)
    # 买卖点是信号标注数组（date + price）
    assert isinstance(data["buy_points"], list)
    assert isinstance(data["sell_points"], list)
    for point in data["buy_points"] + data["sell_points"]:
        assert "date" in point
        assert "price" in point


def test_b5_kline_date_range(kl_client) -> None:
    response = kl_client.get(
        "/api/v1/backtest/kline/600000?start=2024-01-01&end=2024-01-31"
    )
    data = response.json()["data"]
    assert len(data["kline"]) > 0
    assert data["start_date"] == "2024-01-01"
    assert data["end_date"] == "2024-01-31"
    for bar in data["kline"]:
        assert "2024-01-01" <= bar["date"] <= "2024-01-31"


def test_b5_kline_strategy_param(kl_client) -> None:
    response = kl_client.get(
        "/api/v1/backtest/kline/600000?strategy=macd"
    )
    assert response.status_code == 200
    assert response.json()["data"]["strategy"] == "macd"


def test_b5_kline_invalid_code_format_400(kl_client) -> None:
    response = kl_client.get("/api/v1/backtest/kline/abc")
    assert response.status_code == 400
    assert response.json()["code"] == 400


def test_b5_kline_unrecognized_code_400(kl_client) -> None:
    """6 位但代码段无法识别 → 400（fail-loud，不默认归 A 股）。"""
    response = kl_client.get("/api/v1/backtest/kline/710000")
    assert response.status_code == 400
    assert response.json()["code"] == 400


def test_b5_kline_no_data_404(kl_client_empty) -> None:
    """无数据 → 404（显式，fail-loud）。"""
    response = kl_client_empty.get("/api/v1/backtest/kline/600000")
    assert response.status_code == 404
    assert response.json()["code"] == 404


def test_b5_kline_hk_501(kl_client) -> None:
    response = kl_client.get("/api/v1/backtest/kline/600000?market=HK")
    assert response.status_code == 501
    assert response.json()["code"] == 501


def test_b5_kline_invalid_date_range_400(kl_client) -> None:
    response = kl_client.get(
        "/api/v1/backtest/kline/600000?start=2025-01-01&end=2024-01-01"
    )
    assert response.status_code == 400
    assert response.json()["code"] == 400


def test_b5_kline_missing_service_400(tmp_path: Path, jobs) -> None:
    """组合根缺 backtest_service → B5 显式 400（fail-loud）。"""
    from fastapi.testclient import TestClient

    from Kuantix.api.server import create_app

    config = make_config(tmp_path)
    container = ServiceContainer(
        config=config,
        lake=FakeLake(),
        factor_service=FakeFactorService(),
        screen_service=FakeScreenService(),
        jobs=jobs,
    )
    app = create_app(config=config, services=container)
    with TestClient(app) as client:
        response = client.get("/api/v1/backtest/kline/600000")
    assert response.status_code == 400
    assert response.json()["code"] == 400
