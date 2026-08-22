"""optimize 路由单测（O1–O3，v1.3 增量 P1）。

全部用假 L1Reader（不发网络）+ 真 BacktestBridge（调上游 ParamGridOptimizer，
小网格）+ 真 BacktestResultStore（tmp），TestClient 真调。
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
from Kuantix.backtest.optimize_service import OptimizeService
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
    """假 L1Reader：读不到数据（测 job failed 422）。"""

    def read_daily_frame(self, exchange: str, code: str) -> pd.DataFrame:
        return pd.DataFrame(
            columns=["datetime", "open", "high", "low", "close", "vol", "amount"]
        )


def _make_client(tmp_path: Path, jobs: JobManager, reader) -> pytest.FixtureRequest:
    from fastapi.testclient import TestClient

    from Kuantix.api.server import create_app

    config = make_config(tmp_path)
    store = BacktestResultStore(tmp_path / "db" / "backtest_results.db")
    optimize_service = OptimizeService(config, reader=reader, store=store)
    container = ServiceContainer(
        config=config,
        lake=FakeLake(),
        factor_service=FakeFactorService(),
        screen_service=FakeScreenService(),
        jobs=jobs,
        optimize_service=optimize_service,
    )
    app = create_app(config=config, services=container)
    return TestClient(app)


@pytest.fixture()
def opt_client(tmp_path: Path, jobs: JobManager):
    with _make_client(tmp_path, jobs, _FakeReader()) as client:
        yield client


@pytest.fixture()
def opt_client_empty(tmp_path: Path, jobs: JobManager):
    with _make_client(tmp_path, jobs, _EmptyReader()) as client:
        yield client


def _wait_done(client, job_id: str) -> dict:
    for _ in range(200):
        payload = client.get(f"/api/v1/optimize/jobs/{job_id}").json()
        assert_envelope(payload)
        status = payload["data"]["status"]
        if status in ("done", "failed", "cancelled"):
            return payload["data"]
        time.sleep(0.05)
    raise AssertionError("job 未在超时内结束")


# ---------------------------------------------------------------------------
# O1 触发寻优
# ---------------------------------------------------------------------------


def _run_body(**overrides) -> dict:
    body = {
        "market": "CN",
        "code": "600000",
        "strategy": "ma_cross",
        "param_grid": {"fast": [5, 10], "slow": [20]},
        "start": "2024-01-01",
        "end": "2024-12-31",
        "cash": 1000000,
    }
    body.update(overrides)
    return body


def test_o1_run_returns_job(opt_client) -> None:
    response = opt_client.post("/api/v1/optimize/run", json=_run_body())
    assert response.status_code == 200
    payload = response.json()
    assert_envelope(payload)
    job = payload["data"]
    assert job["module"] == "backtest"
    assert job["action"] == "optimize"
    assert job["status"] in ("queued", "running", "done")
    assert job["job_id"].startswith("job_")


def test_o1_run_empty_code_400(opt_client) -> None:
    response = opt_client.post(
        "/api/v1/optimize/run", json=_run_body(code="")
    )
    assert response.status_code == 400
    assert response.json()["code"] == 400


def test_o1_run_blank_code_400(opt_client) -> None:
    response = opt_client.post(
        "/api/v1/optimize/run", json=_run_body(code="   ")
    )
    assert response.status_code == 400
    assert response.json()["code"] == 400


def test_o1_run_empty_param_grid_400(opt_client) -> None:
    response = opt_client.post(
        "/api/v1/optimize/run", json=_run_body(param_grid={})
    )
    assert response.status_code == 400
    assert response.json()["code"] == 400


def test_o1_run_empty_values_400(opt_client) -> None:
    response = opt_client.post(
        "/api/v1/optimize/run", json=_run_body(param_grid={"fast": []})
    )
    assert response.status_code == 400
    assert response.json()["code"] == 400


def test_o1_run_too_many_params_400(opt_client) -> None:
    response = opt_client.post(
        "/api/v1/optimize/run",
        json=_run_body(param_grid={"fast": [5, 10], "slow": [20], "extra": [1]}),
    )
    assert response.status_code == 400
    assert response.json()["code"] == 400


def test_o1_run_grid_too_large_400(opt_client) -> None:
    """笛卡尔积 >200 → 400（fail-loud，后端二次校验，不依赖前端）。"""
    response = opt_client.post(
        "/api/v1/optimize/run",
        json=_run_body(param_grid={"fast": list(range(15)), "slow": list(range(15))}),
    )
    assert response.status_code == 400
    assert response.json()["code"] == 400


def test_o1_run_hk_501(opt_client) -> None:
    response = opt_client.post(
        "/api/v1/optimize/run", json=_run_body(market="HK")
    )
    assert response.status_code == 501
    assert response.json()["code"] == 501


def test_o1_run_all_load_failed_422(opt_client_empty) -> None:
    """K 线读不到 → Job failed + error code 422（fail-loud）。"""
    resp = opt_client_empty.post(
        "/api/v1/optimize/run", json=_run_body()
    )
    assert resp.status_code == 200
    job_id = resp.json()["data"]["job_id"]
    job = _wait_done(opt_client_empty, job_id)
    assert job["status"] == "failed"
    assert job["error"] is not None
    assert job["error"]["code"] == 422


# ---------------------------------------------------------------------------
# O2 进度 + O3 完整结果
# ---------------------------------------------------------------------------


def test_o2_o3_full_flow(opt_client) -> None:
    """O1 → 轮询 O2 → done 后 O3 返回 OptimizeResult。"""
    resp = opt_client.post(
        "/api/v1/optimize/run",
        json=_run_body(param_grid={"fast": [5, 10], "slow": [20, 30]}),
    )
    job = resp.json()["data"]
    job_id = job["job_id"]
    done = _wait_done(opt_client, job_id)
    assert done["status"] == "done"
    assert done["result_summary"] is not None
    summary = done["result_summary"]
    assert summary["action"] == "optimize"
    assert summary["code"] == "600000"
    assert summary["strategy"] == "ma_cross"
    assert summary["grid_size"] == 4
    assert summary["param_names"] == ["fast", "slow"]
    assert summary["result_count"] == 4
    assert "best" in summary
    assert "params" in summary["best"]

    result_payload = opt_client.get(f"/api/v1/optimize/results/{job_id}").json()
    assert_envelope(result_payload)
    result = result_payload["data"]
    assert result["strategy"] == "ma_cross"
    assert result["param_names"] == ["fast", "slow"]
    assert len(result["results"]) == 4
    assert result["best"] is not None
    for key in (
        "params",
        "total_return",
        "sharpe",
        "max_drawdown",
        "total_trades",
        "win_rate",
        "profit_factor",
    ):
        assert key in result["best"]
    # 2 参数 → heatmap 非 null（O3 契约）
    assert result["heatmap"] is not None
    assert result["heatmap"]["x_name"] == "fast"
    assert result["heatmap"]["y_name"] == "slow"


def test_o2_unknown_job_404(opt_client) -> None:
    response = opt_client.get("/api/v1/optimize/jobs/job_nope_123")
    assert response.status_code == 404
    assert response.json()["code"] == 404


def test_o3_unknown_job_404(opt_client) -> None:
    response = opt_client.get("/api/v1/optimize/results/job_nope_123")
    assert response.status_code == 404
    assert response.json()["code"] == 404


def test_o3_result_not_ready_404(opt_client, jobs: JobManager) -> None:
    """job 存在但结果未落库 → 404（显式，不静默空结果）。"""
    job_id = "job_no_result_o1"
    jobs.store.create(job_id, "backtest", "optimize", "CN", {"code": "600000"})
    jobs.store.set_status(job_id, "running")
    response = opt_client.get(f"/api/v1/optimize/results/{job_id}")
    assert response.status_code == 404
    assert response.json()["code"] == 404


def test_o6_delete_job_full_flow(opt_client) -> None:
    """O6 删除单个策略寻优：清理 job + 结果，幂等 404。"""
    job = opt_client.post(
        "/api/v1/optimize/run",
        json=_run_body(param_grid={"fast": [5, 10], "slow": [20]}),
    ).json()["data"]
    job_id = job["job_id"]
    _wait_done(opt_client, job_id)
    # 删除前结果可访问
    assert opt_client.get(f"/api/v1/optimize/results/{job_id}").status_code == 200

    resp = opt_client.delete(f"/api/v1/optimize/jobs/{job_id}")
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["job_id"] == job_id
    assert body["deleted_job"] is True
    assert body["deleted_result"] is True

    # 删除后结果不可访问
    assert opt_client.get(f"/api/v1/optimize/jobs/{job_id}").status_code == 404
    assert opt_client.get(f"/api/v1/optimize/results/{job_id}").status_code == 404
    # 幂等：重复删除 → 404
    again = opt_client.delete(f"/api/v1/optimize/jobs/{job_id}")
    assert again.status_code == 404


def test_o6_delete_unknown_job_404(opt_client) -> None:
    """O6 未知 job（无 job 也无结果）→ 404。"""
    response = opt_client.delete("/api/v1/optimize/jobs/job_nope_o6")
    assert response.status_code == 404
    assert response.json()["code"] == 404


def test_o1_missing_service_400(tmp_path: Path, jobs: JobManager) -> None:
    """组合根缺 optimize_service → O1 显式 400（fail-loud）。"""
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
            "/api/v1/optimize/run",
            json={
                "market": "CN",
                "code": "600000",
                "strategy": "ma_cross",
                "param_grid": {"fast": [5, 10]},
            },
        )
    assert response.status_code == 400
    assert response.json()["code"] == 400
