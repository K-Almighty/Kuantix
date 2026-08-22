"""T02/T03 端到端验收：SQLite 行情湖（写 → 读同构 → verify）。

覆盖（设计文档 08 测试建议）：
- 写 SQLite（SqliteBarWriter 四道闸门）→ L1Reader auto 后端读回同构
  DataFrame（列/类型/升序）；
- SQLite 无数据 → 镜像兜底；两处都无 → 显式 DataIntegrityError；
- ``verify_market_store`` 对 SQLite 湖出完整 VerifyReport；
- 迁移往返（Migrator vipdoc → market.db → --verify 零 mismatch）。

全部离线（vipdoc 用 VipdocWriter 真实写小样本）。
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd

from Kuantix.adapters.factor_bridge import L1Reader
from Kuantix.adapters.vipdoc_writer import SqliteBarWriter, VipdocWriter
from Kuantix.core.contracts import Bar
from Kuantix.core.fail_loud import DataIntegrityError
from Kuantix.core.market import get_market_profile
from Kuantix.data.market_store import MarketStore
from Kuantix.data.migrate import Migrator
from Kuantix.data.quarantine import QuarantineStore
from Kuantix.data.verify import verify_market_store

D1 = dt.date(2024, 1, 2)
D2 = dt.date(2024, 1, 3)
D3 = dt.date(2024, 1, 4)


def _bar(day: dt.date, close: float) -> Bar:
    return Bar(
        date=day,
        open=close - 0.1,
        high=close + 0.2,
        low=close - 0.3,
        close=close,
        vol=1000.0,
        amount=close * 1000.0 * 100.0,
    )


def _write_vipdoc(vipdoc_root: Path) -> None:
    writer = VipdocWriter(vipdoc_root, verify_tail_bars=3)
    sh = [_bar(D1, 10.0), _bar(D2, 10.5), _bar(D3, 11.0)]
    sz = [_bar(D1, 20.0), _bar(D2, 20.3)]
    writer.write_daily(sh, "sh", "600000")
    writer.write_daily(sz, "sz", "002415")


def test_sqlite_write_read_iso(tmp_path: Path) -> None:
    """SqliteBarWriter 写 → L1Reader(auto) 读回同构 DataFrame。"""
    store = MarketStore(tmp_path / "db" / "market.db")
    writer = SqliteBarWriter(store)
    writer.write_daily([_bar(D1, 10.0), _bar(D2, 10.5), _bar(D3, 11.0)], "sh", "600000")

    reader = L1Reader(tmp_path / "vipdoc", backend="auto", store=store)
    frame = reader.read_daily_frame("sh", "600000")
    assert list(frame.columns) == ["datetime", "open", "high", "low", "close", "vol", "amount"]
    assert len(frame) == 3
    assert frame["close"].iloc[-1] == 11.0
    # 升序
    assert frame["datetime"].is_monotonic_increasing


def test_auto_fallback_mirror(tmp_path: Path) -> None:
    """SQLite 无数据 → 镜像（vipdoc）兜底。"""
    _write_vipdoc(tmp_path / "vipdoc")
    store = MarketStore(tmp_path / "db" / "market.db")
    reader = L1Reader(tmp_path / "vipdoc", backend="auto", store=store)
    bars = reader.read_daily_bars("sh", "600000")
    assert len(bars) == 3
    # 两处都无 → 显式 DataIntegrityError
    import pytest

    with pytest.raises(DataIntegrityError):
        reader.read_daily_bars("sh", "999999")


def test_verify_market_store(tmp_path: Path) -> None:
    """verify_market_store 对 SQLite 湖出完整 VerifyReport。"""
    store = MarketStore(tmp_path / "db" / "market.db")
    writer = SqliteBarWriter(store)
    writer.write_daily([_bar(D1, 10.0), _bar(D2, 10.5), _bar(D3, 11.0)], "sh", "600000")
    writer.write_daily([_bar(D1, 20.0), _bar(D2, 20.3)], "sz", "002415")
    from Kuantix.core.contracts import Security

    store.upsert_securities(
        [
            Security(code="600000", exchange="sh", market="CN", security_type="SH_A_STOCK"),
            Security(code="002415", exchange="sz", market="CN", security_type="SZ_A_STOCK"),
        ]
    )
    quarantine = QuarantineStore(tmp_path / "db")
    profile = get_market_profile("CN")
    report = verify_market_store(store, "CN", profile, quarantine)
    assert report.coverage["securities"] == 2
    assert report.coverage["total_bars"] == 5
    assert report.corrupt == []
    assert report.missing_days == []


def test_migrate_then_reader_consistency(tmp_path: Path) -> None:
    """迁移后 L1Reader 从 SQLite 读回与二进制一致（抽查 2 只）。"""
    vipdoc_root = tmp_path / "vipdoc"
    _write_vipdoc(vipdoc_root)
    store = MarketStore(tmp_path / "db" / "market.db")
    migrator = Migrator(store, vipdoc_root=vipdoc_root)
    report = migrator.migrate(market="CN", verify=True, verify_sample=5)
    assert report.verify_mismatches == 0

    reader_sqlite = L1Reader(vipdoc_root, backend="sqlite", store=store)
    reader_mirror = L1Reader(vipdoc_root, backend="mirror")
    for exchange, code in (("sh", "600000"), ("sz", "002415")):
        sqlite_bars = reader_sqlite.read_daily_bars(exchange, code)
        mirror_bars = reader_mirror.read_daily_bars(exchange, code)
        assert len(sqlite_bars) == len(mirror_bars)
        for s, m in zip(sqlite_bars, mirror_bars):
            assert s.date == m.date
            assert abs(s.close - m.close) < 1e-6
            assert abs(s.vol - m.vol) < 1e-6
