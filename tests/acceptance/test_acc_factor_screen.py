"""T04 因子+选股独立验收（B1-B4）。

与工程师白盒单测（tests/unit/test_unit_factor_screen.py）**刻意错开样本与路径**：
- B1 FactorStore 增量：工程师只测 save/load 往返与 NaN 拒绝（未测 compute）；
  本验收用**假 FactorEngine 计数**验证「已算到 end 的重复计算被跳过、force 强制重算」
  （断点续传语义在因子层的体现）。
- B2 FactorCombiner：工程师只测等权抵消与 IC 排序方向；本验收对 equal/ic/ir
  **手工推导期望权重**逐值断言。
- B3 ScreenService：工程师用 ``run()``；本验收用 ``run_batch()``（契约 S2-S6 路径），
  断言 ``excluded_count``（R1.1-1）、SQLite 批次、JSON 导出、GBK CSV + NF-22 免责头。
- B4 factor report 信封：用假 FactorReport（含 NaN 字段）走
  ``FactorAnalyzerBridge.report_to_dict`` → 信封化 → envelope_validator 全绿，
  证明 IC/IR/分层字段口径是 null 而非 NaN（NF-12）。

红线自查：本文件无 ``except: pass`` / 双参 ``.get(k, 默认)``（R4）；全部离线。
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import math
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from Kuantix.core.contracts import ModelHandle
from Kuantix.factor.combiner import FactorCombiner


def _load_envelope_validator():
    """加载 tests/redlines/envelope_validator.py（避免污染 sys.path）。"""
    path = Path(__file__).resolve().parents[1] / "redlines" / "envelope_validator.py"
    spec = importlib.util.spec_from_file_location("envelope_validator_acc", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# B1 FactorStore 增量计算（假引擎计数）
# ---------------------------------------------------------------------------


class _CountingEngine:
    """假 FactorEngineBridge 替身：只记录 compute_cross_section 调用次数。

    返回 2022/2023 两年共 5 个日期、每日期 2 只代码的截面（列名含因子名）。
    """

    def __init__(self) -> None:
        self.calls = 0

    def compute_cross_section(self, pool, factors: list[str], date=None):
        self.calls += 1
        dates = [
            dt.date(2022, 6, 1),
            dt.date(2022, 12, 30),
            dt.date(2023, 1, 3),
            dt.date(2023, 6, 30),
            dt.date(2023, 12, 31),
        ]
        rows: list[dict[str, object]] = []
        for d in dates:
            for code in ("600000", "600036"):
                rows.append(
                    {
                        "date": d.year * 10000 + d.month * 100 + d.day,
                        "code": code,
                        factors[0]: 1.0,
                    }
                )
        return pd.DataFrame(rows)


def test_acc_factor_store_incremental_skip(tmp_path: Path) -> None:
    from Kuantix.factor.store import FactorStore

    store = FactorStore(tmp_path / "factors", tmp_path / "db")
    engine = _CountingEngine()
    pool = {"600000": pd.DataFrame(), "600036": pd.DataFrame()}

    # 第一次：算 2023 全年 → 引擎被调 1 次，落库 3 个日期 × 2 只 = 6 行
    counts1 = store.compute(
        pool,
        ["momentum_20d"],
        dt.date(2023, 1, 1),
        dt.date(2023, 12, 31),
        engine=engine,
    )
    assert counts1["momentum_20d"] == 6
    assert engine.calls == 1

    # 第二次：终点仍是 2023-12-31（如从 1 年扩到 2 年但终点不变）
    # → computed_until 已覆盖 end → 跳过，引擎不再被调
    counts2 = store.compute(
        pool,
        ["momentum_20d"],
        dt.date(2022, 1, 1),
        dt.date(2023, 12, 31),
        engine=engine,
    )
    assert counts2["momentum_20d"] == 0
    assert engine.calls == 1

    # force=True 强制重算 → 引擎再次被调，2022+2023 共 5 日期 × 2 只 = 10 行
    counts3 = store.compute(
        pool,
        ["momentum_20d"],
        dt.date(2022, 1, 1),
        dt.date(2023, 12, 31),
        engine=engine,
        force=True,
    )
    assert counts3["momentum_20d"] == 10
    assert engine.calls == 2

    # 增量落库后：2023 分区已含数据、computed_until 推进到 2023-12-31
    assert store.computed_until("momentum_20d") == "20231231"
    assert store.years_for("momentum_20d") == [2022, 2023]


# ---------------------------------------------------------------------------
# B2 FactorCombiner 三方法（手工推导期望权重）
# ---------------------------------------------------------------------------


def test_acc_combiner_equal_ic_ir_exact_weights() -> None:
    combiner = FactorCombiner()
    values = pd.DataFrame(
        {"f1": [1.0, 2.0, 3.0, 4.0], "f2": [4.0, 3.0, 2.0, 1.0]},
        index=["a", "b", "c", "d"],
    )
    # 独立重算 z-score（ddof=0，与实现同口径）
    z1 = (values["f1"] - values["f1"].mean()) / values["f1"].std(ddof=0)
    z2 = (values["f2"] - values["f2"].mean()) / values["f2"].std(ddof=0)

    # equal：等权平均
    s_equal = combiner.combine(values, "equal")
    expected_equal = (z1 + z2) / 2.0
    pd.testing.assert_series_equal(
        s_equal.sort_index(), expected_equal.sort_index().astype(float)
    )
    assert s_equal.is_monotonic_decreasing  # 降序排序

    # ic：权重 {f1:0.25, f2:0.75} → 归一化后 (0.25, 0.75)
    s_ic = combiner.combine(values, "ic", weights={"f1": 0.25, "f2": 0.75})
    expected_ic = 0.25 * z1 + 0.75 * z2
    pd.testing.assert_series_equal(s_ic.sort_index(), expected_ic.sort_index())

    # ir：权重 {f1:0.5, f2:0.5} → 归一化后 (0.5, 0.5)
    s_ir = combiner.combine(values, "ir", weights={"f1": 0.5, "f2": 0.5})
    expected_ir = 0.5 * z1 + 0.5 * z2
    pd.testing.assert_series_equal(s_ir.sort_index(), expected_ir.sort_index())

    # fail-loud：未知方法名显式报错
    from Kuantix.core.fail_loud import UnknownValueError

    with pytest.raises(UnknownValueError):
        combiner.combine(values, "magic", weights={"f1": 1.0, "f2": 1.0})


# ---------------------------------------------------------------------------
# B3 ScreenService 全链路（run_batch + excluded_count + 导出）
# ---------------------------------------------------------------------------


class _MemStore:
    """内存因子 store（鸭子类型：load / list_factors）。"""

    def __init__(self, data: dict[str, pd.DataFrame]) -> None:
        self._data = data

    def load(self, factor, date=None, code=None, *, start=None, end=None):
        df = self._data[factor]
        if date is not None:
            df = df[df["date"] == int(date)]
        if code is not None:
            df = df[df["code"] == str(code)]
        return df.reset_index(drop=True)

    def list_factors(self):
        return sorted(self._data)


class _FakeReader:
    def __init__(self, frames: dict[str, pd.DataFrame]) -> None:
        self._frames = frames

    def read_daily_frame(self, exchange: str, code: str):
        return self._frames[code]


def test_acc_screen_full_pipeline_batch_export(tmp_path: Path) -> None:
    from Kuantix.screen.service import ScreenRequest, ScreenService

    as_of_int = 20240103
    codes = ["600000", "600036", "601318", "000001", "000002"]
    data = {
        "momentum_20d": pd.DataFrame(
            {
                "date": [as_of_int] * 5,
                "code": codes,
                "value": [1.0, 3.0, 2.0, 1.5, 0.5],
            }
        ),
        "volume_ratio_5d": pd.DataFrame(
            {
                "date": [as_of_int] * 5,
                "code": codes,
                "value": [1.0, 2.0, 1.8, 1.2, 0.8],
            }
        ),
    }
    frames: dict[str, pd.DataFrame] = {}
    for index, code in enumerate(codes):
        base = 10.0 + index
        frames[code] = pd.DataFrame(
            {
                "datetime": pd.to_datetime(["2024-01-02", "2024-01-03"]),
                "open": [base, base],
                "high": [base + 0.2, base + 0.2],
                "low": [base - 0.2, base - 0.2],
                "close": [base, base],
                "vol": [1000.0, 1000.0],
                "amount": [base * 1000.0, base * 1000.0],
            }
        )

    config = SimpleNamespace(
        paths=SimpleNamespace(db=tmp_path, exports=tmp_path / "exports")
    )
    svc = ScreenService(
        config=config,
        store=_MemStore(data),
        reader=_FakeReader(frames),
        combiner=FactorCombiner(),
    )
    req = ScreenRequest(
        market="CN",
        factors=("momentum_20d", "volume_ratio_5d"),
        top_n=2,
        as_of=dt.date(2024, 1, 3),
    )
    batch = svc.run_batch(req, excluded_codes={"000001", "000002"})

    # 契约 R1.1-1：excluded_count 显式计数，不为静默丢弃
    assert batch.result_count == 2
    assert batch.excluded_count == 2
    # 排序：600036 momentum=3.0 最高 → 第一
    assert batch.results[0].code == "600036"
    assert batch.results[0].score > batch.results[1].score

    # SQLite 落盘 + 批次查询
    stored = svc.get_batch(batch.batch_id)
    assert stored is not None
    assert stored["excluded_count"] == 2
    assert stored["result_count"] == 2
    views = svc.get_batch_results(batch.batch_id)
    assert views is not None
    assert views["total"] == 2
    assert views["items"][0]["rank"] == 1

    # JSON 导出（S6 json）
    payload = svc.export_json_payload(batch.batch_id)
    assert payload is not None
    assert payload["total"] == 2
    assert payload["items"][0]["code"] == "600036"

    # CSV 导出（GBK 可读回 + NF-22 免责头 + 契约 §3.4 六列：代码,名称,最新价,
    # 综合得分,触发条件,数据日期）
    raw = svc.export_csv_bytes(batch.batch_id)
    assert raw is not None
    text = raw.decode("gbk")
    assert "仅供人工核对参考，非自动交易指令" in text
    lines = text.strip().splitlines()
    header = lines[1]  # 第一行是 NF-22 免责头，第二行是列头
    assert header == "代码,名称,最新价,综合得分,触发条件,数据日期"
    data_rows = lines[2:]
    assert data_rows, "CSV 应有数据行"
    for row in data_rows:
        cells = row.split(",")
        assert len(cells) == 6, f"每行应为 6 列，实际 {cells}"
        # 末列为数据日期 YYYY-MM-DD
        assert len(cells[5]) == 10 and cells[5][4] == "-" and cells[5][7] == "-"
    assert "600036" in text


# ---------------------------------------------------------------------------
# B4 factor report 信封（null 而非 NaN）
# ---------------------------------------------------------------------------


def test_acc_factor_report_envelope_null_not_nan(tmp_path: Path) -> None:
    from Kuantix.adapters.factor_bridge import FactorAnalyzerBridge
    from Kuantix.core.envelope import Envelope

    validator = _load_envelope_validator()

    class _FakeReport:
        """含 NaN 字段的假上游 FactorReport（NF-12 边界场景）。"""

        name = "momentum_20d"
        ic_mean = float("nan")
        ic_std = 0.084
        ir = float("nan")
        ic_positive_rate = 0.58
        quantile_returns = {
            "Q1": 0.021,
            "Q2": float("nan"),
            "Q3": 0.031,
            "Q4": 0.037,
            "Q5": 0.052,
        }
        top_minus_bottom = 0.031
        turnover_rate = 0.32
        autocorr = float("nan")
        ic_series = pd.Series([0.01, float("nan"), 0.02])

    payload = FactorAnalyzerBridge.report_to_dict(_FakeReport())

    # IC/IR/分层口径：NaN → None（null），而非裸 NaN / 百分数
    assert payload["ic_mean"] is None
    assert payload["ir"] is None
    assert payload["autocorr"] is None
    assert payload["quantile_returns"]["Q2"] is None
    assert payload["quantile_returns"]["Q1"] == pytest.approx(0.021)
    assert payload["ic_positive_rate"] == pytest.approx(0.58)
    assert math.isfinite(payload["ic_std"])
    assert all(math.isfinite(float(item["ic"])) for item in payload["ic_series_tail"])

    # 信封化（CLI `factor report --json` 的服务层等价路径）
    env = Envelope.ok(
        {"factor": "momentum_20d", "report": payload},
        market="CN",
        version="0.1.0",
        data_date="2024-01-03",
    )
    problems = validator.validate_envelope(env.to_dict())
    assert problems == []
    text = env.to_json()
    assert "NaN" not in text
    assert "Infinity" not in text
    assert validator.validate_envelope_json(text) == []


# ---------------------------------------------------------------------------
# B4b（辅助）：FactorService.combine 等权模型落库（契约 F5 路径）
# ---------------------------------------------------------------------------


def test_acc_factor_service_combine_equal_saves_model(tmp_path: Path) -> None:
    from Kuantix.factor.service import FactorService

    svc = FactorService.__new__(FactorService)
    svc._store = None
    svc._models_db = tmp_path / "models.db"
    svc._ensure_models_schema()

    handle = svc.combine(("f1", "f2"), "equal", name="acc_m1", save_model=True)
    assert isinstance(handle, ModelHandle)
    assert handle.method == "equal"
    assert set(handle.weights) == {"f1", "f2"}
    assert handle.weights["f1"] == 1.0

    loaded = svc.load_model("acc_m1")
    assert loaded.name == "acc_m1"
    assert "acc_m1" in svc.list_models()
