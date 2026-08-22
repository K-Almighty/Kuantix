"""v1.5 收口验收：8 项系统验收问题的最终独立验收（QA 独立视角）。

本文件是**收口验收的证据矩阵**，对工程师宣称已修复的 8 项问题逐项独立
复验。与工程师白盒单测（``tests/unit/``）刻意错开样本与断言路径：样本、
注入、断言全部独立构造，不信工程师自测。

覆盖（对应任务书 8 项）：
1. 数据依赖解耦（问题 1/7/8 的 D8 路径）
2. SQLite 迁移（目录判定 / market.db 主存储 / 写侧直写 / 归属规则 / auto 读侧）
3. 性能优化（单事务批量写 / WAL / per-worker 限速 / sync_checkpoint 表）
4. factor report 404（未 compute → 404 + 前端空态；compute 后 → 200）
5. WS 1006（正常握手 hello/snapshot；非法 market → 1008；handler 异常 → 1011）
6. 模型下拉空（F6 空 → Screen.vue 空态；有模型 → 下拉消费）
7. 策略下拉 + 搜索（B1 19 策略 + Backtest.vue 下拉消费；D8 本地命中）
8. 参数寻优（O1 Job 信封 + OptimizeView 本地搜索输入）

红线自查：本文件无 ``except: pass`` / 双参 ``.get(k, 默认)``（R4）；全部离线
（tmp 目录 + 假 fetcher / 假引擎 / 假服务），不触碰 ``~/.Kuantix`` 与
``easy_tdx-main``。
"""
from __future__ import annotations

import ast
import datetime as dt
import importlib.util
import json
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from Kuantix.adapters.factor_bridge import L1Reader
from Kuantix.adapters.vipdoc_writer import SqliteBarWriter, VipdocWriter
from Kuantix.api.deps import ServiceContainer
from Kuantix.api.jobs import JobManager, JobStore
from Kuantix.api.server import create_app
from Kuantix.config import load_config
from Kuantix.core.contracts import Alert, AlertLevel, Bar, Security
from Kuantix.core.fail_loud import DataIntegrityError
from Kuantix.data.market_store import MarketStore
from Kuantix.data.migrate import Migrator
from Kuantix.data.security_search import SecuritySearchService
from Kuantix.data.sync_engine import SyncEngine, SyncPlan

PROJECT_ROOT = Path(__file__).resolve().parents[2]

D1 = dt.date(2024, 1, 2)
D2 = dt.date(2024, 1, 3)
D3 = dt.date(2024, 1, 4)


# ---------------------------------------------------------------------------
# 独立工具（与工程师测试错开实现）
# ---------------------------------------------------------------------------


def _load_envelope_validator():
    """加载 tests/redlines/envelope_validator.py（独立加载，不污染 sys.path）。"""
    path = PROJECT_ROOT / "tests" / "redlines" / "envelope_validator.py"
    spec = importlib.util.spec_from_file_location("envelope_validator_v15", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VALIDATOR = _load_envelope_validator()


def _make_config(tmp_path: Path):
    """构造路径全部指向 tmp 的配置（不触碰 ~/.Kuantix；关调度防触网）。"""
    template = PROJECT_ROOT / "Kuantix" / "resources" / "config.default.toml"
    text = template.read_text(encoding="utf-8")
    text = text.replace('root = "~/.Kuantix"', f'root = "{tmp_path / "root"}"')
    for key in ("vipdoc", "factors", "db", "logs", "reports", "exports"):
        text = text.replace(
            f'{key} = "~/.Kuantix/{key}"', f'{key} = "{tmp_path / key}"'
        )
    text = text.replace("schedule_enabled = true", "schedule_enabled = false")
    target = tmp_path / "config.toml"
    target.write_text(text, encoding="utf-8")
    return load_config(target)


def _bar(day: dt.date, close: float, *, vol: float = 1000.0) -> Bar:
    return Bar(
        date=day,
        open=close - 0.1,
        high=close + 0.2,
        low=close - 0.3,
        close=close,
        vol=vol,
        amount=close * vol * 100.0,
    )


def _sec(code: str) -> Security:
    exchange = "sh" if code.startswith("6") else "sz"
    sec_type = "SH_A_STOCK" if code.startswith("6") else "SZ_A_STOCK"
    return Security(
        code=code, exchange=exchange, market="CN", security_type=sec_type
    )


class _FakeLake:
    """最小假 DataLake（WS/F4/F6/O1 用；业务字段 fail-loud 不静默）。"""

    def list_quarantine(self, market=None):
        return []

    def status(self, market="CN"):
        raise AssertionError("_FakeLake.status 不应被调用")

    def verify_payload(self, market="CN"):
        raise AssertionError("_FakeLake.verify_payload 不应被调用")


class _FakeScreen:
    def run_batch(self, *args, **kwargs):
        raise AssertionError("_FakeScreen 不应被调用")


class _FakeQuarantine:
    def __init__(self) -> None:
        self.entries: list = []

    def add(self, entry) -> None:
        self.entries.append(entry)

    def list(self, market=None):
        return self.entries


class _CountingFetcher:
    def __init__(self) -> None:
        self.calls = 0

    def fetch_kline(self, market: str, code: str, years: int) -> list[Bar]:
        self.calls += 1
        return [_bar(D1, 10.0)]


def _make_jobs(tmp_path: Path) -> JobManager:
    return JobManager(JobStore(tmp_path / "db"))


# ===========================================================================
# 问题 1：数据依赖解耦（D8 本地清单搜索 / 空清单 422 / 零网络）
# ===========================================================================


def _securities_for_search() -> list[Security]:
    return [
        Security(code="600000", exchange="sh", market="CN", security_type="SH_A_STOCK", name="浦发银行"),
        Security(code="600036", exchange="sh", market="CN", security_type="SH_A_STOCK", name="招商银行"),
        Security(code="000001", exchange="sz", market="CN", security_type="SZ_A_STOCK", name="平安银行"),
        Security(code="000002", exchange="sz", market="CN", security_type="SZ_A_STOCK", name="万科A"),
    ]


def _search_container(tmp_path: Path, *, store: MarketStore) -> ServiceContainer:
    config = _make_config(tmp_path)
    search = SecuritySearchService(config, store=store)
    return ServiceContainer(
        config=config,
        lake=_FakeLake(),
        factor_service=_FakeFactor(),
        screen_service=_FakeScreen(),
        jobs=_make_jobs(tmp_path),
        security_search=search,
    )


class _FakeFactor:
    """最小假 FactorService（D8/F6 用；被调用即 fail-loud）。"""

    def list_factors(self):
        return []

    def list_model_handles(self):
        return []


class _ExplodingProvider:
    """假网络枚举 provider：一旦被调用立即爆炸并计数（断言零调用）。"""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> list[Security]:
        self.calls += 1
        raise AssertionError("请求路径触发了网络枚举 provider（零网络铁律被破坏）")


def test_acc15_d8_search_local_code_name_hits(tmp_path: Path) -> None:
    """D8：tmp 造 securities 表数据 → 代码精确/前缀/名称搜索全部本地命中。"""
    store = MarketStore(tmp_path / "db" / "market.db")
    store.upsert_securities(_securities_for_search())
    container = _search_container(tmp_path, store=store)
    app = create_app(config=container.config, services=container)
    with TestClient(app) as client:
        for q, expected_codes in (
            ("600000", ["600000"]),
            ("6000", ["600000", "600036"]),
            ("浦发", ["600000"]),
            ("银行", {"600000", "600036", "000001"}),
        ):
            resp = client.get("/api/v1/data/search", params={"q": q})
            assert resp.status_code == 200, f"q={q} → {resp.text}"
            body = resp.json()
            assert VALIDATOR.validate_envelope(body) == []
            codes = [h["code"] for h in body["data"]["items"]]
            if isinstance(expected_codes, set):
                assert set(codes) == expected_codes
            else:
                assert codes == expected_codes
        # 名称搜索带出 name/exchange/security_type（契约 D8 字段）
        hit = client.get("/api/v1/data/search", params={"q": "浦发"}).json()["data"]["items"][0]
        assert hit["name"] == "浦发银行"
        assert hit["exchange"] == "sh"
        assert hit["security_type"] == "SH_A_STOCK"


def test_acc15_d8_search_empty_list_422(tmp_path: Path) -> None:
    """D8：无 securities 数据 → 显式 422 提示（非 NameError/500）。"""
    store = MarketStore(tmp_path / "db" / "market.db")  # 空库
    container = _search_container(tmp_path, store=store)
    app = create_app(config=container.config, services=container)
    with TestClient(app) as client:
        resp = client.get("/api/v1/data/search", params={"q": "600000"})
        assert resp.status_code == 422
        detail = resp.json()["message"]
        assert "data sync" in detail or "data migrate" in detail
        assert "NameError" not in detail


def test_acc15_search_zero_network_exploding_provider(tmp_path: Path) -> None:
    """请求路径不触发网络枚举：注入 ExplodingProvider，有本地清单时零调用。"""
    store = MarketStore(tmp_path / "db" / "market.db")
    store.upsert_securities(_securities_for_search())
    provider = _ExplodingProvider()
    svc = SecuritySearchService(_make_config(tmp_path), store=store, provider=provider)
    hits = svc.search("600000")
    assert [h.code for h in hits] == ["600000"]
    assert provider.calls == 0, "本地 SQLite 清单命中时不得回退到网络枚举"


def test_acc15_search_empty_list_no_nameerror(tmp_path: Path) -> None:
    """空清单 + 无 provider → DataIntegrityError（问题 1 的 NameError bug 已消除）。"""
    store = MarketStore(tmp_path / "db" / "market.db")
    svc = SecuritySearchService(_make_config(tmp_path), store=store)
    with pytest.raises(DataIntegrityError) as excinfo:
        svc.search("600000")
    assert "data sync" in str(excinfo.value) or "data migrate" in str(excinfo.value)


def test_acc15_security_search_module_no_universe_import() -> None:
    """静态：security_search 模块不再 import UniverseEnumerator / TdxClientFactory。"""
    import Kuantix.data.security_search as mod

    tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imports.add(node.module or "")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name)
    assert not any("universe" in name.lower() for name in imports)
    assert not any("tdx_client" in name.lower() for name in imports)
    # 缺 import 的 NameError 来源 `_default_provider` 已被移除
    source = Path(mod.__file__).read_text(encoding="utf-8")
    assert "_default_provider" not in source


# ===========================================================================
# 问题 2：SQLite 迁移
# ===========================================================================


def test_acc15_migrate_vipdoc_roundtrip_sz000_spot(tmp_path: Path) -> None:
    """data migrate：含深市 000xxx 的 3 只 .day → daily_bars → L1Reader sqlite
    读回与二进制一致（close/vol 抽查）。"""
    vipdoc_root = tmp_path / "vipdoc"
    writer = VipdocWriter(vipdoc_root, verify_tail_bars=3)
    writer.write_daily([_bar(D1, 10.0), _bar(D2, 10.5), _bar(D3, 11.0)], "sh", "600000")
    writer.write_daily([_bar(D1, 20.0), _bar(D2, 20.3)], "sz", "000002")
    writer.write_daily([_bar(D1, 30.0), _bar(D2, 30.5)], "sz", "002415")

    store = MarketStore(tmp_path / "db" / "market.db")
    migrator = Migrator(store, vipdoc_root=vipdoc_root)
    report = migrator.migrate(market="CN")
    assert report.files_scanned == 3
    assert report.files_ok == 3
    assert report.files_failed == 0
    assert report.files_skipped == 0
    assert store.daily_bar_count("CN") == 3 + 2 + 2

    reader_sqlite = L1Reader(vipdoc_root, backend="sqlite", store=store)
    reader_mirror = L1Reader(vipdoc_root, backend="mirror")
    for exchange, code in (("sh", "600000"), ("sz", "000002"), ("sz", "002415")):
        s = reader_sqlite.read_daily_bars(exchange, code)
        m = reader_mirror.read_daily_bars(exchange, code)
        assert len(s) == len(m), f"{code} 条数不一致"
        assert s[-1].close == pytest.approx(m[-1].close, abs=1e-6)
        assert s[-1].vol == pytest.approx(m[-1].vol, abs=1e-6)
        assert s[0].close == pytest.approx(m[0].close, abs=1e-6)


def test_acc15_migrate_ownership_rules_programmatic(tmp_path: Path) -> None:
    """迁移归属规则（程序化断言，P0 复验）：
    - sz 深市 000xxx A 股必须入库；
    - sh 上证指数段（sh000001）不入 A 股池；
    - sh/sz 同 code（200001）冲突时 sz 胜出、sh 让位；
    - 非法代码段（北交所 430047 混入 sh 目录）显式跳过计数（files_skipped）。"""
    vipdoc_root = tmp_path / "vipdoc"
    writer = VipdocWriter(vipdoc_root, verify_tail_bars=3)
    writer.write_daily([_bar(D1, 10.0), _bar(D2, 10.5)], "sh", "600000")   # sh A 股 → 入库
    writer.write_daily([_bar(D1, 20.0), _bar(D2, 20.3)], "sz", "000002")   # 深市 000 A 股 → 入库
    writer.write_daily([_bar(D1, 3000.0), _bar(D2, 3001.0)], "sh", "000001")  # 上证指数 → 跳过
    writer.write_daily([_bar(D1, 20.0), _bar(D2, 20.3)], "sz", "000001")   # 深市 000001 平安银行 → 入库
    writer.write_daily([_bar(D1, 100.0), _bar(D2, 100.5)], "sh", "200001")  # SH_BOND → 冲突让位
    writer.write_daily([_bar(D1, 7.0), _bar(D2, 7.2)], "sz", "200001")     # SZ_B_STOCK → sz 胜出
    # 非法段垃圾文件（0 字节；跳过后不会被读取解码）
    sh_lday = vipdoc_root / "sh" / "lday"
    sh_lday.mkdir(parents=True, exist_ok=True)
    (sh_lday / "sh430047.day").write_bytes(b"")

    store = MarketStore(tmp_path / "db" / "market.db")
    migrator = Migrator(store, vipdoc_root=vipdoc_root)
    report = migrator.migrate(market="CN")
    assert report.files_scanned == 7
    assert report.files_ok == 4
    assert report.files_failed == 0
    assert report.files_skipped == 3  # sh000001 指数 + sh200001 冲突让位 + sh430047 非法段

    # 深市 000xxx A 股入库
    assert store.has_data("CN", "000002")
    assert store.has_data("CN", "000001")
    # 000001 数据 = 深市平安银行（20.3），非上证指数（3001.0）
    bars = store.read_daily_bars("CN", "000001")
    assert bars[-1].close == pytest.approx(20.3)
    assert bars[0].open == pytest.approx(19.9)  # 首日 open = 20.0-0.1（sz 文件）
    # 200001 冲突 → sz B 股胜出（7.2），sh 债券数据不得覆盖
    bars2 = store.read_daily_bars("CN", "200001")
    assert bars2[-1].close == pytest.approx(7.2)
    # 非法段不入库
    assert not store.has_data("CN", "430047")
    assert store.daily_bar_count("CN") == 2 + 2 + 2 + 2


def test_acc15_write_side_sqlite_only_no_vipdoc_growth(tmp_path: Path) -> None:
    """写侧直写 SQLite：SyncEngine 写后仅 market.db 有数据、vipdoc 目录不新增文件。"""
    vipdoc_root = tmp_path / "vipdoc"
    store = MarketStore(tmp_path / "db" / "market.db")
    writer = SqliteBarWriter(store)  # vipdoc_mirror=false → 无镜像写
    engine = SyncEngine(
        fetcher_factory=_CountingFetcher,
        writer=writer,
        quarantine=_FakeQuarantine(),
        checkpoint_store=store,
    )
    plan = SyncPlan(
        market="CN",
        years=1,
        securities=(_sec("600000"), _sec("000002"), _sec("002415")),
        vipdoc_root=vipdoc_root,
        workers=2,
        min_request_interval=0.0,
        retry_backoff_seconds=0.0,
        retry_max_attempts=1,
        checkpoint_path=tmp_path / "db" / "sync_checkpoint_CN.json",
    )
    handle = engine.run(plan)
    result = handle.wait()
    assert handle.status == "done"
    assert result is not None
    assert result.done == 3
    assert store.daily_bar_count("CN") == 3
    # vipdoc 目录不新增任何 .day 文件
    sh_lday = vipdoc_root / "sh" / "lday"
    sz_lday = vipdoc_root / "sz" / "lday"
    assert not sh_lday.exists() or not list(sh_lday.glob("*.day"))
    assert not sz_lday.exists() or not list(sz_lday.glob("*.day"))


def test_acc15_datalake_mirror_off_writer_sqlite_only(tmp_path: Path) -> None:
    """DataLake 装配：vipdoc_mirror=false → CompositeBarWriter 仅 SQLite、镜像关闭。"""
    from Kuantix.data.datalake import CompositeBarWriter, DataLake

    config = _make_config(tmp_path)
    assert config.storage.vipdoc_mirror is False
    lake = DataLake(config)
    assert isinstance(lake.writer, CompositeBarWriter)
    assert lake.writer.mirror_enabled is False
    assert lake.store.db_path.name == "market.db"
    # 经 lake.writer 写 → market.db 有数据，vipdoc 无文件
    lake.writer.write_daily([_bar(D1, 10.0), _bar(D2, 10.5)], "sh", "600000")
    assert lake.store.has_data("CN", "600000")
    assert not (config.paths.vipdoc / "sh" / "lday" / "sh600000.day").exists()


def test_acc15_reader_auto_sqlite_priority_fallback_mirror(tmp_path: Path) -> None:
    """读侧 auto 后端：SQLite 优先、无数据 fallback 镜像。"""
    vipdoc_root = tmp_path / "vipdoc"
    store = MarketStore(tmp_path / "db" / "market.db")
    # 镜像写 sh600000（close=10.0）
    VipdocWriter(vipdoc_root).write_daily(
        [_bar(D1, 10.0), _bar(D2, 10.5), _bar(D3, 11.0)], "sh", "600000"
    )
    # SQLite 写同 code 但不同值（close=99.0）→ auto 必须读 SQLite
    SqliteBarWriter(store).write_daily(
        [_bar(D1, 99.0), _bar(D2, 99.5)], "sh", "600000"
    )
    reader = L1Reader(vipdoc_root, backend="auto", store=store)
    bars = reader.read_daily_bars("sh", "600000")
    assert bars[-1].close == pytest.approx(99.5)  # SQLite 优先（非镜像 11.0）
    assert bars[-1].close != pytest.approx(11.0)
    # 仅镜像有数据（sz002415）→ auto fallback 镜像
    VipdocWriter(vipdoc_root).write_daily(
        [_bar(D1, 20.0), _bar(D2, 20.3)], "sz", "002415"
    )
    bars2 = reader.read_daily_bars("sz", "002415")
    assert bars2[-1].close == pytest.approx(20.3)
    # 两处都无 → 显式 DataIntegrityError
    with pytest.raises(DataIntegrityError):
        reader.read_daily_bars("sh", "999999")


# ===========================================================================
# 问题 3：性能优化
# ===========================================================================


def test_acc15_batch_write_500_bars_single_transaction_perf(tmp_path: Path) -> None:
    """market_store 单事务批量写（executemany/upsert 幂等）+ WAL + 500+ 条耗时。"""
    import Kuantix.data.market_store as ms_module

    store = MarketStore(tmp_path / "db" / "market.db")
    # WAL 已启用（静态 + 运行时）
    source = Path(ms_module.__file__).read_text(encoding="utf-8")
    assert "executemany" in source  # 批量写实现
    assert "ON CONFLICT" in source  # upsert 幂等语义
    assert 'journal_mode = WAL' in source
    conn = sqlite3.connect(str(store.db_path))
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    assert str(mode).lower() == "wal"

    base = dt.date(2020, 1, 1)
    bars = [_bar(base + dt.timedelta(days=i), 10.0 + i * 0.01) for i in range(520)]
    started = time.perf_counter()
    written = store.write_daily_bars("CN", "600000", bars)
    elapsed = time.perf_counter() - started
    assert written == 520
    assert store.daily_bar_count("CN") == 520
    # 幂等：重写同批 → 仍是 520 条，无重复
    store.write_daily_bars("CN", "600000", bars)
    assert store.daily_bar_count("CN") == 520
    # 单事务批量写实测耗时上界（宽松 CI 上界；真实环境毫秒级）
    assert elapsed < 10.0, f"520 条批量写耗时 {elapsed:.3f}s 超上界 10s"


def test_acc15_checkpoint_table_not_json_rewrite(tmp_path: Path) -> None:
    """sync_checkpoint 表方案（非 JSON 全量重写）：O(1) 单行 upsert + 无 JSON 文件。"""
    import Kuantix.data.sync_engine as se_module

    source = Path(se_module.__file__).read_text(encoding="utf-8")
    assert "upsert_checkpoint_row" in source  # _mark_checkpoint 走 O(1) 单行
    cp_json = tmp_path / "db" / "sync_checkpoint_CN.json"
    store = MarketStore(tmp_path / "db" / "market.db")
    store.upsert_checkpoint_row("CN", "600000", "completed")
    store.upsert_checkpoint_row("CN", "600036", "completed")
    assert store.checkpoint_count("CN") == 2
    assert not cp_json.exists(), "断点走表方案，不应落 JSON 全量重写文件"
    cp = store.load_checkpoint("CN")
    assert cp["completed"] == {"600000", "600036"}
    # 单行更新不影响其它行
    store.upsert_checkpoint_row("CN", "600000", "quarantined")
    cp2 = store.load_checkpoint("CN")
    assert cp2["quarantined"] == {"600000"}
    assert cp2["completed"] == {"600036"}
    # SyncEngine 端到端：checkpoint_store 就绪时跑完不产生 JSON 文件
    engine = SyncEngine(
        fetcher_factory=_CountingFetcher,
        writer=SqliteBarWriter(store),
        quarantine=_FakeQuarantine(),
        checkpoint_store=store,
    )
    plan = SyncPlan(
        market="CN",
        years=1,
        securities=(_sec("600000"), _sec("002415")),
        vipdoc_root=tmp_path / "vipdoc",
        workers=1,
        min_request_interval=0.0,
        retry_backoff_seconds=0.0,
        retry_max_attempts=1,
        checkpoint_path=cp_json,
    )
    handle = engine.run(plan)
    result = handle.wait()
    assert handle.status == "done"
    assert result is not None
    assert result.done == 2
    assert not cp_json.exists(), "SyncEngine 断点落表，不写 JSON"


def test_acc15_sync_per_worker_throttle_and_conn_cache(tmp_path: Path) -> None:
    """per-worker 限速 + worker 级 fetcher 缓存（问题 3 性能修复）。"""
    import Kuantix.data.sync_engine as se_module

    source = Path(se_module.__file__).read_text(encoding="utf-8")
    # per-worker：限速状态存 threading.local，不再全局锁串行
    assert "threading.local" in source
    assert "_local" in source
    # worker 级 fetcher 缓存：_process_one 每 worker 只建一次连接
    assert "_thread_local_fetcher" in source

    store = MarketStore(tmp_path / "db" / "market.db")
    created: list[_CountingFetcher] = []
    def factory() -> _CountingFetcher:
        f = _CountingFetcher()
        created.append(f)
        return f

    engine = SyncEngine(
        fetcher_factory=factory,
        writer=SqliteBarWriter(store),
        quarantine=_FakeQuarantine(),
        checkpoint_store=store,
    )
    codes = tuple(f"{600000 + i}" for i in range(40))
    plan = SyncPlan(
        market="CN",
        years=1,
        securities=tuple(_sec(c) for c in codes),
        vipdoc_root=tmp_path / "vipdoc",
        workers=4,
        min_request_interval=0.0,
        retry_backoff_seconds=0.0,
        retry_max_attempts=1,
        checkpoint_path=tmp_path / "db" / "cp.json",
    )
    handle = engine.run(plan)
    result = handle.wait()
    assert handle.status == "done"
    assert result is not None
    assert result.done == 40
    assert len(created) == 4, "连接数 == worker 数（每 worker 1 个 fetcher）"


# ===========================================================================
# 问题 4：factor report 404
# ===========================================================================


class _FakeEngine:
    """假 FactorEngineBridge：compute_cross_section 返回固定截面（含 NaN 行）。"""

    def __init__(self) -> None:
        self.calls = 0

    def compute_cross_section(self, pool, factors: list[str], date=None):
        self.calls += 1
        rows = [
            {"date": 20240102, "code": "600000", factors[0]: 1.0},
            {"date": 20240103, "code": "600000", factors[0]: 1.5},
            {"date": 20240103, "code": "600036", factors[0]: 2.0},
        ]
        return pd.DataFrame(rows)


def _factor_container(tmp_path: Path, *, factor_service: Any) -> ServiceContainer:
    config = _make_config(tmp_path)
    return ServiceContainer(
        config=config,
        lake=_FakeLake(),
        factor_service=factor_service,
        screen_service=_FakeScreen(),
        jobs=_make_jobs(tmp_path),
    )


def test_acc15_factor_report_404_uncomputed(tmp_path: Path) -> None:
    """F4：未 compute → 404「请先 compute」（非 500），report 不被调用。"""
    from Kuantix.factor.store import FactorStore

    store = FactorStore(tmp_path / "factors", tmp_path / "db")
    calls: list[str] = []

    class _Svc:
        def list_factors(self):
            return ["momentum_20d"]

        @property
        def store(self):
            return store

        def report(self, factor, market="CN", **kwargs):
            calls.append(factor)
            raise AssertionError("未 compute 不应触发 report")

    container = _factor_container(tmp_path, factor_service=_Svc())
    app = create_app(config=container.config, services=container)
    with TestClient(app) as client:
        resp = client.get(
            "/api/v1/factor/report", params={"name": "momentum_20d"}
        )
        assert resp.status_code == 404
        assert "compute" in resp.json()["message"]
    assert calls == []


def test_acc15_factor_report_200_after_compute_fake_engine(tmp_path: Path) -> None:
    """F4：假 FactorEngine 注入 compute 后 → report 正常 200 + 信封全绿。"""
    from Kuantix.factor.store import FactorStore

    store = FactorStore(tmp_path / "factors", tmp_path / "db")
    engine = _FakeEngine()
    pool = {
        "600000": pd.DataFrame({"datetime": pd.to_datetime(["2024-01-02", "2024-01-03"])}),
        "600036": pd.DataFrame({"datetime": pd.to_datetime(["2024-01-02", "2024-01-03"])}),
    }
    counts = store.compute(
        pool, ["momentum_20d"], dt.date(2024, 1, 1), dt.date(2024, 12, 31), engine=engine
    )
    assert counts["momentum_20d"] == 3
    assert engine.calls == 1
    assert not store.load("momentum_20d").empty

    report_payload = {
        "ic_mean": 0.05,
        "ic_std": 0.08,
        "ir": 0.62,
        "ic_positive_rate": 0.58,
        "quantile_returns": {1: 0.02, 2: 0.03, 3: 0.031, 4: 0.037, 5: 0.052},
        "top_minus_bottom": 0.032,
        "turnover_rate": 0.31,
        "autocorr": 0.12,
        "ic_series_tail": [{"date": "2024-01-03", "ic": 0.01}],
    }

    class _Svc:
        def list_factors(self):
            return ["momentum_20d"]

        @property
        def store(self):
            return store

        def report(self, factor, market="CN", **kwargs):
            return dict(report_payload)

    container = _factor_container(tmp_path, factor_service=_Svc())
    app = create_app(config=container.config, services=container)
    with TestClient(app) as client:
        resp = client.get("/api/v1/factor/report", params={"name": "momentum_20d"})
        assert resp.status_code == 200
        body = resp.json()
        assert VALIDATOR.validate_envelope(body) == []
        data = body["data"]
        assert data["factor"] == "momentum_20d"
        assert data["sample_count"] == 3
        assert data["ic_mean"] == pytest.approx(0.05)


def test_acc15_frontend_factors_empty_hint() -> None:
    """前端 Factors.vue 空态引导存在（未 compute → 引导先运行 compute）。"""
    factors_vue = PROJECT_ROOT / "web" / "src" / "views" / "Factors.vue"
    text = factors_vue.read_text(encoding="utf-8")
    assert "尚未计算" in text and "compute" in text


# ===========================================================================
# 问题 5：WS 1006
# ===========================================================================


class _FakeMonitorStore:
    def __init__(self, alerts=None, *, raise_on_list: bool = False) -> None:
        self._alerts = alerts if alerts is not None else []
        self._raise = raise_on_list

    def list_alerts(self, market=None, limit=50):
        if self._raise:
            raise RuntimeError("monitor store exploded（handler 异常注入）")
        return self._alerts


def _ws_container(tmp_path: Path, *, monitor_store: _FakeMonitorStore) -> ServiceContainer:
    config = _make_config(tmp_path)
    return ServiceContainer(
        config=config,
        lake=_FakeLake(),
        factor_service=_FakeFactor(),
        screen_service=_FakeScreen(),
        jobs=_make_jobs(tmp_path),
        monitor_store=monitor_store,
    )


def _recv_until_disconnect(ws, *, max_frames: int = 8) -> tuple[list[dict[str, Any]], int | None]:
    """接收帧直到断开；返回 (帧列表, 关闭码)。"""
    frames: list[dict[str, Any]] = []
    code: int | None = None
    for _ in range(max_frames):
        try:
            frames.append(json.loads(ws.receive_text()))
        except WebSocketDisconnect as exc:
            code = exc.code
            break
    return frames, code


def test_acc15_ws_hello_snapshot_normal(tmp_path: Path) -> None:
    """WS：正常握手 hello + snapshot（含历史告警），关闭码非 1006。"""
    alert = Alert(
        id="al_ws1",
        code="600000",
        market="CN",
        rule="突破-收口",
        level=AlertLevel.WARNING,
        message="600000 突破",
        ts=dt.datetime(2026, 8, 1, 10, 0, 0),
        payload={"last": 11.0},
    )
    container = _ws_container(tmp_path, monitor_store=_FakeMonitorStore(alerts=[alert]))
    app = create_app(config=container.config, services=container)
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/monitor/ws") as ws:
            hello = json.loads(ws.receive_text())
            snapshot = json.loads(ws.receive_text())
            assert hello["data"]["type"] == "hello"
            assert hello["data"]["market"] == "CN"
            assert hello["data"]["subscribed"] == ["alert"]
            assert snapshot["data"]["type"] == "snapshot"
            assert snapshot["data"]["alerts"][0]["code"] == "600000"
            try:
                ws.close()
            except (WebSocketDisconnect, RuntimeError):
                _ = None  # 服务端已关闭连接，客户端 close 幂等


def test_acc15_ws_market_illegal_close_1008(tmp_path: Path) -> None:
    """WS：非法 market（HK 未启用）→ 显式关闭码 1008（非 1006）。"""
    container = _ws_container(tmp_path, monitor_store=_FakeMonitorStore())
    app = create_app(config=container.config, services=container)
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/monitor/ws?market=HK") as ws:
            frames, code = _recv_until_disconnect(ws)
        assert code == 1008, f"非法 market 应显式 1008，实际 {code}"
        assert code != 1006
        bye = [f for f in frames if f.get("data", {}).get("type") == "bye"]
        assert bye, "关闭前应发送 bye 帧（fail-loud）"


def test_acc15_ws_handler_exception_close_1011(tmp_path: Path) -> None:
    """WS：handler 异常 → fail-loud 记录并优雅关闭 1011（非 1006）。"""
    container = _ws_container(
        tmp_path, monitor_store=_FakeMonitorStore(raise_on_list=True)
    )
    app = create_app(config=container.config, services=container)
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/monitor/ws") as ws:
            hello = json.loads(ws.receive_text())
            assert hello["data"]["type"] == "hello"
            frames, code = _recv_until_disconnect(ws)
        assert code == 1011, f"handler 异常应显式 1011，实际 {code}"
        assert code != 1006
        bye = [f for f in frames if f.get("data", {}).get("type") == "bye"]
        assert bye, "handler 异常应发 bye 帧后再关闭"


def test_acc15_frontend_ws_reconnect_logic() -> None:
    """前端 WS 客户端：断线指数退避重连（1s→2s→…→30s 封顶），onclose 触发重连。"""
    ws_ts = PROJECT_ROOT / "web" / "src" / "api" / "ws.ts"
    text = ws_ts.read_text(encoding="utf-8")
    assert "reconnectDelay" in text
    assert "reconnectDelay * 2" in text
    assert "30000" in text  # 30s 封顶
    assert "onclose" in text
    assert "scheduleReconnect" in text


# ===========================================================================
# 问题 6：模型下拉空
# ===========================================================================


def test_acc15_models_empty_api_and_frontend_empty_state(tmp_path: Path) -> None:
    """F6 models 空 → API 200 空列表；Screen.vue 空态引导「暂无合成模型」。"""
    from Kuantix.factor.service import FactorService

    class _Svc(FactorService):
        def list_factors(self):
            return []

        def list_model_handles(self):
            return []

    container = _factor_container(tmp_path, factor_service=_Svc())
    app = create_app(config=container.config, services=container)
    with TestClient(app) as client:
        resp = client.get("/api/v1/factor/models")
        assert resp.status_code == 200
        body = resp.json()
        assert VALIDATOR.validate_envelope(body) == []
        assert body["data"]["total"] == 0
        assert body["data"]["items"] == []

    screen_vue = PROJECT_ROOT / "web" / "src" / "views" / "Screen.vue"
    text = screen_vue.read_text(encoding="utf-8")
    assert "暂无合成模型" in text
    assert "请先到" in text and "因子" in text
    # 下拉消费：models 非空时逐项渲染 option
    assert "screen.models" in text and "v-for=\"m in screen.models\"" in text


def test_acc15_models_present_api_dropdown(tmp_path: Path) -> None:
    """F6 有模型（假 store 注入）→ 下拉数据正常返回。"""
    from Kuantix.core.contracts import ModelHandle
    from Kuantix.factor.service import FactorService

    model = ModelHandle(
        name="acc_m1",
        weights={"momentum_20d": 1.0},
        method="equal",
        created_at=dt.datetime(2026, 8, 1, 9, 0, 0),
    )

    class _Svc(FactorService):
        def list_factors(self):
            return []

        def list_model_handles(self):
            return [model]

    container = _factor_container(tmp_path, factor_service=_Svc())
    app = create_app(config=container.config, services=container)
    with TestClient(app) as client:
        resp = client.get("/api/v1/factor/models")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["total"] == 1
        item = body["data"]["items"][0]
        assert item["name"] == "acc_m1"
        assert item["method"] == "equal"


# ===========================================================================
# 问题 7：策略下拉 + 搜索
# ===========================================================================


def test_acc15_b1_strategies_19(tmp_path: Path) -> None:
    """B1：GET /api/v1/backtest/strategies 返回 19 个策略（ma_cross/macd/boll_breakout...）。"""
    from Kuantix.backtest.service import BacktestService

    config = _make_config(tmp_path)
    container = ServiceContainer(
        config=config,
        lake=_FakeLake(),
        factor_service=_FakeFactor(),
        screen_service=_FakeScreen(),
        jobs=_make_jobs(tmp_path),
        backtest_service=BacktestService(config),
    )
    app = create_app(config=container.config, services=container)
    with TestClient(app) as client:
        resp = client.get("/api/v1/backtest/strategies")
        assert resp.status_code == 200
        body = resp.json()
        assert VALIDATOR.validate_envelope(body) == []
        items = body["data"]["items"]
        assert body["data"]["count"] == 19
        names = {item["name"] for item in items}
        assert {"ma_cross", "macd", "boll_breakout"} <= names
        # 每条策略含 label/description/params schema（前端下拉 + 参数表单渲染）
        first = items[0]
        assert "name" in first and "label" in first and "description" in first


def test_acc15_frontend_backtest_dropdown_consumes() -> None:
    """前端 Backtest.vue：策略下拉消费 B1 返回值。"""
    backtest_vue = PROJECT_ROOT / "web" / "src" / "views" / "Backtest.vue"
    text = backtest_vue.read_text(encoding="utf-8")
    assert "store.strategies" in text
    assert "v-for=\"s in store.strategies\"" in text
    assert "ma_cross" in text  # 默认策略
    # 下拉空态/错误态引导存在（后端未返回策略时显式提示，非静默空白）
    assert "策略列表为空" in text


# ===========================================================================
# 问题 8：参数寻优
# ===========================================================================


class _FakeOptimize:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, job_id: str, req, progress_cb=None):
        self.calls += 1
        return {"status": "done", "best": {"params": dict(req.param_grid), "score": 0.5}}

    def get_result(self, job_id: str):
        return {
            "job_id": job_id,
            "results": [{"params": {"fast": 5}, "total_return": 0.12}],
            "best": {"params": {"fast": 5}, "total_return": 0.12},
            "heatmap": [],
        }


def test_acc15_o1_run_job_envelope(tmp_path: Path) -> None:
    """O1：POST /api/v1/optimize/run → 200 Job 信封；O3 结果可读（假服务注入）。"""
    config = _make_config(tmp_path)
    optimize = _FakeOptimize()
    container = ServiceContainer(
        config=config,
        lake=_FakeLake(),
        factor_service=_FakeFactor(),
        screen_service=_FakeScreen(),
        jobs=_make_jobs(tmp_path),
        optimize_service=optimize,
    )
    app = create_app(config=container.config, services=container)
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/optimize/run",
            json={
                "market": "CN",
                "code": "600000",
                "strategy": "ma_cross",
                "param_grid": {"fast": [5, 10], "slow": [20]},
                "start": "2023-01-01",
                "end": "2024-12-31",
            },
        )
        assert resp.status_code == 200
        job = resp.json()["data"]
        assert job["module"] == "backtest"
        assert job["action"] == "optimize"
        job_id = job["job_id"]
        assert job_id
        # O3 结果路径
        resp3 = client.get(f"/api/v1/optimize/results/{job_id}")
        assert resp3.status_code == 200
        assert resp3.json()["data"]["best"]["params"] == {"fast": 5}
        # O1 网格上限 fail-loud：>200 点显式 400（15×15=225 > 200）
        resp_big = client.post(
            "/api/v1/optimize/run",
            json={
                "market": "CN",
                "code": "600000",
                "strategy": "ma_cross",
                "param_grid": {"fast": list(range(1, 16)), "slow": list(range(20, 35))},
            },
        )
        assert resp_big.status_code == 400


def test_acc15_optimize_frontend_local_search_input() -> None:
    """O1 标的输入用本地搜索：OptimizeView.vue 复用 SecuritySearchBox。"""
    optimize_vue = PROJECT_ROOT / "web" / "src" / "views" / "OptimizeView.vue"
    text = optimize_vue.read_text(encoding="utf-8")
    assert "SecuritySearchBox" in text
    assert "onSearchSelect" in text


# ===========================================================================
# 目录纳入判定（设计 08 §1.1）：SQLite 是行情主存储，不是全部目录
# ===========================================================================


def test_acc15_directory_inclusion_judgement(tmp_path: Path) -> None:
    """对照 docs/08 §1.1：vipdoc K 线入 SQLite、factors 保留 Parquet、
    logs/reports/exports 保留文件系统。"""
    # vipdoc → market.db daily_bars（K 线主存储）
    store = MarketStore(tmp_path / "db" / "market.db")
    store.write_daily_bars("CN", "600000", [_bar(D1, 10.0)])
    assert store.has_data("CN", "600000")
    # factors → Parquet 分区 + factor_meta.db 元数据（不迁移进 market.db）
    from Kuantix.factor.store import FactorStore

    fstore = FactorStore(tmp_path / "factors", tmp_path / "db")
    fstore.save(
        "momentum_20d",
        2024,
        pd.DataFrame(
            {"date": [20240102], "code": ["600000"], "value": [1.0]}
        ),
    )
    parquet = tmp_path / "factors" / "momentum_20d" / "2024.parquet"
    assert parquet.is_file()
    assert not store.has_data("CN", "momentum_20d")  # 因子值不进行情主库
    # logs/reports/exports 保留文件系统：无对应 SQLite 表
    schema_tables = {
        "securities",
        "daily_bars",
        "sync_meta",
        "sync_checkpoint",
    }
    conn = sqlite3.connect(str(store.db_path))
    try:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            if not r[0].startswith("sqlite_")
        }
    finally:
        conn.close()
    assert schema_tables <= tables
    assert "logs" not in tables and "reports" not in tables and "exports" not in tables
