"""T09 P1 独立验收（v1.3 寻优 O1-O3 / 任务列表 C1 / K线下钻 B5 + 评级规格核对）。

方法
----
- 与工程师单测（tests/unit/api/test_api_optimize.py 等）**刻意错开**：本文件不
  import 其任何测试模块 / Fake*，用**自己的假服务**与不同样本数据；
- 真 TestClient + 真信封管道；O1-O3 用真 OptimizeService + 真 BacktestBridge
  （调上游 ParamGridOptimizer）+ 假 L1Reader（不发网络）+ 真 BacktestResultStore
  （tmp）；C1 用真 JobManager（tmp）；B5 用真 BacktestService + 真 bridge + 假 reader；
- 每个 JSON 响应过 tests/redlines/envelope_validator（NF-9/NF-12）；
- run_optimize 桥方法：monkeypatch 上游 ParamGridOptimizer 验证参数透传 +
  heatmap 结构 + 网格超限拦截；
- 评级数据支撑：B4 performance 抽查评级所需 19 项指标字段存在。

红线自查：本文件无 ``except: pass`` / 双参 ``.get(k, 默认)``（R4）；全部离线。
"""
from __future__ import annotations

import importlib.util
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from Kuantix.api.deps import ServiceContainer
from Kuantix.api.jobs import JobManager, JobStore
from Kuantix.api.server import create_app
from Kuantix.backtest.optimize_service import OptimizeService
from Kuantix.backtest.store import BacktestResultStore
from Kuantix.config import Config, load_config


def _load_envelope_validator():
    path = Path(__file__).resolve().parents[1] / "redlines" / "envelope_validator.py"
    spec = importlib.util.spec_from_file_location("envelope_validator_acc9", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VALIDATOR = _load_envelope_validator()


# ---------------------------------------------------------------------------
# 独立假服务（与工程师单测的 Fake* 不同样本）
# ---------------------------------------------------------------------------


class _FakeLake:
    def list_quarantine(self, market: str = "CN") -> list[Any]:
        return []


class _FakeFactor:
    def __init__(self) -> None:
        self._factors: list[str] = []

    def list_factors(self) -> list[str]:
        return list(self._factors)

    def list_models(self) -> list[str]:
        return []

    def list_model_handles(self) -> list[Any]:
        return []

    def compute_factors(self, req: Any) -> list[Any]:
        return []

    def report(self, factor: str, market: str = "CN") -> dict[str, Any]:
        return {}

    def combine(self, factors, method, *, name=None, save_model=False, market="CN"):
        return None

    def load_model(self, name: str) -> Any:
        raise LookupError(f"model {name} not found")


class _FakeScreen:
    def list_batches(self, market=None, page=1, page_size=50):
        return {"items": [], "page": page, "page_size": page_size, "total": 0, "total_pages": 0}

    def get_batch(self, batch_id: str):
        return None

    def get_batch_results(self, batch_id, page=1, page_size=50, sort_by="score", order="desc"):
        return None

    def export_json_payload(self, batch_id: str):
        return None

    def export_csv_bytes(self, batch_id: str):
        return None

    def run_batch(self, req, *, pool_codes=None, excluded_codes=None, filters=None, combine="and"):
        return None


def _make_config(tmp_path: Path) -> Config:
    template = Path(__file__).resolve().parents[2] / "Kuantix" / "resources" / "config.default.toml"
    text = template.read_text(encoding="utf-8")
    text = text.replace('root = "~/.Kuantix"', f'root = "{tmp_path / "root"}"')
    for key in ("vipdoc", "factors", "db", "logs", "reports", "exports"):
        text = text.replace(f'{key} = "~/.Kuantix/{key}"', f'{key} = "{tmp_path / key}"')
    target = tmp_path / "config.toml"
    target.write_text(text, encoding="utf-8")
    return load_config(target)


class _RisingReader:
    """假 L1Reader：确定上涨行情（独立样本：指数式上升 + 噪声相位错开）。"""

    def __init__(self, n: int = 300) -> None:
        self._n = n

    def read_daily_frame(self, exchange: str, code: str) -> pd.DataFrame:
        dates = pd.bdate_range("2024-01-02", periods=self._n)
        base = 10.0 if exchange == "sh" else 20.0
        close = base * np.linspace(1.0, 1.6, self._n) + np.sin(
            np.arange(self._n) / 9.0 + int(code) % 7
        ) * 0.4
        return pd.DataFrame(
            {
                "datetime": dates,
                "open": close * 0.994,
                "high": close * 1.02,
                "low": close * 0.98,
                "close": close,
                "vol": np.full(self._n, 15000.0),
                "amount": np.full(self._n, 1.5e7),
            }
        )


class _EmptyReader:
    """假 L1Reader：所有标的读不到数据（测 fail-loud 422）。"""

    def read_daily_frame(self, exchange: str, code: str) -> pd.DataFrame:
        return pd.DataFrame(
            columns=["datetime", "open", "high", "low", "close", "vol", "amount"]
        )


def _make_p1_container(
    tmp_path: Path,
    *,
    reader=None,
    optimize_reader=None,
) -> tuple[ServiceContainer, Path]:
    config = _make_config(tmp_path)
    jobs = JobManager(JobStore(config.paths.db))
    result_db = config.paths.db / "backtest_results.db"
    store = BacktestResultStore(result_db)
    reader = reader if reader is not None else _RisingReader()
    opt_reader = optimize_reader if optimize_reader is not None else reader

    from Kuantix.backtest.service import BacktestService

    backtest_service = BacktestService(config, reader=reader, store=store)
    optimize_service = OptimizeService(config, reader=opt_reader, store=store)
    container = ServiceContainer(
        config=config,
        lake=_FakeLake(),
        factor_service=_FakeFactor(),
        screen_service=_FakeScreen(),
        jobs=jobs,
        backtest_service=backtest_service,
        optimize_service=optimize_service,
    )
    return container, result_db


@pytest.fixture()
def p1_client(tmp_path):
    container, _ = _make_p1_container(tmp_path)
    app = create_app(config=container.config, services=container)
    with TestClient(app) as c:
        yield c, container


@pytest.fixture()
def p1_client_empty(tmp_path):
    container, _ = _make_p1_container(
        tmp_path, reader=_EmptyReader(), optimize_reader=_EmptyReader()
    )
    app = create_app(config=container.config, services=container)
    with TestClient(app) as c:
        yield c


def _wait_done(client, job_id: str, path: str = "/api/v1/optimize/jobs") -> dict[str, Any]:
    for _ in range(300):
        payload = client.get(f"{path}/{job_id}").json()
        assert VALIDATOR.validate_envelope(payload) == []
        status = payload["data"]["status"]
        if status in ("done", "failed", "cancelled"):
            return payload["data"]
        time.sleep(0.05)
    raise AssertionError("job 未在超时内结束")


def _o1_payload(**overrides: Any) -> dict[str, Any]:
    payload = {
        "market": "CN",
        "code": "600000",
        "strategy": "ma_cross",
        "param_grid": {"fast": [5, 10, 20], "slow": [10, 20, 30]},
        "start": "2024-01-01",
        "end": "2024-12-31",
        "cash": 1000000,
        "commission": 0.0003,
        "min_commission": 5.0,
        "stamp_tax": 0.001,
        "slippage": 0.0,
        "execution": "next_open",
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# A1) O1–O3 参数寻优
# ---------------------------------------------------------------------------


def test_o1_run_returns_job(p1_client) -> None:
    """O1 正常 → Job(module=backtest, action=optimize)。"""
    c, _ = p1_client
    r = c.post("/api/v1/optimize/run", json=_o1_payload())
    assert r.status_code == 200
    body = r.json()
    assert VALIDATOR.validate_envelope(body) == []
    job = body["data"]
    assert job["module"] == "backtest"
    assert job["action"] == "optimize"
    assert job["status"] in ("queued", "running", "done")
    assert job["job_id"].startswith("job_")


def test_o1_code_empty_400(p1_client) -> None:
    c, _ = p1_client
    r = c.post("/api/v1/optimize/run", json=_o1_payload(code="  "))
    assert r.status_code == 400
    assert r.json()["code"] == 400


def test_o1_param_grid_empty_400(p1_client) -> None:
    c, _ = p1_client
    r = c.post("/api/v1/optimize/run", json=_o1_payload(param_grid={}))
    assert r.status_code == 400
    assert r.json()["code"] == 400


def test_o1_param_grid_too_many_params_400(p1_client) -> None:
    c, _ = p1_client
    r = c.post(
        "/api/v1/optimize/run",
        json=_o1_payload(param_grid={"a": [1], "b": [1], "c": [1]}),
    )
    assert r.status_code == 400
    assert r.json()["code"] == 400


def test_o1_param_grid_empty_values_400(p1_client) -> None:
    c, _ = p1_client
    r = c.post("/api/v1/optimize/run", json=_o1_payload(param_grid={"fast": []}))
    assert r.status_code == 400
    assert r.json()["code"] == 400


def test_o1_grid_over_200_400(p1_client) -> None:
    """网格笛卡尔积 >200 → 400（后端二次校验，不依赖前端预校验）。"""
    c, _ = p1_client
    # 15 × 15 = 225 > 200
    r = c.post(
        "/api/v1/optimize/run",
        json=_o1_payload(param_grid={"fast": list(range(1, 16)), "slow": list(range(5, 20))}),
    )
    assert r.status_code == 400
    assert r.json()["code"] == 400


def test_o1_market_hk_501(p1_client) -> None:
    c, _ = p1_client
    r = c.post("/api/v1/optimize/run", json=_o1_payload(market="HK"))
    assert r.status_code == 501
    assert r.json()["code"] == 501


def test_o1_kline_read_failed_422(p1_client_empty) -> None:
    """K 线读失败 → job failed error.code=422（fail-loud）。"""
    r = p1_client_empty.post("/api/v1/optimize/run", json=_o1_payload(code="600000"))
    assert r.status_code == 200
    job_id = r.json()["data"]["job_id"]
    job = _wait_done(p1_client_empty, job_id)
    assert job["status"] == "failed"
    assert job["error"] is not None
    assert job["error"]["code"] == 422


def test_o2_unknown_job_404(p1_client) -> None:
    c, _ = p1_client
    r = c.get("/api/v1/optimize/jobs/job_nope_404")
    assert r.status_code == 404
    assert r.json()["code"] == 404
    assert VALIDATOR.validate_envelope(r.json()) == []


def test_o3_unknown_job_404(p1_client) -> None:
    c, _ = p1_client
    r = c.get("/api/v1/optimize/results/job_nope_404")
    assert r.status_code == 404
    assert r.json()["code"] == 404


def test_o3_result_not_ready_404(p1_client) -> None:
    """job 存在但结果未落库 → 404（显式）。"""
    c, container = p1_client
    job_id = "job_no_result_o3"
    container.jobs.store.create(job_id, "backtest", "optimize", "CN", {})
    container.jobs.store.set_status(job_id, "running")
    r = c.get(f"/api/v1/optimize/results/{job_id}")
    assert r.status_code == 404
    assert r.json()["code"] == 404


def test_o1_o2_o3_full_flow_dto(p1_client) -> None:
    """O1→O2 轮询→O3 完整 DTO：strategy/param_names/results[降序]/best/heatmap。"""
    c, _ = p1_client
    resp = c.post("/api/v1/optimize/run", json=_o1_payload())
    job_id = resp.json()["data"]["job_id"]

    done = _wait_done(c, job_id)
    assert done["status"] == "done"
    summary = done["result_summary"]
    assert summary is not None
    assert summary["action"] == "optimize"
    assert summary["grid_size"] == 9
    assert set(summary["param_names"]) == {"fast", "slow"}
    assert "best" in summary

    result_payload = c.get(f"/api/v1/optimize/results/{job_id}").json()
    assert VALIDATOR.validate_envelope(result_payload) == []
    result = result_payload["data"]
    assert result["strategy"] == "ma_cross"
    assert set(result["param_names"]) == {"fast", "slow"}

    results = result["results"]
    assert len(results) == 9
    # results 按 total_return 降序
    returns = [r["total_return"] for r in results]
    assert returns == sorted(returns, reverse=True)

    best = result["best"]
    assert best is not None
    assert best["params"] == results[0]["params"]
    assert best["total_return"] == results[0]["total_return"]

    heatmap = result["heatmap"]
    assert heatmap is not None
    # 契约 §3.8（v1.3 R1.3-5）：heatmap = {x_name, y_name, x, y, data}
    # data = [x_idx, y_idx, value]（上游 ParamGridOptimizer 原生结构）
    assert set(heatmap) >= {"x_name", "y_name", "x", "y", "data"}
    assert len(heatmap["x"]) == 3  # fast 3 个取值
    assert len(heatmap["y"]) == 3  # slow 3 个取值
    assert len(heatmap["data"]) == 9  # 每个网格点一行 [x_idx, y_idx, value]
    assert all(len(row) == 3 for row in heatmap["data"])


def test_o1_single_param_heatmap_null(p1_client) -> None:
    """单参数寻优：heatmap 为 null（前端改画折线/柱状）。"""
    c, _ = p1_client
    r = c.post("/api/v1/optimize/run", json=_o1_payload(param_grid={"fast": [5, 10, 20]}))
    job_id = r.json()["data"]["job_id"]
    done = _wait_done(c, job_id)
    assert done["status"] == "done"
    result = c.get(f"/api/v1/optimize/results/{job_id}").json()["data"]
    assert result["heatmap"] is None
    assert len(result["results"]) == 3


# ---------------------------------------------------------------------------
# A2) run_optimize 桥方法（参数透传 + heatmap 结构 + 网格超限拦截）
# ---------------------------------------------------------------------------


class _RecordingGridOptimizer:
    """记录构造参数的假 ParamGridOptimizer（monkeypatch 上游类）。"""

    last_kwargs: dict[str, Any] | None = None

    def __init__(self, **kwargs: Any) -> None:
        _RecordingGridOptimizer.last_kwargs = dict(kwargs)
        self._kwargs = kwargs

    def run(self) -> Any:
        kw = self._kwargs
        pgrid = kw["param_grid"]
        names = list(pgrid.keys())
        vals0 = pgrid[names[0]]
        vals1 = pgrid[names[1]] if len(names) > 1 else [0]
        rows = [
            {
                "params": dict(zip(names, (vals0[0], vals1[0]), strict=True)),
                "total_return": 0.2,
            },
            {
                "params": dict(zip(names, (vals0[1], vals1[1]), strict=True)),
                "total_return": 0.1,
            },
        ]
        heatmap = (
            {
                "x_name": names[0],
                "y_name": names[1],
                "x": list(vals0),
                "y": list(vals1),
                "data": [[0, 0, 0.2], [1, 1, 0.1]],
            }
            if len(names) == 2
            else None
        )

        class _R:
            def to_dict(self) -> dict[str, Any]:
                return {
                    "strategy": kw["strategy_name"],
                    "param_names": names,
                    "results": rows,
                    "best": rows[0],
                    "heatmap": heatmap,
                }

        return _R()


def test_run_optimize_bridge_param_passthrough(tmp_path, monkeypatch) -> None:
    """run_optimize：策略名/参数网格/成本配置透传 + heatmap 结构。"""
    import Kuantix.adapters.backtest_bridge as bb_module

    container, _ = _make_p1_container(tmp_path)
    monkeypatch.setattr(bb_module, "ParamGridOptimizer", _RecordingGridOptimizer)

    result = container.optimize_service._bridge.run_optimize(
        _RisingReader().read_daily_frame("sh", "600000"),
        "ma_cross",
        {"fast": [5, 10], "slow": [10, 20]},
        cash=500000,
        commission=0.0005,
        min_commission=6.0,
        stamp_tax=0.002,
        slippage=0.001,
        execution="next_close",
    )
    kw = _RecordingGridOptimizer.last_kwargs
    assert kw is not None
    assert kw["strategy_name"] == "ma_cross"
    assert kw["param_grid"] == {"fast": [5, 10], "slow": [10, 20]}
    assert kw["cash"] == 500000
    assert kw["commission"] == 0.0005
    assert kw["min_commission"] == 6.0
    assert kw["stamp_tax"] == 0.002
    assert kw["slippage"] == 0.001
    assert kw["execution"] == "next_close"

    assert result["strategy"] == "ma_cross"
    assert result["param_names"] == ["fast", "slow"]
    assert set(result["heatmap"]) >= {"x_name", "y_name", "x", "y", "data"}


def test_run_optimize_bridge_grid_over_limit(tmp_path) -> None:
    """run_optimize：网格超限 → DataIntegrityError（fail-loud，路由层 400 之外的第二道防线）。"""
    from Kuantix.core.fail_loud import DataIntegrityError

    container, _ = _make_p1_container(tmp_path)
    with pytest.raises(DataIntegrityError):
        container.optimize_service._bridge.run_optimize(
            _RisingReader().read_daily_frame("sh", "600000"),
            "ma_cross",
            {"fast": list(range(1, 16)), "slow": list(range(5, 20))},
            cash=1000000,
        )


def test_run_optimize_bridge_empty_grid(tmp_path) -> None:
    from Kuantix.core.fail_loud import DataIntegrityError

    container, _ = _make_p1_container(tmp_path)
    with pytest.raises(DataIntegrityError):
        container.optimize_service._bridge.run_optimize(
            _RisingReader().read_daily_frame("sh", "600000"),
            "ma_cross",
            {},
        )


# ---------------------------------------------------------------------------
# A3) C1 回测任务列表
# ---------------------------------------------------------------------------


def test_c1_default_all_status(p1_client) -> None:
    """C1 默认返回全部 status + {items, count}。"""
    c, container = p1_client
    container.jobs.store.create("job_c1_a", "backtest", "run", "CN", {})
    container.jobs.store.create("job_c1_b", "backtest", "portfolio", "CN", {})
    container.jobs.store.create("job_c1_c", "factor", "compute", "CN", {})
    container.jobs.store.set_status("job_c1_a", "done")

    r = c.get("/api/v1/backtest/jobs")
    assert r.status_code == 200
    body = r.json()
    assert VALIDATOR.validate_envelope(body) == []
    data = body["data"]
    assert set(data) >= {"items", "count"}
    # 默认 module=backtest → 排除 factor 任务
    ids = [j["job_id"] for j in data["items"]]
    assert "job_c1_c" not in ids
    assert len(data["items"]) == data["count"]


def test_c1_status_filter(p1_client) -> None:
    c, container = p1_client
    container.jobs.store.create("job_c1_d", "backtest", "run", "CN", {})
    container.jobs.store.set_status("job_c1_d", "done")

    r = c.get("/api/v1/backtest/jobs?status=done")
    data = r.json()["data"]
    assert all(j["status"] == "done" for j in data["items"])
    assert r.json()["meta"]["market"] == "CN"


def test_c1_limit_invalid_400(p1_client) -> None:
    c, _ = p1_client
    r = c.get("/api/v1/backtest/jobs?limit=0")
    assert r.status_code == 400
    assert r.json()["code"] == 400
    r = c.get("/api/v1/backtest/jobs?limit=51")
    assert r.status_code == 400
    assert r.json()["code"] == 400


def test_c1_status_invalid_400(p1_client) -> None:
    c, _ = p1_client
    r = c.get("/api/v1/backtest/jobs?status=bogus")
    assert r.status_code == 400
    assert r.json()["code"] == 400


def test_c1_module_filter(p1_client) -> None:
    c, container = p1_client
    container.jobs.store.create("job_c1_m", "factor", "compute", "CN", {})
    r = c.get("/api/v1/backtest/jobs?module=factor")
    data = r.json()["data"]
    assert all(j["module"] == "factor" for j in data["items"])


# ---------------------------------------------------------------------------
# A4) B5 K 线下钻
# ---------------------------------------------------------------------------


def test_b5_kline_with_signals(p1_client) -> None:
    """B5 正常：kline 数组(date/open/high/low/close/vol/amount) + buy/sell_points。"""
    c, _ = p1_client
    r = c.get("/api/v1/backtest/kline/600000")
    assert r.status_code == 200
    body = r.json()
    assert VALIDATOR.validate_envelope(body) == []
    data = body["data"]
    assert data["code"] == "600000"
    assert data["market"] == "CN"
    assert data["strategy"] == "ma_cross"
    assert isinstance(data["kline"], list)
    assert len(data["kline"]) > 0
    bar = data["kline"][0]
    for key in ("date", "open", "high", "low", "close", "vol", "amount"):
        assert key in bar, f"kline bar 缺 {key}"
    assert isinstance(bar["date"], str)
    assert len(bar["date"]) == 10
    for key in ("open", "high", "low", "close"):
        assert isinstance(bar[key], (int, float))
    assert "buy_points" in data and "sell_points" in data
    assert isinstance(data["buy_points"], list)
    assert isinstance(data["sell_points"], list)


def test_b5_code_invalid_400(p1_client) -> None:
    c, _ = p1_client
    r = c.get("/api/v1/backtest/kline/abc")
    assert r.status_code == 400
    assert r.json()["code"] == 400


def test_b5_no_data_404(p1_client_empty) -> None:
    """无 K 线数据 → 404（显式）。"""
    r = p1_client_empty.get("/api/v1/backtest/kline/600000")
    assert r.status_code == 404
    assert r.json()["code"] == 404


def test_b5_hk_501(p1_client) -> None:
    c, _ = p1_client
    r = c.get("/api/v1/backtest/kline/600000?market=HK")
    assert r.status_code == 501
    assert r.json()["code"] == 501


def test_b5_buy_sell_points_signal_format(p1_client) -> None:
    """B5 买卖点为 {date, price} 信号标注（非下单动作，R5）。"""
    c, _ = p1_client
    data = c.get("/api/v1/backtest/kline/600000").json()["data"]
    for point in data["buy_points"] + data["sell_points"]:
        assert "date" in point and "price" in point
        assert isinstance(point["date"], str)
        assert len(point["date"]) == 10


# ---------------------------------------------------------------------------
# A5) 评级数据支撑（19 项指标抽查）
# ---------------------------------------------------------------------------


def test_b4_performance_has_rating_metrics(p1_client) -> None:
    """B4 performance 含评级所需 19 项指标
    （calmar/sharpe/max_drawdown/win_rate/profit_factor 等）。"""
    c, _ = p1_client
    r = c.post(
        "/api/v1/backtest/run",
        json={
            "market": "CN",
            "codes": ["600000"],
            "strategy": "ma_cross",
            "params": {"fast": 5, "slow": 20},
            "start": "2024-01-01",
            "end": "2024-12-31",
            "cash": 1000000,
        },
    )
    job_id = r.json()["data"]["job_id"]
    done = _wait_done(c, job_id, path="/api/v1/backtest/jobs")
    assert done["status"] == "done"

    result = c.get(f"/api/v1/backtest/results/{job_id}").json()["data"]
    perf = result["per_code"]["600000"]["performance"]
    rating_keys = {
        "calmar",
        "sharpe",
        "sortino",
        "max_drawdown",
        "volatility",
        "win_rate",
        "profit_factor",
        "total_trades",
        "annual_return",
        "total_return",
    }
    missing = rating_keys - set(perf.keys())
    assert not missing, f"B4 performance 缺评级所需字段: {missing}"
    # 评级单标的 6 维全部可取值
    for key in ("calmar", "max_drawdown", "win_rate", "profit_factor", "sharpe", "volatility"):
        assert key in perf


def test_o3_best_has_rating_fields(p1_client) -> None:
    """O3 best 含评级（gradeGridPoint）所需 4 字段。"""
    c, _ = p1_client
    r = c.post("/api/v1/optimize/run", json=_o1_payload())
    job_id = r.json()["data"]["job_id"]
    done = _wait_done(c, job_id)
    assert done["status"] == "done"
    best = c.get(f"/api/v1/optimize/results/{job_id}").json()["data"]["best"]
    for key in (
        "params", "total_return", "sharpe", "max_drawdown",
        "total_trades", "win_rate", "profit_factor",
    ):
        assert key in best, f"best 缺 {key}"
