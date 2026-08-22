"""技术指标计算（通达信风格，纯 numpy/pandas 实现，无外部重依赖）。

本模块只做「输入 OHLCV 序列 → 输出指标序列」的纯计算，不触碰数据源，
便于单元测试与前端复用同一套口径。所有指标对缺失头部以 ``None`` 对齐，
与前端 ECharts 的 ``connectNulls=false`` 配合可正确留白。

口径约定（与通达信保持一致）
--------------------------
- MA：简单移动平均。
- MACD：EMA(12) - EMA(26) 为 DIF，DEA = EMA(DIF, 9)，MACD 柱 = 2*(DIF-DEA)。
- KDJ：RSV = (C - L9) / (H9 - L9) * 100；K = EMA(RSV,3)（初值 50），
  D = EMA(K,3)（初值 50），J = 3K - 2D（初值 50）。
- RSI：Wilder 平滑涨跌（初值用首段简单均值），标准 6/12/24 三档。

所有函数接受 ``Sequence[float]``（允许含 ``None``，会被跳过），返回
长度与输入一致的 ``list[float | None]``。
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pandas as pd

__all__ = [
    "ma",
    "macd",
    "kdj",
    "rsi",
    "INDICATOR_NAMES",
]

#: 支持的指标名（路由层据此决定返回哪些指标，避免无谓计算）。
INDICATOR_NAMES = ("ma", "macd", "kdj", "rsi")


def _as_float_array(values: Sequence[float | None]) -> np.ndarray:
    """安全转为 float ndarray；``None`` 转为 ``nan`` 以兼容 numpy 窗口运算。"""
    return np.asarray(
        [np.nan if v is None else float(v) for v in values],
        dtype=float,
    )


def ma(values: Sequence[float | None], window: int = 20) -> list[float | None]:
    """简单移动平均（MA）。前 ``window-1`` 个位置返回 ``None``。

    Args:
        values: 收盘价序列（允许含 ``None``）。
        window: 均线窗口（默认 20，与通达信 MA20 习惯一致）。

    Returns:
        与输入等长；窗口未满为 ``None``，其余为四舍五入 6 位的均值。
    """
    arr = _as_float_array(values)
    out = np.full(len(arr), np.nan, dtype=float)
    if len(arr) >= window:
        conv = np.convolve(arr, np.ones(window) / window, mode="valid")
        out[window - 1 :] = conv
    return [None if np.isnan(v) else round(float(v), 6) for v in out]


def _ema(arr: np.ndarray, span: int) -> np.ndarray:
    """指数移动平均（pandas ewm，``adjust=False`` 即通达信递推口径）。"""
    s = pd.Series(arr)
    return s.ewm(span=span, adjust=False, min_periods=span).mean().to_numpy()


def macd(
    values: Sequence[float | None],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> dict[str, list[float | None]]:
    """MACD（DIF / DEA / MACD 柱）。

    Returns:
        ``{"dif": [...], "dea": [...], "macd": [...]}``（柱 = 2*(DIF-DEA)）。
    """
    arr = _as_float_array(values)
    out_none: list[float | None] = [None] * len(arr)
    if len(arr) < slow:
        return {"dif": list(out_none), "dea": list(out_none), "macd": list(out_none)}
    dif = _ema(arr, fast) - _ema(arr, slow)
    dea = _ema(dif, signal)
    bar = 2.0 * (dif - dea)
    return {
        "dif": [None if np.isnan(v) else round(float(v), 6) for v in dif],
        "dea": [None if np.isnan(v) else round(float(v), 6) for v in dea],
        "macd": [None if np.isnan(v) else round(float(v), 6) for v in bar],
    }


def kdj(
    highs: Sequence[float | None],
    lows: Sequence[float | None],
    closes: Sequence[float | None],
    n: int = 9,
    m1: int = 3,
    m2: int = 3,
) -> dict[str, list[float | None]]:
    """KDJ（K / D / J）。

    RSV 初值缺失段置 ``nan``；K/D 以 50 为初值递推（通达信默认）。
    """
    h = _as_float_array(highs)
    low = _as_float_array(lows)
    c = _as_float_array(closes)
    size = len(c)
    out_none: list[float | None] = [None] * size
    if size < n:
        return {"k": list(out_none), "d": list(out_none), "j": list(out_none)}

    # 滚动 9 日最高/最低
    hh = pd.Series(h).rolling(n, min_periods=n).max().to_numpy()
    ll = pd.Series(low).rolling(n, min_periods=n).min().to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        rsv = np.where((hh - ll) == 0, 50.0, (c - ll) / (hh - ll) * 100.0)
    rsv = np.where(np.isnan(hh), np.nan, rsv)

    k = np.full(size, np.nan, dtype=float)
    d = np.full(size, np.nan, dtype=float)
    prev_k = 50.0
    prev_d = 50.0
    for i in range(size):
        if np.isnan(rsv[i]):
            k[i] = prev_k
            d[i] = prev_d
            continue
        prev_k = (m1 - 1) / m1 * prev_k + 1 / m1 * rsv[i]
        prev_d = (m2 - 1) / m2 * prev_d + 1 / m2 * prev_k
        k[i] = prev_k
        d[i] = prev_d
    j = 3 * k - 2 * d
    return {
        "k": [None if np.isnan(v) else round(float(v), 4) for v in k],
        "d": [None if np.isnan(v) else round(float(v), 4) for v in d],
        "j": [None if np.isnan(v) else round(float(v), 4) for v in j],
    }


def rsi(
    values: Sequence[float | None], windows: tuple[int, ...] = (6, 12, 24)
) -> dict[str, list[float | None]]:
    """RSI（Wilder 平滑，多周期同返）。

    Returns:
        ``{"rsi6": [...], "rsi12": [...], "rsi24": [...]}``。
    """
    arr = _as_float_array(values)
    size = len(arr)
    out_none: list[float | None] = [None] * size
    result: dict[str, list[float | None]] = {
        f"rsi{w}": list(out_none) for w in windows
    }
    if size < 2:
        return result

    deltas = np.diff(arr, prepend=np.nan)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    for w in windows:
        if size <= w:
            continue
        # Wilder 平滑：首值用简单均值，其后递推
        avg_gain = np.full(size, np.nan, dtype=float)
        avg_loss = np.full(size, np.nan, dtype=float)
        g = pd.Series(gains)
        ls = pd.Series(losses)
        avg_gain[w] = g.iloc[: w + 1].mean()
        avg_loss[w] = ls.iloc[: w + 1].mean()
        for i in range(w + 1, size):
            avg_gain[i] = (avg_gain[i - 1] * (w - 1) + gains[i]) / w
            avg_loss[i] = (avg_loss[i - 1] * (w - 1) + losses[i]) / w
        with np.errstate(divide="ignore", invalid="ignore"):
            rs = np.where(avg_loss == 0, np.inf, avg_gain / avg_loss)
            rsi_vals = np.where(avg_loss == 0, 100.0, 100.0 - 100.0 / (1.0 + rs))
        result[f"rsi{w}"] = [
            None if np.isnan(v) else round(float(v), 4) for v in rsi_vals
        ]
    return result
