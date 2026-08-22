"""T05b1 API 层单测 conftest（白盒，TestClient 真调、假服务不发真网络）。

职责：
- 把项目根与 tests/redlines 加入 sys.path（复用 envelope_validator）；
- 提供临时配置（路径全部指向 tmp_path，不触碰 ~/.Kuantix）；
- 提供假服务组合根（FakeLake / FakeFactorService / FakeScreenService）
  与真 JobManager（SQLite 落在 tmp_path），供 TestClient 真调；
- 提供「真 ScreenService + 假 store/reader」的组合根，供 S2–S6 批次流程。
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
REDLINES_DIR = PROJECT_ROOT / "tests" / "redlines"
for _path in (PROJECT_ROOT, REDLINES_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

# 确保自定义因子已注册进上游 FACTORY_REGISTRY（F1 的 _factor_info 直接读注册表）
from Kuantix.factor.factors import discover_factors  # noqa: E402

discover_factors()

from Kuantix.api.deps import ServiceContainer  # noqa: E402
from Kuantix.api.jobs import JobManager, JobStore  # noqa: E402
from Kuantix.api.server import create_app  # noqa: E402
from Kuantix.config import Config, load_config  # noqa: E402
from Kuantix.core.contracts import ModelHandle, QuarantineEntry  # noqa: E402
from Kuantix.factor.service import JobResult  # noqa: E402

# ---------------------------------------------------------------------------
# 临时配置
# ---------------------------------------------------------------------------


def make_config(tmp_path: Path) -> Config:
    """构造路径全部指向 tmp_path 的配置（不触碰 ~/.Kuantix）。

    契约 v1.4（设计二 D2.3）：注入 ``schedule_enabled=false`` —— 配合
    lifespan 的「空湖守卫」双保险，保证 TestClient 启动不挂调度器、零网络。
    """
    template = PROJECT_ROOT / "Kuantix" / "resources" / "config.default.toml"
    text = template.read_text(encoding="utf-8")
    text = text.replace('root = "~/.Kuantix"', f'root = "{tmp_path / "root"}"')
    for key in ("vipdoc", "factors", "db", "logs", "reports", "exports"):
        text = text.replace(
            f'{key} = "~/.Kuantix/{key}"', f'{key} = "{tmp_path / key}"'
        )
    # 测试环境确定性：关调度（lifespan 不挂 APScheduler，绝不触网）
    text = text.replace("schedule_enabled = true", "schedule_enabled = false")
    target = tmp_path / "config.toml"
    target.write_text(text, encoding="utf-8")
    return load_config(target)


@pytest.fixture()
def tmp_config(tmp_path: Path) -> Config:
    return make_config(tmp_path)


# ---------------------------------------------------------------------------
# 假服务
# ---------------------------------------------------------------------------


class FakeSyncResult:
    """SyncResult 替身（runner 只读字段）。"""

    def __init__(
        self,
        total: int = 10,
        done: int = 10,
        failed: int = 0,
        quarantined: int = 0,
        skipped_resumed: int = 0,
        elapsed_ms: int = 5,
    ) -> None:
        self.total = total
        self.done = done
        self.failed = failed
        self.quarantined = quarantined
        self.skipped_resumed = skipped_resumed
        self.elapsed_ms = elapsed_ms


class FakeHandle:
    """SyncHandle 替身（runner 轮询 is_done/progress/result/status）。"""

    def __init__(
        self,
        status: str = "done",
        progress: dict | None = None,
        result: FakeSyncResult | None = None,
        error: str | None = None,
    ) -> None:
        self.status = status
        self.progress = progress
        self.result = result
        self.error = error

    def is_done(self) -> bool:
        return self.status in ("done", "cancelled", "failed")

    def cancel(self) -> None:
        self.status = "cancelled"

    def wait(self, timeout: float | None = None) -> FakeSyncResult | None:
        return self.result


def _make_handle(status: str = "done") -> FakeHandle:
    if status == "running":
        return FakeHandle(status="running", result=None)
    return FakeHandle(
        status=status,
        result=FakeSyncResult(total=10, done=10, failed=0, quarantined=0),
    )


class FakeLake:
    """DataLake 替身：不发网络，返回确定数据。"""

    def __init__(self, entries: list[QuarantineEntry] | None = None) -> None:
        self._entries = list(entries) if entries else []
        self._coverage = {
            "securities": 2,
            "files": 2,
            "bars": 4800,
            "disk_bytes": 153664,
            "first_date": "2016-09-08",
            "last_date": "2026-08-01",
        }

    def status(self, market: str = "CN") -> dict:
        return {
            "market": market,
            "data_date": self._coverage["last_date"],
            "coverage": dict(self._coverage),
            "quarantine_count": len(self._entries),
            "in_sync_window": False,
            # 契约 v1.5：D1 storage 摘要（区分 SQLite 主存储 / vipdoc 镜像）
            "storage": {
                "db_path": "/tmp/fake/market.db",
                "backend": "sqlite",
                "securities": 2,
                "daily_bars": 4800,
                "sync_checkpoint": 0,
                "sync_meta": 0,
                "sqlite_bars": 4800,
                "sqlite_securities": 2,
                "sqlite_codes": 2,
                "mirror_files": 0,
                "mirror_disk_bytes": 0,
                "source": "sqlite",
            },
            # 契约 v1.4：D1 只增两个可空字段（无记录 → last_sync=None）
            "last_sync": getattr(self, "_last_sync", None),
            "schedule": {
                "enabled": False,
                "time": "16:30",
                "startup_check": False,
            },
        }

    def verify_payload(self, market: str = "CN") -> dict:
        return {
            "market": market,
            "coverage": dict(self._coverage),
            "missing_days": [],
            "corrupt": [],
            "quarantined": [e.to_dict() for e in self._entries],
            "excluded_count": len(self._entries),
            "generated_at": "2026-08-01T15:05:00+08:00",
        }

    def sync_full(
        self, market: str, years: int, workers: int | None = None, force: bool = False
    ) -> FakeHandle:
        return _make_handle("done")

    def sync_incremental(
        self, market: str, workers: int | None = None, force: bool = False
    ) -> FakeHandle:
        return _make_handle("done")

    def list_quarantine(self, market: str | None = None) -> list[QuarantineEntry]:
        if market is None:
            return list(self._entries)
        return [e for e in self._entries if e.market == market]

    def remove_quarantine(self, code: str, market: str | None = None) -> int:
        before = len(self._entries)
        if market is None:
            self._entries = [e for e in self._entries if e.code != code]
        else:
            self._entries = [
                e for e in self._entries if not (e.code == code and e.market == market)
            ]
        return before - len(self._entries)


class FakeFactorStore:
    """FactorStore 替身：years_for / load / list_factors。"""

    def __init__(self) -> None:
        self._years = {"momentum_20d": [2021, 2022, 2023]}

    def years_for(self, factor: str) -> list[int]:
        if factor in self._years:
            return list(self._years[factor])
        return []

    def load(
        self,
        factor: str,
        date: int | None = None,
        code: str | None = None,
        *,
        start: int | None = None,
        end: int | None = None,
    ) -> pd.DataFrame:
        return pd.DataFrame(
            {"date": [20240101, 20240102], "code": ["600000", "600036"], "value": [1.0, 2.0]}
        )

    def list_factors(self) -> list[str]:
        return ["momentum_20d"]


class FakeFactorService:
    """FactorService 替身：list/compute/report/combine/models。"""

    def __init__(self) -> None:
        self.store = FakeFactorStore()
        self._factors = ["momentum_20d", "volume_ratio_5d"]
        self._models: list[ModelHandle] = [
            ModelHandle(
                name="m1",
                weights={"momentum_20d": 0.4, "volume_ratio_5d": 0.35},
                method="ir",
                created_at=dt.datetime(2026, 8, 1, 12, 0, 0, tzinfo=dt.timezone.utc),
            )
        ]

    def list_factors(self) -> list[str]:
        return list(self._factors)

    def list_models(self) -> list[str]:
        return [m.name for m in self._models]

    def list_model_handles(self) -> list[ModelHandle]:
        return list(self._models)

    def compute_factors(self, req) -> list[JobResult]:
        return [
            JobResult(factor=f, dates_computed=0, rows=0, elapsed_ms=1, force=req.force)
            for f in req.factors
        ]

    def report(self, factor: str, market: str = "CN") -> dict:
        return {
            "name": factor,
            "ic_mean": 0.043,
            "ic_std": 0.084,
            "ir": 0.511905,
            "ic_positive_rate": 0.58,
            "quantile_returns": {1: 0.021, 2: 0.028, 3: 0.031, 4: 0.037, 5: 0.052},
            "top_minus_bottom": 0.031,
            "turnover_rate": 0.32,
            "autocorr": 0.71,
            "ic_series_tail": [{"date": "2024-01-05", "ic": 0.031}],
        }

    def combine(
        self,
        factors,
        method: str,
        *,
        name: str | None = None,
        save_model: bool = False,
        market: str = "CN",
    ) -> ModelHandle:
        weights = dict.fromkeys(factors, 1.0)
        return ModelHandle(
            name=name or "combined",
            weights=weights,
            method=method,
            created_at=dt.datetime.now().astimezone(),
        )

    def load_model(self, name: str) -> ModelHandle:
        for model in self._models:
            if model.name == name:
                return model
        raise LookupError(f"model {name} not found")


class FakeScreenService:
    """ScreenService 替身（仅供 data/factor/health 用例占位，批次流程用真服务）。"""

    def list_batches(
        self, market: str | None = None, page: int = 1, page_size: int = 50
    ) -> dict:
        return {
            "items": [],
            "page": page,
            "page_size": page_size,
            "total": 0,
            "total_pages": 0,
        }

    def get_batch(self, batch_id: str) -> dict | None:
        return None

    def get_batch_results(
        self,
        batch_id: str,
        page: int = 1,
        page_size: int = 50,
        sort_by: str = "score",
        order: str = "desc",
    ) -> dict | None:
        return None

    def export_json_payload(self, batch_id: str) -> dict | None:
        return None

    def export_csv_bytes(self, batch_id: str) -> bytes | None:
        return None

    def run_batch(self, req, *, pool_codes=None, excluded_codes=None, filters=None, combine="and"):
        raise NotImplementedError("批次流程用例应使用真 ScreenService")


def make_fake_services(tmp_config: Config, jobs: JobManager) -> ServiceContainer:
    """假组合根：不发网络，适合 data/factor/health/错误路径用例。"""
    return ServiceContainer(
        config=tmp_config,
        lake=FakeLake(),
        factor_service=FakeFactorService(),
        screen_service=FakeScreenService(),
        jobs=jobs,
    )


# ---------------------------------------------------------------------------
# 真 ScreenService（批次流程 S2–S6）
# ---------------------------------------------------------------------------


class _MemStore:
    """内存版因子 store（load 返回 DataFrame，list_factors 返回因子名）。"""

    def __init__(self, data: dict[str, pd.DataFrame]) -> None:
        self._data = data

    def load(self, factor, date=None, code=None, *, start=None, end=None):
        df = self._data[factor]
        if date is not None:
            df = df[df["date"] == int(date)]
        if code is not None:
            df = df[df["code"] == str(code)]
        return df.reset_index(drop=True)

    def load_latest_per_code(self, factor, *, as_of=None):
        """与 FactorStore.load_latest_per_code 同语义：≤as_of 每码最新行。"""
        df = self._data[factor]
        if as_of is not None:
            df = df[df["date"] <= int(as_of)]
        latest = df.loc[df.groupby("code")["date"].idxmax()]
        return latest.reset_index(drop=True)

    def list_factors(self):
        return sorted(self._data)


class _FakeReader:
    def __init__(self, frames: dict[str, pd.DataFrame]) -> None:
        self._frames = frames

    def read_daily_frame(self, exchange: str, code: str):
        return self._frames[code]


class _NoFilter:
    def tech_filter(self, df, cond):
        return True

    def chanlun_filter(self, df, cond):
        return True


def make_real_screen_service(tmp_config: Config) -> object:
    """构造真 ScreenService（假 store/reader/filter，真批次落盘到 tmp）。"""
    from Kuantix.factor.combiner import FactorCombiner
    from Kuantix.screen.service import ScreenService

    data = {
        "momentum_20d": pd.DataFrame(
            {"date": [20240101, 20240101], "code": ["600000", "600036"], "value": [1.0, 3.0]}
        ),
        "volume_ratio_5d": pd.DataFrame(
            {"date": [20240101, 20240101], "code": ["600000", "600036"], "value": [1.0, 2.0]}
        ),
    }
    frames = {
        "600000": pd.DataFrame(
            {
                "datetime": pd.to_datetime(["2024-01-02", "2024-01-03"]),
                "close": [10.0, 10.1],
                "open": [10.0, 10.0],
                "high": [10.2, 10.2],
                "low": [9.9, 9.9],
                "vol": [1000.0, 1000.0],
                "amount": [10000.0, 10100.0],
            }
        ),
        "600036": pd.DataFrame(
            {
                "datetime": pd.to_datetime(["2024-01-02", "2024-01-03"]),
                "close": [20.0, 20.1],
                "open": [20.0, 20.0],
                "high": [20.2, 20.2],
                "low": [19.9, 19.9],
                "vol": [2000.0, 2000.0],
                "amount": [40000.0, 40200.0],
            }
        ),
    }
    screen = ScreenService.__new__(ScreenService)
    screen._config = tmp_config
    screen._store = _MemStore(data)
    screen._model_loader = None
    screen._reader = _FakeReader(frames)
    screen._combiner = FactorCombiner()
    screen._filter = _NoFilter()
    screen._profile = None
    screen._results_db = tmp_config.paths.db / "screen_results.db"
    screen._ensure_schema()
    return screen


def make_screen_services(
    tmp_config: Config, jobs: JobManager, lake: FakeLake | None = None
) -> ServiceContainer:
    """真 ScreenService + 假 lake（隔离区查询）+ 假 factor 的组合根。"""
    return ServiceContainer(
        config=tmp_config,
        lake=lake if lake is not None else FakeLake(),
        factor_service=FakeFactorService(),
        screen_service=make_real_screen_service(tmp_config),
        jobs=jobs,
    )


# ---------------------------------------------------------------------------
# 真 Monitor（M1-M17 用假 feed 避免网络）
# ---------------------------------------------------------------------------


class _FakeQuoteFeed:
    """QuoteFeed 替身：不发网络，返回空报价（启动循环不真拉行情）。"""

    def poll(self, codes, market="CN"):
        return []


def make_real_monitor(tmp_config: Config) -> tuple[Any, Any, Any, Any, Any]:
    """构造真监控组件（假 feed，store 落在 tmp，通道为空）。

    Returns:
        ``(loop, store, engine, tracker, notifier)``。
    """
    from Kuantix.monitor.loop import MonitorLoop
    from Kuantix.monitor.notifier import Notifier
    from Kuantix.monitor.position import PositionTracker
    from Kuantix.monitor.rules import RuleEngine
    from Kuantix.monitor.store import MonitorStore

    store = MonitorStore(tmp_config.paths.db / "monitor.db")
    engine = RuleEngine(store=store)
    tracker = PositionTracker(store=store)
    notifier = Notifier(channels=[])
    loop = MonitorLoop(
        feed=_FakeQuoteFeed(),
        store=store,
        engine=engine,
        tracker=tracker,
        notifier=notifier,
        market="CN",
        poll_interval_seconds=1.0,
        trading_hours_only=False,
    )
    return loop, store, engine, tracker, notifier


def make_monitor_services(
    tmp_config: Config, jobs: JobManager
) -> ServiceContainer:
    """真监控组件 + 假 lake/factor/screen 的组合根（M1-M17 用）。"""
    loop, store, engine, tracker, notifier = make_real_monitor(tmp_config)
    return ServiceContainer(
        config=tmp_config,
        lake=FakeLake(),
        factor_service=FakeFactorService(),
        screen_service=FakeScreenService(),
        jobs=jobs,
        monitor=loop,
        monitor_engine=engine,
        monitor_tracker=tracker,
        monitor_store=store,
        monitor_notifier=notifier,
    )


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def jobs(tmp_path: Path) -> JobManager:
    return JobManager(JobStore(tmp_path / "db"))


@pytest.fixture()
def services(tmp_config: Config, jobs: JobManager) -> ServiceContainer:
    return make_fake_services(tmp_config, jobs)


@pytest.fixture()
def screen_services(tmp_config: Config, jobs: JobManager) -> ServiceContainer:
    return make_screen_services(tmp_config, jobs)


@pytest.fixture()
def monitor_services(tmp_config: Config, jobs: JobManager) -> ServiceContainer:
    return make_monitor_services(tmp_config, jobs)


@pytest.fixture()
def client(tmp_config: Config, services: ServiceContainer):
    app = create_app(config=tmp_config, services=services)
    from fastapi.testclient import TestClient

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def screen_client(tmp_config: Config, screen_services: ServiceContainer):
    app = create_app(config=tmp_config, services=screen_services)
    from fastapi.testclient import TestClient

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def monitor_client(tmp_config: Config, monitor_services: ServiceContainer):
    app = create_app(config=tmp_config, services=monitor_services)
    from fastapi.testclient import TestClient

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def clear_event_bus():
    """M17 WS 测试前清空全局 EVENT_BUS 订阅（避免跨用例串流）。"""
    from Kuantix.core.eventbus import EVENT_BUS

    EVENT_BUS.clear()
    yield
    EVENT_BUS.clear()
