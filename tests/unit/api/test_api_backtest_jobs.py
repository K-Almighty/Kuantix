"""backtest jobs 列表路由单测（C1，v1.3 增量 P1，Compare 页）。

- 默认返回全部 status、module=backtest（前端过滤 done，决策 D-7-A）；
- limit 越界 / status 非法 → 400（fail-loud）。
"""
from __future__ import annotations

import time
from pathlib import Path

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
    """最小假 L1Reader（C1 用例不触发真实回测）。"""

    def read_daily_frame(self, exchange: str, code: str):
        import numpy as np
        import pandas as pd

        dates = pd.bdate_range("2024-01-01", periods=60)
        close = np.linspace(10, 20, 60)
        return pd.DataFrame(
            {
                "datetime": dates,
                "open": close * 0.99,
                "high": close * 1.02,
                "low": close * 0.98,
                "close": close,
                "vol": np.full(60, 10000.0),
                "amount": np.full(60, 1e7),
            }
        )


@pytest.fixture()
def jobs_client(tmp_path: Path, jobs):
    """组合根带真 JobManager + 真 BacktestService；TestClient 真调。"""
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


def _seed_jobs(jobs) -> None:
    """种入 backtest / data 两种模块的 job。"""
    jobs.store.create(
        "job_bt_done", "backtest", "run", "CN", {"codes": ["600000"]}
    )
    jobs.store.set_status("job_bt_done", "done")
    jobs.store.create(
        "job_bt_run", "backtest", "portfolio", "CN", {"codes": ["600000"]}
    )
    jobs.store.set_status("job_bt_run", "running")
    jobs.store.create(
        "job_data", "data", "sync_full", "CN", {"years": 10}
    )
    jobs.store.set_status("job_data", "done")


def test_c1_list_defaults_module_backtest(jobs_client, jobs) -> None:
    _seed_jobs(jobs)
    response = jobs_client.get("/api/v1/backtest/jobs")
    assert response.status_code == 200
    payload = response.json()
    assert_envelope(payload)
    data = payload["data"]
    assert data["count"] == 2
    ids = {item["job_id"] for item in data["items"]}
    assert ids == {"job_bt_done", "job_bt_run"}
    # Job 含 result_summary / error 字段（Compare 卡片展示）
    first = data["items"][0]
    for key in (
        "job_id",
        "module",
        "action",
        "status",
        "market",
        "progress",
        "result_summary",
        "error",
        "created_at",
        "updated_at",
    ):
        assert key in first


def test_c1_list_limit(jobs_client, jobs) -> None:
    _seed_jobs(jobs)
    response = jobs_client.get("/api/v1/backtest/jobs?limit=1")
    data = response.json()["data"]
    assert data["count"] == 1


def test_c1_list_status_filter(jobs_client, jobs) -> None:
    _seed_jobs(jobs)
    response = jobs_client.get("/api/v1/backtest/jobs?status=done")
    data = response.json()["data"]
    ids = {item["job_id"] for item in data["items"]}
    assert ids == {"job_bt_done"}


def test_c1_list_status_all_default(jobs_client, jobs) -> None:
    """默认返回全部 status（含 running），前端过滤 done（决策 D-7-A）。"""
    _seed_jobs(jobs)
    response = jobs_client.get("/api/v1/backtest/jobs")
    statuses = {item["status"] for item in response.json()["data"]["items"]}
    assert statuses == {"done", "running"}


def test_c1_list_module_override(jobs_client, jobs) -> None:
    _seed_jobs(jobs)
    response = jobs_client.get("/api/v1/backtest/jobs?module=data")
    data = response.json()["data"]
    ids = {item["job_id"] for item in data["items"]}
    assert ids == {"job_data"}


def test_c1_list_limit_too_large_400(jobs_client, jobs) -> None:
    _seed_jobs(jobs)
    response = jobs_client.get("/api/v1/backtest/jobs?limit=51")
    assert response.status_code == 400
    assert response.json()["code"] == 400


def test_c1_list_limit_zero_400(jobs_client, jobs) -> None:
    _seed_jobs(jobs)
    response = jobs_client.get("/api/v1/backtest/jobs?limit=0")
    assert response.status_code == 400
    assert response.json()["code"] == 400


def test_c1_list_invalid_status_400(jobs_client, jobs) -> None:
    _seed_jobs(jobs)
    response = jobs_client.get("/api/v1/backtest/jobs?status=bogus")
    assert response.status_code == 400
    assert response.json()["code"] == 400


def test_c1_list_empty(jobs_client) -> None:
    response = jobs_client.get("/api/v1/backtest/jobs")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data == {"items": [], "count": 0}
