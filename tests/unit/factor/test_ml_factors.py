"""ML 因子单测（F4）：遗传规划因子可用；深度因子在 torch 缺失时清晰报错。"""

from __future__ import annotations

import importlib.util

import numpy as np
import pandas as pd
import pytest

from Kuantix.factor.factors import FACTORY_REGISTRY, discover_factors


def _synthetic_df(n: int = 400) -> pd.DataFrame:
    rng = np.random.default_rng(0)
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    df = pd.DataFrame({"open": close})
    df["close"] = df["open"]
    df["high"] = df["close"] + np.abs(rng.normal(0, 1, n))
    df["low"] = df["close"] - np.abs(rng.normal(0, 1, n))
    df["vol"] = rng.integers(1e5, 1e6, n).astype(float)
    return df


@pytest.fixture(scope="module", autouse=True)
def _discover():
    discover_factors()


def test_gp_factor_computes() -> None:
    """遗传规划因子纯标准库实现，可在无 ML 依赖环境下产出信号。"""
    assert "ml_gp_signal" in FACTORY_REGISTRY
    s = FACTORY_REGISTRY["ml_gp_signal"]().compute(_synthetic_df())
    assert isinstance(s, pd.Series)
    assert s.notna().sum() > 0


def test_deep_factor_clear_error_without_torch() -> None:
    """torch 缺失时深度因子应给出清晰报错，而非静默全 NaN。"""
    if importlib.util.find_spec("torch") is not None:
        pytest.skip("torch 已安装，跳过缺失依赖路径")
    assert "ml_lstm_return" in FACTORY_REGISTRY
    with pytest.raises(Exception):  # ModuleNotFoundError: No module named 'torch'
        FACTORY_REGISTRY["ml_lstm_return"]().compute(_synthetic_df())
