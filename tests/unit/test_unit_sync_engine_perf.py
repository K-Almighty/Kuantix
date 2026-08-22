"""SyncEngine 性能/断点表单测（T03 问题 3：worker 缓存 + per-worker 限速 + D6）。

白盒验证：
- **worker 级 fetcher 缓存**：``fetcher_factory`` 调用次数 == worker 数
  （不再 == 标的数，B1 修复）；
- **per-worker 限速**：``min_request_interval`` 语义保留，但 4 workers 并发
  时总耗时显著低于全局串行估计（D10）；
- **断点表**：``checkpoint_store`` 注入后断点走 sync_checkpoint 表（O(1)
  单行 upsert），续传跳过已完成标的。
"""
from __future__ import annotations

import datetime as dt
import time
from pathlib import Path
from typing import Any

import pytest

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


class _FakeFetcher:
    """可计数假 fetcher（每 worker 一个实例）。"""

    def __init__(self) -> None:
        self.calls = 0

    def fetch_kline(self, market: str, code: str, years: int) -> list[Bar]:
        self.calls += 1
        return [_bar()]


class _FakeWriter:
    def __init__(self) -> None:
        self.written: list[str] = []

    def write_daily(self, bars, exchange, code, *, path=None):
        self.written.append(code)
        return type("R", (), {"written": len(bars)})()


class _FakeQuarantine:
    def __init__(self) -> None:
        self.entries: list[Any] = []

    def add(self, entry: Any) -> None:
        self.entries.append(entry)

    def list(self, market=None):
        return self.entries


def _make_plan(tmp_path: Path, codes: tuple[str, ...], workers: int, interval: float) -> SyncPlan:
    return SyncPlan(
        market="CN",
        years=1,
        securities=tuple(_sec(c) for c in codes),
        vipdoc_root=tmp_path,
        workers=workers,
        min_request_interval=interval,
        retry_backoff_seconds=0.0,
        retry_max_attempts=1,
        checkpoint_path=tmp_path / "checkpoint.json",
    )


def test_fetcher_factory_called_once_per_worker(tmp_path: Path) -> None:
    """B1 修复：工厂调用次数 == worker 数（4），而非标的数（40）。"""
    created: list[_FakeFetcher] = []

    def factory() -> _FakeFetcher:
        f = _FakeFetcher()
        created.append(f)
        return f

    engine = SyncEngine(
        fetcher_factory=factory,
        writer=_FakeWriter(),
        quarantine=_FakeQuarantine(),
    )
    codes = tuple(f"{600000 + i}" for i in range(40))
    plan = _make_plan(tmp_path, codes, workers=4, interval=0.0)
    handle = engine.run(plan)
    result = handle.wait()
    assert handle.status == "done"
    assert result is not None
    assert result.done == 40
    assert len(created) == 4  # 每 worker 1 次（B1 修复）
    # 线程池调度可能不均（9/10/10/11），但每个 worker 都复用同一 fetcher
    assert sum(f.calls for f in created) == 40
    assert all(f.calls > 0 for f in created)


def test_per_worker_throttle_not_global_serial(tmp_path: Path) -> None:
    """D10：per-worker 限速下 4 workers 总耗时 ≈ 1/4 全局串行。"""
    created: list[_FakeFetcher] = []

    def factory() -> _FakeFetcher:
        f = _FakeFetcher()
        created.append(f)
        return f

    engine = SyncEngine(
        fetcher_factory=factory,
        writer=_FakeWriter(),
        quarantine=_FakeQuarantine(),
    )
    # 8 标的 × 4 workers × interval=0.08 → 若全局串行需 8×0.08=0.64s；
    # per-worker 每 worker 只处理 2 只 → ≈2×0.08=0.16s。给 0.45s 上界。
    codes = tuple(f"{600000 + i}" for i in range(8))
    plan = _make_plan(tmp_path, codes, workers=4, interval=0.08)
    started = time.perf_counter()
    handle = engine.run(plan)
    result = handle.wait()
    elapsed = time.perf_counter() - started
    assert handle.status == "done"
    assert result is not None and result.done == 8
    assert elapsed < 0.45, f"per-worker 限速失效：{elapsed:.3f}s（应 <0.45s）"


def test_checkpoint_table_resume(tmp_path: Path) -> None:
    """D6：checkpoint_store 注入后断点走 sync_checkpoint 表，续传跳过已完成。"""
    store = MarketStore(tmp_path / "db" / "market.db")
    # 预置：600000 已完成（等价上次运行落表）
    store.upsert_checkpoint_row("CN", "600000", "completed")
    engine = SyncEngine(
        fetcher_factory=lambda: _FakeFetcher(),
        writer=_FakeWriter(),
        quarantine=_FakeQuarantine(),
        checkpoint_store=store,
    )
    plan = _make_plan(tmp_path, ("600000", "600036"), workers=2, interval=0.0)
    handle = engine.run(plan)
    result = handle.wait()
    assert handle.status == "done"
    assert result is not None
    assert result.skipped_resumed == 1
    assert result.completed_codes == ("600036",)
    # 运行后 600036 也落表
    cp = store.load_checkpoint("CN")
    assert "600036" in cp["completed"]
    assert "600000" in cp["completed"]
