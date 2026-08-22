"""T03 性能验收：离线假 fetcher 下 500 标的建湖吞吐 + worker 连接数上界。

设计文档 08 验收指标（全 A 5209 只建湖 ≤ 90s 需真网络，本验收用离线
假 fetcher 验证**非网络部分**）：
- worker 级连接缓存生效：工厂调用次数 == worker 数（4）；
- 500 标的写 SQLite 建湖耗时上界（CI 宽松：< 30s，实测通常 < 5s）；
- 完成后 market.db 可读回、断点表已落。
"""
from __future__ import annotations

import datetime as dt
import time
from pathlib import Path

from Kuantix.adapters.vipdoc_writer import SqliteBarWriter
from Kuantix.core.contracts import Bar, Security
from Kuantix.data.market_store import MarketStore
from Kuantix.data.sync_engine import SyncEngine, SyncPlan

D1 = dt.date(2024, 1, 2)


def _bar() -> Bar:
    return Bar(date=D1, open=10.0, high=10.5, low=9.8, close=10.2, vol=1000.0, amount=10200.0)


def _sec(code: str) -> Security:
    return Security(
        code=code,
        exchange="sh" if code.startswith("6") else "sz",
        market="CN",
        security_type="SH_A_STOCK" if code.startswith("6") else "SZ_A_STOCK",
    )


class _CountingFetcher:
    def __init__(self) -> None:
        self.calls = 0

    def fetch_kline(self, market: str, code: str, years: int) -> list[Bar]:
        self.calls += 1
        return [_bar()]


class _FakeQuarantine:
    def __init__(self) -> None:
        self.entries: list = []

    def add(self, entry) -> None:
        self.entries.append(entry)

    def list(self, market=None):
        return self.entries


def test_sync_500_targets_sqlite_throughput(tmp_path: Path) -> None:
    store = MarketStore(tmp_path / "db" / "market.db")
    writer = SqliteBarWriter(store)
    created: list[_CountingFetcher] = []

    def factory() -> _CountingFetcher:
        f = _CountingFetcher()
        created.append(f)
        return f

    engine = SyncEngine(
        fetcher_factory=factory,
        writer=writer,
        quarantine=_FakeQuarantine(),
        checkpoint_store=store,
    )
    codes = tuple(f"{600000 + i}" for i in range(500))
    plan = SyncPlan(
        market="CN",
        years=1,
        securities=tuple(_sec(c) for c in codes),
        vipdoc_root=tmp_path,
        workers=4,
        min_request_interval=0.0,
        retry_backoff_seconds=0.0,
        retry_max_attempts=1,
        checkpoint_path=tmp_path / "checkpoint.json",
    )
    started = time.perf_counter()
    handle = engine.run(plan)
    result = handle.wait()
    elapsed = time.perf_counter() - started
    assert handle.status == "done"
    assert result is not None
    assert result.done == 500
    assert len(created) == 4  # 连接数 == worker 数（B1 修复）
    assert elapsed < 30.0, f"500 标的建湖耗时 {elapsed:.2f}s 超上界 30s"
    # 落库可读回 + 断点表已落
    assert store.daily_bar_count("CN") == 500
    cp = store.load_checkpoint("CN")
    assert len(cp["completed"]) == 500
