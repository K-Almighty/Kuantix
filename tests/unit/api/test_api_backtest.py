"""backtest 路由单测（B1–B4，v1.2 增量）。

全部用假 L1Reader（不发网络）+ 真 BacktestBridge（调上游引擎）+ 真
BacktestResultStore（tmp），TestClient 真调。
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from envelope_validator import assert_envelope

from Kuantix.api.deps import ServiceContainer
from Kuantix.backtest.service import BacktestService
from Kuantix.backtest.store import BacktestResultStore
from Kuantix.core.contracts import QuarantineEntry
from tests.unit.api.conftest import make_config, FakeFactorService, FakeScreenService


class _FakeLake:
    """最小 DataLake 替身（backtest 用例不触碰 data 端点）。"""

    def __init__(self) -> None:
        self._entries: list[QuarantineEntry] = []

    def list_quarantine(self, market: str = "CN") -> list[QuarantineEntry]:
        return list(self._entries)


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


class _FakeFetcher:
    """假 QuotationFetcher：返回确定 Bar 列表（live 路径，不发网络）。"""

    def __init__(self, n: int = 300) -> None:
        self._n = n

    def fetch_kline(
        self,
        market: str,
        code: str,
        years: int = 10,
        *,
        exchange: str | None = None,
        count: int | None = None,
        adjust=None,
    ) -> list:
        from Kuantix.core.contracts import Bar

        dates = pd.bdate_range("2024-01-01", periods=self._n)
        close = np.linspace(10, 20, self._n) + np.sin(np.arange(self._n) / 10) * 0.5
        return [
            Bar(
                date=pd.Timestamp(d).date(),
                open=float(close[i] * 0.99),
                high=float(close[i] * 1.02),
                low=float(close[i] * 0.98),
                close=float(close[i]),
                vol=10000.0,
                amount=1e7,
            )
            for i, d in enumerate(dates)
        ]


class _EmptyReader:
    """假 L1Reader：所有标的读不到数据（测 fail-loud）。"""

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
        lake=_FakeLake(),
        factor_service=FakeFactorService(),
        screen_service=FakeScreenService(),
        jobs=None,  # 由 create_app 惰性装配？不 —— jobs 必填，下面单独处理
    )
    # jobs 由 conftest 的 jobs fixture 提供，这里避免 None
    return container, store


@pytest.fixture()
def bt_client(tmp_path: Path, jobs):
    """组合根带真 BacktestService + 假 reader；TestClient 真调。"""
    config = make_config(tmp_path)
    store = BacktestResultStore(tmp_path / "db" / "backtest_results.db")
    backtest_service = BacktestService(config, reader=_FakeReader(), store=store)
    container = ServiceContainer(
        config=config,
        lake=_FakeLake(),
        factor_service=FakeFactorService(),
        screen_service=FakeScreenService(),
        jobs=jobs,
        backtest_service=backtest_service,
    )
    from fastapi.testclient import TestClient
    from Kuantix.api.server import create_app

    app = create_app(config=config, services=container)
    with TestClient(app) as client:
        yield client


@pytest.fixture()
def bt_client_empty(tmp_path: Path, jobs):
    """假 reader 读不到数据 → B2 应 422 fail-loud。"""
    config = make_config(tmp_path)
    store = BacktestResultStore(tmp_path / "db" / "backtest_results.db")
    backtest_service = BacktestService(config, reader=_EmptyReader(), store=store)
    container = ServiceContainer(
        config=config,
        lake=_FakeLake(),
        factor_service=FakeFactorService(),
        screen_service=FakeScreenService(),
        jobs=jobs,
        backtest_service=backtest_service,
    )
    from fastapi.testclient import TestClient
    from Kuantix.api.server import create_app

    app = create_app(config=config, services=container)
    with TestClient(app) as client:
        yield client


@pytest.fixture()
def bt_client_live(tmp_path: Path, jobs):
    """假 reader + 假 fetcher：live 路径不发网络（B2/B5 data_source 用例）。"""
    config = make_config(tmp_path)
    store = BacktestResultStore(tmp_path / "db" / "backtest_results.db")
    backtest_service = BacktestService(
        config, reader=_EmptyReader(), store=store, fetcher=_FakeFetcher()
    )
    container = ServiceContainer(
        config=config,
        lake=_FakeLake(),
        factor_service=FakeFactorService(),
        screen_service=FakeScreenService(),
        jobs=jobs,
        backtest_service=backtest_service,
    )
    from fastapi.testclient import TestClient
    from Kuantix.api.server import create_app

    app = create_app(config=config, services=container)
    with TestClient(app) as client:
        yield client


# ---------------------------------------------------------------------------
# B1 策略列表
# ---------------------------------------------------------------------------


def test_b1_strategies_list(bt_client) -> None:
    payload = bt_client.get("/api/v1/backtest/strategies").json()
    assert_envelope(payload)
    data = payload["data"]
    assert data["count"] >= 10
    names = {s["name"] for s in data["items"]}
    assert "ma_cross" in names
    assert "macd" in names
    first = next(s for s in data["items"] if s["name"] == "ma_cross")
    for key in ("name", "label", "description", "params"):
        assert key in first
    assert len(first["params"]) >= 1


# ---------------------------------------------------------------------------
# B2 触发回测（Job）
# ---------------------------------------------------------------------------


def test_b2_run_returns_job(bt_client) -> None:
    response = bt_client.post(
        "/api/v1/backtest/run",
        json={
            "market": "CN",
            "codes": ["600000", "600036"],
            "strategy": "ma_cross",
            "params": {"fast": 5, "slow": 20},
            "start": "2024-01-01",
            "end": "2024-12-31",
            "cash": 1000000,
            "commission": 0.0003,
            "execution": "next_open",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert_envelope(payload)
    job = payload["data"]
    assert job["module"] == "backtest"
    assert job["action"] == "run"
    assert job["status"] in ("queued", "running", "done")
    assert job["job_id"].startswith("job_")


def test_b2_run_missing_service_500(tmp_path: Path, jobs) -> None:
    """组合根缺 backtest_service → B1 显式 400（fail-loud，不静默）。"""
    config = make_config(tmp_path)
    container = ServiceContainer(
        config=config,
        lake=_FakeLake(),
        factor_service=FakeFactorService(),
        screen_service=FakeScreenService(),
        jobs=jobs,
    )
    from fastapi.testclient import TestClient
    from Kuantix.api.server import create_app

    app = create_app(config=config, services=container)
    with TestClient(app) as client:
        response = client.get("/api/v1/backtest/strategies")
    assert response.status_code == 400
    assert response.json()["code"] == 400


def test_b2_run_empty_codes_422(bt_client) -> None:
    response = bt_client.post(
        "/api/v1/backtest/run",
        json={"market": "CN", "codes": [], "strategy": "ma_cross"},
    )
    assert response.status_code == 400  # Pydantic min_length=1 → RequestValidationError
    assert response.json()["code"] == 400


def test_b2_run_all_load_failed_422(bt_client_empty) -> None:
    """所有标的读不到数据 → Job failed + error code 422（fail-loud，不静默空结果）。

    B2 返回 Job 信封（200）；后台执行体抛 DataIntegrityError，由 JobManager
    落 failed 态并记 error.code=422 —— 轮询 B3 可见。
    """
    import time

    resp = bt_client_empty.post(
        "/api/v1/backtest/run",
        json={"market": "CN", "codes": ["600000"], "strategy": "ma_cross"},
    )
    assert resp.status_code == 200
    job_id = resp.json()["data"]["job_id"]
    failed = False
    for _ in range(100):
        job = bt_client_empty.get(f"/api/v1/backtest/jobs/{job_id}").json()["data"]
        if job["status"] in ("failed", "done", "cancelled"):
            failed = job["status"] == "failed"
            break
        time.sleep(0.05)
    assert failed, "回测 job 未在超时内失败"
    assert job["error"] is not None
    assert job["error"]["code"] == 422


def test_b2_run_hk_501(bt_client) -> None:
    response = bt_client.post(
        "/api/v1/backtest/run",
        json={"market": "HK", "codes": ["600000"], "strategy": "ma_cross"},
    )
    assert response.status_code == 501
    assert response.json()["code"] == 501


# ---------------------------------------------------------------------------
# B3 进度 + B4 完整结果
# ---------------------------------------------------------------------------


def test_b3_b4_full_flow(bt_client) -> None:
    """B2 → 轮询 B3 → done 后 B4 返回完整结果。"""
    resp = bt_client.post(
        "/api/v1/backtest/run",
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

    # B3 轮询
    done = False
    for _ in range(100):
        job_payload = bt_client.get(f"/api/v1/backtest/jobs/{job_id}").json()
        assert_envelope(job_payload)
        status = job_payload["data"]["status"]
        if status in ("done", "failed", "cancelled"):
            done = status == "done"
            break
        import time

        time.sleep(0.05)
    assert done, "回测 job 未在超时内完成"

    # B4 完整结果
    result_payload = bt_client.get(f"/api/v1/backtest/results/{job_id}").json()
    assert_envelope(result_payload)
    result = result_payload["data"]
    assert result["strategy"] == "ma_cross"
    assert result["codes"] == ["600000", "600036"]
    assert set(result["per_code"].keys()) == {"600000", "600036"}
    assert len(result["combined"]["equity_curve"]) > 0
    perf = result["combined"]["performance"]
    for key in ("total_return", "annual_return", "max_drawdown", "sharpe"):
        assert key in perf
    # per-code 完整结果含净值序列与成交明细
    one = result["per_code"]["600000"]
    assert "equity_curve" in one
    assert "performance" in one
    assert "trades" in one


def test_b3_unknown_job_404(bt_client) -> None:
    response = bt_client.get("/api/v1/backtest/jobs/job_nope_123")
    assert response.status_code == 404
    assert response.json()["code"] == 404


def test_b4_unknown_job_404(bt_client) -> None:
    response = bt_client.get("/api/v1/backtest/results/job_nope_123")
    assert response.status_code == 404
    assert response.json()["code"] == 404


def test_b4_result_not_ready_404(bt_client, jobs) -> None:
    """job 存在但结果未落库 → 404（显式，不静默空结果）。"""
    from Kuantix.api.jobs import JobManager, JobStore

    # 构造一个无结果的 job：直接建 job 行但不在 backtest store 存结果
    job_id = "job_no_result_1"
    jobs.store.create(job_id, "backtest", "run", "CN", {"codes": ["600000"]})
    jobs.store.set_status(job_id, "running")
    response = bt_client.get(f"/api/v1/backtest/results/{job_id}")
    assert response.status_code == 404
    assert response.json()["code"] == 404


# ---------------------------------------------------------------------------
# v1.4：data_source 三模式（B2）+ B5 query
# ---------------------------------------------------------------------------


def _read_job_params(jobs, job_id: str) -> dict:
    """白盒：从 JobStore 读 params 列（B2 回显断言，契约 v1.4）。"""
    import json
    import sqlite3

    with sqlite3.connect(str(jobs.store._db_path)) as conn:
        row = conn.execute(
            "SELECT params FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
    assert row is not None, f"job {job_id} 不存在"
    return json.loads(row[0])


def test_b2_data_source_auto_default(bt_client, jobs) -> None:
    """默认 data_source=auto → job params 回显 auto（向后兼容既有调用方）。"""
    response = bt_client.post(
        "/api/v1/backtest/run",
        json={
            "market": "CN",
            "codes": ["600000"],
            "strategy": "ma_cross",
            "start": "2024-01-01",
            "end": "2024-12-31",
        },
    )
    assert response.status_code == 200
    job = response.json()["data"]
    assert _read_job_params(jobs, job["job_id"])["data_source"] == "auto"


def test_b2_data_source_local_ok(bt_client, jobs) -> None:
    """显式 local → job 正常提交（本地分支，假 reader 有数据）。"""
    response = bt_client.post(
        "/api/v1/backtest/run",
        json={
            "market": "CN",
            "codes": ["600000"],
            "strategy": "ma_cross",
            "start": "2024-01-01",
            "end": "2024-12-31",
            "data_source": "local",
        },
    )
    assert response.status_code == 200
    job = response.json()["data"]
    assert _read_job_params(jobs, job["job_id"])["data_source"] == "local"


def test_b2_data_source_live_single_ok(bt_client_live, jobs) -> None:
    """live + 单标的 → job done（假 fetcher，不发网络）。"""
    import time

    response = bt_client_live.post(
        "/api/v1/backtest/run",
        json={
            "market": "CN",
            "codes": ["600519"],
            "strategy": "ma_cross",
            "start": "2024-01-01",
            "end": "2024-12-31",
            "data_source": "live",
        },
    )
    assert response.status_code == 200
    job = response.json()["data"]
    assert _read_job_params(jobs, job["job_id"])["data_source"] == "live"
    job_id = job["job_id"]
    done = False
    for _ in range(100):
        status = bt_client_live.get(f"/api/v1/backtest/jobs/{job_id}").json()["data"]["status"]
        if status in ("done", "failed", "cancelled"):
            done = status == "done"
            break
        time.sleep(0.05)
    assert done, "live 单标的回测 job 未在超时内完成"


def test_b2_data_source_live_multi_422(bt_client_live) -> None:
    """live + 多标的 → 422 显式拒绝（D-3，路由层拦截，不提交 job）。"""
    response = bt_client_live.post(
        "/api/v1/backtest/run",
        json={
            "market": "CN",
            "codes": ["600519", "600036"],
            "strategy": "ma_cross",
            "start": "2024-01-01",
            "end": "2024-12-31",
            "data_source": "live",
        },
    )
    assert response.status_code == 422
    payload = response.json()
    assert payload["code"] == 422
    assert "单标的" in payload["message"]


def test_b2_data_source_invalid_400(bt_client) -> None:
    """data_source 非法 → 400（Literal 校验，fail-loud）。"""
    response = bt_client.post(
        "/api/v1/backtest/run",
        json={
            "market": "CN",
            "codes": ["600000"],
            "strategy": "ma_cross",
            "data_source": "bogus",
        },
    )
    assert response.status_code == 400
    assert response.json()["code"] == 400


def test_b5_data_source_live(bt_client_live) -> None:
    """B5 kline?data_source=live → 200（假 fetcher 出数据，不发网络）。"""
    response = bt_client_live.get(
        "/api/v1/backtest/kline/600519?data_source=live&start=2024-01-01&end=2024-12-31"
    )
    assert response.status_code == 200
    payload = response.json()
    assert_envelope(payload)
    data = payload["data"]
    assert data["code"] == "600519"
    assert len(data["kline"]) > 0


def test_b5_data_source_invalid_400(bt_client) -> None:
    """B5 data_source 非法 → 400（fail-loud）。"""
    response = bt_client.get(
        "/api/v1/backtest/kline/600000?data_source=bogus"
    )
    assert response.status_code == 400
    assert response.json()["code"] == 400
