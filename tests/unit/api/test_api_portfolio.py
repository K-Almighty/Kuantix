"""portfolio 路由单测（P1–P3，v1.3 增量）。

全部用假 L1Reader（不发网络）+ 真 BacktestBridge（调上游组合引擎）+ 真
BacktestResultStore（tmp），TestClient 真调。
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
from Kuantix.backtest.portfolio_service import PortfolioService
from Kuantix.backtest.store import BacktestResultStore
from tests.unit.api.conftest import (
    FakeFactorService,
    FakeLake,
    FakeScreenService,
    make_config,
)


class _FakeReader:
    """假 L1Reader：返回确定日线（上升趋势，含 datetime 列）。"""

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


class _EmptyReader:
    """假 L1Reader：所有标的读不到数据（测 fail-loud 422）。"""

    def read_daily_frame(self, exchange: str, code: str) -> pd.DataFrame:
        return pd.DataFrame(
            columns=["datetime", "open", "high", "low", "close", "vol", "amount"]
        )


def _make_client(tmp_path: Path, jobs: JobManager, reader) -> pytest.FixtureRequest:
    from fastapi.testclient import TestClient

    from Kuantix.api.server import create_app

    config = make_config(tmp_path)
    store = BacktestResultStore(tmp_path / "db" / "backtest_results.db")
    portfolio_service = PortfolioService(config, reader=reader, store=store)
    container = ServiceContainer(
        config=config,
        lake=FakeLake(),
        factor_service=FakeFactorService(),
        screen_service=FakeScreenService(),
        jobs=jobs,
        portfolio_service=portfolio_service,
    )
    app = create_app(config=config, services=container)
    return TestClient(app)


@pytest.fixture()
def pf_client(tmp_path: Path, jobs: JobManager):
    with _make_client(tmp_path, jobs, _FakeReader()) as client:
        yield client


@pytest.fixture()
def pf_client_empty(tmp_path: Path, jobs: JobManager):
    with _make_client(tmp_path, jobs, _EmptyReader()) as client:
        yield client


def _wait_done(client, job_id: str, path: str = "/api/v1/portfolio/jobs") -> dict:
    for _ in range(200):
        payload = client.get(f"{path}/{job_id}").json()
        assert_envelope(payload)
        status = payload["data"]["status"]
        if status in ("done", "failed", "cancelled"):
            return payload["data"]
        time.sleep(0.05)
    raise AssertionError("job 未在超时内结束")


# ---------------------------------------------------------------------------
# P1 触发组合回测
# ---------------------------------------------------------------------------


def test_p1_run_returns_job(pf_client) -> None:
    response = pf_client.post(
        "/api/v1/portfolio/run",
        json={
            "market": "CN",
            "codes": ["600000", "600036"],
            "strategy": "ma_cross",
            "params": {"fast": 5, "slow": 20},
            "start": "2024-01-01",
            "end": "2024-12-31",
            "cash": 1000000,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert_envelope(payload)
    job = payload["data"]
    assert job["module"] == "backtest"
    assert job["action"] == "portfolio"
    assert job["status"] in ("queued", "running", "done")
    assert job["job_id"].startswith("job_")


def test_p1_run_empty_codes_400(pf_client) -> None:
    response = pf_client.post(
        "/api/v1/portfolio/run",
        json={"market": "CN", "codes": [], "strategy": "ma_cross"},
    )
    assert response.status_code == 400
    assert response.json()["code"] == 400


def test_p1_run_whitespace_codes_400(pf_client) -> None:
    response = pf_client.post(
        "/api/v1/portfolio/run",
        json={"market": "CN", "codes": ["  ", ""], "strategy": "ma_cross"},
    )
    assert response.status_code == 400
    assert response.json()["code"] == 400


def test_p1_run_too_many_codes_400(pf_client) -> None:
    codes = [f"{600000 + i}" for i in range(21)]
    response = pf_client.post(
        "/api/v1/portfolio/run",
        json={"market": "CN", "codes": codes, "strategy": "ma_cross"},
    )
    assert response.status_code == 400
    assert response.json()["code"] == 400


def test_p1_run_hk_501(pf_client) -> None:
    response = pf_client.post(
        "/api/v1/portfolio/run",
        json={"market": "HK", "codes": ["600000"], "strategy": "ma_cross"},
    )
    assert response.status_code == 501
    assert response.json()["code"] == 501


def test_p1_run_all_load_failed_422(pf_client_empty) -> None:
    """所有标的读不到数据 → Job failed + error code 422（fail-loud）。"""
    resp = pf_client_empty.post(
        "/api/v1/portfolio/run",
        json={"market": "CN", "codes": ["600000"], "strategy": "ma_cross"},
    )
    assert resp.status_code == 200
    job_id = resp.json()["data"]["job_id"]
    job = _wait_done(pf_client_empty, job_id)
    assert job["status"] == "failed"
    assert job["error"] is not None
    assert job["error"]["code"] == 422


# ---------------------------------------------------------------------------
# P2 进度 + P3 完整结果
# ---------------------------------------------------------------------------


def test_p2_p3_full_flow(pf_client) -> None:
    """P1 → 轮询 P2 → done 后 P3 返回 PortfolioResult。"""
    resp = pf_client.post(
        "/api/v1/portfolio/run",
        json={
            "market": "CN",
            "codes": ["600000", "600036"],
            "strategy": "ma_cross",
            "params": {"fast": 5, "slow": 20},
            "start": "2024-01-01",
            "end": "2024-12-31",
            "cash": 1000000,
        },
    )
    job = resp.json()["data"]
    job_id = job["job_id"]
    done = _wait_done(pf_client, job_id)
    assert done["status"] == "done"
    assert done["result_summary"] is not None
    total = done["result_summary"]["total"]
    for key in (
        "total_return",
        "annual_return",
        "total_stocks",
        "total_cash",
        "combined_points",
    ):
        assert key in total

    result_payload = pf_client.get(f"/api/v1/portfolio/results/{job_id}").json()
    assert_envelope(result_payload)
    result = result_payload["data"]
    assert result["strategy"] == "ma_cross"
    assert result["codes"] == ["600000", "600036"]
    # 组合结果结构：total_performance / individual_results / equity_allocation / combined_equity
    assert set(result["individual_results"].keys()) == {"600000", "600036"}
    assert result["equity_allocation"] == {"600000": 0.5, "600036": 0.5}
    assert len(result["combined_equity"]) > 0
    point = result["combined_equity"][0]
    for key in ("datetime", "total", "drawdown", "drawdown_pct"):
        assert key in point
    assert result["total_performance"]["total_stocks"] == 2
    # 单个标的完整结果（净值/绩效/成交）
    one = result["individual_results"]["600000"]
    assert "equity_curve" in one
    assert "performance" in one
    assert "trades" in one


def test_p2_unknown_job_404(pf_client) -> None:
    response = pf_client.get("/api/v1/portfolio/jobs/job_nope_123")
    assert response.status_code == 404
    assert response.json()["code"] == 404


def test_p3_unknown_job_404(pf_client) -> None:
    response = pf_client.get("/api/v1/portfolio/results/job_nope_123")
    assert response.status_code == 404
    assert response.json()["code"] == 404


def test_p3_result_not_ready_404(pf_client, jobs: JobManager) -> None:
    """job 存在但结果未落库 → 404（显式，不静默空结果）。"""
    job_id = "job_no_result_p1"
    jobs.store.create(job_id, "backtest", "portfolio", "CN", {"codes": ["600000"]})
    jobs.store.set_status(job_id, "running")
    response = pf_client.get(f"/api/v1/portfolio/results/{job_id}")
    assert response.status_code == 404
    assert response.json()["code"] == 404


def test_p1_missing_service_400(tmp_path: Path, jobs: JobManager) -> None:
    """组合根缺 portfolio_service → P1 显式 400（fail-loud）。"""
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
        response = client.post(
            "/api/v1/portfolio/run",
            json={"market": "CN", "codes": ["600000"], "strategy": "ma_cross"},
        )
    assert response.status_code == 400
    assert response.json()["code"] == 400
