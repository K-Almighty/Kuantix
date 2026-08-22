"""T04 因子/选股层白盒单测（FactorStore / FactorCombiner / FactorService / ScreenService）。

全部离线：用合成 vipdoc + 假 store，验证：
- FactorStore 增量计算（跳过已算区间）；
- FactorCombiner 等权 / IC / IR 加权；
- FactorService combine 生成模型并落库；
- ScreenService 排序 / 过滤 / 落盘（GBK CSV）。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from Kuantix.core.contracts import ModelHandle
from Kuantix.factor.combiner import FactorCombiner
from Kuantix.factor.service import FactorService
from Kuantix.factor.store import FactorStore
from Kuantix.screen.service import ScreenRequest, ScreenService


# ---------------------------------------------------------------------------
# FactorStore
# ---------------------------------------------------------------------------


def test_factor_store_save_load_roundtrip(tmp_path: Path) -> None:
    store = FactorStore(tmp_path / "factors", tmp_path / "db")
    df = pd.DataFrame(
        {
            "date": [20240101, 20240102, 20240101],
            "code": ["600000", "600000", "600036"],
            "value": [1.0, 2.0, 3.0],
        }
    )
    store.save("momentum_20d", 2024, df)
    loaded = store.load("momentum_20d", date=20240101)
    assert set(loaded["code"]) == {"600000", "600036"}
    assert store.list_factors() == ["momentum_20d"]
    assert store.years_for("momentum_20d") == [2024]
    assert store.computed_until("momentum_20d") is not None


def test_factor_store_rejects_nan(tmp_path: Path) -> None:
    from Kuantix.core.fail_loud import DataIntegrityError

    store = FactorStore(tmp_path / "factors", tmp_path / "db")
    df = pd.DataFrame(
        {"date": [20240101], "code": ["600000"], "value": [float("nan")]}
    )
    with pytest.raises(DataIntegrityError):
        store.save("bad_factor", 2024, df)


# ---------------------------------------------------------------------------
# FactorCombiner
# ---------------------------------------------------------------------------


def test_combiner_equal_weights() -> None:
    combiner = FactorCombiner()
    values = pd.DataFrame(
        {"f1": [1.0, 2.0, 3.0], "f2": [3.0, 2.0, 1.0]},
        index=["000001", "000002", "000003"],
    )
    series = combiner.combine(values, "equal")
    assert len(series) == 3
    # f1/f2 反向，等权后应接近 0（z-score 抵消）
    assert abs(float(series.iloc[0])) < 1e-9


def test_combiner_ic_weights_require_weights() -> None:
    from Kuantix.core.fail_loud import MissingKeyError

    combiner = FactorCombiner()
    values = pd.DataFrame({"f1": [1.0, 2.0], "f2": [3.0, 1.0]}, index=["a", "b"])
    with pytest.raises(MissingKeyError):
        combiner.combine(values, "ic")  # 未给权重 → fail-loud


def test_combiner_ic_weighted() -> None:
    combiner = FactorCombiner()
    values = pd.DataFrame(
        {"f1": [1.0, 2.0, 3.0], "f2": [3.0, 2.0, 1.0]},
        index=["a", "b", "c"],
    )
    series = combiner.combine(values, "ic", weights={"f1": 0.1, "f2": 0.9})
    assert len(series) == 3
    # f2 权重更高 → 排序与 f2 一致（降序）
    assert series.index[0] == "a"


# ---------------------------------------------------------------------------
# FactorService.combine + 模型落库
# ---------------------------------------------------------------------------


class _FakeStore(FactorStore):
    """假 store：避免 Parquet，用内存 dict 模拟 load。"""

    def __init__(self) -> None:
        self._data: dict[str, pd.DataFrame] = {}
        self._factors: list[str] = []

    def save(self, factor, year, df):
        self._factors.append(factor)
        return len(df)

    def load(self, factor, date=None, code=None, *, start=None, end=None):
        return self._data.get(factor, pd.DataFrame(columns=["date", "code", "value"]))

    def list_factors(self):
        return sorted(set(self._factors))

    def computed_until(self, factor):
        return "20240101"


def test_service_combine_equal_saves_model(tmp_path: Path) -> None:
    svc = FactorService.__new__(FactorService)
    svc._store = _FakeStore()
    svc._models_db = tmp_path / "models.db"
    svc._ensure_models_schema()
    handle = svc.combine(("f1", "f2"), "equal", name="m1", save_model=True)
    assert isinstance(handle, ModelHandle)
    assert handle.method == "equal"
    assert set(handle.weights) == {"f1", "f2"}
    loaded = svc.load_model("m1")
    assert loaded.name == "m1"
    assert svc.list_models() == ["m1"]


# ---------------------------------------------------------------------------
# ScreenService 排序 / 过滤
# ---------------------------------------------------------------------------


class _MemStore:
    """内存版因子 store（load 返回 DataFrame，list_factors 返回因子名）。"""

    def __init__(self, data: dict[str, pd.DataFrame]) -> None:
        self._data = data

    def load(self, factor, date=None, code=None, *, start=None, end=None):
        df = self._data[factor]
        if date is not None:
            df = df[df["date"] == int(date)]
        if code is not None:
            df = df[df["code"] == str(code)]
        return df.reset_index(drop=True)

    def load_latest_per_code(self, factor, *, as_of=None):
        """与 FactorStore.load_latest_per_code 同语义：≤as_of 每码最新行。"""
        df = self._data[factor]
        if as_of is not None:
            df = df[df["date"] <= int(as_of)]
        latest = df.loc[df.groupby("code")["date"].idxmax()]
        return latest.reset_index(drop=True)

    def list_factors(self):
        return sorted(self._data)


class _FakeReader:
    def __init__(self, frames: dict[str, pd.DataFrame]) -> None:
        self._frames = frames

    def read_daily_frame(self, exchange: str, code: str):
        return self._frames[code]


class _NoFilter:
    def tech_filter(self, df, cond):
        return True

    def chanlun_filter(self, df, cond):
        return True


def test_screen_service_scores_and_sorts(tmp_path: Path) -> None:
    data = {
        "momentum_20d": pd.DataFrame(
            {"date": [20240101, 20240101], "code": ["600000", "600036"], "value": [1.0, 3.0]}
        ),
        "volume_ratio_5d": pd.DataFrame(
            {"date": [20240101, 20240101], "code": ["600000", "600036"], "value": [1.0, 2.0]}
        ),
    }
    store = _MemStore(data)
    frames = {
        "600000": pd.DataFrame(
            {"datetime": pd.to_datetime(["2024-01-02", "2024-01-03"]),
             "close": [10.0, 10.1], "open": [10.0, 10.0], "high": [10.2, 10.2],
             "low": [9.9, 9.9], "vol": [1000.0, 1000.0], "amount": [10000.0, 10100.0]}
        ),
        "600036": pd.DataFrame(
            {"datetime": pd.to_datetime(["2024-01-02", "2024-01-03"]),
             "close": [20.0, 20.1], "open": [20.0, 20.0], "high": [20.2, 20.2],
             "low": [19.9, 19.9], "vol": [2000.0, 2000.0], "amount": [40000.0, 40200.0]}
        ),
    }
    reader = _FakeReader(frames)
    screen = ScreenService.__new__(ScreenService)
    screen._config = type("C", (), {"paths": type("P", (), {"db": tmp_path, "exports": tmp_path / "exports"})()})()
    screen._config.paths.exports.mkdir(parents=True, exist_ok=True)
    screen._store = store
    screen._model_loader = None
    screen._reader = reader
    screen._combiner = FactorCombiner()
    screen._filter = _NoFilter()
    screen._profile = None
    screen._results_db = tmp_path / "screen_results.db"
    screen._ensure_schema()

    results = screen.run(ScreenRequest(market="CN", top_n=2, factors=("momentum_20d", "volume_ratio_5d")))
    assert len(results) == 2
    # 600036 momentum 高 → 综合分应排第一
    assert results[0].code == "600036"
    assert results[0].score > results[1].score
    # CSV 已落盘（GBK）
    csv_files = list((tmp_path / "exports").glob("screen_*.csv"))
    assert len(csv_files) == 1
    # 同花顺兼容头（契约 §3.4 六列，与 S6 export_csv_bytes 同一口径）
    text = csv_files[0].read_bytes().decode("gbk")
    header = "代码,名称,最新价,综合得分,触发条件,数据日期"
    assert header in text
    assert "仅供人工核对参考，非自动交易指令" in text
    row = text.strip().splitlines()[2]
    assert len(row.split(",")) == 6
    assert row.split(",")[-1] == "2024-01-01"  # 数据日期 = as_of
