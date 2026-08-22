"""strategies 路由单测（S1–S5，v1.3 增量）。

策略库 CRUD 用真 StrategyStore（tmp，不触碰 ~/.Kuantix）；多策略回测
（S5）用假 L1Reader + 真 BacktestBridge（不发网络）。
"""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from envelope_validator import assert_envelope

from Kuantix.api.deps import ServiceContainer
from Kuantix.api.jobs import JobManager
from Kuantix.backtest.portfolio_service import MultiStrategyService
from Kuantix.backtest.store import BacktestResultStore
from Kuantix.backtest.strategy_store import StrategyStore
from tests.unit.api.conftest import (
    FakeFactorService,
    FakeLake,
    FakeScreenService,
    make_config,
)


class _FakeReader:
    """假 L1Reader：返回确定日线。"""

    def __init__(self, n: int = 300) -> None:
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


def _make_client(tmp_path: Path, jobs: JobManager, with_multi: bool = True):
    from fastapi.testclient import TestClient

    from Kuantix.api.server import create_app

    config = make_config(tmp_path)
    strategy_store = StrategyStore(tmp_path / "db" / "strategies.db")
    kwargs: dict = {
        "config": config,
        "lake": FakeLake(),
        "factor_service": FakeFactorService(),
        "screen_service": FakeScreenService(),
        "jobs": jobs,
        "strategy_store": strategy_store,
    }
    if with_multi:
        store = BacktestResultStore(tmp_path / "db" / "backtest_results.db")
        kwargs["multi_strategy_service"] = MultiStrategyService(
            config, reader=_FakeReader(), store=store
        )
        # S5 结果可经 B4（/backtest/results/{id}）或 P3 读取；测试容器与
        # 生产 build_container 一样同时装配 backtest_service
        from Kuantix.backtest.service import BacktestService

        kwargs["backtest_service"] = BacktestService(
            config, reader=_FakeReader(), store=store
        )
    container = ServiceContainer(**kwargs)
    app = create_app(config=config, services=container)
    return TestClient(app), strategy_store


@pytest.fixture()
def st_client(tmp_path: Path, jobs: JobManager):
    with _make_client(tmp_path, jobs)[0] as client:
        yield client


def _create_one(client, **overrides):
    payload = {
        "name": "双均线-茅台",
        "kind": "single",
        "strategy": "ma_cross",
        "strategy_label": "双均线交叉",
        "params": {"fast": 5, "slow": 20},
        "context": {"symbol": "SH:600519"},
        "trade_config": {"cash": 1000000},
        "snapshot": {"total_return": 0.26},
        "tags": ["优选"],
        "notes": "测试",
    }
    payload.update(overrides)
    return client.post("/api/v1/strategies", json=payload)


# ---------------------------------------------------------------------------
# S1 列表 / S2 创建
# ---------------------------------------------------------------------------


def test_s1_empty_list_pagination(st_client) -> None:
    response = st_client.get("/api/v1/strategies")
    assert response.status_code == 200
    payload = response.json()
    assert_envelope(payload)
    data = payload["data"]
    assert data["items"] == []
    assert data["page"] == 1
    assert data["page_size"] == 50
    assert data["total"] == 0
    assert data["total_pages"] == 0


def test_s2_create_201_saved_strategy(st_client) -> None:
    response = _create_one(st_client)
    assert response.status_code == 201
    payload = response.json()
    assert_envelope(payload)
    view = payload["data"]
    assert view["id"].startswith("strat_")
    assert view["name"] == "双均线-茅台"
    assert view["kind"] == "single"
    assert view["strategy"] == "ma_cross"
    assert view["params"] == {"fast": 5, "slow": 20}
    assert view["context"] == {"symbol": "SH:600519"}
    assert view["tags"] == ["优选"]
    assert view["created_at"]
    assert view["updated_at"]
    assert view["app_version"]


def test_s1_kind_filter(st_client) -> None:
    _create_one(st_client, name="单标的", kind="single")
    _create_one(st_client, name="组合", kind="portfolio")
    _create_one(st_client, name="多策略", kind="multi")

    response = st_client.get("/api/v1/strategies?kind=portfolio")
    data = response.json()["data"]
    assert data["total"] == 1
    assert data["items"][0]["kind"] == "portfolio"

    response = st_client.get("/api/v1/strategies?kind=single")
    assert response.json()["data"]["total"] == 1

    response = st_client.get("/api/v1/strategies")
    assert response.json()["data"]["total"] == 3


def test_s1_invalid_kind_400(st_client) -> None:
    response = st_client.get("/api/v1/strategies?kind=bogus")
    assert response.status_code == 400
    assert response.json()["code"] == 400


def test_s2_invalid_kind_400(st_client) -> None:
    response = _create_one(st_client, kind="bogus")
    assert response.status_code == 400
    assert response.json()["code"] == 400


def test_s2_missing_name_400(st_client) -> None:
    response = st_client.post(
        "/api/v1/strategies", json={"kind": "single", "strategy": "ma_cross"}
    )
    assert response.status_code == 400
    assert response.json()["code"] == 400


# ---------------------------------------------------------------------------
# S3 详情 / S4 删除
# ---------------------------------------------------------------------------


def test_s3_get(st_client) -> None:
    sid = _create_one(st_client).json()["data"]["id"]
    response = st_client.get(f"/api/v1/strategies/{sid}")
    assert response.status_code == 200
    assert_envelope(response.json())
    assert response.json()["data"]["id"] == sid


def test_s3_unknown_404(st_client) -> None:
    response = st_client.get("/api/v1/strategies/strat_nope_123")
    assert response.status_code == 404
    assert response.json()["code"] == 404


def test_s4_delete(st_client) -> None:
    sid = _create_one(st_client).json()["data"]["id"]
    response = st_client.delete(f"/api/v1/strategies/{sid}")
    assert response.status_code == 200
    assert response.json()["data"] == {"removed": sid}
    # 删除后再查 → 404
    assert st_client.get(f"/api/v1/strategies/{sid}").status_code == 404


def test_s4_delete_unknown_404(st_client) -> None:
    response = st_client.delete("/api/v1/strategies/strat_nope_123")
    assert response.status_code == 404
    assert response.json()["code"] == 404


def test_s1_missing_store_400(tmp_path: Path, jobs: JobManager) -> None:
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
        response = client.get("/api/v1/strategies")
    assert response.status_code == 400
    assert response.json()["code"] == 400


# ---------------------------------------------------------------------------
# S5 多策略组合回测
# ---------------------------------------------------------------------------


def test_s5_run_returns_job(st_client) -> None:
    response = st_client.post(
        "/api/v1/strategies/run-multi",
        json={
            "market": "CN",
            "items": [
                {
                    "strategy": "ma_cross",
                    "label": "双均线交叉",
                    "code": "600000",
                    "params": {"fast": 5, "slow": 20},
                },
                {"strategy": "macd", "label": "MACD", "code": "600036", "params": {}},
            ],
            "cash": 1000000,
            "start": "2024-01-01",
            "end": "2024-12-31",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert_envelope(payload)
    job = payload["data"]
    assert job["module"] == "backtest"
    assert job["action"] == "multi"
    assert job["status"] in ("queued", "running", "done")


def test_s5_full_flow(st_client) -> None:
    """S5 触发 → 轮询 → done → 结果 individual_results key = {label}@{symbol}。"""
    resp = st_client.post(
        "/api/v1/strategies/run-multi",
        json={
            "market": "CN",
            "items": [
                {
                    "strategy": "ma_cross",
                    "label": "双均线交叉",
                    "code": "600000",
                    "params": {"fast": 5, "slow": 20},
                },
                {"strategy": "macd", "label": "MACD", "code": "600036", "params": {}},
            ],
            "cash": 1000000,
            "start": "2024-01-01",
            "end": "2024-12-31",
        },
    )
    job_id = resp.json()["data"]["job_id"]
    done = False
    for _ in range(200):
        payload = st_client.get(f"/api/v1/backtest/jobs/{job_id}").json()
        assert_envelope(payload)
        status = payload["data"]["status"]
        if status in ("done", "failed", "cancelled"):
            done = status == "done"
            break
        time.sleep(0.05)
    assert done, "多策略 job 未在超时内完成"

    result = st_client.get(f"/api/v1/backtest/results/{job_id}").json()["data"]
    keys = set(result["individual_results"].keys())
    assert "双均线交叉@SH:600000" in keys
    assert "MACD@SH:600036" in keys
    assert len(result["combined_equity"]) > 0
    assert result["equity_allocation"]["双均线交叉@SH:600000"] == 0.5


def test_s5_empty_items_400(st_client) -> None:
    response = st_client.post(
        "/api/v1/strategies/run-multi",
        json={"market": "CN", "items": [], "cash": 1000000},
    )
    assert response.status_code == 400
    assert response.json()["code"] == 400


def test_s5_hk_501(st_client) -> None:
    response = st_client.post(
        "/api/v1/strategies/run-multi",
        json={
            "market": "HK",
            "items": [{"strategy": "ma_cross", "label": "a", "code": "600000"}],
            "cash": 1000000,
        },
    )
    assert response.status_code == 501
    assert response.json()["code"] == 501
