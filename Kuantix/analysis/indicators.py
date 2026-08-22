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
- BOLL：MID = MA(C, N)，UPPER/LOWER = MID ± P * STD(C, N)（总体标准差，
  通达信用 ddof=0）。
- ENE：UPPER = (1+M1/100)*MA(C,N)，LOWER = (1-M2/100)*MA(C,N)，
  ENE = (UPPER+LOWER)/2。
- SAR：抛物线转向（AF 初值 0.02、步长 0.02、上限 0.2，多头初值取
  前两根低点、空头取前两根高点）。
- WR：威廉指标 = 100*(HHV(H,N)-C)/(HHV(H,N)-LLV(L,N))，标准 6/10 两档。
- BIAS：乖离率 = (C-MA(C,N))/MA(C,N)*100，标准 6/12/24 三档。
- OBV：能量潮，累积 sign(ΔC)*VOL。

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
    "boll",
    "ene",
    "sar",
    "wr",
    "bias",
    "obv",
    "vwap",
    "INDICATOR_NAMES",
]

#: 支持的指标名（路由层据此决定返回哪些指标，避免无谓计算）。
INDICATOR_NAMES = ("ma", "macd", "kdj", "rsi", "boll", "ene", "sar", "wr", "bias", "obv")


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


def _rolling(arr: np.ndarray, window: int, func: str) -> np.ndarray:
    """滚动窗口聚合（min_periods=window，窗口不满为 nan）。"""
    return getattr(pd.Series(arr).rolling(window, min_periods=window), func)().to_numpy()


def boll(
    values: Sequence[float | None], n: int = 20, p: float = 2.0
) -> dict[str, list[float | None]]:
    """布林带（BOLL）：MID ± P 倍总体标准差。

    Returns:
        ``{"upper": [...], "mid": [...], "lower": [...]}``。
    """
    arr = _as_float_array(values)
    out_none: list[float | None] = [None] * len(arr)
    if len(arr) < n:
        return {"upper": list(out_none), "mid": list(out_none), "lower": list(out_none)}
    mid = _rolling(arr, n, "mean")
    # 通达信用总体标准差（ddof=0）
    std = pd.Series(arr).rolling(n, min_periods=n).std(ddof=0).to_numpy()
    upper = mid + p * std
    lower = mid - p * std
    return {
        "upper": [None if np.isnan(v) else round(float(v), 6) for v in upper],
        "mid": [None if np.isnan(v) else round(float(v), 6) for v in mid],
        "lower": [None if np.isnan(v) else round(float(v), 6) for v in lower],
    }


def ene(
    values: Sequence[float | None], n: int = 10, m1: float = 11.0, m2: float = 9.0
) -> dict[str, list[float | None]]:
    """轨道线（ENE）：UPPER/LOWER = (1±M/100)*MA(C,N)，ENE 为中轨。

    Returns:
        ``{"upper": [...], "ene": [...], "lower": [...]}``（通达信三线）。
    """
    arr = _as_float_array(values)
    out_none: list[float | None] = [None] * len(arr)
    if len(arr) < n:
        return {"upper": list(out_none), "ene": list(out_none), "lower": list(out_none)}
    base = _rolling(arr, n, "mean")
    upper = base * (1 + m1 / 100.0)
    lower = base * (1 - m2 / 100.0)
    mid = (upper + lower) / 2.0
    return {
        "upper": [None if np.isnan(v) else round(float(v), 6) for v in upper],
        "ene": [None if np.isnan(v) else round(float(v), 6) for v in mid],
        "lower": [None if np.isnan(v) else round(float(v), 6) for v in lower],
    }


def sar(
    highs: Sequence[float | None],
    lows: Sequence[float | None],
    af_step: float = 0.02,
    af_max: float = 0.2,
) -> dict[str, list[float | None]]:
    """抛物线转向（SAR），通达信默认 AF 步长 0.02、上限 0.2。

    算法：首根以多头假设（SAR=前两根低点最小值）；此后 SAR 沿趋势外推，
    被价格穿越即翻转方向并重置 AF。头部不足两根时全 ``None``。

    Returns:
        ``{"sar": [...]}``。
    """
    h = _as_float_array(highs)
    low = _as_float_array(lows)
    size = len(h)
    out: list[float | None] = [None] * size
    if size < 2:
        return {"sar": out}

    # 初始方向：第 2 根相对第 1 根涨 → 多头
    up_trend = bool(h[1] >= low[0])
    af = af_step
    if up_trend:
        sar_val = float(min(low[0], low[1]))
        ep = float(max(h[0], h[1]))
    else:
        sar_val = float(max(h[0], h[1]))
        ep = float(min(low[0], low[1]))
    out[1] = round(sar_val, 6)

    for i in range(2, size):
        if np.isnan(h[i]) or np.isnan(low[i]):
            out[i] = out[i - 1]
            continue
        sar_val = sar_val + af * (ep - sar_val)
        if up_trend:
            # SAR 不得高于前两根低点（支撑约束）
            sar_val = min(sar_val, low[i - 1], low[i - 2] if i >= 2 else low[i - 1])
            if low[i] < sar_val:  # 跌破 → 翻空
                up_trend = False
                sar_val = float(ep)  # 新 SAR = 前期高点
                ep = float(low[i])
                af = af_step
            else:
                if h[i] > ep:
                    ep = float(h[i])
                    af = min(af + af_step, af_max)
        else:
            sar_val = max(sar_val, h[i - 1], h[i - 2] if i >= 2 else h[i - 1])
            if h[i] > sar_val:  # 突破 → 翻多
                up_trend = True
                sar_val = float(ep)
                ep = float(h[i])
                af = af_step
            else:
                if low[i] < ep:
                    ep = float(low[i])
                    af = min(af + af_step, af_max)
        out[i] = round(float(sar_val), 6)
    return {"sar": out}


def wr(
    highs: Sequence[float | None],
    lows: Sequence[float | None],
    closes: Sequence[float | None],
    windows: tuple[int, ...] = (6, 10),
) -> dict[str, list[float | None]]:
    """威廉指标（WR）：100*(HHV(H,N)-C)/(HHV(H,N)-LLV(L,N))。

    Returns:
        ``{"wr6": [...], "wr10": [...]}``（按 windows 动态命名）。
    """
    h = _as_float_array(highs)
    low = _as_float_array(lows)
    c = _as_float_array(closes)
    size = len(c)
    out_none: list[float | None] = [None] * size
    result: dict[str, list[float | None]] = {
        f"wr{w}": list(out_none) for w in windows
    }
    for w in windows:
        if size < w:
            continue
        hh = _rolling(h, w, "max")
        ll = _rolling(low, w, "min")
        with np.errstate(divide="ignore", invalid="ignore"):
            denom = hh - ll
            vals = np.where(denom == 0, 50.0, (hh - c) / denom * 100.0)
        vals = np.where(np.isnan(hh) | np.isnan(ll), np.nan, vals)
        result[f"wr{w}"] = [
            None if np.isnan(v) else round(float(v), 4) for v in vals
        ]
    return result


def bias(
    values: Sequence[float | None], windows: tuple[int, ...] = (6, 12, 24)
) -> dict[str, list[float | None]]:
    """乖离率（BIAS）：(C-MA(C,N))/MA(C,N)*100。

    Returns:
        ``{"bias6": [...], "bias12": [...], "bias24": [...]}``。
    """
    arr = _as_float_array(values)
    size = len(arr)
    out_none: list[float | None] = [None] * size
    result: dict[str, list[float | None]] = {
        f"bias{w}": list(out_none) for w in windows
    }
    for w in windows:
        if size < w:
            continue
        m = _rolling(arr, w, "mean")
        with np.errstate(divide="ignore", invalid="ignore"):
            vals = np.where(m == 0, 0.0, (arr - m) / m * 100.0)
        vals = np.where(np.isnan(m), np.nan, vals)
        result[f"bias{w}"] = [
            None if np.isnan(v) else round(float(v), 4) for v in vals
        ]
    return result


def obv(
    closes: Sequence[float | None], vols: Sequence[float | None]
) -> dict[str, list[float | None]]:
    """能量潮（OBV）：累积 sign(ΔC)*VOL，首根为 0。

    Returns:
        ``{"obv": [...]}``。
    """
    c = _as_float_array(closes)
    v = _as_float_array(vols)
    size = len(c)
    out: list[float | None] = [None] * size
    if size == 0:
        return {"obv": out}
    cum = 0.0
    out[0] = 0.0
    for i in range(1, size):
        if np.isnan(c[i]) or np.isnan(c[i - 1]) or np.isnan(v[i]):
            out[i] = round(cum, 4)
            continue
        if c[i] > c[i - 1]:
            cum += v[i]
        elif c[i] < c[i - 1]:
            cum -= v[i]
        out[i] = round(float(cum), 4)
    return {"obv": out}


def vwap(
    closes: Sequence[float | None],
    vols: Sequence[float | None],
    amounts: Sequence[float | None],
) -> list[float | None]:
    """成交量加权均价（分时均价线）：cumsum(amount)/cumsum(vol)。

    用于分时图均价线（当日累计口径）；vol 单位为手时结果与通达信一致
    （分子分母同乘 100 不影响比值）。

    Returns:
        与输入等长的 VWAP 序列（头部 vol 累计为 0 时为 ``None``）。
    """
    c = _as_float_array(closes)
    v = _as_float_array(vols)
    a = _as_float_array(amounts)
    size = len(c)
    if size == 0:
        return []
    cum_a = np.nancumsum(a)
    cum_v = np.nancumsum(v)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where(cum_v > 0, cum_a / cum_v, np.nan)
    return [None if np.isnan(x) else round(float(x), 6) for x in out]
