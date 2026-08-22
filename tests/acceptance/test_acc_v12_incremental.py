"""T06 增量修复独立验收（v1.2 四项：D8 搜索 / B1–B4 回测 / 后端持久化）。

方法
----
- 与工程师单测（tests/unit/api/test_api_backtest.py 等）**刻意错开**：本文件
  不 import 其任何测试模块 / Fake*，用**自己的假服务**与不同样本数据；
- 真 TestClient + 真信封管道；D8 用真 SecuritySearchService（注入假证券清单
  provider，不发网络）；B1–B4 用真 BacktestService + 真 BacktestBridge（调
  上游引擎）+ 假 L1Reader（不发网络）+ 真 BacktestResultStore（tmp）；
- 每个 JSON 响应过 tests/redlines/envelope_validator（NF-9/NF-12）。

红线自查：本文件无 ``except: pass`` / 双参 ``.get(k, 默认)``（R4）；全部离线。
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
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
from Kuantix.backtest.service import BacktestService
from Kuantix.backtest.store import BacktestResultStore
from Kuantix.config import Config, load_config
from Kuantix.core.contracts import Security


def _load_envelope_validator():
    """加载 tests/redlines/envelope_validator.py（避免污染 sys.path）。"""
    path = Path(__file__).resolve().parents[1] / "redlines" / "envelope_validator.py"
    spec = importlib.util.spec_from_file_location("envelope_validator_acc6", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VALIDATOR = _load_envelope_validator()


# ---------------------------------------------------------------------------
# 独立假服务（与工程师单测的 Fake* 不同样本）
# ---------------------------------------------------------------------------


class _FakeLake:
    """最小 DataLake 替身（D8/B 用例不触碰 data 业务端点）。"""

    def __init__(self) -> None:
        self._codes: set[str] = set()

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
# D8 搜索假证券清单（含浦发银行 600000 / 平安银行 000001）
# ---------------------------------------------------------------------------

_FAKE_SECURITIES: list[Security] = [
    Security(code="600000", exchange="sh", market="CN", security_type="SH_A_STOCK", name="浦发银行"),
    Security(code="600036", exchange="sh", market="CN", security_type="SH_A_STOCK", name="招商银行"),
    Security(code="000001", exchange="sz", market="CN", security_type="SZ_A_STOCK", name="平安银行"),
    Security(code="000002", exchange="sz", market="CN", security_type="SZ_A_STOCK", name="万科A"),
    Security(code="600519", exchange="sh", market="CN", security_type="SH_A_STOCK", name="贵州茅台"),
    Security(code="510300", exchange="sh", market="CN", security_type="SH_ETF", name="沪深300ETF"),
]


def _fake_search_provider() -> list[Security]:
    return list(_FAKE_SECURITIES)


def _make_search_container(tmp_path: Path, *, provider=None, cache_path=None) -> ServiceContainer:
    from Kuantix.data.security_search import SecuritySearchService

    config = _make_config(tmp_path)
    jobs = JobManager(JobStore(config.paths.db))
    search = SecuritySearchService(
        config,
        provider=provider if provider is not None else _fake_search_provider,
        cache_path=cache_path,
    )
    return ServiceContainer(
        config=config,
        lake=_FakeLake(),
        factor_service=_FakeFactor(),
        screen_service=_FakeScreen(),
        jobs=jobs,
        security_search=search,
    )


@pytest.fixture()
def search_client(tmp_path):
    container = _make_search_container(tmp_path)
    app = create_app(config=container.config, services=container)
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# B1–B4 回测假 reader（上升趋势日线，独立样本）
# ---------------------------------------------------------------------------


class _FakeReader:
    """假 L1Reader：确定性上涨行情（含 datetime 列），全程不发网络。"""

    def read_daily_frame(self, exchange: str, code: str) -> pd.DataFrame:
        n = 250
        dates = pd.bdate_range("2024-01-02", periods=n)
        close = np.linspace(10.0, 24.0, n) + np.sin(np.arange(n) / 12.0) * 0.8
        return pd.DataFrame(
            {
                "datetime": dates,
                "open": close * 0.995,
                "high": close * 1.015,
                "low": close * 0.985,
                "close": close,
                "vol": np.full(n, 12000.0),
                "amount": np.full(n, 1.2e7),
            }
        )


class _EmptyReader:
    """假 L1Reader：所有标的读不到数据（测全部失败 fail-loud）。"""

    def read_daily_frame(self, exchange: str, code: str) -> pd.DataFrame:
        return pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "vol", "amount"])


def _make_backtest_container(tmp_path: Path, *, reader=None) -> tuple[ServiceContainer, Path]:
    config = _make_config(tmp_path)
    jobs = JobManager(JobStore(config.paths.db))
    db_path = tmp_path / "db" / "backtest_results.db"
    store = BacktestResultStore(db_path)
    bt = BacktestService(
        config,
        reader=reader if reader is not None else _FakeReader(),
        store=store,
    )
    container = ServiceContainer(
        config=config,
        lake=_FakeLake(),
        factor_service=_FakeFactor(),
        screen_service=_FakeScreen(),
        jobs=jobs,
        backtest_service=bt,
    )
    return container, db_path


@pytest.fixture()
def bt_client(tmp_path):
    container, _ = _make_backtest_container(tmp_path)
    app = create_app(config=container.config, services=container)
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def bt_client_empty(tmp_path):
    container, _ = _make_backtest_container(tmp_path, reader=_EmptyReader())
    app = create_app(config=container.config, services=container)
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# 1) D8 证券搜索
# ---------------------------------------------------------------------------


def test_d8_code_exact_hits_pufa(search_client) -> None:
    """q=600000 → 浦发银行（代码精确，含 exchange/security_type）。"""
    r = search_client.get("/api/v1/data/search", params={"q": "600000"})
    assert r.status_code == 200
    body = r.json()
    assert VALIDATOR.validate_envelope(body) == []
    data = body["data"]
    assert data["count"] == 1
    hit = data["items"][0]
    assert hit["code"] == "600000"
    assert hit["name"] == "浦发银行"
    assert hit["exchange"] == "sh"
    assert hit["market"] == "CN"
    assert hit["security_type"] == "SH_A_STOCK"


def test_d8_code_prefix_hits(search_client) -> None:
    """q=6000 → 代码前缀命中 600000/600036/600519/510300。"""
    r = search_client.get("/api/v1/data/search", params={"q": "6000"})
    assert r.status_code == 200
    codes = {h["code"] for h in r.json()["data"]["items"]}
    assert "600000" in codes
    assert "600036" in codes


def test_d8_name_fuzzy_hits_pufa(search_client) -> None:
    """q=浦发 → 浦发银行（名称模糊）。"""
    r = search_client.get("/api/v1/data/search", params={"q": "浦发"})
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["count"] == 1
    assert data["items"][0]["code"] == "600000"
    assert data["items"][0]["name"] == "浦发银行"


def test_d8_name_fuzzy_partial(search_client) -> None:
    """q=银行 → 名称子串命中多只（浦发银行/招商银行/平安银行）。"""
    r = search_client.get("/api/v1/data/search", params={"q": "银行"})
    assert r.status_code == 200
    names = {h["name"] for h in r.json()["data"]["items"]}
    assert names == {"浦发银行", "招商银行", "平安银行"}


def test_d8_exchange_prefix_behavior_observed(search_client) -> None:
    """q=sz000001（带交易所前缀）→ 观察行为并记录。

    契约 §2.1 D8 匹配规则只含：代码精确/前缀（6 位代码）+ 名称模糊，
    未承诺交易所前缀归一化。验证返回合法信封并记录实际结果供报告。
    """
    r = search_client.get("/api/v1/data/search", params={"q": "sz000001"})
    assert r.status_code == 200
    body = r.json()
    assert VALIDATOR.validate_envelope(body) == []
    data = body["data"]
    # 记录观察结果：契约未承诺交易所前缀 → 当前实现返回空数组（合法态）
    assert data["count"] == 0
    assert data["items"] == []


def test_d8_no_match_empty_array(search_client) -> None:
    """无匹配 → 显式空数组（合法态），不是错误。"""
    r = search_client.get("/api/v1/data/search", params={"q": "99999999"})
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["items"] == []
    assert data["count"] == 0


def test_d8_empty_q_400(search_client) -> None:
    """q 为空 → 400（fail-loud，不静默空返回）。"""
    r = search_client.get("/api/v1/data/search", params={"q": ""})
    assert r.status_code == 400
    assert r.json()["code"] == 400
    assert VALIDATOR.validate_envelope(r.json()) == []


def test_d8_whitespace_q_400(search_client) -> None:
    """q 全空白 → 400。"""
    r = search_client.get("/api/v1/data/search", params={"q": "   "})
    assert r.status_code == 400
    assert r.json()["code"] == 400


def test_d8_market_hk_501(search_client) -> None:
    """market=HK → 501（市场未启用）。"""
    r = search_client.get("/api/v1/data/search", params={"q": "600000", "market": "HK"})
    assert r.status_code == 501
    assert r.json()["code"] == 501


def test_d8_limit_validation(search_client) -> None:
    """limit 越界 → 400（ge=1, le=50）。"""
    r0 = search_client.get("/api/v1/data/search", params={"q": "600000", "limit": 0})
    assert r0.status_code == 400
    r999 = search_client.get("/api/v1/data/search", params={"q": "600000", "limit": 999})
    assert r999.status_code == 400


def test_d8_provider_failure_422(tmp_path) -> None:
    """清单源不可用（缓存缺失且枚举失败）→ 422 fail-loud。"""

    def _boom() -> list[Security]:
        raise ConnectionError("fake enumeration down")

    container = _make_search_container(tmp_path, provider=_boom)
    app = create_app(config=container.config, services=container)
    with TestClient(app) as c:
        r = c.get("/api/v1/data/search", params={"q": "600000"})
    assert r.status_code == 422
    assert r.json()["code"] == 422
    assert r.json()["data"]["error_type"] == "DataIntegrityError"


def test_d8_cache_persisted(tmp_path) -> None:
    """首次搜索后证券清单缓存落盘 security_catalog.json。"""
    cache_path = tmp_path / "db" / "security_catalog.json"
    container = _make_search_container(tmp_path, cache_path=cache_path)
    app = create_app(config=container.config, services=container)
    with TestClient(app) as c:
        c.get("/api/v1/data/search", params={"q": "600000"})
    assert cache_path.is_file()
    rows = json.loads(cache_path.read_text(encoding="utf-8"))
    assert len(rows) == len(_FAKE_SECURITIES)
    # 第二次搜索直接读缓存（provider 不再被调用）
    container.security_search._catalog = None  # 强制重新加载（走缓存）
    with TestClient(app) as c:
        r = c.get("/api/v1/data/search", params={"q": "浦发"})
    assert r.status_code == 200
    assert r.json()["data"]["items"][0]["code"] == "600000"


# ---------------------------------------------------------------------------
# 2) B1–B4 选股回测
# ---------------------------------------------------------------------------


def test_b1_strategies_list_envelope(bt_client) -> None:
    """B1 策略列表：信封 + 19 个上游策略 + schema 字段。"""
    r = bt_client.get("/api/v1/backtest/strategies")
    assert r.status_code == 200
    body = r.json()
    assert VALIDATOR.validate_envelope(body) == []
    data = body["data"]
    assert data["count"] == 19
    names = {s["name"] for s in data["items"]}
    assert "ma_cross" in names
    for s in data["items"]:
        assert s["name"]
        assert s["label"]
        assert s["description"]
        assert isinstance(s["params"], list)
        for p in s["params"]:
            assert p["name"] and p["type"] and p["label"]
            assert "default" in p


def test_b2_run_returns_job(bt_client) -> None:
    """B2 提交 → Job 信封（module=backtest, action=run）。"""
    r = bt_client.post(
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
    assert r.status_code == 200
    body = r.json()
    assert VALIDATOR.validate_envelope(body) == []
    job = body["data"]
    assert job["module"] == "backtest"
    assert job["action"] == "run"
    assert job["job_id"].startswith("job_")


def test_b2_run_empty_codes_400(bt_client) -> None:
    """B2 标的池空 → 400（契约 §2.1b）。"""
    r = bt_client.post(
        "/api/v1/backtest/run",
        json={"market": "CN", "codes": [], "strategy": "ma_cross"},
    )
    assert r.status_code == 400
    assert r.json()["code"] == 400


def test_b2_run_hk_501(bt_client) -> None:
    """B2 market=HK → 501。"""
    r = bt_client.post(
        "/api/v1/backtest/run",
        json={"market": "HK", "codes": ["600000"], "strategy": "ma_cross"},
    )
    assert r.status_code == 501
    assert r.json()["code"] == 501


def test_b2_all_load_failed_422(bt_client_empty) -> None:
    """B2 全部标的读不到数据 → Job failed + error.code=422（fail-loud）。"""
    import time

    r = bt_client_empty.post(
        "/api/v1/backtest/run",
        json={"market": "CN", "codes": ["600000"], "strategy": "ma_cross"},
    )
    assert r.status_code == 200
    job_id = r.json()["data"]["job_id"]
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


def test_b3_b4_full_flow_and_dto(bt_client) -> None:
    """B2→B3 进度→B4 完整 DTO（净值序列/绩效/成交 + 组合视图）。"""
    import time

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
    job_id = resp.json()["data"]["job_id"]

    done = False
    for _ in range(100):
        job_payload = bt_client.get(f"/api/v1/backtest/jobs/{job_id}").json()
        assert VALIDATOR.validate_envelope(job_payload) == []
        status = job_payload["data"]["status"]
        if status in ("done", "failed", "cancelled"):
            done = status == "done"
            break
        time.sleep(0.05)
    assert done, "回测 job 未在超时内完成"

    # B3 done → result_summary 含绩效摘要
    summary = job_payload["data"]["result_summary"]
    assert summary is not None
    assert summary["strategy"] == "ma_cross"
    assert set(summary["combined"]) >= {
        "total_return",
        "annual_return",
        "max_drawdown",
        "sharpe",
        "total_trades",
        "win_rate",
        "equity_points",
    }

    # B4 完整结果
    result_payload = bt_client.get(f"/api/v1/backtest/results/{job_id}").json()
    assert VALIDATOR.validate_envelope(result_payload) == []
    result = result_payload["data"]
    assert result["strategy"] == "ma_cross"
    assert set(result["codes"]) == {"600000", "600036"}
    assert set(result["per_code"].keys()) == {"600000", "600036"}

    # per-code：净值序列 + 绩效 + 成交明细 + 持仓
    one = result["per_code"]["600000"]
    assert "equity_curve" in one and len(one["equity_curve"]) > 0
    first, last = one["equity_curve"][0], one["equity_curve"][-1]
    assert set(first) >= {"datetime", "total", "drawdown", "drawdown_pct"}
    perf = one["performance"]
    for key in ("total_return", "annual_return", "max_drawdown", "sharpe", "win_rate"):
        assert key in perf, f"per-code 绩效缺 {key}"
    assert isinstance(one["trades"], list)
    assert "positions" in one

    # 组合视图：等权净值 + 上游绩效
    combined = result["combined"]
    assert combined["config"]["combine"] == "equal_weight"
    assert len(combined["equity_curve"]) > 0
    cperf = combined["performance"]
    for key in ("total_return", "annual_return", "max_drawdown", "sharpe"):
        assert key in cperf

    # 数字均为有限值（envelope_validator 已查 NaN/Inf，这里再抽查）
    assert abs(cperf["total_return"]) < 100


def test_b3_unknown_job_404(bt_client) -> None:
    """B3 job 不存在 → 404。"""
    r = bt_client.get("/api/v1/backtest/jobs/job_nope_404")
    assert r.status_code == 404
    assert r.json()["code"] == 404


def test_b4_unknown_job_404(bt_client) -> None:
    """B4 job 不存在 → 404。"""
    r = bt_client.get("/api/v1/backtest/results/job_nope_404")
    assert r.status_code == 404
    assert r.json()["code"] == 404


def test_backtest_results_db_location_and_schema(tmp_path) -> None:
    """回测完整结果落独立 backtest_results.db（位置 + schema）。"""
    container, db_path = _make_backtest_container(tmp_path)
    assert db_path.parent.is_dir()
    app = create_app(config=container.config, services=container)
    import time

    with TestClient(app) as c:
        r = c.post(
            "/api/v1/backtest/run",
            json={"market": "CN", "codes": ["600000"], "strategy": "ma_cross"},
        )
        job_id = r.json()["data"]["job_id"]
        for _ in range(100):
            job = c.get(f"/api/v1/backtest/jobs/{job_id}").json()["data"]
            if job["status"] in ("done", "failed", "cancelled"):
                break
            time.sleep(0.05)
    assert db_path.is_file()
    conn = sqlite3.connect(str(db_path))
    tables = [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]
    conn.close()
    assert "backtest_results" in tables
    cols = [row[1] for row in sqlite3.connect(str(db_path)).execute(
        "PRAGMA table_info(backtest_results)"
    ).fetchall()]
    assert cols == ["job_id", "result_json", "created_at"]
    assert job["status"] == "done"


# ---------------------------------------------------------------------------
# 3) 后端 MonitorStore SQLite 持久化（真接口模式未改动且正确）
# ---------------------------------------------------------------------------


def test_monitor_store_sqlite_persistence_across_reopen(tmp_path) -> None:
    """真接口模式自选/持仓 SQLite 持久化：重建实例后数据仍在。"""
    from Kuantix.monitor.store import MonitorStore, WatchlistItem

    config = _make_config(tmp_path)
    db_path = config.paths.db / "monitor.db"

    store = MonitorStore(db_path)
    store.add_watch(
        WatchlistItem(
            code="600000",
            name="浦发银行",
            market="CN",
            source="manual",
            added_at=dt.datetime.now().astimezone(),
        )
    )
    store.close()

    # 模拟“重启”：新实例同一 db 文件
    reopened = MonitorStore(db_path)
    codes = reopened.watch_codes("CN")
    reopened.close()
    assert "600000" in codes


def test_monitor_store_position_persisted_across_reopen(tmp_path) -> None:
    """持仓 SQLite 持久化：重建实例后仍在。"""
    from Kuantix.core.contracts import Position
    from Kuantix.monitor.store import MonitorStore

    config = _make_config(tmp_path)
    db_path = config.paths.db / "monitor.db"

    store = MonitorStore(db_path)
    store.add_position(
        Position(
            code="600000",
            market="CN",
            shares=100,
            cost_price=10.0,
            opened_at=dt.date(2024, 1, 1),
        ),
        name="浦发银行",
    )
    store.close()

    reopened = MonitorStore(db_path)
    positions = reopened.list_positions("CN")
    reopened.close()
    assert any(p["code"] == "600000" for p in positions)
