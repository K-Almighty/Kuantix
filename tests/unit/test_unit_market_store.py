"""MarketStore 纯单测（T01：SQLite 行情主存储四表 CRUD / 批量 / 并发）。

白盒贴近实现：直接构造 :class:`~Kuantix.data.market_store.MarketStore`，
验证 securities / daily_bars / sync_meta / sync_checkpoint 四张表的
upsert、批量读写、断点续传 O(1)、并发无锁死（busy_timeout 生效）。
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import threading
from pathlib import Path

import pandas as pd
import pytest

from Kuantix.core.contracts import Bar, Security
from Kuantix.core.fail_loud import MissingConfigError, MissingKeyError
from Kuantix.data.market_store import (
    CHECKPOINT_STATUS_COMPLETED,
    CHECKPOINT_STATUS_FAILED,
    CHECKPOINT_STATUS_QUARANTINED,
    MarketStore,
    MinuteBar,
)

D1 = dt.date(2024, 1, 2)
D2 = dt.date(2024, 1, 3)
D3 = dt.date(2024, 1, 4)


def _bar(day: dt.date, close: float = 10.0) -> Bar:
    return Bar(
        date=day,
        open=close - 0.1,
        high=close + 0.2,
        low=close - 0.3,
        close=close,
        vol=1000.0,
        amount=close * 1000.0 * 100.0,
    )


def _sec(code: str, name: str = "") -> Security:
    return Security(
        code=code,
        exchange="sh" if code.startswith("6") else "sz",
        market="CN",
        security_type="SH_A_STOCK" if code.startswith("6") else "SZ_A_STOCK",
        name=name,
    )


@pytest.fixture()
def store(tmp_path: Path) -> MarketStore:
    return MarketStore(tmp_path / "db" / "market.db")


def test_schema_tables_created(store: MarketStore) -> None:
    """四张表 + WAL 就位。"""
    with sqlite3.connect(str(store.db_path)) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    names = {row[0] for row in rows}
    assert {"securities", "daily_bars", "sync_meta", "sync_checkpoint"} <= names
    mode = sqlite3.connect(str(store.db_path)).execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal"


def test_upsert_securities_idempotent(store: MarketStore) -> None:
    """upsert 幂等：重复写更新 name 不新增行。"""
    store.upsert_securities([_sec("600000", "浦发银行")])
    store.upsert_securities([_sec("600000", "浦发银行(改)"), _sec("000001", "平安银行")])
    secs = store.list_securities("CN")
    assert len(secs) == 2
    by_code = {s.code: s for s in secs}
    assert by_code["600000"].name == "浦发银行(改)"


def _mbar(date: int, time: int, close: float) -> MinuteBar:
    return MinuteBar(
        market="CN", code="600000", date=date, time=time,
        open=close - 0.1, high=close + 0.2, low=close - 0.3,
        close=close, vol=1000.0, amount=close * 1000.0 * 100.0,
    )


def test_write_read_minute_bars_partitioned(store: MarketStore) -> None:
    """分钟线按月份分区落库，跨分区合并读取有序（F1 存储侧）。"""
    bars = [
        _mbar(20240102, 930, 10.0),
        _mbar(20240102, 1000, 10.5),
        _mbar(20240201, 930, 11.0),  # 不同月份 -> 不同分区文件
    ]
    written = store.write_minute_bars("CN", "600000", bars, partition="month")
    assert written == 3

    # 跨月份合并读取，按 (date, time) 升序
    out = store.read_minute_bars("CN", "600000")
    assert [b.date for b in out] == [20240102, 20240102, 20240201]
    assert len(out) == 3

    # 单分区读取
    jan = store.read_minute_bars("CN", "600000", partition="2024-01")
    assert len(jan) == 2

    # upsert 幂等：重写首条不新增
    store.write_minute_bars("CN", "600000", [_mbar(20240102, 930, 10.0)])
    assert len(store.read_minute_bars("CN", "600000", partition="2024-01")) == 2


def test_write_minute_bars_empty_noop(store: MarketStore) -> None:
    """空列表安全返回 0，不创建分区文件。"""
    assert store.write_minute_bars("CN", "600000", []) == 0


def test_upsert_empty_raises(store: MarketStore) -> None:
    with pytest.raises(MissingConfigError):
        store.upsert_securities([])


def test_search_securities_code_prefix(store: MarketStore) -> None:
    store.upsert_securities(
        [_sec("600000", "浦发银行"), _sec("600036", "招商银行"), _sec("000001", "平安银行")]
    )
    hits = store.search_securities("6000", "CN")
    assert [h.code for h in hits] == ["600000", "600036"]
    # 名称前缀（NOCASE）
    hits_name = store.search_securities("浦发", "CN")
    assert [h.code for h in hits_name] == ["600000"]


def test_write_read_daily_bars_roundtrip(store: MarketStore) -> None:
    bars = [_bar(D1, 10.0), _bar(D2, 10.5), _bar(D3, 11.0)]
    assert store.write_daily_bars("CN", "600000", bars) == 3
    readback = store.read_daily_bars("CN", "600000")
    assert len(readback) == 3
    assert readback[0].date == D1
    assert readback[-1].date == D3
    assert readback[-1].close == 11.0
    # 无数据返回空列表（合法态）
    assert store.read_daily_bars("CN", "999999") == []


def test_write_daily_bars_upsert_overwrites(store: MarketStore) -> None:
    store.write_daily_bars("CN", "600000", [_bar(D1, 10.0)])
    store.write_daily_bars("CN", "600000", [_bar(D1, 12.0), _bar(D2, 12.5)])
    readback = store.read_daily_bars("CN", "600000")
    assert len(readback) == 2
    assert readback[0].close == 12.0  # 冲突日被覆盖


def test_has_data_and_last_bar_date(store: MarketStore) -> None:
    assert not store.has_data("CN", "600000")
    assert store.last_bar_date("CN", "600000") is None
    store.write_daily_bars("CN", "600000", [_bar(D1), _bar(D2)])
    assert store.has_data("CN", "600000")
    assert store.last_bar_date("CN", "600000") == 20240103


def test_read_daily_frames_batch(store: MarketStore) -> None:
    store.write_daily_bars("CN", "600000", [_bar(D1), _bar(D2)])
    store.write_daily_bars("CN", "600036", [_bar(D1)])
    frames = store.read_daily_frames(["600000", "600036"], "CN")
    assert set(frames.keys()) == {"600000", "600036"}
    assert list(frames["600000"].columns) == [
        "datetime", "open", "high", "low", "close", "vol", "amount",
    ]
    assert isinstance(frames["600000"], pd.DataFrame)
    assert len(frames["600000"]) == 2
    assert len(frames["600036"]) == 1


def test_checkpoint_roundtrip(store: MarketStore) -> None:
    store.save_checkpoint("CN", {"600000", "600036"}, {"000001"}, {"999999"})
    cp = store.load_checkpoint("CN")
    assert cp == {
        "completed": {"600000", "600036"},
        "quarantined": {"000001"},
        "failed": {"999999"},
    }
    # 幂等更新：completed → quarantined 迁移状态
    store.save_checkpoint("CN", {"600000"}, {"000001", "600036"}, set())
    cp2 = store.load_checkpoint("CN")
    assert "600036" in cp2["quarantined"]
    assert "600036" not in cp2["completed"]


def test_checkpoint_status_constants() -> None:
    assert CHECKPOINT_STATUS_COMPLETED == "completed"
    assert CHECKPOINT_STATUS_QUARANTINED == "quarantined"
    assert CHECKPOINT_STATUS_FAILED == "failed"


def test_sync_meta_roundtrip(store: MarketStore) -> None:
    assert store.sync_meta_view("CN") is None
    store.save_sync_meta("CN", last_full_sync="2026-08-01T10:00:00", total_securities=3, total_bars=10)
    view = store.sync_meta_view("CN")
    assert view is not None
    assert view["market"] == "CN"
    assert view["total_bars"] == 10
    # 增量同步不覆盖 full 时间戳（COALESCE）
    store.save_sync_meta("CN", last_incremental_sync="2026-08-02T10:00:00")
    view2 = store.sync_meta_view("CN")
    assert view2["last_full_sync"] == "2026-08-01T10:00:00"
    assert view2["last_incremental_sync"] == "2026-08-02T10:00:00"


def test_counts_and_summary(store: MarketStore) -> None:
    store.upsert_securities([_sec("600000"), _sec("600036")])
    store.write_daily_bars("CN", "600000", [_bar(D1), _bar(D2)])
    counts = store.counts()
    assert counts["securities"] == 2
    assert counts["daily_bars"] == 2
    assert counts["sync_checkpoint"] == 0
    summary = store.summary()
    assert summary["backend"] == "sqlite"
    assert summary["db_path"] == str(store.db_path)


def test_concurrent_writes_no_locked(store: MarketStore, tmp_path: Path) -> None:
    """4 线程 × 125 标的各 10 行：busy_timeout 生效，无 database is locked。"""
    store2 = MarketStore(tmp_path / "db" / "market_concurrent.db")
    errors: list[Exception] = []

    def worker(offset: int) -> None:
        try:
            for i in range(125):
                code = f"{600000 + offset * 125 + i}"
                bars = [_bar(D1 + dt.timedelta(days=d)) for d in range(10)]
                store2.write_daily_bars("CN", code, bars)
        except Exception as exc:  # noqa: BLE001 - 收集并发异常供断言
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(w,)) for w in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert store2.daily_bar_count() == 4 * 125 * 10


def test_bulk_context_restores_synchronous(store: MarketStore) -> None:
    """bulk 窗口内写连接走 synchronous=OFF，退出后恢复（白盒断言标志）。"""
    assert not getattr(store, "_bulk_sync_off", False)
    with store.bulk():
        assert getattr(store, "_bulk_sync_off", False) is True
        # 窗口内写连接确实命中 OFF（用一条 store 内部连接自证）
        with store._connect() as conn:
            mode = conn.execute("PRAGMA synchronous").fetchone()[0]
        assert mode == 0  # OFF
    assert getattr(store, "_bulk_sync_off", False) is False
    # 退出后新连接恢复默认（FULL=2）
    with store._connect() as conn:
        mode = conn.execute("PRAGMA synchronous").fetchone()[0]
    assert mode == 2


def test_bulk_nested_rejected(store: MarketStore) -> None:
    from Kuantix.core.fail_loud import DataIntegrityError

    with store.bulk():
        with pytest.raises(DataIntegrityError):
            with store.bulk():
                pass
