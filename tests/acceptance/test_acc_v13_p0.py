"""T08 P0 收口独立验收（v1.3 组合回测 /portfolio + 策略库 /strategies）。

方法
----
- 与工程师单测（tests/unit/api/test_api_portfolio.py / test_api_strategies.py）
  **刻意错开**：本文件不 import 其任何测试模块 / Fake*，用**自己的假服务**
  与不同样本数据（不同价格序列、不同断言样本）；
- 真 TestClient + 真信封管道；P1/P3/S5 用真 PortfolioService/MultiStrategyService
  + 真 BacktestBridge（调上游 PortfolioBacktestEngine / MultiStrategyEngine）+ 假
  L1Reader（不发网络）+ 真 BacktestResultStore（tmp）；S1–S4 用真 StrategyStore
  （tmp，不触碰 ~/.Kuantix）；
- 每个 JSON 响应过 tests/redlines/envelope_validator（NF-9/NF-12）；
- D-8 金额求和语义：假引擎构造已知输入，验证组合净值 = 各标的金额求和
  （非归一化平均），total == 初始资金 + 收益求和；
- 持久化：StrategyStore 创建 → 重建实例（模拟重启）→ list 仍在；删除 → 消失；
  落库位置测试注入 tmp 路径。

红线自查：本文件无 ``except: pass`` / 双参 ``.get(k, 默认)``（R4）；全部离线。
"""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from Kuantix.api.deps import ServiceContainer
from Kuantix.api.jobs import JobManager, JobStore
from Kuantix.api.server import create_app
from Kuantix.backtest.portfolio_service import (
    MultiStrategyService,
    PortfolioService,
)
from Kuantix.backtest.store import BacktestResultStore
from Kuantix.backtest.strategy_store import STRATEGY_KINDS, StrategyStore
from Kuantix.config import Config, load_config


def _load_envelope_validator():
    """加载 tests/redlines/envelope_validator.py（避免污染 sys.path）。"""
    path = Path(__file__).resolve().parents[1] / "redlines" / "envelope_validator.py"
    spec = importlib.util.spec_from_file_location("envelope_validator_acc8", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VALIDATOR = _load_envelope_validator()


# ---------------------------------------------------------------------------
# 独立假服务（与工程师单测的 Fake* 不同样本）
# ---------------------------------------------------------------------------


class _FakeLake:
    """最小 DataLake 替身（本批次不触碰 data 业务端点）。"""

    def list_quarantine(self, market: str = "CN") -> list[Any]:
        return []


class _FakeFactor:
    """最小 FactorService 替身（本批次不触碰 factor 端点）。"""

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
    """最小 ScreenService 替身（本批次不触碰 screen 端点）。"""

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
    """构造路径全部指向 tmp 的配置（不触碰 ~/.Kuantix）。"""
    template = Path(__file__).resolve().parents[2] / "Kuantix" / "resources" / "config.default.toml"
    text = template.read_text(encoding="utf-8")
    text = text.replace('root = "~/.Kuantix"', f'root = "{tmp_path / "root"}"')
    for key in ("vipdoc", "factors", "db", "logs", "reports", "exports"):
        text = text.replace(f'{key} = "~/.Kuantix/{key}"', f'{key} = "{tmp_path / key}"')
    target = tmp_path / "config.toml"
    target.write_text(text, encoding="utf-8")
    return load_config(target)


# ---------------------------------------------------------------------------
# 假 L1Reader（确定性行情，不发网络）
# ---------------------------------------------------------------------------


class _RisingReader:
    """假 L1Reader：确定上涨行情（不同样本：指数式上升 + 噪声相位错开）。"""

    def __init__(self, n: int = 260) -> None:
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
    """假 L1Reader：所有标的读不到数据（测全部失败 fail-loud 422）。"""

    def read_daily_frame(self, exchange: str, code: str) -> pd.DataFrame:
        return pd.DataFrame(
            columns=["datetime", "open", "high", "low", "close", "vol", "amount"]
        )


def _make_p0_container(
    tmp_path: Path,
    *,
    reader=None,
    strategy_db: Path | None = None,
) -> tuple[ServiceContainer, Path, Path]:
    """组合根：真 Portfolio/MultiStrategyService + 真 StrategyStore + 真 Job。

    Returns:
        (container, backtest_results_db, strategies_db)
    """
    config = _make_config(tmp_path)
    jobs = JobManager(JobStore(config.paths.db))
    result_db = config.paths.db / "backtest_results.db"
    store = BacktestResultStore(result_db)
    reader = reader if reader is not None else _RisingReader()
    portfolio_service = PortfolioService(config, reader=reader, store=store)
    multi_service = MultiStrategyService(config, reader=reader, store=store)
    strategy_db = strategy_db if strategy_db is not None else config.paths.db / "strategies.db"
    strategy_store = StrategyStore(strategy_db)
    container = ServiceContainer(
        config=config,
        lake=_FakeLake(),
        factor_service=_FakeFactor(),
        screen_service=_FakeScreen(),
        jobs=jobs,
        portfolio_service=portfolio_service,
        multi_strategy_service=multi_service,
        strategy_store=strategy_store,
    )
    return container, result_db, strategy_db


@pytest.fixture()
def p0_client(tmp_path):
    container, _, _ = _make_p0_container(tmp_path)
    app = create_app(config=container.config, services=container)
    with TestClient(app) as c:
        yield c, container


@pytest.fixture()
def p0_client_empty(tmp_path):
    container, _, _ = _make_p0_container(tmp_path, reader=_EmptyReader())
    app = create_app(config=container.config, services=container)
    with TestClient(app) as c:
        yield c


def _wait_done(client, job_id: str, path: str = "/api/v1/portfolio/jobs") -> dict[str, Any]:
    """轮询 job 直到终态；返回 Job 字典。"""
    for _ in range(200):
        payload = client.get(f"{path}/{job_id}").json()
        assert VALIDATOR.validate_envelope(payload) == []
        status = payload["data"]["status"]
        if status in ("done", "failed", "cancelled"):
            return payload["data"]
        import time

        time.sleep(0.05)
    raise AssertionError("job 未在超时内结束")


def _p1_payload(**overrides: Any) -> dict[str, Any]:
    payload = {
        "market": "CN",
        "codes": ["600000", "600036"],
        "strategy": "ma_cross",
        "params": {"fast": 5, "slow": 20},
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
# A1) P1–P3 组合回测
# ---------------------------------------------------------------------------


def test_p1_run_returns_job(p0_client) -> None:
    """P1 正常 → Job(module=backtest, action=portfolio) + 信封合规。"""
    c, _ = p0_client
    r = c.post("/api/v1/portfolio/run", json=_p1_payload())
    assert r.status_code == 200
    body = r.json()
    assert VALIDATOR.validate_envelope(body) == []
    job = body["data"]
    assert job["module"] == "backtest"
    assert job["action"] == "portfolio"
    assert job["job_id"].startswith("job_")
    assert job["market"] == "CN"
    assert job["status"] in ("queued", "running", "done")


def test_p1_empty_codes_400(p0_client) -> None:
    """P1 空标的池 → 400（契约 §2.1c）。"""
    c, _ = p0_client
    r = c.post("/api/v1/portfolio/run", json=_p1_payload(codes=[]))
    assert r.status_code == 400
    assert r.json()["code"] == 400


def test_p1_whitespace_codes_400(p0_client) -> None:
    """P1 全空白标的 → 400（路由层 strip 后判空）。"""
    c, _ = p0_client
    r = c.post("/api/v1/portfolio/run", json=_p1_payload(codes=["  ", ""]))
    assert r.status_code == 400
    assert r.json()["code"] == 400


def test_p1_too_many_codes_400(p0_client) -> None:
    """P1 标的池超限（21 只）→ 400（契约 1..20）。"""
    c, _ = p0_client
    codes = [f"{600000 + i}" for i in range(21)]
    r = c.post("/api/v1/portfolio/run", json=_p1_payload(codes=codes))
    assert r.status_code == 400
    assert r.json()["code"] == 400


def test_p1_hk_501(p0_client) -> None:
    """P1 market=HK → 501（市场未启用）。"""
    c, _ = p0_client
    r = c.post("/api/v1/portfolio/run", json=_p1_payload(market="HK"))
    assert r.status_code == 501
    assert r.json()["code"] == 501


def test_p1_all_load_failed_422(p0_client_empty) -> None:
    """P1 全部标的读不到数据 → Job failed + error.code=422（fail-loud）。"""
    r = p0_client_empty.post(
        "/api/v1/portfolio/run", json=_p1_payload(codes=["600000"])
    )
    assert r.status_code == 200
    job_id = r.json()["data"]["job_id"]
    job = _wait_done(p0_client_empty, job_id)
    assert job["status"] == "failed"
    assert job["error"] is not None
    assert job["error"]["code"] == 422


def test_p2_unknown_job_404(p0_client) -> None:
    """P2 job 不存在 → 404。"""
    c, _ = p0_client
    r = c.get("/api/v1/portfolio/jobs/job_nope_404")
    assert r.status_code == 404
    assert r.json()["code"] == 404
    assert VALIDATOR.validate_envelope(r.json()) == []


def test_p3_unknown_job_404(p0_client) -> None:
    """P3 job 不存在 → 404。"""
    c, _ = p0_client
    r = c.get("/api/v1/portfolio/results/job_nope_404")
    assert r.status_code == 404
    assert r.json()["code"] == 404


def test_p3_result_not_ready_404(p0_client) -> None:
    """P3 job 存在但结果未落库 → 404（显式，不静默空结果）。"""
    c, container = p0_client
    job_id = "job_no_result_p0"
    container.jobs.store.create(job_id, "backtest", "portfolio", "CN", {"codes": ["600000"]})
    container.jobs.store.set_status(job_id, "running")
    r = c.get(f"/api/v1/portfolio/results/{job_id}")
    assert r.status_code == 404
    assert r.json()["code"] == 404


def test_p1_p2_p3_full_flow_dto(p0_client) -> None:
    """P1→P2 轮询→P3 完整 DTO：total_performance 五字段 + individual_results
    索引签名 + equity_allocation + combined_equity 含 drawdown_pct。"""
    c, _ = p0_client
    resp = c.post("/api/v1/portfolio/run", json=_p1_payload())
    job_id = resp.json()["data"]["job_id"]

    done = _wait_done(c, job_id)
    assert done["status"] == "done"
    summary = done["result_summary"]
    assert summary is not None
    for key in ("strategy", "codes", "result_count", "skipped_count", "total"):
        assert key in summary
    total = summary["total"]
    for key in ("total_return", "annual_return", "total_stocks", "total_cash", "combined_points"):
        assert key in total

    result_payload = c.get(f"/api/v1/portfolio/results/{job_id}").json()
    assert VALIDATOR.validate_envelope(result_payload) == []
    result = result_payload["data"]
    assert result["strategy"] == "ma_cross"

    # total_performance 关键字段（上游 Portfolio 口径 4 + 实际可含更多）
    tp = result["total_performance"]
    for key in ("total_return", "annual_return", "total_stocks", "total_cash"):
        assert key in tp
    assert tp["total_cash"] == pytest.approx(1_000_000.0)

    # individual_results：key=6 位 code，值为 PerCodeResult
    # （performance/equity_curve/trades/positions/config）
    ir = result["individual_results"]
    assert isinstance(ir, dict)
    assert set(ir.keys()) == {"600000", "600036"}
    one = ir["600000"]
    for key in ("performance", "equity_curve", "trades", "positions", "config"):
        assert key in one, f"individual_results 缺 {key}"
    assert len(one["equity_curve"]) > 0
    first_point = one["equity_curve"][0]
    assert set(first_point) >= {"datetime", "total", "drawdown", "drawdown_pct"}

    # equity_allocation：均分各 0.5
    alloc = result["equity_allocation"]
    assert alloc == {"600000": pytest.approx(0.5), "600036": pytest.approx(0.5)}
    assert abs(sum(alloc.values()) - 1.0) < 1e-6

    # combined_equity：含 drawdown_pct；datetime 为 YYYY-MM-DD
    combined = result["combined_equity"]
    assert len(combined) > 0
    cpoint = combined[0]
    assert set(cpoint) >= {"datetime", "total", "drawdown", "drawdown_pct"}
    assert isinstance(cpoint["datetime"], str)
    assert len(cpoint["datetime"]) == 10
    # 组合净值起点 = 初始资金（金额求和语义：各标的 cash/2 求和）
    assert combined[0]["total"] == pytest.approx(1_000_000.0, rel=1e-3)
    # 组合净值终点 = 初始资金 + 收益求和（各标的终值之和）
    ind_final = [ir[k]["equity_curve"][-1]["total"] for k in ("600000", "600036")]
    assert combined[-1]["total"] == pytest.approx(sum(ind_final), rel=1e-3)


def test_p1_d8_sum_semantics_known_input(p0_client) -> None:
    """【D-8 金额求和语义】用假引擎（注入固定结果）构造已知输入。

    组合净值 = 各标的金额求和（非归一化平均）；total == 初始资金 + 收益求和。
    这里绕过上游引擎，直接给 PortfolioService 注入返回已知 PortfolioResult 的
    假 bridge，验证服务层/路由层把「金额求和」结果原样透传（不做归一化重算）。
    """
    c, container = p0_client
    # 替换 service 的 bridge：返回已知金额结果（本金 50w×2，收益 +5w / -3w）
    class _KnownBridge:
        def run_portfolio_backtest(self, stocks, strategy, params, cash, **kw):
            # stocks: [(code, exchange, df), ...] 本假实现只看数量
            n = len(stocks)
            per = cash / n
            return {
                "total_performance": {
                    "total_return": 0.02,  # (50000-30000)/1000000
                    "annual_return": 0.02,
                    "total_stocks": n,
                    "total_cash": cash,
                },
                "individual_results": {
                    "600000": {
                        "performance": {"total_return": 0.1, "annual_return": 0.1},
                        "equity_curve": [
                            {
                                "datetime": "2024-01-02", "total": per,
                                "drawdown": 0.0, "drawdown_pct": 0.0,
                            },
                            {
                                "datetime": "2024-01-03", "total": per + 50000,
                                "drawdown": 0.0, "drawdown_pct": 0.0,
                            },
                        ],
                        "trades": [],
                        "positions": [],
                        "config": {"cash": per},
                    },
                    "600036": {
                        "performance": {"total_return": -0.06, "annual_return": -0.06},
                        "equity_curve": [
                            {
                                "datetime": "2024-01-02", "total": per,
                                "drawdown": 0.0, "drawdown_pct": 0.0,
                            },
                            {
                                "datetime": "2024-01-03", "total": per - 30000,
                                "drawdown": 0.0, "drawdown_pct": 0.0,
                            },
                        ],
                        "trades": [],
                        "positions": [],
                        "config": {"cash": per},
                    },
                },
                "equity_allocation": {"600000": 0.5, "600036": 0.5},
                "combined_equity": [
                    {
                        "datetime": "2024-01-02", "total": cash,
                        "drawdown": 0.0, "drawdown_pct": 0.0,
                    },
                    # 金额求和：50w+5w + 50w-3w = 102w = 初始资金 100w + 收益 2w
                    {
                        "datetime": "2024-01-03", "total": cash + 50000 - 30000,
                        "drawdown": 0.0, "drawdown_pct": 0.0,
                    },
                ],
            }

    container.portfolio_service._bridge = _KnownBridge()
    resp = c.post("/api/v1/portfolio/run", json=_p1_payload())
    job_id = resp.json()["data"]["job_id"]
    done = _wait_done(c, job_id)
    assert done["status"] == "done"

    result = c.get(f"/api/v1/portfolio/results/{job_id}").json()["data"]
    # 金额求和语义：组合终值 = 初始资金 + 收益求和（各标的金额求和，非归一化平均）
    total_cash = 1_000_000.0
    profit_sum = 50000 - 30000
    assert result["combined_equity"][0]["total"] == pytest.approx(total_cash)
    assert result["combined_equity"][-1]["total"] == pytest.approx(total_cash + profit_sum)
    # total_performance.total_cash = 初始资金
    assert result["total_performance"]["total_cash"] == pytest.approx(total_cash)
    # individual_results 各标的本金 = cash/N
    first_total = result["individual_results"]["600000"]["equity_curve"][0]["total"]
    assert first_total == pytest.approx(500000.0)
    second_total = result["individual_results"]["600036"]["equity_curve"][0]["total"]
    assert second_total == pytest.approx(500000.0)

# ---------------------------------------------------------------------------
# A2) S1–S4 策略库
# ---------------------------------------------------------------------------


def _s2_payload(**overrides: Any) -> dict[str, Any]:
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
    return payload


def test_s1_empty_list_pagination(p0_client) -> None:
    """S1 空库 → 分页壳（items/page/page_size/total/total_pages）。"""
    c, _ = p0_client
    r = c.get("/api/v1/strategies")
    assert r.status_code == 200
    body = r.json()
    assert VALIDATOR.validate_envelope(body) == []
    data = body["data"]
    assert data["items"] == []
    assert data["page"] == 1
    assert data["page_size"] == 50
    assert data["total"] == 0
    assert data["total_pages"] == 0


def test_s2_create_201_saved_strategy(p0_client) -> None:
    """S2 创建 → 201 + SavedStrategy（含服务端生成 id/created_at/updated_at/app_version）。"""
    c, _ = p0_client
    r = c.post("/api/v1/strategies", json=_s2_payload())
    assert r.status_code == 201
    body = r.json()
    assert VALIDATOR.validate_envelope(body) == []
    view = body["data"]
    assert view["id"].startswith("strat_")
    assert view["name"] == "双均线-茅台"
    assert view["kind"] == "single"
    assert view["strategy"] == "ma_cross"
    assert view["strategy_label"] == "双均线交叉"
    assert view["params"] == {"fast": 5, "slow": 20}
    assert view["context"] == {"symbol": "SH:600519"}
    assert view["trade_config"] == {"cash": 1000000}
    assert view["snapshot"] == {"total_return": 0.26}
    assert view["tags"] == ["优选"]
    assert view["notes"] == "测试"
    assert view["created_at"]
    assert view["updated_at"]
    assert view["app_version"]


def test_s2_invalid_json_400(p0_client) -> None:
    """S2 非法 JSON 请求体 → 400（请求参数校验失败）。"""
    c, _ = p0_client
    r = c.post(
        "/api/v1/strategies",
        content="{bad json",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 400
    assert r.json()["code"] == 400
    assert VALIDATOR.validate_envelope(r.json()) == []


def test_s2_missing_name_400(p0_client) -> None:
    """S2 缺 name → 400。"""
    c, _ = p0_client
    payload = _s2_payload()
    payload.pop("name")
    r = c.post("/api/v1/strategies", json=payload)
    assert r.status_code == 400
    assert r.json()["code"] == 400


def test_s2_invalid_kind_400(p0_client) -> None:
    """S2 kind 非法 → 400。"""
    c, _ = p0_client
    r = c.post("/api/v1/strategies", json=_s2_payload(kind="bogus"))
    assert r.status_code == 400
    assert r.json()["code"] == 400


def test_s1_kind_filter_and_pagination(p0_client) -> None:
    """S1 kind 过滤 + 分页。"""
    c, _ = p0_client
    for kind in ("single", "portfolio", "multi"):
        c.post("/api/v1/strategies", json=_s2_payload(name=f"策略-{kind}", kind=kind))

    r = c.get("/api/v1/strategies?kind=portfolio")
    data = r.json()["data"]
    assert data["total"] == 1
    assert data["items"][0]["kind"] == "portfolio"

    r = c.get("/api/v1/strategies")
    assert r.json()["data"]["total"] == 3

    r = c.get("/api/v1/strategies?page_size=2")
    data = r.json()["data"]
    assert data["total"] == 3
    assert data["total_pages"] == 2
    assert len(data["items"]) == 2

    # kind 非法 → 400（fail-loud）
    r = c.get("/api/v1/strategies?kind=bogus")
    assert r.status_code == 400
    assert r.json()["code"] == 400


def test_s3_get_and_unknown_404(p0_client) -> None:
    """S3 详情 + 不存在 → 404。"""
    c, _ = p0_client
    sid = c.post("/api/v1/strategies", json=_s2_payload()).json()["data"]["id"]
    r = c.get(f"/api/v1/strategies/{sid}")
    assert r.status_code == 200
    assert VALIDATOR.validate_envelope(r.json()) == []
    assert r.json()["data"]["id"] == sid

    r = c.get("/api/v1/strategies/strat_nope_123")
    assert r.status_code == 404
    assert r.json()["code"] == 404


def test_s4_delete_and_unknown_404(p0_client) -> None:
    """S4 删除 → {removed}；不存在 → 404（fail-loud）。"""
    c, _ = p0_client
    sid = c.post("/api/v1/strategies", json=_s2_payload()).json()["data"]["id"]
    r = c.delete(f"/api/v1/strategies/{sid}")
    assert r.status_code == 200
    assert r.json()["data"] == {"removed": sid}
    # 删除后再查 → 404
    assert c.get(f"/api/v1/strategies/{sid}").status_code == 404

    r = c.delete("/api/v1/strategies/strat_nope_123")
    assert r.status_code == 404
    assert r.json()["code"] == 404


# ---------------------------------------------------------------------------
# A3) S5 多策略组合回测
# ---------------------------------------------------------------------------


def _s5_payload(**overrides: Any) -> dict[str, Any]:
    payload = {
        "market": "CN",
        "items": [
            {
                "strategy": "ma_cross", "label": "双均线交叉",
                "code": "600000", "params": {"fast": 5, "slow": 20},
            },
            {"strategy": "macd", "label": "MACD", "code": "600036", "params": {}},
        ],
        "cash": 1000000,
        "commission": 0.0003,
        "min_commission": 5.0,
        "stamp_tax": 0.001,
        "slippage": 0.0,
        "execution": "next_open",
        "start": "2024-01-01",
        "end": "2024-12-31",
    }
    payload.update(overrides)
    return payload


def test_s5_run_returns_job(p0_client) -> None:
    """S5 正常 → Job(module=backtest, action=multi)。"""
    c, _ = p0_client
    r = c.post("/api/v1/strategies/run-multi", json=_s5_payload())
    assert r.status_code == 200
    body = r.json()
    assert VALIDATOR.validate_envelope(body) == []
    job = body["data"]
    assert job["module"] == "backtest"
    assert job["action"] == "multi"
    assert job["status"] in ("queued", "running", "done")


def test_s5_full_flow_key_format(p0_client) -> None:
    """S5 → done → 结果 key = {label}@{symbol}，金额求和结构同 PortfolioResult。"""
    c, _ = p0_client
    resp = c.post("/api/v1/strategies/run-multi", json=_s5_payload())
    job_id = resp.json()["data"]["job_id"]
    done = _wait_done(c, job_id, path="/api/v1/portfolio/jobs")
    assert done["status"] == "done"

    result = c.get(f"/api/v1/portfolio/results/{job_id}").json()["data"]
    keys = set(result["individual_results"].keys())
    assert "双均线交叉@SH:600000" in keys
    assert "MACD@SH:600036" in keys
    # 资金 1/N 均分
    alloc = result["equity_allocation"]
    assert alloc["双均线交叉@SH:600000"] == pytest.approx(0.5)
    assert alloc["MACD@SH:600036"] == pytest.approx(0.5)
    # combined_equity 含 drawdown_pct + datetime 日期格式
    combined = result["combined_equity"]
    assert len(combined) > 0
    assert set(combined[0]) >= {"datetime", "total", "drawdown", "drawdown_pct"}
    assert len(combined[0]["datetime"]) == 10
    # 组合起点 = 初始资金
    assert combined[0]["total"] == pytest.approx(1_000_000.0, rel=1e-3)


def test_s5_multi_result_readable_via_p3(p0_client) -> None:
    """【multi action 兼容性】S5 跑出来的 job 用 P3 拉——确认能返回。"""
    c, _ = p0_client
    resp = c.post("/api/v1/strategies/run-multi", json=_s5_payload())
    job_id = resp.json()["data"]["job_id"]
    done = _wait_done(c, job_id)
    assert done["status"] == "done"
    # P3 读取（薄转发到 BacktestResultStore，同一 job_id）
    r = c.get(f"/api/v1/portfolio/results/{job_id}")
    assert r.status_code == 200
    assert VALIDATOR.validate_envelope(r.json()) == []
    result = r.json()["data"]
    assert "individual_results" in result
    assert "combined_equity" in result


def test_s5_empty_items_400(p0_client) -> None:
    """S5 items 空 → 400。"""
    c, _ = p0_client
    r = c.post("/api/v1/strategies/run-multi", json=_s5_payload(items=[]))
    assert r.status_code == 400
    assert r.json()["code"] == 400


def test_s5_blank_items_400(p0_client) -> None:
    """S5 items 全空字段 → 400（路由层 strip 后判空）。"""
    c, _ = p0_client
    items = [{"strategy": "  ", "label": "", "code": ""}]
    r = c.post("/api/v1/strategies/run-multi", json=_s5_payload(items=items))
    assert r.status_code == 400
    assert r.json()["code"] == 400


def test_s5_hk_501(p0_client) -> None:
    """S5 market=HK → 501。"""
    c, _ = p0_client
    r = c.post("/api/v1/strategies/run-multi", json=_s5_payload(market="HK"))
    assert r.status_code == 501
    assert r.json()["code"] == 501


def test_s5_all_load_failed_422(p0_client_empty) -> None:
    """S5 全部槽位读不到数据 → Job failed + error.code=422。"""
    payload = {
        "market": "CN",
        "items": [{"strategy": "ma_cross", "label": "a", "code": "600000"}],
        "cash": 1000000,
        "start": "2024-01-01",
        "end": "2024-12-31",
    }
    r = p0_client_empty.post("/api/v1/strategies/run-multi", json=payload)
    assert r.status_code == 200
    job_id = r.json()["data"]["job_id"]
    job = _wait_done(p0_client_empty, job_id)
    assert job["status"] == "failed"
    assert job["error"] is not None
    assert job["error"]["code"] == 422


# ---------------------------------------------------------------------------
# A4) 策略库持久化（模拟重启）
# ---------------------------------------------------------------------------


def test_strategy_store_persist_across_reopen(tmp_path) -> None:
    """策略库持久化：创建 → 新实例（模拟重启）→ list 仍在；删除 → 消失。"""
    config = _make_config(tmp_path)
    db_path = config.paths.db / "strategies.db"

    store = StrategyStore(db_path)
    created = store.create(
        {
            "name": "持久化策略",
            "kind": "single",
            "strategy": "ma_cross",
            "strategy_label": "双均线交叉",
            "params": {"fast": 5, "slow": 20},
            "context": {"symbol": "SH:600519"},
            "trade_config": {"cash": 1000000},
            "snapshot": {"total_return": 0.26},
            "tags": ["优选"],
            "notes": "",
        },
        app_version="0.1.0",
    )
    sid = created["id"]
    store.close()

    # 模拟重启：新实例同一 db 文件
    reopened = StrategyStore(db_path)
    views = reopened.list(kind=None, page=1, page_size=50)
    reopened.close()
    assert views["total"] == 1
    assert views["items"][0]["id"] == sid
    assert views["items"][0]["name"] == "持久化策略"
    assert views["items"][0]["params"] == {"fast": 5, "slow": 20}
    assert views["items"][0]["context"] == {"symbol": "SH:600519"}

    # 删除 → 消失（再重启仍不在）
    store2 = StrategyStore(db_path)
    assert store2.delete(sid) is True
    store2.close()

    store3 = StrategyStore(db_path)
    views3 = store3.list(kind=None, page=1, page_size=50)
    store3.close()
    assert views3["total"] == 0


def test_strategy_store_db_location(tmp_path) -> None:
    """策略库落库位置：注入 tmp 路径，文件存在且为 strategies.db 语义。"""
    config = _make_config(tmp_path)
    db_path = config.paths.db / "strategies.db"
    store = StrategyStore(db_path)
    store.create(
        {
            "name": "落库验证",
            "kind": "single",
            "strategy": "ma_cross",
            "strategy_label": "双均线交叉",
            "params": {},
            "context": {},
            "trade_config": {},
            "snapshot": {},
            "tags": [],
            "notes": "",
        }
    )
    store.close()
    assert db_path.is_file()
    conn = sqlite3.connect(str(db_path))
    tables = [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]
    cols = [row[1] for row in conn.execute(
        "PRAGMA table_info(strategies)"
    ).fetchall()]
    conn.close()
    assert "strategies" in tables
    assert set(cols) >= {
        "id", "name", "kind", "strategy", "strategy_label", "params", "context",
        "trade_config", "snapshot", "tags", "notes", "created_at", "updated_at",
        "app_version",
    }


def test_strategy_store_kinds_enum() -> None:
    """STRATEGY_KINDS 枚举符合契约 single/portfolio/multi。"""
    assert STRATEGY_KINDS == ("single", "portfolio", "multi")


# ---------------------------------------------------------------------------
# A5) JobStore.list（R1.3-3，C1 依赖，P0 一并落地）
# ---------------------------------------------------------------------------


def test_jobstore_list_basic(tmp_path) -> None:
    """JobStore.list：按 created_at 倒序 + module/market/status 过滤。"""
    store = JobStore(tmp_path / "db")
    store.create("job_a", "backtest", "portfolio", "CN", {})
    store.create("job_b", "backtest", "multi", "CN", {})
    store.create("job_c", "factor", "compute", "CN", {})
    store.set_status("job_a", "done")

    rows = store.list(module="backtest", limit=10)
    assert [r["job_id"] for r in rows] == ["job_b", "job_a"]

    rows = store.list(module="backtest", status="done", limit=10)
    assert [r["job_id"] for r in rows] == ["job_a"]

    rows = store.list(module="backtest", status="queued", limit=10)
    assert [r["job_id"] for r in rows] == ["job_b"]


def test_jobstore_list_limit_and_status_validation(tmp_path) -> None:
    """JobStore.list：limit 越界 / status 非法 → 400（fail-loud）。"""
    from Kuantix.core.fail_loud import MissingKeyError

    store = JobStore(tmp_path / "db")
    store.create("job_a", "backtest", "portfolio", "CN", {})
    with pytest.raises(MissingKeyError):
        store.list(limit=0)
    with pytest.raises(MissingKeyError):
        store.list(limit=51)
    with pytest.raises(MissingKeyError):
        store.list(status="bogus")
