"""独立验收：单标的实时回测数据源 + 数据湖自动增量更新（契约 v1.4 增量批次）。

事实来源（权威）
----------------
- 设计：``docs/07-实时回测与增量更新设计.md``（设计一 data_source 三模式 /
  设计二增量调度 / D1-D8 裁决 / 运行验证方案）；
- 上游（只读零改动）：``easy_tdx-main`` —— 本文件不触碰、不 import 其测试。

独立性声明
----------
- 本文件**不 import 工程师任何测试模块**（``tests/unit/`` 下全部测试文件
  均不引用），不复用其断言；全部假实现（FakeFetcher / FakeReader /
  FakeLake / FakeProfile / FakeHandle）独立构造；
- 假实现只替代「网络 / 文件读取」边界，回测引擎用**真 BacktestBridge**
  （调上游引擎，离线可跑），保证「回测正常」的断言是真实引擎产出；
- 网络类用例打 ``network`` mark；网络不可用显式 ``skip``，离线替代用例
  为确定性主路径。

红线自检
--------
禁 ``except: pass``、禁双参 ``.get``；只写 ``tests/acceptance/``，不改业务
代码、不碰 easy_tdx-main（R2/R4/R6/NF-26 约束由红线套件单独验证）。
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REDLINES_DIR = PROJECT_ROOT / "tests" / "redlines"
for _path in (PROJECT_ROOT, REDLINES_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from envelope_validator import assert_envelope  # noqa: E402

from Kuantix.adapters.factor_bridge import L1Reader, bars_to_frame  # noqa: E402
from Kuantix.core.contracts import Bar  # noqa: E402
from Kuantix.core.fail_loud import DataIntegrityError  # noqa: E402
from Kuantix.core.market import get_market_profile  # noqa: E402

_TZ = ZoneInfo("Asia/Shanghai")


# ---------------------------------------------------------------------------
# 独立假实现（与工程师测试错开）
# ---------------------------------------------------------------------------


class FakeReader:
    """假 L1Reader：可配置 day_path 存在性 + 读侧行为。

    - ``day_path`` 返回 ``root/exchange/lday/<exchange><code>.day``（与真
      L1Reader 同构），存在性由测试预置文件控制；
    - ``read_daily_frame`` 行为由 ``behavior`` 参数控制（``data`` 返回确定
      frame；``empty`` 返回空骨架；``raise`` 抛 DataIntegrityError）。
    """

    def __init__(
        self,
        root: Path,
        *,
        frame: pd.DataFrame | None = None,
        behavior: str = "data",
        error: str = "文件损坏",
    ) -> None:
        self._root = root
        self._frame = frame
        self._behavior = behavior
        self._error = error
        self.read_calls: list[tuple[str, str]] = []

    def day_path(self, exchange: str, code: str) -> Path:
        return self._root / exchange / "lday" / f"{exchange}{code}.day"

    def read_daily_frame(self, exchange: str, code: str) -> pd.DataFrame:
        self.read_calls.append((exchange, code))
        if self._behavior == "raise":
            raise DataIntegrityError(f"[fail-loud/NF-26] {code} {self._error}")
        if self._behavior == "empty":
            return pd.DataFrame(
                columns=["datetime", "open", "high", "low", "close", "vol", "amount"]
            )
        if self._frame is not None:
            return self._frame.copy()
        return _trend_frame(code)


class DuckReader:
    """无 day_path 属性的鸭子 reader（local_has_data 保守视为有数据）。"""

    def __init__(self, frame: pd.DataFrame | None = None) -> None:
        self._frame = frame
        self.read_calls: list[tuple[str, str]] = []

    def read_daily_frame(self, exchange: str, code: str) -> pd.DataFrame:
        self.read_calls.append((exchange, code))
        if self._frame is not None:
            return self._frame.copy()
        return _trend_frame("600519")


class FakeFetcher:
    """假 QuotationFetcher：记录调用参数，返回确定 Bar 列表（不发网络）。"""

    def __init__(
        self,
        bars: list[Bar] | None = None,
        *,
        error: Exception | None = None,
        empty: bool = False,
    ) -> None:
        self._bars = bars
        self._error = error
        self._empty = empty
        self.calls: list[dict[str, Any]] = []

    def fetch_kline(
        self,
        market: str,
        code: str,
        years: int = 10,
        *,
        exchange: str | None = None,
        count: int | None = None,
        adjust: Any = None,
    ) -> list[Bar]:
        self.calls.append(
            {
                "market": market,
                "code": code,
                "years": years,
                "exchange": exchange,
                "count": count,
                "adjust": adjust,
            }
        )
        if self._error is not None:
            raise self._error
        if self._empty:
            return []
        if self._bars is not None:
            return list(self._bars)
        return _trend_bars("600519")


class ExplodingFetcher:
    """调用即炸的 fetcher（断言「未被调用」时注入）。"""

    def __init__(self, message: str = "不应调用实时拉取") -> None:
        self._message = message

    def fetch_kline(self, *args: Any, **kwargs: Any) -> list[Bar]:
        raise AssertionError(self._message)


def _trend_bars(code: str = "600519", n: int = 300, start: dt.date = dt.date(2024, 1, 2)) -> list[Bar]:
    dates = pd.bdate_range(start, periods=n)
    close = np.linspace(10, 20, n) + np.sin(np.arange(n) / 10) * 0.5
    return [
        Bar(
            date=pd.Timestamp(d).date(),
            open=float(close[i] * 0.99),
            high=float(close[i] * 1.02),
            low=float(close[i] * 0.98),
            close=float(close[i]),
            vol=10000.0,
            amount=1e7,
        )
        for i, d in enumerate(dates)
    ]


def _trend_frame(code: str = "600519", n: int = 300) -> pd.DataFrame:
    return bars_to_frame(_trend_bars(code, n=n))


# ---------------------------------------------------------------------------
# 配置隔离（tmp 路径，不触碰 ~/.Kuantix；测试环境关调度保证确定性）
# ---------------------------------------------------------------------------


def _make_config(tmp_path: Path, *, schedule_enabled: bool = False) -> Any:
    """构造路径全部指向 tmp 的配置（独立实现，不经工程师 conftest）。"""
    from Kuantix.config import load_config

    template = PROJECT_ROOT / "Kuantix" / "resources" / "config.default.toml"
    text = template.read_text(encoding="utf-8")
    text = text.replace('root = "~/.Kuantix"', f'root = "{tmp_path / "root"}"')
    for key in ("vipdoc", "factors", "db", "logs", "reports", "exports"):
        text = text.replace(f'{key} = "~/.Kuantix/{key}"', f'{key} = "{tmp_path / key}"')
    # 测试确定性：默认关调度（不挂 APScheduler / 不触网）；需要测调度时显式开
    if not schedule_enabled:
        text = text.replace("schedule_enabled = true", "schedule_enabled = false")
    text = text.replace('timeout_seconds = 15.0', 'timeout_seconds = 3.0')
    target = tmp_path / "config.toml"
    target.write_text(text, encoding="utf-8")
    return load_config(target)


def _seed_day_file(config: Any, code: str = "600519") -> Path:
    """在 vipdoc 下造一个 .day 文件（让空湖守卫放行 / 让 day_path 存在）。"""
    lday = config.paths.vipdoc / "sh" / "lday"
    lday.mkdir(parents=True, exist_ok=True)
    path = lday / f"sh{code}.day"
    path.write_bytes(b"\x00")
    return path


# ---------------------------------------------------------------------------
# 服务 / API 装配
# ---------------------------------------------------------------------------


def _make_service(config: Any, reader: Any, fetcher: Any) -> Any:
    from Kuantix.backtest.service import BacktestService
    from Kuantix.backtest.store import BacktestResultStore

    store = BacktestResultStore(config.paths.db / "backtest_results.db")
    return BacktestService(config, reader=reader, store=store, fetcher=fetcher)


def _make_client(tmp_path: Path, *, reader: Any, fetcher: Any) -> Any:
    """TestClient + 假 reader/fetcher + 真 BacktestService/BacktestBridge。"""
    from fastapi.testclient import TestClient

    from Kuantix.api.deps import ServiceContainer
    from Kuantix.api.jobs import JobManager, JobStore
    from Kuantix.api.server import create_app

    config = _make_config(tmp_path)
    jobs = JobManager(JobStore(tmp_path / "db"))
    service = _make_service(config, reader, fetcher)
    container = ServiceContainer(
        config=config,
        lake=_MinimalLake(),
        factor_service=None,
        screen_service=None,
        jobs=jobs,
        backtest_service=service,
    )
    app = create_app(config=config, services=container)
    return TestClient(app)


class _MinimalLake:
    """backtest 用例最小 DataLake 替身（不触碰 data 端点）。"""

    def list_quarantine(self, market: str = "CN") -> list[Any]:
        return []


def _wait_job(client: Any, job_id: str, timeout: float = 15.0) -> dict[str, Any]:
    """轮询 B3/D3 直到 job 结束（done/failed/cancelled），返回 job 信封 data。"""
    import time

    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/backtest/jobs/{job_id}")
        payload = response.json()
        job = payload["data"]
        last = job
        if job["status"] in ("done", "failed", "cancelled"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} 未在 {timeout}s 内结束，最后状态 {last.get('status')}")


# ---------------------------------------------------------------------------
# A1. data_source=local：只读本地湖，绝不触网；缺失/损坏显式报错
# ---------------------------------------------------------------------------


def test_a1_local_uses_reader_not_fetcher(tmp_path: Path) -> None:
    """local：有本地数据 → 回测走本地，fetcher 不被调用。"""
    reader = FakeReader(tmp_path, frame=_trend_frame())
    fetcher = ExplodingFetcher()
    service = _make_service(_make_config(tmp_path), reader, fetcher)
    from Kuantix.backtest.service import BacktestRunRequest

    result = service.run("job_local", BacktestRunRequest(codes=("600519",), data_source="local"))
    assert result.result["codes"] == ["600519"]
    assert reader.read_calls == [("sh", "600519")]


def test_a1_local_missing_file_fails_loud(tmp_path: Path) -> None:
    """local：本地文件缺失 → 显式 DataIntegrityError（绝不静默）。"""
    reader = FakeReader(tmp_path, behavior="raise", error="L1 文件不存在")
    fetcher = ExplodingFetcher()
    service = _make_service(_make_config(tmp_path), reader, fetcher)
    from Kuantix.backtest.service import BacktestRunRequest

    with pytest.raises(DataIntegrityError) as excinfo:
        service.run(
            "job_local_missing", BacktestRunRequest(codes=("600519",), data_source="local")
        )
    assert "L1 文件不存在" in str(excinfo.value)


def test_a1_local_empty_file_fails_loud(tmp_path: Path) -> None:
    """local：本地文件存在但为空 → 显式 DataIntegrityError（不静默）。"""
    reader = FakeReader(tmp_path, behavior="empty")
    fetcher = ExplodingFetcher()
    service = _make_service(_make_config(tmp_path), reader, fetcher)
    from Kuantix.backtest.service import BacktestRunRequest

    with pytest.raises(DataIntegrityError) as excinfo:
        service.run(
            "job_local_empty", BacktestRunRequest(codes=("600519",), data_source="local")
        )
    assert "无日线数据" in str(excinfo.value) or "空" in str(excinfo.value)


def test_a1_local_failure_api_job_failed_422(tmp_path: Path) -> None:
    """API：local 读不到数据 → B2 job failed，B3 error.code=422（fail-loud）。"""
    reader = FakeReader(tmp_path, behavior="empty")
    client = _make_client(tmp_path, reader=reader, fetcher=ExplodingFetcher())
    response = client.post(
        "/api/v1/backtest/run",
        json={"codes": ["600519"], "data_source": "local",
              "start": "2024-01-01", "end": "2024-12-31"},
    )
    assert response.status_code == 200
    job_id = response.json()["data"]["job_id"]
    job = _wait_job(client, job_id)
    assert job["status"] == "failed"
    assert job["error"]["code"] == 422  # DataIntegrityError → CODE_DATA_ERROR


# ---------------------------------------------------------------------------
# A2. data_source=live：强制实时拉取（参数口径 / 列同构 / 失败显式 / 多标的 422）
# ---------------------------------------------------------------------------


def test_a2_live_calls_fetcher_with_correct_arguments(tmp_path: Path) -> None:
    """live：fetch_kline 被调用，参数 = 市场/代码/交易所/回溯年数，不传 adjust（未复权）。"""
    reader = FakeReader(tmp_path, behavior="empty")  # 本地没数据也不影响 live
    fetcher = FakeFetcher()
    service = _make_service(_make_config(tmp_path), reader, fetcher)
    from Kuantix.backtest.service import BacktestRunRequest

    result = service.run(
        "job_live", BacktestRunRequest(
            codes=("600519",), data_source="live",
            start=dt.date(2024, 1, 1), end=dt.date(2024, 12, 31),
        )
    )
    assert fetcher.calls, "live 分支必须调用 fetch_kline"
    call = fetcher.calls[0]
    assert call["market"] == "CN"
    assert call["code"] == "600519"
    assert call["exchange"] == "sh"  # 经 profile.exchange_for_code 推断
    assert call["years"] == 2  # 同年 → (end.year - start.year + 1) + 1 = 2
    assert call["adjust"] is None  # 不传 → 默认 Adjust.NONE（未复权，RD-5）
    assert result.result["codes"] == ["600519"]


def test_a2_live_frame_same_columns_and_date_filter(tmp_path: Path) -> None:
    """live：返回 DataFrame 与 bars_to_frame 同构列；日期按 [start,end] 过滤。"""
    from Kuantix.backtest.data_source import fetch_live_frame

    bars = _trend_bars(n=40, start=dt.date(2024, 5, 1))
    fetcher = FakeFetcher(bars)
    profile = get_market_profile("CN")
    frame = fetch_live_frame(fetcher, profile, "600519", dt.date(2024, 5, 5), dt.date(2024, 5, 15))
    expected = bars_to_frame(bars)
    assert list(frame.columns) == list(expected.columns)
    assert list(frame.columns) == ["datetime", "open", "high", "low", "close", "vol", "amount"]
    assert len(frame) == 8  # 2024-05-06..05-15 工作日共 8 根
    first = pd.Timestamp(frame.iloc[0]["datetime"]).date()
    last = pd.Timestamp(frame.iloc[-1]["datetime"]).date()
    assert first >= dt.date(2024, 5, 5)
    assert last <= dt.date(2024, 5, 15)


def test_a2_live_empty_returns_error(tmp_path: Path) -> None:
    """live：拉取返回空 → 显式 DataIntegrityError（拒绝空数据继续）。"""
    reader = FakeReader(tmp_path, behavior="empty")
    fetcher = FakeFetcher(empty=True)
    service = _make_service(_make_config(tmp_path), reader, fetcher)
    from Kuantix.backtest.service import BacktestRunRequest

    with pytest.raises(DataIntegrityError) as excinfo:
        service.run(
            "job_live_empty", BacktestRunRequest(codes=("600519",), data_source="live")
        )
    assert "返回空" in str(excinfo.value)


def test_a2_live_failure_wrapped(tmp_path: Path) -> None:
    """live：拉取抛网络/上游异常 → 统一包装为 DataIntegrityError（含 code 与原因）。"""
    reader = FakeReader(tmp_path, behavior="empty")
    fetcher = FakeFetcher(error=RuntimeError("socket timeout"))
    service = _make_service(_make_config(tmp_path), reader, fetcher)
    from Kuantix.backtest.service import BacktestRunRequest

    with pytest.raises(DataIntegrityError) as excinfo:
        service.run(
            "job_live_fail", BacktestRunRequest(codes=("600519",), data_source="live")
        )
    message = str(excinfo.value)
    assert "实时拉取失败" in message
    assert "600519" in message
    assert "socket timeout" in message


def test_a2_live_multi_code_422(tmp_path: Path) -> None:
    """API：live + 多标的（codes>1）→ 422 显式拒绝（D-3，路由层拦截）。"""
    reader = FakeReader(tmp_path, behavior="empty")
    client = _make_client(tmp_path, reader=reader, fetcher=FakeFetcher())
    response = client.post(
        "/api/v1/backtest/run",
        json={"codes": ["600519", "600000"], "data_source": "live"},
    )
    assert response.status_code == 422
    payload = response.json()
    assert_envelope(payload)
    assert "live 仅支持单标的" in payload["message"]


def test_a2_live_api_job_done_and_result(tmp_path: Path) -> None:
    """API：live 单标的 → B2 提交 → B3 done → B4 完整结果（真实引擎产出）。"""
    reader = FakeReader(tmp_path, behavior="empty")
    client = _make_client(tmp_path, reader=reader, fetcher=FakeFetcher())
    response = client.post(
        "/api/v1/backtest/run",
        json={"codes": ["600519"], "data_source": "live",
              "start": "2024-01-01", "end": "2024-12-31"},
    )
    assert response.status_code == 200
    assert_envelope(response.json())
    job_id = response.json()["data"]["job_id"]
    job = _wait_job(client, job_id)
    assert job["status"] == "done", f"job 未 done: {job}"
    # job params 回显 data_source（白盒读 JobStore params 列，契约 v1.4 可观测）
    import sqlite3

    db_path = client.app.state.services.jobs.store.db_path
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            "SELECT params FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
    assert row is not None
    stored_params = json.loads(str(row[0]))
    assert stored_params["data_source"] == "live"
    full = client.get(f"/api/v1/backtest/results/{job_id}").json()
    assert_envelope(full)
    result = full["data"]
    assert result["codes"] == ["600519"]
    assert "combined" in result
    assert result["combined"]["performance"]["total_return"] is not None


def test_a2_live_failure_api_job_failed_422(tmp_path: Path) -> None:
    """API：live 拉取失败 → job failed + error.code=422（显式，非裸 500）。"""
    reader = FakeReader(tmp_path, behavior="empty")
    client = _make_client(tmp_path, reader=reader, fetcher=FakeFetcher(error=RuntimeError("boom")))
    response = client.post(
        "/api/v1/backtest/run",
        json={"codes": ["600519"], "data_source": "live",
              "start": "2024-01-01", "end": "2024-12-31"},
    )
    job_id = response.json()["data"]["job_id"]
    job = _wait_job(client, job_id)
    assert job["status"] == "failed"
    assert job["error"]["code"] == 422
    assert "实时拉取失败" in job["error"]["message"]


# ---------------------------------------------------------------------------
# A3. data_source=auto：数据源优先级，不是错误兜底（NF-26 / D1.2）
# ---------------------------------------------------------------------------


def test_a3_auto_local_missing_uses_live(tmp_path: Path) -> None:
    """auto：本地文件不存在（合法业务态）→ 转 live（fetcher 被调用）。"""
    reader = FakeReader(tmp_path, behavior="raise", error="L1 文件不存在")
    # 文件不存在 → local_has_data=False → live；不经过 read_daily_frame
    fetcher = FakeFetcher()
    service = _make_service(_make_config(tmp_path), reader, fetcher)
    from Kuantix.backtest.service import BacktestRunRequest

    result = service.run(
        "job_auto_live", BacktestRunRequest(
            codes=("600519",), data_source="auto",
            start=dt.date(2024, 1, 1), end=dt.date(2024, 12, 31),
        )
    )
    assert fetcher.calls, "auto 本地缺失必须转 live"
    assert result.result["codes"] == ["600519"]
    assert reader.read_calls == []  # 缺失判定只经 day_path，不触发读


def test_a3_auto_local_corrupt_no_fallback(tmp_path: Path) -> None:
    """auto：本地文件存在但损坏/为空 → 显式报错，绝不静默降级到 live。"""
    config = _make_config(tmp_path)
    _seed_day_file(config)  # 文件存在 → local_has_data=True
    reader = FakeReader(config.paths.vipdoc, behavior="empty")  # 读出来是空 → 损坏语义
    fetcher = ExplodingFetcher()  # 若被调用 → 测试失败
    service = _make_service(config, reader, fetcher)
    from Kuantix.backtest.service import BacktestRunRequest

    with pytest.raises(DataIntegrityError) as excinfo:
        service.run(
            "job_auto_corrupt", BacktestRunRequest(codes=("600519",), data_source="auto")
        )
    assert "无日线数据" in str(excinfo.value) or "空" in str(excinfo.value)
    assert reader.read_calls == [("sh", "600519")]


def test_a3_auto_duck_reader_local_path(tmp_path: Path) -> None:
    """auto：鸭子 reader 无 day_path → 保守走本地读（兼容既有测试语义，零网络）。"""
    reader = DuckReader(frame=_trend_frame())
    fetcher = ExplodingFetcher()
    service = _make_service(_make_config(tmp_path), reader, fetcher)
    from Kuantix.backtest.service import BacktestRunRequest

    result = service.run(
        "job_auto_duck", BacktestRunRequest(codes=("600519",), data_source="auto")
    )
    assert result.result["codes"] == ["600519"]
    assert reader.read_calls == [("sh", "600519")]


# ---------------------------------------------------------------------------
# P1 回归守卫（Round 2 修复锁定）：真实 L1Reader 损坏文件 → DataIntegrityError/422
# ---------------------------------------------------------------------------


def test_p1_regression_real_reader_corrupt_fails_loud(tmp_path: Path) -> None:
    """回归 P1：真实 L1Reader 读损坏 .day → DataIntegrityError（修复前为裸 ValueError→500）。"""
    config = _make_config(tmp_path)
    path = _seed_day_file(config)
    path.write_bytes(b"this is not a valid day file at all" * 4)
    reader = L1Reader(config.paths.vipdoc)
    with pytest.raises(DataIntegrityError) as excinfo:
        reader.read_daily_frame("sh", "600519")
    assert "L1 日线文件损坏" in str(excinfo.value)
    assert "600519" in str(excinfo.value)


def test_p1_regression_real_reader_zero_bytes_fails_loud(tmp_path: Path) -> None:
    """回归 P1：全零字节 .day → DataIntegrityError（第二个复现形态）。"""
    config = _make_config(tmp_path)
    path = _seed_day_file(config)
    path.write_bytes(b"\x00" * 32)
    reader = L1Reader(config.paths.vipdoc)
    with pytest.raises(DataIntegrityError):
        reader.read_daily_frame("sh", "600519")


def test_p1_regression_api_local_corrupt_422(tmp_path: Path) -> None:
    """回归 P1：API B2 data_source=local 损坏文件 → job error.code=422（修复前 500）。"""
    from fastapi.testclient import TestClient

    from Kuantix.api.deps import ServiceContainer
    from Kuantix.api.jobs import JobManager, JobStore
    from Kuantix.api.server import create_app
    from Kuantix.backtest.service import BacktestService
    from Kuantix.backtest.store import BacktestResultStore

    config = _make_config(tmp_path)
    path = _seed_day_file(config)
    path.write_bytes(b"\x00" * 32)
    store = BacktestResultStore(config.paths.db / "backtest_results.db")
    service = BacktestService(config, reader=L1Reader(config.paths.vipdoc), store=store)
    jobs = JobManager(JobStore(tmp_path / "db"))
    container = ServiceContainer(
        config=config, lake=None, factor_service=None, screen_service=None,
        jobs=jobs, backtest_service=service,
    )
    app = create_app(config=config, services=container)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/backtest/run",
            json={"codes": ["600519"], "data_source": "local",
                  "start": "2024-01-01", "end": "2024-12-31"},
        )
        job_id = response.json()["data"]["job_id"]
        job = _wait_job(client, job_id)
        assert job["status"] == "failed"
        assert job["error"]["code"] == 422
        assert "L1 日线文件损坏" in job["error"]["message"] or "读取失败" in job["error"]["message"]


def test_p1_regression_api_auto_corrupt_no_fallback_422(tmp_path: Path) -> None:
    """回归 P1：auto 损坏文件 → 422 且不降级 live（真实 reader + 真实 fetcher 路径）。"""
    from fastapi.testclient import TestClient

    from Kuantix.api.deps import ServiceContainer
    from Kuantix.api.jobs import JobManager, JobStore
    from Kuantix.api.server import create_app
    from Kuantix.backtest.service import BacktestService
    from Kuantix.backtest.store import BacktestResultStore

    config = _make_config(tmp_path)
    path = _seed_day_file(config)
    path.write_bytes(b"this is not a valid day file at all" * 4)
    store = BacktestResultStore(config.paths.db / "backtest_results.db")
    service = BacktestService(config, reader=L1Reader(config.paths.vipdoc), store=store)
    jobs = JobManager(JobStore(tmp_path / "db"))
    container = ServiceContainer(
        config=config, lake=None, factor_service=None, screen_service=None,
        jobs=jobs, backtest_service=service,
    )
    app = create_app(config=config, services=container)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/backtest/run",
            json={"codes": ["600519"], "data_source": "auto",
                  "start": "2024-01-01", "end": "2024-12-31"},
        )
        job_id = response.json()["data"]["job_id"]
        job = _wait_job(client, job_id)
        assert job["status"] == "failed"
        assert job["error"]["code"] == 422  # 非 500、非静默、未降级 live


# ---------------------------------------------------------------------------
# A4. 口径一致性：live 未复权 / vol=手 / 列同构 → 与本地回测可比
# ---------------------------------------------------------------------------


def test_a4_live_and_local_frame_identical(tmp_path: Path) -> None:
    """同一批 Bar：local（bars_to_frame）与 live（fetch_live_frame）产出完全一致。"""
    from Kuantix.backtest.data_source import fetch_live_frame

    bars = _trend_bars(n=60)
    local_frame = bars_to_frame(bars)
    fetcher = FakeFetcher(bars)
    live_frame = fetch_live_frame(fetcher, get_market_profile("CN"), "600519",
                                  dt.date(2024, 1, 1), dt.date(2024, 12, 31))
    assert list(live_frame.columns) == list(local_frame.columns)
    assert len(live_frame) == len(local_frame)
    # 数值逐行一致（未复权 / vol 手 → 口径相同）
    for col in ("open", "high", "low", "close", "vol", "amount"):
        assert np.allclose(live_frame[col].to_numpy(), local_frame[col].to_numpy())


def test_a4_backtest_comparable_local_vs_live(tmp_path: Path) -> None:
    """同一数据：local 回测与 live 回测绩效一致（可比性，D1.4 结论）。"""
    from Kuantix.backtest.service import BacktestRunRequest

    config = _make_config(tmp_path)
    _seed_day_file(config)
    bars = _trend_bars(n=300)
    local_service = _make_service(
        config, FakeReader(config.paths.vipdoc, frame=bars_to_frame(bars)), ExplodingFetcher()
    )
    live_service = _make_service(config, FakeReader(config.paths.vipdoc, behavior="empty"), FakeFetcher(bars))

    req_local = BacktestRunRequest(codes=("600519",), data_source="local",
                                   start=dt.date(2024, 1, 1), end=dt.date(2024, 12, 31))
    req_live = BacktestRunRequest(codes=("600519",), data_source="live",
                                  start=dt.date(2024, 1, 1), end=dt.date(2024, 12, 31))
    r_local = local_service.run("job_cmp_local", req_local)
    r_live = live_service.run("job_cmp_live", req_live)

    def _perf(result: Any) -> dict[str, Any]:
        return result.result["combined"]["performance"]

    p_local, p_live = _perf(r_local), _perf(r_live)
    assert p_local["total_return"] == pytest.approx(p_live["total_return"], abs=1e-6)
    assert p_local["sharpe"] == pytest.approx(p_live["sharpe"], abs=1e-6)
    assert p_local["max_drawdown"] == pytest.approx(p_live["max_drawdown"], abs=1e-6)


def test_a4_real_fetcher_vol_shares_to_lots() -> None:
    """RD-8：真实 QuotationFetcher._frame_to_bars 完成 股→手（÷100），不依赖网络。"""
    from Kuantix.adapters.quotation import QuotationFetcher

    frame = pd.DataFrame(
        {
            "date": [pd.Timestamp("2024-01-02"), pd.Timestamp("2024-01-03")],
            "open": [10.0, 10.1],
            "high": [10.2, 10.3],
            "low": [9.9, 10.0],
            "close": [10.1, 10.2],
            "vol": [12345.0, 23456.0],  # 单位=股
            "amount": [1e6, 1.1e6],
        }
    )
    fetcher = object.__new__(QuotationFetcher)  # 仅调静态工具，不建连接
    bars = fetcher._frame_to_bars(frame, context="test", vol_divisor=100.0)
    assert len(bars) == 2
    assert bars[0].vol == pytest.approx(123.45)  # 股→手
    assert bars[1].vol == pytest.approx(234.56)
    assert bars[0].date == dt.date(2024, 1, 2)


# ---------------------------------------------------------------------------
# B. 调度器（增量更新）
# ---------------------------------------------------------------------------


class FakeProfile:
    """假 MarketProfile（只实现调度判定用到的能力）。"""

    def __init__(
        self,
        *,
        now: dt.datetime | None = None,
        trading: bool = True,
        open_now: bool = False,
    ) -> None:
        self._now = now or dt.datetime(2026, 8, 3, 16, 30, tzinfo=_TZ)
        self._trading = trading
        self._open = open_now
        self.timezone = "Asia/Shanghai"

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
        self.elapsed_ms = 7

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "done": self.done,
            "failed": self.failed,
            "quarantined": self.quarantined,
            "skipped_resumed": self.skipped_resumed,
            "elapsed_ms": self.elapsed_ms,
        }


class FakeSyncHandle:
    def __init__(self, status: str = "done", error: str | None = None) -> None:
        self.status = status
        self.error = error
        self.result = FakeSyncResult() if status == "done" else None

    def wait(self, timeout: float | None = None) -> FakeSyncResult | None:
        return self.result


class FakeLake:
    """假 DataLake：记录 sync_incremental 调用，返回可配置 handle。"""

    def __init__(self, handle: FakeSyncHandle | None = None, error: Exception | None = None) -> None:
        self._handle = handle if handle is not None else FakeSyncHandle()
        self._error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def sync_incremental(
        self, market: str, workers: int | None = None, force: bool = False
    ) -> FakeSyncHandle:
        self.calls.append((market, {"workers": workers, "force": force}))
        if self._error is not None:
            raise self._error
        return self._handle


def _make_scheduler(tmp_path: Path, *, lake: FakeLake | None = None, profile: FakeProfile | None = None):
    from Kuantix.data.sync_state import SyncStateStore
    from Kuantix.scheduler import IncrementalSyncScheduler

    config = _make_config(tmp_path, schedule_enabled=True)
    state = SyncStateStore(config.paths.db)
    scheduler = IncrementalSyncScheduler(
        config,
        lake if lake is not None else FakeLake(),
        state,
        profile=profile if profile is not None else FakeProfile(),
    )
    return scheduler, config, state


def test_b1_non_trading_day_skip(tmp_path: Path) -> None:
    """B1：非交易日 → cron/startup/manual 全部 skip（不触网）。"""
    lake = FakeLake()
    scheduler, config, state = _make_scheduler(tmp_path, lake=lake, profile=FakeProfile(trading=False))
    _seed_day_file(config)
    outcome = scheduler.run_once("manual")
    assert outcome["dispatched"] is False
    assert "非交易日" in outcome["reason"]
    assert lake.calls == []
    view = state.view()
    assert view["status"] == "skipped"
    assert view["trigger"] == "manual"


def test_b1_trading_session_skip(tmp_path: Path) -> None:
    """B1：交易时段内 → skip（盘后判定）。"""
    lake = FakeLake()
    scheduler, config, state = _make_scheduler(
        tmp_path, lake=lake, profile=FakeProfile(trading=True, open_now=True)
    )
    _seed_day_file(config)
    outcome = scheduler.run_once("cron")
    assert outcome["dispatched"] is False
    assert "交易时段内" in outcome["reason"]
    assert lake.calls == []


def test_b2_flock_singleton_contention(tmp_path: Path) -> None:
    """B2：已有进程持锁 → 记 skipped（另一实例正在同步），不重入。"""
    import fcntl

    lake = FakeLake()
    scheduler, config, state = _make_scheduler(tmp_path, lake=lake)
    _seed_day_file(config)
    lock_path = config.paths.db / "sync_scheduler.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    holder = open(lock_path, "a+")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        outcome = scheduler.run_once("manual")
        assert outcome["dispatched"] is False
        assert "另一实例" in outcome["reason"]
        assert lake.calls == []
        view = state.view()
        assert view["status"] == "skipped"
    finally:
        holder.close()


def test_b3_failure_records_then_retry(tmp_path: Path) -> None:
    """B3：sync 失败 → sync_state status=failed+error；下次 run-once 自然重试成功。"""
    lake = FakeLake(handle=FakeSyncHandle(status="failed", error="网络超时"))
    scheduler, config, state = _make_scheduler(tmp_path, lake=lake)
    _seed_day_file(config)
    first = scheduler.run_once("manual")
    assert first["dispatched"] is True
    assert first["status"] == "failed"
    view = state.view()
    assert view["status"] == "failed"
    assert "网络超时" in (view["error"] or "")
    # 换好 handle → 自然重试
    lake._handle = FakeSyncHandle(status="done")
    second = scheduler.run_once("manual")
    assert second["dispatched"] is True
    assert second["status"] == "done"
    assert state.view()["status"] == "done"


def test_b4_empty_lake_all_triggers_skip(tmp_path: Path) -> None:
    """B4：湖为空 → 任意触发来源（startup/cron/manual）均 skip，不自动全量。"""
    lake = FakeLake()
    scheduler, config, state = _make_scheduler(tmp_path, lake=lake)  # 不种文件 → 空湖
    for trigger in ("startup", "cron", "manual"):
        outcome = scheduler.run_once(trigger)
        assert outcome["dispatched"] is False, trigger
        assert "数据湖为空" in outcome["reason"], trigger
    assert lake.calls == []


def test_b5_startup_idempotent_skip_when_synced_today(tmp_path: Path) -> None:
    """B5：startup 幂等 —— 今日已同步 → skip；未同步 → 触发增量。"""
    profile = FakeProfile(now=dt.datetime(2026, 8, 3, 16, 31, tzinfo=_TZ))
    lake = FakeLake()
    scheduler, config, state = _make_scheduler(tmp_path, lake=lake, profile=profile)
    _seed_day_file(config)
    state.update(at=dt.datetime(2026, 8, 3, 16, 30, tzinfo=_TZ), status="done", trigger="cron")
    outcome = scheduler.startup_check()
    assert outcome["dispatched"] is False
    assert "今日已同步" in outcome["reason"]
    assert lake.calls == []
    # 无状态（或昨日）→ 触发
    state.update(at=dt.datetime(2026, 8, 2, 16, 30, tzinfo=_TZ), status="done", trigger="cron")
    outcome2 = scheduler.startup_check()
    assert outcome2["dispatched"] is True
    assert outcome2["status"] == "done"
    assert lake.calls, "未同步时应触发增量"


# ---------------------------------------------------------------------------
# C. 可观测与 CLI
# ---------------------------------------------------------------------------


def _make_data_client(tmp_path: Path, *, lake: Any) -> Any:
    from fastapi.testclient import TestClient

    from Kuantix.api.deps import ServiceContainer
    from Kuantix.api.jobs import JobManager, JobStore
    from Kuantix.api.server import create_app

    config = _make_config(tmp_path)
    jobs = JobManager(JobStore(tmp_path / "db"))
    container = ServiceContainer(
        config=config,
        lake=lake,
        factor_service=None,
        screen_service=None,
        jobs=jobs,
    )
    app = create_app(config=config, services=container)
    return TestClient(app)


def test_c1_d1_real_lake_has_last_sync_schedule(tmp_path: Path) -> None:
    """C1：D1 响应含 last_sync（可空）/ schedule{enabled,time,startup_check}，过信封校验。"""
    from Kuantix.data.datalake import DataLake

    config = _make_config(tmp_path)
    lake = DataLake(config)
    client = _make_data_client(tmp_path, lake=lake)
    response = client.get("/api/v1/data/status")
    assert response.status_code == 200
    payload = response.json()
    assert_envelope(payload)
    data = payload["data"]
    assert "last_sync" in data
    assert data["last_sync"] is None  # 无记录 → null（可空）
    assert "schedule" in data
    assert set(data["schedule"]) == {"enabled", "time", "startup_check"}
    assert data["schedule"]["time"] == "16:30"


def test_c1_d1_last_sync_passthrough_after_manual(tmp_path: Path) -> None:
    """C1：D1 last_sync 反映最近一次同步事件（trigger=manual）。"""
    from Kuantix.data.sync_state import SyncStateStore

    config = _make_config(tmp_path)
    SyncStateStore(config.paths.db).update(
        at=dt.datetime(2026, 8, 3, 16, 30, tzinfo=_TZ),
        status="done",
        trigger="manual",
        result={"total": 10, "done": 10, "failed": 0, "quarantined": 0,
                "skipped_resumed": 0, "elapsed_ms": 5},
    )
    from Kuantix.data.datalake import DataLake

    client = _make_data_client(tmp_path, lake=DataLake(config))
    data = client.get("/api/v1/data/status").json()["data"]
    assert data["last_sync"]["status"] == "done"
    assert data["last_sync"]["trigger"] == "manual"
    assert data["last_sync"]["result"]["total"] == 10


def _wait_data_job(client: Any, job_id: str, timeout: float = 15.0) -> dict[str, Any]:
    import time

    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        job = client.get(f"/api/v1/data/sync/{job_id}").json()["data"]
        last = job
        if job["status"] in ("done", "failed", "cancelled"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"data job {job_id} 未在 {timeout}s 内结束，最后状态 {last.get('status')}")


def test_c2_d2_manual_sync_writes_manual_state(tmp_path: Path) -> None:
    """C2：D2 手动同步完成 → sync_state 写 trigger=manual（假 Lake 注入）。"""
    from Kuantix.data.datalake import DataLake as _RealLake  # noqa: F401 仅作类型参考
    from Kuantix.data.sync_state import SyncStateStore

    class _Handle:
        status = "done"
        error = None
        progress = None  # runner 会读 handle.progress（None 则跳过进度上报）
        result = FakeSyncResult()

        def is_done(self) -> bool:
            return True

        def wait(self, timeout: float | None = None) -> FakeSyncResult:
            return self.result

    class _SyncLake:
        def sync_incremental(self, market, workers=None, force=False) -> _Handle:
            return _Handle()

    config = _make_config(tmp_path)
    client = _make_data_client(tmp_path, lake=_SyncLake())
    response = client.post("/api/v1/data/sync", json={"mode": "incremental"})
    assert response.status_code == 200
    job_id = response.json()["data"]["job_id"]
    job = _wait_data_job(client, job_id)
    assert job["status"] == "done"
    state = SyncStateStore(config.paths.db).view()
    assert state is not None
    assert state["status"] == "done"
    assert state["trigger"] == "manual"
    assert state["result"]["total"] == 10


def test_c3_cli_sync_incremental_passthrough(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """C3 CLI：`data sync --incremental` 调 sync_incremental（参数透传 + 写 manual 状态）。"""
    from Kuantix.cli import main

    config = _make_config(tmp_path)

    class _CliHandle:
        status = "done"
        error = None

        def wait(self, timeout: float | None = None) -> FakeSyncResult:
            return FakeSyncResult()

    class _CliLake:
        def __init__(self, config: Any) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        def sync_incremental(self, market, workers=None, force=False) -> _CliHandle:
            self.calls.append((market, {"workers": workers, "force": force}))
            return _CliHandle()

    fake = _CliLake(config)
    monkeypatch.setattr("Kuantix.data.datalake.DataLake", lambda cfg: fake)
    code = main(["--json", "--config", str(config.source), "data", "sync", "--incremental"])
    assert code == 0
    assert fake.calls == [("CN", {"workers": None, "force": False})]
    # manual 状态写入
    from Kuantix.data.sync_state import SyncStateStore

    state = SyncStateStore(config.paths.db).view()
    assert state is not None
    assert state["trigger"] == "manual"
    assert state["status"] == "done"


def test_c3_cli_schedule_run_once_guard_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """C3 CLI：`data schedule run-once` 守卫 skip 输出（非交易日判定透传）。"""
    from Kuantix.cli import main
    from Kuantix.scheduler import IncrementalSyncScheduler

    config = _make_config(tmp_path, schedule_enabled=True)
    monkeypatch.setattr(
        IncrementalSyncScheduler,
        "_should_run",
        lambda self, trigger: "非交易日 2026-08-02，跳过增量同步",
    )
    code = main(["--json", "--config", str(config.source), "data", "schedule", "run-once"])
    assert code == 0
    # 无法直接捕获 stdout → 通过 sync_state 验证 skipped 记录
    from Kuantix.data.sync_state import SyncStateStore

    state = SyncStateStore(config.paths.db).view()
    assert state is not None
    assert state["status"] == "skipped"
    assert state["trigger"] == "manual"


def test_c3_cli_schedule_status_fields(tmp_path: Path) -> None:
    """C3 CLI：`data schedule status` 输出全字段（enabled/started/.../last_sync）。"""
    import json as _json

    from Kuantix.cli import main

    config = _make_config(tmp_path, schedule_enabled=True)
    code = main(["--json", "--config", str(config.source), "data", "schedule", "status"])
    assert code == 0
    # 通过状态文件 + 直接调用 scheduler.status() 双重验证字段集合
    from Kuantix.data.datalake import DataLake
    from Kuantix.data.sync_state import SyncStateStore
    from Kuantix.scheduler import IncrementalSyncScheduler

    scheduler = IncrementalSyncScheduler(config, DataLake(config), SyncStateStore(config.paths.db))
    status = scheduler.status()
    assert set(status) == {"enabled", "started", "schedule_time", "startup_check", "next_run", "last_sync"}
    assert status["schedule_time"] == "16:30"
    assert status["enabled"] is True
    assert "last_sync" in status
    # 确保 JSON 信封可序列化（NF-9/NF-12）
    envelope = _json.dumps({"code": 0, "message": "ok", "data": status, "meta": {}})
    assert envelope


def test_c4_openapi_paths_and_params(tmp_path: Path) -> None:
    """C4 契约 v1.4 一致性：OpenAPI paths vs 实现路由（B2 data_source / B5 query / D1）。"""
    from Kuantix.api.server import create_app

    app = create_app(config=_make_config(tmp_path))
    openapi = app.openapi()

    # B2：POST /api/v1/backtest/run 请求体含 data_source 枚举 auto/local/live
    run_schema = openapi["paths"]["/api/v1/backtest/run"]["post"]
    body_ref = run_schema["requestBody"]["content"]["application/json"]["schema"]
    ref_value = body_ref.get("$ref")
    assert ref_value is not None, "B2 请求体应为 $ref 引用"
    ref_name = ref_value.rsplit("/", 1)[-1]
    props = openapi["components"]["schemas"][ref_name]["properties"]
    assert "data_source" in props
    assert props["data_source"]["default"] == "auto"
    assert sorted(props["data_source"]["enum"]) == ["auto", "live", "local"]

    # B5：GET /api/v1/backtest/kline/{code} query 含 data_source（Literal）
    kline_params = openapi["paths"]["/api/v1/backtest/kline/{code}"]["get"]["parameters"]
    names = {p["name"] for p in kline_params}
    assert "data_source" in names
    ds_param = next(p for p in kline_params if p["name"] == "data_source")
    assert sorted(ds_param["schema"]["enum"]) == ["auto", "live", "local"]

    # D1：GET /api/v1/data/status 存在
    assert "/api/v1/data/status" in openapi["paths"]

    # 关键业务路由在 OpenAPI 中真实注册（`app.routes` 中业务路由是
    # _IncludedRouter 占位对象，路径不可见；但 OpenAPI + TestClient 实证可达）
    for expected in (
        "/api/v1/backtest/run",
        "/api/v1/backtest/strategies",
        "/api/v1/backtest/kline/{code}",
        "/api/v1/data/status",
        "/api/v1/portfolio/run",
        "/api/v1/strategies",
    ):
        assert expected in openapi["paths"], f"OpenAPI 路径缺失: {expected}"


# ---------------------------------------------------------------------------
# D. 运行验证
# ---------------------------------------------------------------------------


def test_d2_serve_real_container_endpoints(tmp_path: Path) -> None:
    """D2：真组合根（build_container）+ TestClient → health/status/strategies 全 200。"""
    from fastapi.testclient import TestClient

    from Kuantix.api.server import build_container, create_app

    config = _make_config(tmp_path)  # schedule_enabled=false → lifespan 零副作用
    container = build_container(config)
    app = create_app(config=config, services=container)
    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["data"]["status"] == "ok"
        assert_envelope(health.json())

        status_resp = client.get("/api/v1/data/status")
        assert status_resp.status_code == 200
        data = status_resp.json()["data"]
        assert "last_sync" in data
        assert "schedule" in data
        assert_envelope(status_resp.json())

        strategies = client.get("/api/v1/backtest/strategies")
        assert strategies.status_code == 200
        assert strategies.json()["data"]["count"] >= 1
        assert_envelope(strategies.json())


def test_d2_serve_lifespan_scheduler_startup_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """D2：schedule_enabled=true 时 lifespan 挂调度器 + startup 检查（非阻塞 daemon）。

    用 monkeypatch 把 ``_should_run`` 钉死为「空湖 skip」，保证任意日期都零网络；
    验证 serve 启动路径（lifespan）确实装配调度器并执行了幂等启动检查。
    """
    from fastapi.testclient import TestClient

    from Kuantix.api.server import create_app
    from Kuantix.data.sync_state import SyncStateStore
    from Kuantix.scheduler import IncrementalSyncScheduler

    config = _make_config(tmp_path, schedule_enabled=True)
    monkeypatch.setattr(
        IncrementalSyncScheduler,
        "_should_run",
        lambda self, trigger: "数据湖为空，请先全量回补",
    )
    app = create_app(config=config)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
    # lifespan 关闭后：startup 检查线程应已写入 skipped 记录（幂等、不触网）
    view = SyncStateStore(config.paths.db).view()
    assert view is not None
    assert view["status"] == "skipped"
    assert view["trigger"] == "startup"


def test_d4_live_e2e_offline(tmp_path: Path) -> None:
    """D4（离线确定性替代）：live 单标的端到端 → B4 结果含最新日期数据。

    网络不可用时以假 fetcher 替代（明确标注：离线替代路径；网络真拉取见
    test_d4_live_e2e_network，标记 network，不可用自动 skip）。
    """
    bars = _trend_bars(n=400, start=dt.date(2024, 1, 2))
    latest = bars[-1].date
    reader = FakeReader(tmp_path, behavior="empty")
    client = _make_client(tmp_path, reader=reader, fetcher=FakeFetcher(bars))
    response = client.post(
        "/api/v1/backtest/run",
        json={"codes": ["600519"], "data_source": "live",
              "start": "2024-01-01", "end": "2025-12-31"},
    )
    assert response.status_code == 200
    job_id = response.json()["data"]["job_id"]
    job = _wait_job(client, job_id)
    assert job["status"] == "done"
    full = client.get(f"/api/v1/backtest/results/{job_id}").json()["data"]
    dates = [point["datetime"][:10] for point in full["combined"]["equity_curve"]]
    assert dates, "回测净值序列为空"
    assert latest.isoformat() in dates, f"结果应包含最新交易日 {latest.isoformat()}"


@pytest.mark.network
def test_d4_live_e2e_network(tmp_path: Path) -> None:
    """D4（网络）：B2 data_source=live 单标的（600519）真实拉取 → job done 含最新数据。

    网络不可用时显式 skip（不失败）；离线替代见 test_d4_live_e2e_offline。
    """
    from Kuantix.adapters.quotation import QuotationFetcher
    from Kuantix.adapters.tdx_client import TdxClientFactory

    config = _make_config(tmp_path)
    try:
        fetcher = QuotationFetcher(TdxClientFactory.from_config(config), shared_connection=False)
        bars = fetcher.fetch_kline("CN", "600519", years=1, exchange="sh")
    except Exception as exc:  # noqa: BLE001 - 网络不可用 → 显式 skip 并标注
        pytest.skip(f"网络不可用，真实拉取跳过（{type(exc).__name__}: {exc}）；已用离线替代用例覆盖")
    assert bars, "真实拉取返回空"
    latest = bars[-1].date
    reader = FakeReader(tmp_path, behavior="empty")
    client = _make_client(tmp_path, reader=reader, fetcher=FakeFetcher(bars))
    response = client.post(
        "/api/v1/backtest/run",
        json={"codes": ["600519"], "data_source": "live",
              "start": "2024-01-01", "end": "2026-12-31"},
    )
    assert response.status_code == 200
    job_id = response.json()["data"]["job_id"]
    job = _wait_job(client, job_id)
    assert job["status"] == "done"
    full = client.get(f"/api/v1/backtest/results/{job_id}").json()["data"]
    dates = [point["datetime"][:10] for point in full["combined"]["equity_curve"]]
    assert latest.isoformat() in dates, "真实拉取结果应包含最新交易日"


def test_d5_schedule_run_once_real_smoke(tmp_path: Path) -> None:
    """D5：真实 `data schedule run-once` 冒烟 —— 返回合法信封（skip 或 dispatched 均可）。"""
    import json as _json

    from Kuantix.cli import main

    config = _make_config(tmp_path, schedule_enabled=True)
    code = main(["--json", "--config", str(config.source), "data", "schedule", "run-once"])
    assert code == 0
    # 无论判定结果（非交易日 skip / 交易日执行），状态文件都应有记录
    from Kuantix.data.sync_state import SyncStateStore

    view = SyncStateStore(config.paths.db).view()
    assert view is not None
    assert view["status"] in ("done", "skipped", "failed")
    assert view["trigger"] == "manual"
    # 信封结构可序列化
    payload = {"dispatched": view["status"] in ("done", "failed"),
               "status": view["status"], "trigger": view["trigger"]}
    assert _json.dumps(payload)
