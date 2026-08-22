"""T03 DataLake 独立验收（A1-A4）。

与工程师白盒单测（tests/unit/test_unit_data_layer.py）**刻意错开样本与路径**：
- A1 断点续传：工程师用「已完成 code 跳过 + 失败入隔离区」的混合场景；
  本验收用**全新代码池**走完整「跑两遍 + checkpoint 落盘内容核对」，并断言
  第二次 ``skipped_resumed == total``（全部被续传跳过）。
- A2 隔离区持久化：工程师直接测 QuarantineStore 的 upsert/remove；本验收从
  SyncEngine 全链路触发 UNKNOWN 异常，再**新建实例**验证 SQLite 重启可见。
- A3 verify 报告：工程师只测「缺失交易日 + 隔离区」两段；本验收构造
  干净/缺日/损坏三类文件 + 预置隔离区，四段（coverage/missing/corrupt/quarantined）
  同时断言，并核对 ``to_dict()`` 契约字段类型。
- A4 NF-28 交易时段软限制：monkeypatch ``is_open_now=True`` 验证
  ``force=False`` 显式抛错、``force=True`` 放行（注入假引擎避免触网）。

红线自查：本文件无 ``except: pass`` / 双参 ``.get(k, 默认)``（R4）；全部离线。
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from Kuantix.core.contracts import Bar, QuarantineEntry, Security
from Kuantix.core.fail_loud import NotSupportedError, UnknownValueError


def _bar(date: dt.date, close: float = 10.2) -> Bar:
    """构造一根合法日线（open/low/high/close 自洽）。"""
    return Bar(
        date=date,
        open=10.0,
        high=10.5,
        low=9.8,
        close=close,
        vol=1000.0,
        amount=10200.0,
    )


def _securities(*codes: str) -> tuple[Security, ...]:
    """按 6 位代码构造 A 股 Security（sh 前缀段用 sh，其余 sz）。"""
    out: list[Security] = []
    for code in codes:
        out.append(
            Security(
                code=code,
                exchange="sh" if code.startswith("6") else "sz",
                market="CN",
                security_type="SH_A_STOCK",
                name=code,
            )
        )
    return tuple(out)


class _FakeFetcher:
    """可编程假 fetcher：可配置对指定 code 抛 UNKNOWN 异常。"""

    def __init__(self, fail_codes: set[str] | None = None) -> None:
        self._fail_codes = set(fail_codes or ())
        self.fetch_count = 0

    def fetch_kline(self, market: str, code: str, years: int) -> list[Bar]:
        self.fetch_count += 1
        if code in self._fail_codes:
            raise UnknownValueError(
                f"[fail-loud/NF-26] 测试用 UNKNOWN 证券类型: {code}"
            )
        return [_bar(dt.date(2024, 1, 2))]


class _FakeWriter:
    """记录写盘调用的假 writer（不真正落盘）。"""

    def __init__(self) -> None:
        self.written: list[tuple[str, str, int]] = []

    def write_daily(self, bars, exchange, code, **kwargs):
        self.written.append((exchange, code, len(bars)))
        return SimpleNamespace(security_type="SH_A_STOCK", price_coeff=0.01, vol_coeff=0.01)


# ---------------------------------------------------------------------------
# A1 断点续传
# ---------------------------------------------------------------------------


def test_acc_checkpoint_resume_filters_completed(tmp_path: Path) -> None:
    from Kuantix.data.quarantine import QuarantineStore
    from Kuantix.data.sync_engine import SyncEngine, SyncPlan

    checkpoint = tmp_path / "sync_checkpoint.json"
    store = QuarantineStore(tmp_path / "db")
    secs = _securities("600000", "600036", "601318")

    def make_plan() -> SyncPlan:
        return SyncPlan(
            market="CN",
            years=1,
            securities=secs,
            vipdoc_root=tmp_path / "vipdoc",
            workers=2,
            min_request_interval=0.0,
            retry_backoff_seconds=0.0,
            retry_max_attempts=1,
            checkpoint_path=checkpoint,
            event_bus=False,
        )

    def make_engine() -> SyncEngine:
        return SyncEngine(
            fetcher_factory=_FakeFetcher,
            writer=_FakeWriter(),
            quarantine=store,
        )

    # 第一次全量：3 只全部成功
    handle = make_engine().run(make_plan())
    result = handle.wait(timeout=10)
    assert handle.status == "done"
    assert result is not None
    assert result.done == 3
    assert result.skipped_resumed == 0
    assert set(result.completed_codes) == {"600000", "600036", "601318"}

    # checkpoint 已落盘，包含市场与已完成 codes
    assert checkpoint.is_file()
    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert payload["market"] == "CN"
    assert set(payload["completed"]) == {"600000", "600036", "601318"}

    # 第二次（同 checkpoint）：已完成 codes 被过滤 → 全部跳过
    handle2 = make_engine().run(make_plan())
    result2 = handle2.wait(timeout=10)
    assert handle2.status == "done"
    assert result2 is not None
    assert result2.total == 3
    assert result2.done == 0
    assert result2.skipped_resumed == 3


# ---------------------------------------------------------------------------
# A2 隔离区持久化（跨实例重启）
# ---------------------------------------------------------------------------


def test_acc_quarantine_persists_across_restart(tmp_path: Path) -> None:
    from Kuantix.data.quarantine import QuarantineStore
    from Kuantix.data.sync_engine import SyncEngine, SyncPlan

    db_dir = tmp_path / "db"
    store = QuarantineStore(db_dir)
    fetcher = _FakeFetcher(fail_codes={"430047"})  # 430047 触发 UNKNOWN → 隔离区
    plan = SyncPlan(
        market="CN",
        years=1,
        securities=_securities("600000", "430047"),
        vipdoc_root=tmp_path / "vipdoc",
        workers=2,
        min_request_interval=0.0,
        retry_backoff_seconds=0.0,
        retry_max_attempts=1,
        checkpoint_path=tmp_path / "sync_checkpoint.json",
        event_bus=False,
    )
    engine = SyncEngine(
        fetcher_factory=lambda: fetcher,
        writer=_FakeWriter(),
        quarantine=store,
    )
    handle = engine.run(plan)
    result = handle.wait(timeout=10)
    assert result is not None
    assert result.done == 1
    assert result.quarantined == 1
    assert "430047" in result.quarantined_codes

    # 重启：全新 QuarantineStore 实例（同一 db_dir）→ 仍能看到
    store2 = QuarantineStore(db_dir)
    codes = {e.code for e in store2.list("CN")}
    assert "430047" in codes
    assert "600000" not in codes  # 成功的标的不得误入隔离区

    # remove 后消失
    assert store2.remove("430047", "CN") == 1
    assert store2.count("CN") == 0


# ---------------------------------------------------------------------------
# A3 verify 报告完整性（四段）
# ---------------------------------------------------------------------------


def test_acc_verify_report_four_sections(tmp_path: Path) -> None:
    from Kuantix.adapters.vipdoc_writer import VipdocWriter
    from Kuantix.core.market import get_market_profile
    from Kuantix.data.quarantine import QuarantineStore
    from Kuantix.data.verify import verify_vipdoc

    lday = tmp_path / "sh" / "lday"
    lday.mkdir(parents=True, exist_ok=True)
    writer = VipdocWriter(tmp_path)

    # 1) 干净文件：2024-01-02..01-05 连续 4 个交易日
    dates_clean = [
        dt.date(2024, 1, 2),
        dt.date(2024, 1, 3),
        dt.date(2024, 1, 4),
        dt.date(2024, 1, 5),
    ]
    writer.write_daily(
        [_bar(d) for d in dates_clean], "sh", "600000", path=lday / "sh600000.day"
    )
    # 2) 缺交易日文件：抽掉 01-04
    dates_gap = [dt.date(2024, 1, 2), dt.date(2024, 1, 3), dt.date(2024, 1, 5)]
    writer.write_daily(
        [_bar(d) for d in dates_gap], "sh", "600036", path=lday / "sh600036.day"
    )
    # 3) 损坏文件：垃圾字节（读回为空 → 记入 corrupt）
    (lday / "sh601318.day").write_bytes(b"\x00\x01\x02 garbage not vipdoc")

    store = QuarantineStore(tmp_path / "db")
    now = dt.datetime.now()
    store.add(
        QuarantineEntry(
            code="430047",
            market="CN",
            reason="UNKNOWN_SECURITY_TYPE",
            detail="bj 前缀",
            occurred_at=now,
            last_try=now,
        )
    )

    report = verify_vipdoc(tmp_path, "CN", get_market_profile("CN"), store)

    # coverage 段：3 只 / 3 文件 / 7 根 K 线 / 日期范围
    assert report.coverage["securities"] == 3
    assert report.coverage["files"] == 3
    assert report.coverage["total_bars"] == 7
    assert report.coverage["first_date"] == "2024-01-02"
    assert report.coverage["last_date"] == "2024-01-05"

    # missing 段
    assert dt.date(2024, 1, 4) in report.missing_days

    # corrupt 段
    assert any("sh601318.day" in c for c in report.corrupt)

    # quarantined 段
    assert len(report.quarantined) == 1

    # to_dict 契约字段类型（R1.1 口径：missing_days 为 YYYY-MM-DD 字符串数组）
    d = report.to_dict()
    assert d["market"] == "CN"
    assert all(isinstance(x, str) and len(x) == 10 for x in d["missing_days"])
    assert isinstance(d["corrupt"], list)
    assert "generated_at" in d
    assert d["quarantined"][0]["reason"] == "UNKNOWN_SECURITY_TYPE"


# ---------------------------------------------------------------------------
# A4 NF-28 交易时段软限制
# ---------------------------------------------------------------------------


def test_acc_sync_full_blocked_during_trading_hours(tmp_path: Path, monkeypatch) -> None:
    from Kuantix.core.market import get_market_profile
    from Kuantix.data.datalake import DataLake
    from Kuantix.data.quarantine import QuarantineStore

    profile = get_market_profile("CN")
    monkeypatch.setattr(profile, "is_open_now", lambda moment=None: True)

    paths = SimpleNamespace(
        root=tmp_path,
        vipdoc=tmp_path / "vipdoc",
        factors=tmp_path / "factors",
        db=tmp_path / "db",
        logs=tmp_path / "logs",
        reports=tmp_path / "reports",
        exports=tmp_path / "exports",
    )
    sync_cfg = SimpleNamespace(
        workers=2,
        default_years=1,
        page_size=100,
        min_request_interval=0.0,
        retry_backoff_seconds=0.0,
        retry_max_attempts=1,
        verify_tail_bars=3,
        verify_price_tolerance=0.001,
    )
    config = SimpleNamespace(paths=paths, sync=sync_cfg)

    class _EnumResult:
        securities = _securities("600000", "600036")
        rejected: list[QuarantineEntry] = []

    class _FakeEnumerator:
        def enumerate_full(self, market: str) -> _EnumResult:
            return _EnumResult()

    store = QuarantineStore(tmp_path / "db")
    lake = DataLake(
        config=config,
        # 注入假 factory：本用例不触网，仅需让 __init__ 不读 config.tdx
        factory=SimpleNamespace(),
        enumerator=_FakeEnumerator(),
        quarantine=store,
        writer=_FakeWriter(),
    )

    # 交易时段 + force=False → 显式抛错（NF-28，fail-loud）
    with pytest.raises(NotSupportedError):
        lake.sync_full("CN", years=1)

    # force=True 可绕过软限制；用假引擎避免触网
    captured: dict[str, object] = {}

    class _FakeHandle:
        status = "running"

    class _FakeEngine:
        def run(self, plan):
            captured["plan"] = plan
            return _FakeHandle()

    monkeypatch.setattr(lake, "_build_engine", lambda: _FakeEngine())
    handle = lake.sync_full("CN", years=1, force=True)
    assert handle.status == "running"
    plan = captured["plan"]
    assert plan.market == "CN"
    assert len(plan.securities) == 2
