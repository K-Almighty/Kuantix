"""IncrementalSyncScheduler 单测（设计二：盘后 cron + 启动检查，契约 v1.4）。

覆盖（monkeypatch 假 profile/时钟 + 假 lake，不发真网络）：
- ``_should_run``：非交易日 skip / 交易时段 skip / 启动且湖空 skip /
  启动且今日已同步 skip / 条件满足 → None；
- ``run_once``：done 后更新 sync_state；失败记录 error；异常记 failed；
- 跨进程 flock 单例锁：抢锁失败 → 跳过；
- ``_parse_schedule_time``：合法/非法边界。
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from Kuantix.config import Config, load_config
from Kuantix.core.fail_loud import DataIntegrityError
from Kuantix.data.sync_state import SyncStateStore
from Kuantix.scheduler import IncrementalSyncScheduler, SYNC_LOCK_FILENAME

#: 2026-08-03 为周一（交易日候选）；假档案完全由参数控制
_TZ = "Asia/Shanghai"


def _make_config(tmp_path: Path) -> Config:
    """构造路径全部指向 tmp 的配置（不触碰 ~/.Kuantix）。"""
    template = (
        Path(__file__).resolve().parents[2]
        / "Kuantix" / "resources" / "config.default.toml"
    )
    text = template.read_text(encoding="utf-8")
    text = text.replace('root = "~/.Kuantix"', f'root = "{tmp_path / "root"}"')
    for key in ("vipdoc", "factors", "db", "logs", "reports", "exports"):
        text = text.replace(f'{key} = "~/.Kuantix/{key}"', f'{key} = "{tmp_path / key}"')
    # 单测直接构造调度器，不需要 serve；关调度避免任何生命周期副作用
    text = text.replace("schedule_enabled = true", "schedule_enabled = false")
    target = tmp_path / "config.toml"
    target.write_text(text, encoding="utf-8")
    return load_config(target)


class FakeProfile:
    """假 MarketProfile（只实现调度判定用到的能力）。"""

    def __init__(
        self,
        *,
        now: dt.datetime | None = None,
        trading: bool = True,
        open_now: bool = False,
    ) -> None:
        self._now = now or dt.datetime(2026, 8, 3, 16, 30, tzinfo=ZoneInfo(_TZ))
        self._trading = trading
        self._open = open_now
        self.timezone = _TZ

    def now(self) -> dt.datetime:
        return self._now

    def is_trading_day(self, date: dt.date) -> bool:
        return self._trading

    def is_open_now(self, moment: dt.datetime | None = None) -> bool:
        return self._open


class FakeSyncResult:
    def __init__(self, total: int = 10, done: int = 10, failed: int = 0) -> None:
        self.total = total
        self.done = done
        self.failed = failed
        self.quarantined = 0
        self.skipped_resumed = 0
        self.elapsed_ms = 5

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "done": self.done,
            "failed": self.failed,
            "quarantined": self.quarantined,
            "skipped_resumed": self.skipped_resumed,
            "elapsed_ms": self.elapsed_ms,
        }


class FakeHandle:
    def __init__(self, status: str = "done", error: str | None = None) -> None:
        self.status = status
        self.error = error
        self.result = FakeSyncResult() if status == "done" else None

    def wait(self, timeout: float | None = None) -> FakeSyncResult | None:
        return self.result


class FakeLake:
    def __init__(self, handle: FakeHandle | None = None, error: Exception | None = None) -> None:
        self._handle = handle if handle is not None else FakeHandle()
        self._error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def sync_incremental(
        self, market: str, workers: int | None = None, force: bool = False
    ) -> FakeHandle:
        self.calls.append((market, {"workers": workers, "force": force}))
        if self._error is not None:
            raise self._error
        return self._handle


def _make_scheduler(
    tmp_path: Path,
    profile: FakeProfile | None = None,
    lake: FakeLake | None = None,
) -> tuple[IncrementalSyncScheduler, Config, SyncStateStore]:
    config = _make_config(tmp_path)
    state = SyncStateStore(config.paths.db)
    scheduler = IncrementalSyncScheduler(
        config,
        lake if lake is not None else FakeLake(),
        state,
        profile=profile if profile is not None else FakeProfile(),
    )
    return scheduler, config, state


def _seed_day_file(config: Config) -> None:
    """在 vipdoc 下造一个 .day 文件，让空湖守卫放行。"""
    lday = config.paths.vipdoc / "sh" / "lday"
    lday.mkdir(parents=True, exist_ok=True)
    (lday / "sh600519.day").write_bytes(b"\x00")


# ---------------------------------------------------------------------------
# _should_run
# ---------------------------------------------------------------------------


def test_should_run_non_trading_day(tmp_path: Path) -> None:
    scheduler, config, _ = _make_scheduler(tmp_path, profile=FakeProfile(trading=False))
    _seed_day_file(config)
    reason = scheduler._should_run("startup")
    assert reason is not None
    assert "非交易日" in reason


def test_should_run_trading_session(tmp_path: Path) -> None:
    scheduler, config, _ = _make_scheduler(
        tmp_path, profile=FakeProfile(trading=True, open_now=True)
    )
    _seed_day_file(config)
    reason = scheduler._should_run("cron")
    assert reason is not None
    assert "交易时段内" in reason


def test_should_run_empty_lake(tmp_path: Path) -> None:
    """空湖 → 任意触发来源都跳过（不自动全量，D2.1/D-6）。"""
    scheduler, _, _ = _make_scheduler(tmp_path)
    reason = scheduler._should_run("startup")
    assert reason is not None
    assert "数据湖为空" in reason
    reason_cron = scheduler._should_run("cron")
    assert reason_cron is not None
    assert "数据湖为空" in reason_cron


def test_should_run_startup_already_synced_today(tmp_path: Path) -> None:
    # 假档案"今天"= 2026-08-03；状态文件 last_sync_at 同日 → 幂等跳过
    profile = FakeProfile(now=dt.datetime(2026, 8, 3, 16, 31, tzinfo=ZoneInfo(_TZ)))
    scheduler, config, state = _make_scheduler(tmp_path, profile=profile)
    _seed_day_file(config)
    state.update(
        at=dt.datetime(2026, 8, 3, 16, 30, tzinfo=ZoneInfo(_TZ)),
        status="done",
        trigger="cron",
    )
    reason = scheduler._should_run("startup")
    assert reason is not None
    assert "今日已同步" in reason


def test_should_run_ok_when_conditions_met(tmp_path: Path) -> None:
    scheduler, config, _ = _make_scheduler(tmp_path)
    _seed_day_file(config)
    assert scheduler._should_run("startup") is None
    assert scheduler._should_run("cron") is None
    assert scheduler._should_run("manual") is None


# ---------------------------------------------------------------------------
# run_once
# ---------------------------------------------------------------------------


def test_run_once_done_updates_state(tmp_path: Path) -> None:
    lake = FakeLake(handle=FakeHandle(status="done"))
    scheduler, config, state = _make_scheduler(tmp_path, lake=lake)
    _seed_day_file(config)
    outcome = scheduler.run_once("manual")
    assert outcome["dispatched"] is True
    assert outcome["status"] == "done"
    assert lake.calls[0][0] == "CN"
    view = state.view()
    assert view is not None
    assert view["status"] == "done"
    assert view["trigger"] == "manual"
    assert view["result"]["total"] == 10
    assert view["error"] is None


def test_run_once_failed_records_error(tmp_path: Path) -> None:
    lake = FakeLake(handle=FakeHandle(status="failed", error="网络超时"))
    scheduler, config, state = _make_scheduler(tmp_path, lake=lake)
    _seed_day_file(config)
    outcome = scheduler.run_once("manual")
    assert outcome["dispatched"] is True
    assert outcome["status"] == "failed"
    view = state.view()
    assert view is not None
    assert view["status"] == "failed"
    assert "网络超时" in (view["error"] or "")


def test_run_once_exception_records_failed(tmp_path: Path) -> None:
    lake = FakeLake(error=RuntimeError("boom"))
    scheduler, config, state = _make_scheduler(tmp_path, lake=lake)
    _seed_day_file(config)
    outcome = scheduler.run_once("manual")
    assert outcome["dispatched"] is True
    assert outcome["status"] == "failed"
    view = state.view()
    assert view is not None
    assert view["status"] == "failed"
    assert "boom" in (view["error"] or "")


def test_run_once_skip_records_skipped(tmp_path: Path) -> None:
    """非交易日 run-once → skipped + reason（不触网，FakeLake 零调用）。"""
    lake = FakeLake()
    scheduler, config, state = _make_scheduler(
        tmp_path, profile=FakeProfile(trading=False), lake=lake
    )
    _seed_day_file(config)
    outcome = scheduler.run_once("manual")
    assert outcome["dispatched"] is False
    assert "非交易日" in outcome["reason"]
    assert lake.calls == []
    view = state.view()
    assert view is not None
    assert view["status"] == "skipped"
    assert view["trigger"] == "manual"


# ---------------------------------------------------------------------------
# flock 单例锁
# ---------------------------------------------------------------------------


def test_acquire_lock_contention(tmp_path: Path) -> None:
    scheduler, config, _ = _make_scheduler(tmp_path)
    lock_path = config.paths.db / SYNC_LOCK_FILENAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # 先持有一把锁
    import fcntl

    holder = open(lock_path, "a+")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert scheduler._acquire_lock() is False
    finally:
        holder.close()
    # 释放后可抢到
    assert scheduler._acquire_lock() is True
    scheduler._release_lock()


def test_release_lock_allows_reacquire(tmp_path: Path) -> None:
    scheduler, config, _ = _make_scheduler(tmp_path)
    assert scheduler._acquire_lock() is True
    scheduler._release_lock()
    assert scheduler._acquire_lock() is True
    scheduler._release_lock()


# ---------------------------------------------------------------------------
# _parse_schedule_time
# ---------------------------------------------------------------------------


def test_parse_schedule_time_valid() -> None:
    assert IncrementalSyncScheduler._parse_schedule_time("16:30") == (16, 30)
    assert IncrementalSyncScheduler._parse_schedule_time("9:05") == (9, 5)
    assert IncrementalSyncScheduler._parse_schedule_time("00:00") == (0, 0)


def test_parse_schedule_time_invalid() -> None:
    with pytest.raises(DataIntegrityError):
        IncrementalSyncScheduler._parse_schedule_time("16:70")
    with pytest.raises(DataIntegrityError):
        IncrementalSyncScheduler._parse_schedule_time("25:00")
    with pytest.raises(DataIntegrityError):
        IncrementalSyncScheduler._parse_schedule_time("16-30")
    with pytest.raises(DataIntegrityError):
        IncrementalSyncScheduler._parse_schedule_time("")
