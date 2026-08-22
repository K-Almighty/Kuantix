"""T03 数据层白盒单测（QuarantineStore / verify / SyncEngine 断点续传）。

验收台与红线检查器不覆盖业务层内部行为；本文件从白盒角度验证：
- QuarantineStore 幂等 upsert（attempts 累加）与 remove；
- verify 能发现缺失交易日 / 损坏文件 / 隔离区；
- SyncEngine 断点续传：已完成 code 跳过、失败入隔离区。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from Kuantix.core.contracts import Bar, QuarantineEntry
from Kuantix.data.quarantine import QuarantineStore
from Kuantix.data.sync_engine import SyncEngine, SyncPlan
from Kuantix.data.verify import verify_vipdoc
from Kuantix.core.market import get_market_profile


# ---------------------------------------------------------------------------
# QuarantineStore
# ---------------------------------------------------------------------------


def test_quarantine_upsert_increments_attempts(tmp_path: Path) -> None:
    store = QuarantineStore(tmp_path)
    now = dt.datetime(2026, 1, 1, 9, 0, 0)
    entry = QuarantineEntry(
        code="430047", market="CN", reason="UNKNOWN_SECURITY_TYPE",
        detail="first", occurred_at=now, last_try=now,
    )
    store.add(entry)
    store.add(entry)  # 再次写入 → attempts+1, last_try 刷新
    rows = store.list("CN")
    assert len(rows) == 1
    assert rows[0].attempts == 2
    assert rows[0].reason == "UNKNOWN_SECURITY_TYPE"


def test_quarantine_remove_and_count(tmp_path: Path) -> None:
    store = QuarantineStore(tmp_path)
    now = dt.datetime.now()
    for code in ("430047", "830799"):
        store.add(QuarantineEntry(
            code=code, market="CN", reason="UNKNOWN_SECURITY_TYPE",
            detail="x", occurred_at=now, last_try=now,
        ))
    assert store.count("CN") == 2
    removed = store.remove("430047", "CN")
    assert removed == 1
    assert store.count("CN") == 1
    # 不存在 → 返回 0（合法结果，非静默吞错）
    assert store.remove("999999", "CN") == 0


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


def _write_bar(writer, path: Path, exchange: str, code: str, dates: list[dt.date]) -> None:
    from Kuantix.adapters.vipdoc_writer import VipdocWriter

    writer = writer if isinstance(writer, VipdocWriter) else VipdocWriter(path)
    bars = [
        Bar(date=d, open=10.0, high=10.5, low=9.8, close=10.2, vol=1000.0, amount=10200.0)
        for d in dates
    ]
    writer.write_daily(bars, exchange, code, path=path / f"{exchange}{code}.day")


def test_verify_missing_trading_days_detected(tmp_path: Path) -> None:
    from Kuantix.adapters.vipdoc_writer import VipdocWriter

    lday = tmp_path / "sh" / "lday"
    lday.mkdir(parents=True, exist_ok=True)
    writer = VipdocWriter(tmp_path)
    # 2024-01-02..2024-01-05 写 4 天，但中间抽掉 01-04 → 缺失日
    dates = [dt.date(2024, 1, 2), dt.date(2024, 1, 3), dt.date(2024, 1, 5)]
    writer.write_daily(
        [Bar(date=d, open=10.0, high=10.5, low=9.8, close=10.2, vol=1000.0, amount=10200.0) for d in dates],
        "sh", "600000", path=lday / "sh600000.day",
    )
    profile = get_market_profile("CN")
    quarantine = QuarantineStore(tmp_path / "db")
    report = verify_vipdoc(tmp_path, "CN", profile, quarantine)
    # 2024-01-04 是交易日且缺失
    assert dt.date(2024, 1, 4) in report.missing_days
    assert report.coverage["securities"] == 1
    assert report.corrupt == []


def test_verify_reports_quarantine_entries(tmp_path: Path) -> None:
    quarantine = QuarantineStore(tmp_path)
    now = dt.datetime.now()
    quarantine.add(QuarantineEntry(
        code="430047", market="CN", reason="UNKNOWN_SECURITY_TYPE",
        detail="bj", occurred_at=now, last_try=now,
    ))
    profile = get_market_profile("CN")
    report = verify_vipdoc(tmp_path, "CN", profile, quarantine)
    assert len(report.quarantined) == 1
    assert report.quarantined[0].code == "430047"
    assert report.is_clean is False


# ---------------------------------------------------------------------------
# SyncEngine 断点续传
# ---------------------------------------------------------------------------


class _FakeFetcher:
    """离线替身：返回固定 bars 或抛错。"""

    def __init__(self, codes_ok: set[str]) -> None:
        self._ok = codes_ok

    def fetch_kline(self, market: str, code: str, years: int):
        if code not in self._ok:
            raise ConnectionError("fake network failure")
        return [
            Bar(date=dt.date(2024, 1, 2), open=10.0, high=10.5, low=9.8, close=10.2,
                vol=1000.0, amount=10200.0)
        ]


class _FakeWriter:
    def __init__(self) -> None:
        self.written: list[str] = []

    def write_daily(self, bars, exchange, code, *, path=None):
        self.written.append(code)
        return type("R", (), {"written": len(bars)})()


class _FakeQuarantine:
    def __init__(self) -> None:
        self.entries: list[QuarantineEntry] = []

    def add(self, entry: QuarantineEntry) -> None:
        self.entries.append(entry)

    def list(self, market=None):
        return self.entries


def _sec(code: str):
    from Kuantix.core.contracts import Security

    return Security(
        code=code,
        exchange="sh" if code.startswith("6") else "sz",
        market="CN",
        security_type="SH_A_STOCK" if code.startswith("6") else "SZ_A_STOCK",
    )


def test_sync_engine_resume_skips_completed(tmp_path: Path) -> None:
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text(
        '{"market": "CN", "completed": ["600000"], "updated_at": "2026-01-01"}',
        encoding="utf-8",
    )
    engine = SyncEngine(
        fetcher_factory=lambda: _FakeFetcher({"600036"}),
        writer=_FakeWriter(),
        quarantine=_FakeQuarantine(),
    )
    plan = SyncPlan(
        market="CN",
        years=1,
        securities=(_sec("600000"), _sec("600036")),
        vipdoc_root=tmp_path,
        workers=2,
        min_request_interval=0.0,
        checkpoint_path=checkpoint,
    )
    handle = engine.run(plan)
    result = handle.wait()
    assert handle.status == "done"
    assert result is not None
    assert result.done == 1  # 600000 跳过，只有 600036 成功
    assert result.skipped_resumed == 1
    assert result.completed_codes == ("600036",)


def test_sync_engine_failure_goes_quarantine(tmp_path: Path) -> None:
    quarantine = _FakeQuarantine()
    engine = SyncEngine(
        fetcher_factory=lambda: _FakeFetcher(set()),
        writer=_FakeWriter(),
        quarantine=quarantine,
    )
    plan = SyncPlan(
        market="CN",
        years=1,
        securities=(_sec("600000"),),
        vipdoc_root=tmp_path,
        workers=1,
        min_request_interval=0.0,
        retry_max_attempts=1,
        checkpoint_path=None,
    )
    handle = engine.run(plan)
    result = handle.wait()
    assert handle.status == "done"
    assert result is not None
    assert result.done == 0
    assert result.quarantined == 1
    assert quarantine.entries[0].code == "600000"


# ---------------------------------------------------------------------------
# L1Reader 损坏文件归一（QA P1：裸 ValueError/struct → DataIntegrityError 422）
# ---------------------------------------------------------------------------


def test_l1reader_corrupt_day_file_raises_data_integrity(tmp_path: Path) -> None:
    """真实 L1Reader 读损坏 .day → DataIntegrityError（NF-26，不穿透裸 ValueError）。

    回归守卫：easy_tdx read_daily_bars 对部分损坏内容抛裸 ValueError/struct 异常，
    适配层必须统一归一，否则 BacktestService.run 逐标的只捕获 DataIntegrityError
    → 裸异常穿透成 job 500（应为 422）。
    """
    from Kuantix.adapters.factor_bridge import L1Reader
    from Kuantix.core.fail_loud import DataIntegrityError

    lday = tmp_path / "sh" / "lday"
    lday.mkdir(parents=True)
    path = lday / "sh600519.day"
    for content in (b"this is not a valid day file at all" * 4, b"\x00" * 32):
        path.write_bytes(content)
        with pytest.raises(DataIntegrityError) as excinfo:
            L1Reader(tmp_path).read_daily_frame("sh", "600519")
        assert "损坏" in str(excinfo.value)
        assert "600519" in str(excinfo.value)


def test_l1reader_missing_file_raises_data_integrity(tmp_path: Path) -> None:
    """文件不存在 → DataIntegrityError（现状语义，回归锚点）。"""
    from Kuantix.adapters.factor_bridge import L1Reader
    from Kuantix.core.fail_loud import DataIntegrityError

    with pytest.raises(DataIntegrityError) as excinfo:
        L1Reader(tmp_path).read_daily_frame("sh", "600519")
    assert "不存在" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 调整 1（用户指令）：vipdoc_mirror 默认 false —— 同步直接写 SQLite，不默认双写镜像
# ---------------------------------------------------------------------------


def test_datalake_default_assembly_sqlite_only(tmp_path: Path) -> None:
    """默认配置（vipdoc_mirror=false）下 DataLake 只装配 SQLite 写后端。

    回归守卫（用户指令）：数据同步直接写 SQLite、不搞两步/双写。
    - ``CompositeBarWriter.mirror_enabled is False`` 且不构造 VipdocWriter；
    - 写侧只落 market.db，vipdoc 目录不新增任何文件；
    - 同步引擎 writer 与装配 writer 是同一个（无镜像写调用）；
    - ``status()`` 载荷 ``vipdoc_mirror=false``。
    """
    import datetime as dt
    import os

    from Kuantix.config import load_config
    from Kuantix.core.contracts import Bar
    from Kuantix.data.datalake import CompositeBarWriter, DataLake

    # Kuantix__PATHS__* 覆盖到临时目录（模板路径是 ~/.Kuantix，必须隔离）
    env = dict(os.environ)
    env.update(
        {
            "Kuantix__PATHS__ROOT": str(tmp_path),
            "Kuantix__PATHS__VIPDOC": str(tmp_path / "vipdoc"),
            "Kuantix__PATHS__FACTORS": str(tmp_path / "factors"),
            "Kuantix__PATHS__DB": str(tmp_path / "db"),
            "Kuantix__PATHS__LOGS": str(tmp_path / "logs"),
            "Kuantix__PATHS__REPORTS": str(tmp_path / "reports"),
            "Kuantix__PATHS__EXPORTS": str(tmp_path / "exports"),
        }
    )
    cfg = load_config(env=env, ensure_dirs=False)
    assert cfg.storage.vipdoc_mirror is False, "用户指令：默认必须 false"

    class _EnumResult:
        securities = []
        rejected = []

    class _FakeEnumerator:
        def enumerate_full(self, market):
            return _EnumResult()

    class _FakeFactory:
        pass

    cfg.paths.ensure()
    lake = DataLake(cfg, factory=_FakeFactory(), enumerator=_FakeEnumerator())
    writer = lake.writer
    assert isinstance(writer, CompositeBarWriter)
    assert writer.mirror_enabled is False
    assert writer._mirror is None, "vipdoc_mirror=false 时不得构造 VipdocWriter"

    bars = [
        Bar(date=dt.date(2024, 1, 2), open=10.0, high=10.5, low=9.8, close=10.2,
            vol=1000.0, amount=10200.0),
        Bar(date=dt.date(2024, 1, 3), open=10.2, high=10.8, low=10.0, close=10.6,
            vol=1200.0, amount=12720.0),
    ]
    writer.write_daily(bars, "sh", "600000")

    assert (cfg.paths.db / "market.db").exists()
    assert lake.store.daily_bar_count("CN") == 2
    assert not (cfg.paths.vipdoc / "sh" / "lday" / "sh600000.day").exists()
    assert list(cfg.paths.vipdoc.rglob("*")) == [], "vipdoc 目录必须为空（无镜像写）"

    # 同步引擎 writer 与装配 writer 是同一个 → 同步路径无镜像写调用
    engine = lake._build_engine()
    assert engine._writer is writer

    # status 载荷 vipdoc_mirror=false（契约增量字段如实反映配置）
    assert lake.status("CN")["vipdoc_mirror"] is False

    # 读回：L1Reader 从 SQLite 读到这 2 根（主存储可用）
    from Kuantix.adapters.factor_bridge import L1Reader

    reader = L1Reader(cfg.paths.vipdoc, backend="sqlite", store=lake.store)
    assert len(reader.read_daily_frame("sh", "600000")) == 2
