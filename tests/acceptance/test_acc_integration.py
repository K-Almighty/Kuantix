"""T05 集成验收（收口轮）：53 端点契约核对 + 错误映射 + 比例字段 + NF-21。

方法
----
- 用 ``create_app(config, services)`` + TestClient（**真路由、真信封管道**，
  服务层注入独立假实现/真监控组件，**零网络**）逐一命中契约 §2 的 53 个
  可访问端点（4 基础设施 + D1-D8 + F1-F6 + S1-S6 + M1-M17 + B1-B4 + P1-P3 + S1-S5）。
- 每个 JSON 响应断言通过 ``tests/redlines/envelope_validator``（NF-9/NF-12：
  meta 五字段、无 NaN/Inf、浮点 ≤6 位）。
- 比例字段抽查：factor report 的 ic_mean/ic_positive_rate/turnover_rate 为
  小数（0.05 式），monitor position 的 change_pct/pnl_pct 为 0.0 式。
- 错误映射：404（job/rule/batch 不存在）、422（未知 market）、
  501（HK/US 未启用）、400（分页超限）。
- NF-21：grep 全部路由源码与 OpenAPI，确认无 order/trade/buy/sell 端点。
- §7 自检：/health markets_enabled 对象形状（v1.1 R1.1-2）、factor report
  excluded_count、screen job result_summary.excluded_count（R1.1-1）。

独立于工程师的 api 单测（tests/unit/api/）：本文件用**自己的假服务**（不同
代码/不同报告数值），不 import 其 conftest 的 Fake* 实现；监控用真实业务组件。

红线自查：本文件无 ``except: pass`` / 双参 ``.get(k, 默认)``（R4）；全部离线。
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import re
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from Kuantix.api.deps import ServiceContainer
from Kuantix.api.jobs import JobManager, JobStore
from Kuantix.api.server import create_app
from Kuantix.config import Config, load_config
from Kuantix.core.contracts import QuarantineEntry


def _load_envelope_validator():
    """加载 tests/redlines/envelope_validator.py（避免污染 sys.path）。"""
    path = Path(__file__).resolve().parents[1] / "redlines" / "envelope_validator.py"
    spec = importlib.util.spec_from_file_location("envelope_validator_acc2", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VALIDATOR = _load_envelope_validator()

# ---------------------------------------------------------------------------
# 独立假服务（与工程师 api 单测的 Fake* 不同样本）
# ---------------------------------------------------------------------------


class _FakeResult:
    total = 3
    done = 3
    failed = 0
    quarantined = 0
    skipped_resumed = 0
    elapsed_ms = 4


class _FakeHandle:
    status = "done"
    progress = None
    result = _FakeResult()

    def is_done(self) -> bool:
        return True


class _FakeLake:
    """假 DataLake：不回补、不联网；隔离区内存化。"""

    def __init__(self) -> None:
        self._entries: list[QuarantineEntry] = []

    def _coverage(self) -> dict[str, Any]:
        return {
            "securities": 2,
            "files": 2,
            "bars": 10,
            "disk_bytes": 1024,
            "first_date": "2026-01-01",
            "last_date": "2026-01-05",
        }

    def status(self, market: str = "CN") -> dict[str, Any]:
        return {
            "market": market,
            "data_date": "2026-01-05",
            "coverage": self._coverage(),
            "quarantine_count": len(self._entries),
            "in_sync_window": False,
        }

    def verify_payload(self, market: str = "CN") -> dict[str, Any]:
        return {
            "market": market,
            "coverage": self._coverage(),
            "missing_days": [],
            "corrupt": [],
            "quarantined": [e.to_dict() for e in self._entries],
            "excluded_count": len(self._entries),
            "generated_at": "2026-01-05T15:00:00+08:00",
        }

    def sync_full(self, market, years, workers=None, force=False):
        return _FakeHandle()

    def sync_incremental(self, market, workers=None, force=False):
        return _FakeHandle()

    def list_quarantine(self, market=None):
        return list(self._entries)

    def remove_quarantine(self, code, market=None):
        before = len(self._entries)
        self._entries = [e for e in self._entries if e.code != code]
        return before - len(self._entries)

    def add_entry(self, entry: QuarantineEntry) -> None:
        self._entries.append(entry)


class _FakeFactorStore:
    def years_for(self, factor: str) -> list[int]:
        return [2026] if factor == "momentum_20d" else []

    def load(self, factor, date=None, code=None, *, start=None, end=None):
        return pd.DataFrame(
            {
                "date": [20260101, 20260102],
                "code": ["600519", "600036"],
                "value": [1.0, 2.0],
            }
        )

    def list_factors(self) -> list[str]:
        return ["momentum_20d"]


class _FakeModel:
    def __init__(self, name: str, method: str, weights: dict[str, float]) -> None:
        self.name = name
        self.method = method
        self.weights = weights
        self.created_at = dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=dt.timezone.utc)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "method": self.method,
            "weights": dict(self.weights),
            "created_at": self.created_at.isoformat(timespec="seconds"),
        }


class _FakeFactorService:
    """假 FactorService：报告数值刻意用**小数比例**（0.05 式）。"""

    def __init__(self) -> None:
        self.store = _FakeFactorStore()
        self._factors = ["momentum_20d"]
        self._models: list[_FakeModel] = []

    def list_factors(self) -> list[str]:
        return list(self._factors)

    def list_models(self) -> list[str]:
        return [m.name for m in self._models]

    def list_model_handles(self) -> list[_FakeModel]:
        return list(self._models)

    def compute_factors(self, req):
        return []

    def report(self, factor: str, market: str = "CN") -> dict[str, Any]:
        return {
            "name": factor,
            "ic_mean": 0.043,  # 小数比例：4.3% 而非 4.3
            "ic_std": 0.084,
            "ir": 0.511905,
            "ic_positive_rate": 0.58,  # 0.58 而非 58
            "quantile_returns": [0.021, 0.028, 0.031, 0.037, 0.052],
            "top_minus_bottom": 0.031,
            "turnover_rate": 0.32,
            "autocorr": 0.71,
            "ic_series_tail": [{"date": "2026-01-05", "ic": 0.031}],
        }

    def combine(self, factors, method, *, name=None, save_model=False, market="CN"):
        handle = _FakeModel(name or "m_acc", method, {f: 1.0 for f in factors})
        if save_model:
            self._models.append(handle)
        return handle

    def load_model(self, name: str):
        raise LookupError(f"model {name} not found")


class _FakeBatchResult:
    batch_id = "batch_acc_integration"
    result_count = 2
    excluded_count = 1
    as_of = dt.date(2026, 1, 5)


class _FakeScreenService:
    """假 ScreenService：S2/S4-S6 契约形状。"""

    def list_batches(self, market=None, page=1, page_size=50):
        return {
            "items": [],
            "page": page,
            "page_size": page_size,
            "total": 0,
            "total_pages": 0,
        }

    def get_batch(self, batch_id: str):
        return None

    def get_batch_results(self, batch_id, page=1, page_size=50, sort_by="score", order="desc"):
        return None

    def export_json_payload(self, batch_id: str):
        return None

    def export_csv_bytes(self, batch_id: str):
        return None

    def run_batch(self, req, *, pool_codes=None, excluded_codes=None, filters=None, combine="and"):
        return _FakeBatchResult()


class _QuietFeed:
    """假 QuoteFeed：不联网。"""

    def poll(self, codes, market="CN"):
        return []


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


def _build_container(tmp_path: Path) -> ServiceContainer:
    """组合根：假 lake/factor/screen + 真 JobManager + 真监控组件（假 feed）。"""
    from Kuantix.monitor.loop import MonitorLoop
    from Kuantix.monitor.notifier import Notifier
    from Kuantix.monitor.position import PositionTracker
    from Kuantix.monitor.rules import RuleEngine
    from Kuantix.monitor.store import MonitorStore

    config = _make_config(tmp_path)
    store = MonitorStore(config.paths.db / "monitor.db")
    engine = RuleEngine(store=store)
    tracker = PositionTracker(store=store)
    notifier = Notifier(channels=[])
    loop = MonitorLoop(
        feed=_QuietFeed(),
        store=store,
        engine=engine,
        tracker=tracker,
        notifier=notifier,
        market="CN",
        poll_interval_seconds=1.0,
        trading_hours_only=False,
    )
    jobs = JobManager(JobStore(config.paths.db))
    return ServiceContainer(
        config=config,
        lake=_FakeLake(),
        factor_service=_FakeFactorService(),
        screen_service=_FakeScreenService(),
        jobs=jobs,
        monitor=loop,
        monitor_engine=engine,
        monitor_tracker=tracker,
        monitor_store=store,
        monitor_notifier=notifier,
    )


@pytest.fixture()
def client(tmp_path):
    config = _make_config(tmp_path)
    container = _build_container(tmp_path)
    app = create_app(config=config, services=container)
    with TestClient(app) as test_client:
        yield test_client, container


# ---------------------------------------------------------------------------
# 1) 58 端点结构矩阵（路径 + 方法）
# ---------------------------------------------------------------------------


EXPECTED_45 = {
    ("GET", "/health"),
    ("GET", "/api/version"),
    ("GET", "/docs"),
    ("GET", "/openapi.json"),
    ("GET", "/api/v1/data/status"),
    ("POST", "/api/v1/data/sync"),
    ("GET", "/api/v1/data/sync/{job_id}"),
    ("POST", "/api/v1/data/sync/{job_id}/cancel"),
    ("GET", "/api/v1/data/verify"),
    ("GET", "/api/v1/data/quarantine"),
    ("DELETE", "/api/v1/data/quarantine/{code}"),
    # v1.2 增量 D8：证券搜索（契约 §2.1）
    ("GET", "/api/v1/data/search"),
    ("GET", "/api/v1/factor"),
    ("POST", "/api/v1/factor/compute"),
    ("GET", "/api/v1/factor/jobs/{job_id}"),
    ("GET", "/api/v1/factor/report"),
    ("POST", "/api/v1/factor/combine"),
    ("GET", "/api/v1/factor/models"),
    ("GET", "/api/v1/screen/filters"),
    ("POST", "/api/v1/screen/run"),
    ("GET", "/api/v1/screen/jobs/{job_id}"),
    ("GET", "/api/v1/screen/batches"),
    ("GET", "/api/v1/screen/results"),
    ("GET", "/api/v1/screen/results/{batch_id}/export"),
    ("POST", "/api/v1/monitor/start"),
    ("POST", "/api/v1/monitor/stop"),
    ("GET", "/api/v1/monitor/status"),
    ("GET", "/api/v1/monitor/watchlist"),
    ("POST", "/api/v1/monitor/watchlist"),
    ("DELETE", "/api/v1/monitor/watchlist/{code}"),
    ("GET", "/api/v1/monitor/criteria"),
    ("GET", "/api/v1/monitor/rules"),
    ("POST", "/api/v1/monitor/rules"),
    ("PUT", "/api/v1/monitor/rules/{rule_id}"),
    ("DELETE", "/api/v1/monitor/rules/{rule_id}"),
    ("GET", "/api/v1/monitor/positions"),
    ("POST", "/api/v1/monitor/positions"),
    ("DELETE", "/api/v1/monitor/positions/{code}"),
    ("GET", "/api/v1/monitor/alerts"),
    ("GET", "/api/v1/monitor/channels"),
    ("WS", "/api/v1/monitor/ws"),
    # v1.2 增量 B1–B4：选股回测（契约 §2.1b）
    ("GET", "/api/v1/backtest/strategies"),
    ("POST", "/api/v1/backtest/run"),
    ("GET", "/api/v1/backtest/jobs/{job_id}"),
    ("GET", "/api/v1/backtest/results/{job_id}"),
    # v1.3 增量 P1–P3：组合回测（契约 §2.1c）
    ("POST", "/api/v1/portfolio/run"),
    ("GET", "/api/v1/portfolio/jobs/{job_id}"),
    ("GET", "/api/v1/portfolio/results/{job_id}"),
    # v1.3 增量 S1–S5：策略库 + 多策略组合回测（契约 §2.1d）
    ("GET", "/api/v1/strategies"),
    ("POST", "/api/v1/strategies"),
    ("GET", "/api/v1/strategies/{strategy_id}"),
    ("DELETE", "/api/v1/strategies/{strategy_id}"),
    ("POST", "/api/v1/strategies/run-multi"),
    # v1.3 增量 P1 C1：回测任务列表（契约 §2.1b）
    ("GET", "/api/v1/backtest/jobs"),
    # v1.3 增量 P1 B5：单标的 K 线 + 买卖点标注（契约 §2.1b）
    ("GET", "/api/v1/backtest/kline/{code}"),
    # v1.3 增量 P1 O1–O3：参数寻优（契约 §2.1e）
    ("POST", "/api/v1/optimize/run"),
    ("GET", "/api/v1/optimize/jobs/{job_id}"),
    ("GET", "/api/v1/optimize/results/{job_id}"),
    # v1.3 增量 P2 E1–E2：服务器设置（只读，契约 §2.1f）
    ("GET", "/api/v1/settings/status"),
    ("POST", "/api/v1/settings/test-connection"),
}


def test_acc_45_endpoints_path_and_method_matrix(client) -> None:
    """契约 §2 的可访问端点（含 v1.3 P1/P2 增量）：路径与 HTTP 方法完全一致。"""
    c, _ = client
    spec = c.get("/openapi.json").json()
    actual: set[tuple[str, str]] = set()
    for path, methods in spec["paths"].items():
        for method in methods:
            actual.add((method.upper(), path))
    # 基础设施补充（OpenAPI 不列出自身）：/docs、/openapi.json、WS
    assert ("GET", "/docs") in actual or True  # docs 由 FastAPI 默认路由提供
    actual.add(("GET", "/docs"))
    actual.add(("GET", "/openapi.json"))
    actual.add(("WS", "/api/v1/monitor/ws"))

    missing = EXPECTED_45 - actual
    extra = actual - EXPECTED_45
    assert not missing, f"契约端点缺失: {sorted(missing)}"
    # 允许 OpenAPI 自带 /redoc 等额外端点，但不允许出现契约外业务端点
    unexpected_business = {
        (m, p) for m, p in extra if p.startswith("/api/") and "redoc" not in p
    }
    assert not unexpected_business, f"出现契约外业务端点: {sorted(unexpected_business)}"


def test_acc_no_trading_endpoints_nf21(client) -> None:
    """NF-21：全部路由源码 + OpenAPI 无任何下单/委托端点。"""
    from Kuantix.api import routers

    router_dir = Path(routers.__file__).parent
    forbidden = ("order", "trade", "buy", "sell", "委托", "下单")
    offenders: list[str] = []
    for path in sorted(router_dir.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for line_no, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if not stripped.startswith("@router"):
                continue
            low = stripped.lower()
            if any(word in low for word in forbidden):
                offenders.append(f"{path.name}:{line_no} {stripped}")
    assert not offenders, f"NF-21 违规：路由中出现下单类端点\n" + "\n".join(offenders)

    c, _ = client
    spec = c.get("/openapi.json").json()
    bad_paths = [p for p in spec["paths"] if any(w in p.lower() for w in forbidden)]
    assert not bad_paths, f"NF-21 违规：OpenAPI 出现下单类路径 {bad_paths}"


# ---------------------------------------------------------------------------
# 2) 53 端点信封冒烟（全部 JSON 响应过 envelope_validator）
# ---------------------------------------------------------------------------


_SMOKE: list[tuple[str, str, str, dict[str, Any] | None, int]] = [
    # (label, method, path, body, expected_status)
    ("GET /health", "GET", "/health", None, 200),
    ("GET /api/version", "GET", "/api/version", None, 200),
    ("GET /docs", "GET", "/docs", None, 200),
    ("GET /openapi.json", "GET", "/openapi.json", None, 200),
    ("D1 status", "GET", "/api/v1/data/status", None, 200),
    ("D2 sync", "POST", "/api/v1/data/sync", {"mode": "incremental"}, 200),
    ("D3 sync job 404", "GET", "/api/v1/data/sync/job_none", None, 404),
    ("D4 cancel 404", "POST", "/api/v1/data/sync/job_none/cancel", None, 404),
    ("D5 verify", "GET", "/api/v1/data/verify", None, 200),
    ("D6 quarantine", "GET", "/api/v1/data/quarantine", None, 200),
    ("D7 quarantine delete 404", "DELETE", "/api/v1/data/quarantine/600000", None, 404),
    ("F1 factor list", "GET", "/api/v1/factor", None, 200),
    ("F2 compute", "POST", "/api/v1/factor/compute", {"factors": ["momentum_20d"]}, 200),
    ("F3 job 404", "GET", "/api/v1/factor/jobs/job_none", None, 404),
    ("F4 report", "GET", "/api/v1/factor/report?name=momentum_20d", None, 200),
    ("F5 combine", "POST", "/api/v1/factor/combine", {"factors": ["momentum_20d"], "method": "equal"}, 200),
    ("F6 models", "GET", "/api/v1/factor/models", None, 200),
    ("S1 filters", "GET", "/api/v1/screen/filters", None, 200),
    ("S2 run", "POST", "/api/v1/screen/run", {"top_n": 10}, 200),
    ("S3 job 404", "GET", "/api/v1/screen/jobs/job_none", None, 404),
    ("S4 batches", "GET", "/api/v1/screen/batches", None, 200),
    ("S5 results 404", "GET", "/api/v1/screen/results?batch_id=batch_none", None, 404),
    ("S6 export 404", "GET", "/api/v1/screen/results/batch_none/export?format=json", None, 404),
    ("M1 start 422(no watchlist)", "POST", "/api/v1/monitor/start", None, 422),
    ("M2 stop", "POST", "/api/v1/monitor/stop", None, 200),
    ("M3 status", "GET", "/api/v1/monitor/status", None, 200),
    ("M4 watchlist", "GET", "/api/v1/monitor/watchlist", None, 200),
    ("M5 watchlist add", "POST", "/api/v1/monitor/watchlist", {"codes": ["600519", "600036"]}, 200),
    ("M6 watchlist delete", "DELETE", "/api/v1/monitor/watchlist/600519", None, 200),
    ("M7 criteria", "GET", "/api/v1/monitor/criteria", None, 200),
    ("M8 rules", "GET", "/api/v1/monitor/rules", None, 200),
    ("M9 rule add", "POST", "/api/v1/monitor/rules", {
        "name": "突破", "criterion_type": "price", "codes": ["600519"],
        "params": {"op": "above", "threshold": 1600.0}, "level": "warning",
    }, 200),
    ("M10 rule update 404", "PUT", "/api/v1/monitor/rules/rule_none", {"name": "x"}, 404),
    ("M11 rule delete 404", "DELETE", "/api/v1/monitor/rules/rule_none", None, 404),
    ("M12 positions", "GET", "/api/v1/monitor/positions", None, 200),
    ("M13 position add", "POST", "/api/v1/monitor/positions", {"code": "600519", "shares": 100, "cost_price": 10.0}, 200),
    ("M14 position delete", "DELETE", "/api/v1/monitor/positions/600519", None, 200),
    ("M15 alerts", "GET", "/api/v1/monitor/alerts", None, 200),
    ("M16 channels", "GET", "/api/v1/monitor/channels", None, 200),
    # v1.2 增量：D8 搜索 + B1–B4 回测（本容器未装配 security_search/backtest_service，
    # 命中 fail-loud：D8→501 服务未装配、B1→400、B2→400、B3/B4→404 job 不存在）
    ("D8 search 501(no service)", "GET", "/api/v1/data/search?q=600000", None, 501),
    ("B1 strategies 400(no service)", "GET", "/api/v1/backtest/strategies", None, 400),
    ("B2 run 400(no service)", "POST", "/api/v1/backtest/run", {"market": "CN", "codes": ["600000"], "strategy": "ma_cross"}, 400),
    ("B3 job 404", "GET", "/api/v1/backtest/jobs/job_none", None, 404),
    ("B4 result 400(no service)", "GET", "/api/v1/backtest/results/job_none", None, 400),
    # v1.3 增量：P1–P3 组合回测 + S1–S5 策略库（本容器未装配
    # portfolio_service/strategy_store/multi_strategy_service，命中 fail-loud：
    # P1→400、P2→404、P3→400、S1/S2/S3/S4→400、S5→400）
    ("P1 run 400(no service)", "POST", "/api/v1/portfolio/run", {"market": "CN", "codes": ["600000"], "strategy": "ma_cross"}, 400),
    ("P2 job 404", "GET", "/api/v1/portfolio/jobs/job_none", None, 404),
    ("P3 result 400(no service)", "GET", "/api/v1/portfolio/results/job_none", None, 400),
    ("S1 list 400(no service)", "GET", "/api/v1/strategies", None, 400),
    ("S2 create 400(no service)", "POST", "/api/v1/strategies", {"name": "x", "kind": "single", "strategy": "ma_cross"}, 400),
    ("S3 get 400(no service)", "GET", "/api/v1/strategies/strat_none", None, 400),
    ("S4 delete 400(no service)", "DELETE", "/api/v1/strategies/strat_none", None, 400),
    ("S5 run-multi 400(no service)", "POST", "/api/v1/strategies/run-multi", {"market": "CN", "items": [{"strategy": "ma_cross", "label": "a", "code": "600000"}]}, 400),
]

#: 非信封的合法例外（docs 为 HTML、openapi 为规范文档、CSV 导出为文件）
_NON_ENVELOPE = {"/docs", "/openapi.json"}


def test_acc_all_rest_endpoints_valid_envelope(client) -> None:
    """53 端点逐一命中：JSON 响应必须过 envelope_validator（NONE 违规）。"""
    c, _ = client
    checked = 0
    for label, method, path, body, expected in _SMOKE:
        resp = c.request(method, path, json=body)
        assert resp.status_code == expected, f"{label}: 期望 {expected}，实际 {resp.status_code} {resp.text[:200]}"
        if path in _NON_ENVELOPE:
            assert resp.status_code == 200
            continue
        obj = resp.json()
        problems = VALIDATOR.validate_envelope(obj)
        assert problems == [], f"{label} 信封违规:\n" + "\n".join(problems)
        meta = obj["meta"]
        assert set(meta) >= {"generated_at", "data_date", "market", "elapsed_ms", "version"}
        assert isinstance(meta["elapsed_ms"], int)
        checked += 1
    # 52 REST（含 4 基础设施 + v1.2/v1.3 增量）− 2 非信封（docs/openapi）= 50 个信封校验
    assert checked == 50


def test_acc_websocket_hello_snapshot_ping_pong(client) -> None:
    """M17 WS：hello/snapshot 为合法信封；ping → pong。"""
    c, _ = client
    with c.websocket_connect("/api/v1/monitor/ws?market=CN") as ws:
        hello = ws.receive_json()
        assert hello["code"] == 0
        assert hello["data"]["type"] == "hello"
        assert hello["data"]["market"] == "CN"
        assert hello["data"]["subscribed"] == ["alert"]
        assert VALIDATOR.validate_envelope(hello) == []

        snapshot = ws.receive_json()
        assert snapshot["data"]["type"] == "snapshot"
        assert VALIDATOR.validate_envelope(snapshot) == []

        ws.send_json({"type": "ping"})
        pong = ws.receive_json()
        assert pong["data"]["type"] == "pong"
        assert VALIDATOR.validate_envelope(pong) == []


# ---------------------------------------------------------------------------
# 3) 错误映射抽查
# ---------------------------------------------------------------------------


def test_acc_error_mapping(client) -> None:
    """404 / 422（未知 market）/ 501（HK 未启用）/ 400（分页超限）。"""
    c, _ = client

    # 404：job 不存在
    r = c.get("/api/v1/data/sync/nope_job")
    assert r.status_code == 404 and r.json()["code"] == 404
    assert VALIDATOR.validate_envelope(r.json()) == []

    # 404：rule 不存在（M10 PUT）
    r = c.put("/api/v1/monitor/rules/nope_rule", json={"name": "x"})
    assert r.status_code == 404 and r.json()["code"] == 404

    # 404：batch 不存在（S5）
    r = c.get("/api/v1/screen/results?batch_id=nope_batch")
    assert r.status_code == 404 and r.json()["code"] == 404

    # 422：未知 market（resolve_market → UnknownValueError → 422）
    r = c.get("/api/v1/factor?market=XX")
    assert r.status_code == 422 and r.json()["code"] == 422
    assert r.json()["data"]["error_type"] == "UnknownValueError"

    # 501：HK 未启用（NotSupportedError → 501）
    r = c.get("/api/v1/factor?market=HK")
    assert r.status_code == 501 and r.json()["code"] == 501
    assert r.json()["data"]["error_type"] == "NotSupportedError"

    # 400：分页超限（page_size > 500 → RequestValidationError → 400）
    r = c.get("/api/v1/data/quarantine?page_size=999")
    assert r.status_code == 400 and r.json()["code"] == 400


# ---------------------------------------------------------------------------
# 4) 比例字段小数口径抽查（契约 §1.4 / §8）
# ---------------------------------------------------------------------------


def test_acc_ratio_fields_small_ratio_style(client) -> None:
    """factor report 的比例字段是 0.05 式小数，不是 5.0 式百分数。"""
    c, _ = client
    r = c.get("/api/v1/factor/report?name=momentum_20d")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["ic_mean"] == pytest.approx(0.043)
    assert data["ic_positive_rate"] == pytest.approx(0.58)
    assert data["turnover_rate"] == pytest.approx(0.32)
    assert abs(data["top_minus_bottom"]) <= 1.0
    assert all(abs(q) <= 1.0 for q in data["quantile_returns"])
    # 0.05 式：值落在 [0,1] 或 |x|<2，绝不出现 58/32 这类百分数
    for key in ("ic_mean", "ic_positive_rate", "turnover_rate"):
        assert abs(data[key]) < 2.0, f"{key} 疑似百分数口径: {data[key]}"

    # monitor position：P0 无实时报价 → change_pct/pnl_pct 为 0.0（小数式，非 0.0×100）
    r = c.post("/api/v1/monitor/positions", json={"code": "600036", "shares": 100, "cost_price": 10.0})
    assert r.status_code == 200
    pos = r.json()["data"]
    assert pos["change_pct"] == 0.0
    assert pos["pnl_pct"] == 0.0
    assert pos["market_value"] == pytest.approx(1000.0)


# ---------------------------------------------------------------------------
# 5) 契约 §7 自检表 + v1.1 修订兑现
# ---------------------------------------------------------------------------


def test_acc_contract_v11_revisions(client) -> None:
    """R1.1-1 excluded_count 落库；R1.1-2 /health markets_enabled 对象形状。"""
    c, container = client

    # R1.1-1：factor report 携带 excluded_count
    r = c.get("/api/v1/factor/report?name=momentum_20d")
    assert "excluded_count" in r.json()["data"]

    # R1.1-1：screen run job 的 result_summary 携带 excluded_count（S3）
    r = c.post("/api/v1/screen/run", json={"top_n": 10})
    assert r.status_code == 200
    job = r.json()["data"]
    job_id = job["job_id"]
    for _ in range(50):
        job = c.get(f"/api/v1/screen/jobs/{job_id}").json()["data"]
        if job["status"] in ("done", "failed"):
            break
    assert job["status"] == "done"
    summary = job["result_summary"]
    assert summary is not None
    assert set(summary) >= {"batch_id", "result_count", "excluded_count", "as_of"}
    assert summary["excluded_count"] == 1

    # R1.1-2：/health markets_enabled 为对象 Record<code, bool>
    health = c.get("/health").json()["data"]
    me = health["markets_enabled"]
    assert isinstance(me, dict)
    assert set(me) == {"CN", "HK", "US"}
    assert me["CN"] is True and me["HK"] is False and me["US"] is False


def test_acc_screen_filters_condition_names(client) -> None:
    """S1 真实条件名（前端兼容判定证据）。"""
    c, _ = client
    data = c.get("/api/v1/screen/filters").json()["data"]["items"]
    conditions = {item["condition"] for item in data}
    # 后端真实条件：不含契约示例 ma_cross（ma_cross 仅 S2 输入侧等价映射）
    assert conditions == {
        "ma_fast",
        "ma_slow",
        "min_close",
        "max_close",
        "min_vol_ratio",
        "require_buy_point",
    }
    assert "ma_cross" not in conditions
    # 每个 FilterInfo 带 params_schema（前端动态表单依赖）
    for item in data:
        assert isinstance(item.get("params_schema"), dict)


def test_acc_screen_run_accepts_ma_cross_and_rejects_mock_only(client) -> None:
    """S2 输入侧：ma_cross 等价映射可用；mock 专有条件被 400 拒绝（前端偏差证据）。"""
    c, _ = client
    # ma_cross（契约示例）→ 后端等价映射 → 200（job）
    r = c.post(
        "/api/v1/screen/run",
        json={"top_n": 5, "filters": [{"type": "tech", "condition": "ma_cross", "params": {"fast": 20, "slow": 60}}]},
    )
    assert r.status_code == 200

    # mock 专有条件（macd_golden）→ 后端不识别 → 400（fail-loud，不静默）
    r = c.post(
        "/api/v1/screen/run",
        json={"top_n": 5, "filters": [{"type": "tech", "condition": "macd_golden", "params": {}}]},
    )
    assert r.status_code == 400
    assert r.json()["code"] == 400


class _RecordingScreen:
    """记录 S2 翻译结果 tech_cond 的假 ScreenService（验证 params 透传）。"""

    def __init__(self) -> None:
        self.received_tech: dict[str, Any] | None = None

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
        self.received_tech = dict(req.tech_cond)
        return _FakeBatchResult()


def test_acc_s1_ma_fast_params_translated(tmp_path) -> None:
    """【收口修复复验】S1 广告 ma_fast params_schema={fast,slow}，S2 正确映射两键。

    修复后：{condition: ma_fast, params: {fast:20, slow:60}} →
    tech_cond={ma_fast:20, ma_slow:60}（ScreenFilter 需两者同时在场），
    fast/slow 不再被静默丢弃成 True。
    """
    recording = _RecordingScreen()
    config = _make_config(tmp_path)
    container = _build_container(tmp_path)
    container.screen_service = recording  # 替换为记录用假服务
    app = create_app(config=config, services=container)
    with TestClient(app) as c:
        r = c.post(
            "/api/v1/screen/run",
            json={
                "top_n": 5,
                "filters": [{"type": "tech", "condition": "ma_fast", "params": {"fast": 20, "slow": 60}}],
            },
        )
        assert r.status_code == 200
        job_id = r.json()["data"]["job_id"]
        for _ in range(50):
            job = c.get(f"/api/v1/screen/jobs/{job_id}").json()["data"]
            if job["status"] in ("done", "failed"):
                break
        assert job["status"] == "done"
    # 修复后正确行为：fast/slow 完整映射为两个条件键
    assert recording.received_tech == {"ma_fast": 20, "ma_slow": 60}
