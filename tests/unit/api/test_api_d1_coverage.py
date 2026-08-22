"""D1 空湖判定三场景验收（UX/判定修复）。

覆盖（用户反馈 bug：508M vipdoc 镜像 4730 只却提示"数据湖为空"）：
- 场景 A（都空）：SQLite 与 vipdoc 镜像都无数据 → ``storage.source ==
  "empty"``，``coverage.securities == 0``，前端应引导「data sync 建湖」；
- 场景 B（仅镜像有 / 未迁移）：SQLite 空、vipdoc 有 ``.day`` →
  ``storage.source == "mirror_only"``，``storage.mirror_files == N``，
  ``coverage.securities == N``，前端应引导「data migrate」而不是重拉 508M；
- 场景 C（migrate 后 / 正常）：SQLite 有数据（镜像文件保留 → ``"both"``；
  删除镜像 → ``"sqlite"``）→ 前端不显示空湖引导；
- 回归锚点：SQLite 有 daily_bars 但 securities 表为空（用户实测：daily_bars
  2 行 + 镜像 4730 只）→ coverage 不得误判为 0 / ``"empty"``；
- D5 verify 报告同样带 ``storage`` 字段区分镜像/SQLite 覆盖；
- 一致性：L1Reader auto 后端批量读（``read_daily_frames``）在"仅镜像"
  状态下回退镜像（因子/选股/回测不被空湖判定误拦）。

全部离线（vipdoc 用 VipdocWriter 写小样本），TestClient 真调、真 DataLake。
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from types import SimpleNamespace

from envelope_validator import assert_envelope

from Kuantix.adapters.vipdoc_writer import VipdocWriter
from Kuantix.core.contracts import Bar


def _bar(day: dt.date, close: float = 10.2) -> Bar:
    """构造一根合法日线（open/low/high/close 自洽）。"""
    return Bar(
        date=day,
        open=close - 0.1,
        high=close + 0.2,
        low=close - 0.3,
        close=close,
        vol=1000.0,
        amount=close * 1000.0 * 100.0,
    )


def _write_mirror(vipdoc_root: Path, count: int = 2) -> None:
    """写 count 只 ``.day`` 到 vipdoc（sh 600xxx / sz 002xxx 交替）。

    Args:
        vipdoc_root: vipdoc 根目录。
        count: 文件数。
    """
    writer = VipdocWriter(vipdoc_root, verify_tail_bars=3)
    bars = [_bar(dt.date(2024, 1, 2)), _bar(dt.date(2024, 1, 3))]
    for index in range(count):
        if index % 2 == 0:
            writer.write_daily(bars, "sh", f"600{index:03d}")
        else:
            writer.write_daily(bars, "sz", f"002{index:03d}")


def _real_lake_client(tmp_path: Path):
    """构造真 DataLake + 假 factor/screen + 真 JobManager 的 TestClient。

    Returns:
        ``(TestClient, DataLake, Config)`` —— 全部离线（fake factory/enumerator，
        ``schedule_enabled=false`` 由 conftest.make_config 注入）。
    """
    from conftest import FakeFactorService, FakeScreenService, make_config
    from fastapi.testclient import TestClient

    from Kuantix.api.deps import ServiceContainer
    from Kuantix.api.jobs import JobManager, JobStore
    from Kuantix.api.server import create_app
    from Kuantix.data.datalake import DataLake

    cfg = make_config(tmp_path)
    jobs = JobManager(JobStore(tmp_path / "db"))
    lake = DataLake(cfg, factory=SimpleNamespace(), enumerator=SimpleNamespace())
    services = ServiceContainer(
        config=cfg,
        lake=lake,
        factor_service=FakeFactorService(),
        screen_service=FakeScreenService(),
        jobs=jobs,
    )
    app = create_app(config=cfg, services=services)
    return TestClient(app), lake, cfg


# ---------------------------------------------------------------------------
# 场景 A：都空（真未建湖）
# ---------------------------------------------------------------------------


def test_d1_scenario_a_both_empty(tmp_path: Path) -> None:
    """SQLite 与镜像都空 → source=empty / coverage 全 0（前端引导 data sync）。"""
    client, _lake, _cfg = _real_lake_client(tmp_path)
    payload = client.get("/api/v1/data/status").json()
    assert_envelope(payload)
    data = payload["data"]
    storage = data["storage"]
    assert storage["source"] == "empty"
    assert storage["sqlite_bars"] == 0
    assert storage["mirror_files"] == 0
    assert storage["sqlite_securities"] == 0
    assert data["coverage"]["securities"] == 0
    assert data["coverage"]["bars"] == 0
    # 旧字段兼容：storage 保留 summary 原键
    assert storage["backend"] == "sqlite"
    assert "daily_bars" in storage


# ---------------------------------------------------------------------------
# 场景 B：仅镜像有（未迁移）
# ---------------------------------------------------------------------------


def test_d1_scenario_b_mirror_only(tmp_path: Path) -> None:
    """仅镜像有 → source=mirror_only / coverage 反映镜像（前端引导 data migrate）。"""
    _write_mirror(tmp_path / "vipdoc", count=3)
    client, _lake, _cfg = _real_lake_client(tmp_path)
    payload = client.get("/api/v1/data/status").json()
    data = payload["data"]
    storage = data["storage"]
    assert storage["source"] == "mirror_only"
    assert storage["sqlite_bars"] == 0
    assert storage["mirror_files"] == 3
    assert data["coverage"]["securities"] == 3
    assert data["coverage"]["files"] == 3
    assert data["coverage"]["bars"] == 6  # 3 只 × 2 根
    assert data["data_date"] == "2024-01-03"


# ---------------------------------------------------------------------------
# 场景 C：migrate 后（正常）
# ---------------------------------------------------------------------------


def test_d1_scenario_c_migrated_both(tmp_path: Path) -> None:
    """迁移后镜像保留 → source=both（SQLite 与镜像都有数据，正常）。"""
    _write_mirror(tmp_path / "vipdoc", count=2)
    client, lake, _cfg = _real_lake_client(tmp_path)

    from Kuantix.data.migrate import Migrator

    report = Migrator(lake.store, vipdoc_root=tmp_path / "vipdoc").migrate(
        market="CN", verify=True, verify_sample=5
    )
    assert report.verify_mismatches == 0

    payload = client.get("/api/v1/data/status").json()
    data = payload["data"]
    storage = data["storage"]
    assert storage["source"] == "both"
    assert storage["sqlite_bars"] == 4  # 2 只 × 2 根
    assert storage["mirror_files"] == 2
    assert data["coverage"]["securities"] == 2
    assert data["coverage"]["bars"] == 4
    assert data["data_date"] == "2024-01-03"


def test_d1_scenario_c_sqlite_only(tmp_path: Path) -> None:
    """迁移后删除镜像 → source=sqlite（纯主存储，正常）。"""
    _write_mirror(tmp_path / "vipdoc", count=2)
    client, lake, cfg = _real_lake_client(tmp_path)

    from Kuantix.data.migrate import Migrator

    Migrator(lake.store, vipdoc_root=cfg.paths.vipdoc).migrate(market="CN")
    # 删除镜像目录，模拟"纯 SQLite 主存储"（vipdoc_mirror=false 的默认态）
    import shutil

    shutil.rmtree(cfg.paths.vipdoc / "sh")
    shutil.rmtree(cfg.paths.vipdoc / "sz")

    payload = client.get("/api/v1/data/status").json()
    data = payload["data"]
    storage = data["storage"]
    assert storage["source"] == "sqlite"
    assert storage["sqlite_bars"] == 4
    assert storage["mirror_files"] == 0
    assert data["coverage"]["securities"] == 2
    assert data["coverage"]["bars"] == 4


# ---------------------------------------------------------------------------
# 回归锚点：SQLite 有 daily_bars 但 securities 表空（用户实测场景）
# ---------------------------------------------------------------------------


def test_d1_partial_migrate_securities_table_empty(tmp_path: Path) -> None:
    """SQLite daily_bars 有数据但 securities 表为空 + 镜像有文件 → 不误判空湖。

    用户实测：market.db 只有 daily_bars 2 行（未完成 migrate）+ 镜像 4730 只，
    旧实现 coverage 走 verify_market_store（遍历 securities 表）→ 全 0 →
    前端误判"数据湖为空"并引导重拉 508M。修复后必须：
    - ``coverage.securities`` 反映有效可用标的（取 SQLite/镜像较大值）；
    - ``storage.source`` 为 ``both``（有数据即正常），前端不显示空湖引导。
    """
    _write_mirror(tmp_path / "vipdoc", count=2)
    client, lake, _cfg = _real_lake_client(tmp_path)
    # 只写 daily_bars、不写 securities 表 —— 复刻"未迁移完成"的脏状态
    lake.store.write_daily_bars("CN", "600000", [_bar(dt.date(2024, 1, 2))])

    payload = client.get("/api/v1/data/status").json()
    data = payload["data"]
    storage = data["storage"]
    assert storage["source"] == "both"
    assert storage["sqlite_bars"] == 1
    assert storage["mirror_files"] == 2
    assert data["coverage"]["securities"] == 2  # 不再误判 0
    assert data["coverage"]["bars"] == 1        # SQLite 侧实际行数
    assert data["data_date"] == "2024-01-02"    # 首末日从 daily_bars 聚合补齐


# ---------------------------------------------------------------------------
# D5 verify 报告区分镜像/SQLite 覆盖
# ---------------------------------------------------------------------------


def test_d5_verify_report_has_storage_field(tmp_path: Path) -> None:
    """D5 /verify 报告带 storage（source/sqlite_bars/mirror_files），覆盖同口径。"""
    _write_mirror(tmp_path / "vipdoc", count=1)
    client, _lake, _cfg = _real_lake_client(tmp_path)
    payload = client.get("/api/v1/data/verify").json()
    assert_envelope(payload)
    data = payload["data"]
    assert "storage" in data
    assert data["storage"]["source"] == "mirror_only"
    assert data["storage"]["mirror_files"] == 1
    assert data["coverage"]["securities"] == 1
    # 契约旧字段不变
    assert set(data["coverage"]) >= {
        "securities", "files", "bars", "disk_bytes", "first_date", "last_date",
    }


# ---------------------------------------------------------------------------
# 一致性：L1Reader auto 批量读在"仅镜像"状态回退镜像
# ---------------------------------------------------------------------------


def test_l1reader_auto_read_daily_frames_falls_back_to_mirror(tmp_path: Path) -> None:
    """auto 后端批量读：SQLite 空（仅镜像）→ 逐只回退镜像（因子喂数据可用）。"""
    from Kuantix.adapters.factor_bridge import L1Reader
    from Kuantix.data.market_store import MarketStore

    _write_mirror(tmp_path / "vipdoc", count=2)
    store = MarketStore(tmp_path / "db" / "market.db")
    reader = L1Reader(tmp_path / "vipdoc", backend="auto", store=store)
    frames = reader.read_daily_frames(["600000", "002001"], market="CN")
    assert set(frames) == {"600000", "002001"}
    assert len(frames["600000"]) == 2
    assert list(frames["600000"].columns) == [
        "datetime", "open", "high", "low", "close", "vol", "amount",
    ]


def test_l1reader_sqlite_backend_does_not_fall_back(tmp_path: Path) -> None:
    """sqlite 后端批量读：缺码不降级（NF-26 显式语义，保持原契约）。"""
    from Kuantix.adapters.factor_bridge import L1Reader
    from Kuantix.data.market_store import MarketStore

    _write_mirror(tmp_path / "vipdoc", count=2)
    store = MarketStore(tmp_path / "db" / "market.db")
    reader = L1Reader(tmp_path / "vipdoc", backend="sqlite", store=store)
    frames = reader.read_daily_frames(["600000", "002001"], market="CN")
    assert frames == {}  # SQLite 空 → 空结果，不回退镜像
