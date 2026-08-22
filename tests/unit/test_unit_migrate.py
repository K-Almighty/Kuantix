"""Migrator 纯单测（T02：vipdoc → market.db 一次性迁移）。

用 :class:`VipdocWriter` 真实写少量 ``.day`` 文件构造样本 vipdoc，
再经 :class:`Migrator` 导入 market.db，验证：
- 日线条数与数值一致（往返）；
- ``--verify`` 抽样比对零 mismatch；
- catalog JSON / 旧断点 JSON 导入；
- ``--dry-run`` 只扫描不写库。
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from Kuantix.adapters.vipdoc_writer import VipdocWriter
from Kuantix.core.contracts import Bar
from Kuantix.data.market_store import MarketStore
from Kuantix.data.migrate import Migrator

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
    """用真实 VipdocWriter 写 3 只标的 .day（sh/sz 混合）。"""
    writer = VipdocWriter(vipdoc_root, verify_tail_bars=3)
    sh = [_bar(D1, 10.0), _bar(D2, 10.5), _bar(D3, 11.0)]
    sz = [_bar(D1, 20.0), _bar(D2, 20.3)]
    writer.write_daily(sh, "sh", "600000")
    writer.write_daily(sh, "sh", "600036")
    writer.write_daily(sz, "sz", "002415")


@pytest.fixture()
def vipdoc(tmp_path: Path) -> Path:
    root = tmp_path / "vipdoc"
    _write_vipdoc(root)
    return root


@pytest.fixture()
def store(tmp_path: Path) -> MarketStore:
    return MarketStore(tmp_path / "db" / "market.db")


def test_migrate_daily_bars_roundtrip(vipdoc: Path, store: MarketStore) -> None:
    migrator = Migrator(store, vipdoc_root=vipdoc)
    report = migrator.migrate(market="CN")
    assert report.files_scanned == 3
    assert report.files_ok == 3
    assert report.files_failed == 0
    assert report.bars_imported == 3 + 3 + 2
    # 读回比对
    bars = store.read_daily_bars("CN", "600000")
    assert len(bars) == 3
    assert bars[-1].close == 11.0
    assert len(store.read_daily_bars("CN", "002415")) == 2


def test_migrate_verify_no_mismatch(vipdoc: Path, store: MarketStore) -> None:
    migrator = Migrator(store, vipdoc_root=vipdoc)
    migrator.migrate(market="CN")
    mismatches, verified = migrator.verify(sample=10, market="CN")
    assert verified == 3
    assert mismatches == []


def test_migrate_dry_run_writes_nothing(vipdoc: Path, store: MarketStore) -> None:
    migrator = Migrator(store, vipdoc_root=vipdoc)
    report = migrator.migrate(market="CN", dry_run=True)
    assert report.files_scanned == 3
    assert report.files_ok == 3
    assert report.bars_imported == 8  # 只统计不写
    assert store.daily_bar_count() == 0


def test_migrate_catalog_json(vipdoc: Path, store: MarketStore, tmp_path: Path) -> None:
    catalog = tmp_path / "security_catalog.json"
    catalog.write_text(
        json.dumps(
            [
                {
                    "code": "600000",
                    "exchange": "sh",
                    "market": "CN",
                    "security_type": "SH_A_STOCK",
                    "name": "浦发银行",
                },
                {
                    "code": "002415",
                    "exchange": "sz",
                    "market": "CN",
                    "security_type": "SZ_A_STOCK",
                    "name": "平安银行",
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    migrator = Migrator(store, vipdoc_root=vipdoc)
    report = migrator.migrate(catalog_path=catalog, market="CN")
    assert report.securities_imported == 2
    secs = store.list_securities("CN")
    assert len(secs) == 2
    assert {s.code for s in secs} == {"600000", "002415"}


def test_migrate_checkpoint_json(vipdoc: Path, store: MarketStore, tmp_path: Path) -> None:
    checkpoint = tmp_path / "sync_checkpoint_CN.json"
    checkpoint.write_text(
        json.dumps(
            {
                "market": "CN",
                "years": 10,
                "completed": ["600000", "600036"],
                "quarantined": ["002415"],
                "failed": [],
            }
        ),
        encoding="utf-8",
    )
    migrator = Migrator(store, vipdoc_root=vipdoc)
    report = migrator.migrate(checkpoint_path=checkpoint, market="CN")
    assert report.checkpoints_imported == 3
    cp = store.load_checkpoint("CN")
    assert cp["completed"] == {"600000", "600036"}
    assert cp["quarantined"] == {"002415"}


def test_migrate_missing_catalog_fail_loud(store: MarketStore, tmp_path: Path) -> None:
    from Kuantix.core.fail_loud import DataIntegrityError

    migrator = Migrator(store, vipdoc_root=tmp_path / "vipdoc")
    with pytest.raises(DataIntegrityError):
        migrator.migrate(catalog_path=tmp_path / "nope.json", market="CN")


def test_migrate_corrupt_checkpoint_fail_loud(vipdoc: Path, store: MarketStore, tmp_path: Path) -> None:
    from Kuantix.core.fail_loud import DataIntegrityError

    checkpoint = tmp_path / "sync_checkpoint_CN.json"
    checkpoint.write_text("not-json{", encoding="utf-8")
    migrator = Migrator(store, vipdoc_root=vipdoc)
    with pytest.raises(DataIntegrityError):
        migrator.migrate(checkpoint_path=checkpoint, market="CN")


# --------------------------------------------------------------------------- #
# P0 数据正确性回归：migrate 归属规则 = 目录为准 + 代码段校验（不依赖
# exchange_for_code 的歧义判定）。000xxx 既是上证指数（sh000001 上证指数）又是
# 深市主板 A 股（sz000001 平安银行 / sz000002 万科），目录已知时深市 000 段
# A 股必须入库、上证指数段不入 A 股池、代码段非法文件显式跳过（NF-26）。
# --------------------------------------------------------------------------- #


def _write_vipdoc_sz_000(vipdoc_root: Path) -> None:
    """写 sh600000 + sz000002 + sh510300（深市 000 段 A 股场景）。"""
    writer = VipdocWriter(vipdoc_root, verify_tail_bars=3)
    sh = [_bar(D1, 10.0), _bar(D2, 10.5), _bar(D3, 11.0)]
    sz = [_bar(D1, 20.0), _bar(D2, 20.3)]
    writer.write_daily(sh, "sh", "600000")
    writer.write_daily(sz, "sz", "000002")
    writer.write_daily(sh, "sh", "510300")


def test_migrate_sz_000xxx_a_stock_imported(tmp_path: Path) -> None:
    """回归：深市 000xxx 段 A 股必须入库（根因 sz000002 被 exchange_for_code
    误判为 sh 而丢弃）。"""
    vipdoc = tmp_path / "vipdoc"
    _write_vipdoc_sz_000(vipdoc)
    store = MarketStore(tmp_path / "db" / "market.db")
    migrator = Migrator(store, vipdoc_root=vipdoc)
    report = migrator.migrate(market="CN")
    assert report.files_scanned == 3
    assert report.files_ok == 3
    assert report.files_skipped == 0
    assert store.has_data("CN", "000002")
    bars = store.read_daily_bars("CN", "000002")
    assert bars[-1].close == 20.3
    # verify 走同一套归属规则，深市 000 段也在抽样内且零 mismatch
    mismatches, verified = migrator.verify(sample=10, market="CN")
    assert verified == 3
    assert mismatches == []


def test_migrate_sh_index_conflict_sz_wins(tmp_path: Path) -> None:
    """主键冲突：sh000001（上证指数）与 sz000001（平安银行）同 code，
    A 股优先（sz 胜出），sh 指数跳过不入库。"""
    vipdoc = tmp_path / "vipdoc"
    writer = VipdocWriter(vipdoc, verify_tail_bars=3)
    index = [_bar(D1, 3000.0), _bar(D2, 3001.0)]
    stock = [_bar(D1, 20.0), _bar(D2, 20.3)]
    writer.write_daily(index, "sh", "000001")
    writer.write_daily(stock, "sz", "000001")
    store = MarketStore(tmp_path / "db" / "market.db")
    migrator = Migrator(store, vipdoc_root=vipdoc)
    report = migrator.migrate(market="CN")
    assert report.files_scanned == 2
    assert report.files_ok == 1
    assert report.files_skipped == 1
    # 000001 在 A 股池内必须是平安银行（sz 文件）数据，而非上证指数
    bars = store.read_daily_bars("CN", "000001")
    assert bars[-1].close == pytest.approx(20.3)
    assert bars[0].open == pytest.approx(19.9)  # sz 文件首日 open（指数为 2999.9）


def test_migrate_sh_index_only_skipped(tmp_path: Path) -> None:
    """无冲突时 sh 上证指数（head3=000）也跳过：数据湖是 A 股研究平台，
    指数不进 A 股池（避免将来与 sz A 股主键冲突）。"""
    vipdoc = tmp_path / "vipdoc"
    writer = VipdocWriter(vipdoc, verify_tail_bars=3)
    writer.write_daily([_bar(D1, 3000.0), _bar(D2, 3001.0)], "sh", "000001")
    store = MarketStore(tmp_path / "db" / "market.db")
    migrator = Migrator(store, vipdoc_root=vipdoc)
    report = migrator.migrate(market="CN")
    assert report.files_scanned == 1
    assert report.files_ok == 0
    assert report.files_skipped == 1
    assert store.daily_bar_count() == 0
    assert not store.has_data("CN", "000001")


def test_migrate_illegal_segment_skipped_fail_loud(tmp_path: Path) -> None:
    """代码段与目录不符（北交所段混入 sh/sz 目录的垃圾文件）显式跳过并计数，
    不静默入库（NF-26），也不影响同目录合法文件。"""
    vipdoc = tmp_path / "vipdoc"
    writer = VipdocWriter(vipdoc, verify_tail_bars=3)
    writer.write_daily([_bar(D1, 10.0), _bar(D2, 10.5)], "sh", "600000")
    # 垃圾文件：bj 代码段混入 sh 目录（0 字节即可——归属判定只看目录+代码段，
    # 跳过后根本不会被读取解码）
    sh_lday = vipdoc / "sh" / "lday"
    sh_lday.mkdir(parents=True, exist_ok=True)
    (sh_lday / "sh430047.day").write_bytes(b"")
    store = MarketStore(tmp_path / "db" / "market.db")
    migrator = Migrator(store, vipdoc_root=vipdoc)
    report = migrator.migrate(market="CN")
    assert report.files_scanned == 2
    assert report.files_ok == 1
    assert report.files_skipped == 1
    assert report.files_failed == 0
    assert store.has_data("CN", "600000")
    assert not store.has_data("CN", "430047")
