"""上游技术指标桥（NF-1 收敛，R2 合规）。

监控层 RuleEngine 的「指标判据」（MA 金叉死叉 / MACD / RSI）需要调用
上游 easy_tdx 的指标模块。按 R2 红线，``import easy_tdx`` 只允许出现在
``Kuantix/adapters/`` 内，因此本模块是唯一入口 —— monitor 层**不直接**触碰
上游指标函数，而是经本桥间接调用。

设计说明
--------
- 全部指标基于收盘价序列（``list[float]``）计算，返回 Python 原生类型
  （``float`` / ``list[float]``），不向业务层泄漏 pandas / numpy；
- 输入为空或样本不足时**显式报错**（NF-26），不返回 NaN / 静默 0；
- 复刻 MyTT 同名指标（``easy_tdx.MyTT``：MA / MACD / RSI）；
- 扩展盘前/盘后技术指标（KDJ / BOLL / 支撑压力位 / 趋势判定）。
"""

from __future__ import annotations

from typing import Sequence

import numpy as np
from easy_tdx.MyTT import MA, MACD, RSI

from Kuantix.core.fail_loud import DataIntegrityError, require_finite

__all__ = [
    "IndicatorBridge",
    "SMA",
    "compute_macd",
    "compute_rsi",
    "compute_kdj",
    "compute_boll",
]


def _as_float_array(closes: Sequence[float], context: str) -> np.ndarray:
    """把收盘价序列转成 float ndarray，逐值校验有限性（NF-12/NF-26）。"""
    if closes is None or len(closes) == 0:
        raise DataIntegrityError(
            f"[fail-loud/NF-26] {context}：收盘价序列为空，无法计算指标"
        )
    values: list[float] = []
    for item in closes:
        values.append(require_finite(float(item), f"{context}.close"))
    return np.asarray(values, dtype=float)


class IndicatorBridge:
    """上游技术指标统一入口（monitor 层唯一指标来源）。

    Examples:
        >>> closes = [1.0, 2.0, 3.0, 4.0, 5.0]
        >>> IndicatorBridge.sma(closes, period=3)
        4.0
    """

    # ------------------------------------------------------------------ #
    # 简单移动平均（MA）
    # ------------------------------------------------------------------ #

    @staticmethod
    def sma(closes: Sequence[float], period: int) -> float:
        """简单移动平均（最后一个窗口值）。

        Args:
            closes: 收盘价序列（升序）。
            period: 窗口长度。

        Returns:
            最新 SMA 值。

        Raises:
            DataIntegrityError: 样本不足或窗口非法。
        """
        values = _as_float_array(closes, "SMA")
        if period <= 0:
            raise DataIntegrityError(f"[fail-loud/NF-26] SMA period 必须为正，实际 {period!r}")
        if len(values) < period:
            raise DataIntegrityError(
                f"[fail-loud/NF-26] SMA 样本不足：len={len(values)} < period={period}"
            )
        window = values[-period:]
        return require_finite(float(np.mean(window)), "SMA.result")

    @staticmethod
    def sma_cross(closes: Sequence[float], fast: int, slow: int) -> str:
        """判断快慢均线的相对关系。

        Args:
            closes: 收盘价序列。
            fast: 快线窗口。
            slow: 慢线窗口（必须 > fast）。

        Returns:
            ``"above"``（快线在慢线上方）/ ``"below"``（快线在慢线下方）。

        Raises:
            DataIntegrityError: 窗口非法或样本不足。
        """
        if slow <= fast:
            raise DataIntegrityError(
                f"[fail-loud/NF-26] SMA 交叉要求 slow > fast，实际 fast={fast} slow={slow}"
            )
        fast_ma = IndicatorBridge.sma(closes, fast)
        slow_ma = IndicatorBridge.sma(closes, slow)
        if fast_ma > slow_ma:
            return "above"
        if fast_ma < slow_ma:
            return "below"
        raise DataIntegrityError(
            f"[fail-loud/NF-26] SMA 快慢线相等 fast={fast_ma} slow={slow_ma}，"
            f"无法判定上下（拒绝静默取默认）"
        )

    @staticmethod
    def ma_series(closes: Sequence[float], period: int) -> list[float]:
        """返回 MA 全序列（含前导 NaN 占位，业务层自行裁剪）。"""
        values = _as_float_array(closes, "MA")
        if period <= 0:
            raise DataIntegrityError(f"[fail-loud/NF-26] MA period 必须为正，实际 {period!r}")
        raw = MA(values, period)
        return [float(np.nan_to_num(x)) if not np.isnan(x) else float("nan") for x in raw]

    # ------------------------------------------------------------------ #
    # MACD
    # ------------------------------------------------------------------ #

    @staticmethod
    def macd(
        closes: Sequence[float],
        *,
        fast: int = 12,
        slow: int = 26,
        signal: int = 9,
    ) -> tuple[list[float], list[float], list[float]]:
        """MACD 三线（DIF / DEA / HIST），返回与输入等长的 Python 列表。

        Raises:
            DataIntegrityError: 样本不足。
        """
        values = _as_float_array(closes, "MACD")
        if len(values) < slow + signal:
            raise DataIntegrityError(
                f"[fail-loud/NF-26] MACD 样本不足：len={len(values)} < slow+signal={slow + signal}"
            )
        dif, dea, hist = MACD(values, fast, slow, signal)
        return (
            [float(np.nan_to_num(x)) if not np.isnan(x) else float("nan") for x in dif],
            [float(np.nan_to_num(x)) if not np.isnan(x) else float("nan") for x in dea],
            [float(np.nan_to_num(x)) if not np.isnan(x) else float("nan") for x in hist],
        )

    # ------------------------------------------------------------------ #
    # RSI
    # ------------------------------------------------------------------ #

    @staticmethod
    def rsi(closes: Sequence[float], period: int = 14) -> list[float]:
        """RSI 序列（与输入等长，前导 NaN 占位）。

        Raises:
            DataIntegrityError: 样本不足。
        """
        values = _as_float_array(closes, "RSI")
        if len(values) < period + 1:
            raise DataIntegrityError(
                f"[fail-loud/NF-26] RSI 样本不足：len={len(values)} < period+1={period + 1}"
            )
        raw = RSI(values, period)
        return [float(np.nan_to_num(x)) if not np.isnan(x) else float("nan") for x in raw]

    # ------------------------------------------------------------------ #
    # KDJ
    # ------------------------------------------------------------------ #

    @staticmethod
    def kdj(
        closes: Sequence[float],
        highs: Sequence[float],
        lows: Sequence[float],
        *,
        n: int = 9,
        m1: int = 3,
        m2: int = 3,
    ) -> tuple[list[float], list[float], list[float]]:
        """KDJ 三线（K / D / J），返回与输入等长的 Python 列表。

        指标实现口径：
        - RSV := (close - LLV(low, N)) / (HHV(high, N) - LLV(low, N)) * 100
        - K_{t} = (2/3) * K_{t-1} + (1/3) * RSV_t （前导初值取 50）
        - D_{t} = (2/3) * D_{t-1} + (1/3) * K_t
        - J   = 3 * K - 2 * D

        Raises:
            DataIntegrityError: 三个序列长度不一致或总样本不足 ``n + m1 + m2``。
        """
        close_arr = _as_float_array(closes, "KDJ.close")
        high_arr = _as_float_array(highs, "KDJ.high")
        low_arr = _as_float_array(lows, "KDJ.low")
        size = len(close_arr)
        if not (len(high_arr) == size and len(low_arr) == size):
            raise DataIntegrityError(
                f"[fail-loud/NF-26] KDJ 三序列长度不一致："
                f"close={size} high={len(high_arr)} low={len(low_arr)}"
            )
        if size < n + m1 + m2:
            raise DataIntegrityError(
                f"[fail-loud/NF-26] KDJ 样本不足：len={size} < n+m1+m2={n + m1 + m2}"
            )
        # 向量化 HHV / LLV
        high_win = np.empty(size, dtype=float)
        low_win = np.empty(size, dtype=float)
        for i in range(size):
            start = max(0, i - n + 1)
            high_win[i] = np.nanmax(high_arr[start : i + 1])
            low_win[i] = np.nanmin(low_arr[start : i + 1])
        denom = high_win - low_win
        with np.errstate(divide="ignore", invalid="ignore"):
            rsv = np.where(denom > 0, (close_arr - low_win) / denom * 100.0, 50.0)
        # 前导平滑（KD 初始化 50）
        k_ary = np.empty(size, dtype=float)
        d_ary = np.empty(size, dtype=float)
        alpha_k, alpha_d = 1.0 / float(m1), 1.0 / float(m2)
        k_prev = 50.0
        d_prev = 50.0
        for i in range(size):
            k_prev = (1.0 - alpha_k) * k_prev + alpha_k * float(rsv[i])
            d_prev = (1.0 - alpha_d) * d_prev + alpha_d * k_prev
            k_ary[i] = k_prev
            d_ary[i] = d_prev
        j_ary = 3.0 * k_ary - 2.0 * d_ary
        return (
            [float(np.nan_to_num(x)) for x in k_ary],
            [float(np.nan_to_num(x)) for x in d_ary],
            [float(np.nan_to_num(x)) for x in j_ary],
        )

    # ------------------------------------------------------------------ #
    # BOLL（布林带）
    # ------------------------------------------------------------------ #

    @staticmethod
    def boll(
        closes: Sequence[float],
        *,
        period: int = 20,
        k: float = 2.0,
    ) -> tuple[list[float], list[float], list[float]]:
        """布林带（upper / mid / lower）三线等长输出。

        - mid   = 滚动 MA(period)
        - upper = mid + k * std(period)
        - lower = mid - k * std(period)
        - 前 ``period - 1`` 个位置以 NaN 占位，外部渲染时跳过（统一 MACD 风格）。

        Raises:
            DataIntegrityError: ``len(closes) < period`` 或窗口 / 倍数非法。
        """
        values = _as_float_array(closes, "BOLL")
        if period <= 0:
            raise DataIntegrityError(f"[fail-loud/NF-26] BOLL period 必须为正，实际 {period!r}")
        if not (isinstance(k, (int, float)) and float(k) > 0):
            raise DataIntegrityError(f"[fail-loud/NF-26] BOLL k 必须为正数，实际 {k!r}")
        if len(values) < period:
            raise DataIntegrityError(
                f"[fail-loud/NF-26] BOLL 样本不足：len={len(values)} < period={period}"
            )
        mid = np.empty_like(values, dtype=float)
        std = np.empty_like(values, dtype=float)
        mid[:] = np.nan
        std[:] = np.nan
        for i in range(period - 1, len(values)):
            window = values[i - period + 1 : i + 1]
            mid[i] = float(np.mean(window))
            # 使用总体标准差（与 TA-lib / 多数行情软件一致）
            std[i] = float(np.std(window))
        upper = mid + float(k) * std
        lower = mid - float(k) * std
        return (
            [float(x) if not np.isnan(x) else float("nan") for x in upper],
            [float(x) if not np.isnan(x) else float("nan") for x in mid],
            [float(x) if not np.isnan(x) else float("nan") for x in lower],
        )

    # ------------------------------------------------------------------ #
    # 支撑 / 压力位
    # ------------------------------------------------------------------ #

    @staticmethod
    def support_resistance(
        closes: Sequence[float],
        highs: Sequence[float],
        lows: Sequence[float],
        *,
        lookback: int = 60,
        window: int = 5,
    ) -> tuple[list[float], list[float]]:
        """基于局部极值的支撑位 / 压力位（NF-26：空序列直接抛错）。

        - 压力位：窗口内 ``high`` 为左右 ``window`` 条内的局部最高点；
        - 支撑位：窗口内 ``low``  为左右 ``window`` 条内的局部最低点。
        - 结果去重（同一价位出现多次只保留 1 条），按距当前价的距离取最近 8 条。

        Returns:
            ``(supports_sorted, resistances_sorted)``。``supports`` 升序（< last close），
            ``resistances`` 降序（> last close）。
        """
        close_arr = _as_float_array(closes, "SR.close")
        high_arr = _as_float_array(highs, "SR.high")
        low_arr = _as_float_array(lows, "SR.low")
        size = len(close_arr)
        if not (len(high_arr) == size and len(low_arr) == size):
            raise DataIntegrityError(
                f"[fail-loud/NF-26] SR 三序列长度不一致："
                f"close={size} high={len(high_arr)} low={len(low_arr)}"
            )
        if lookback <= 0 or window <= 0:
            raise DataIntegrityError(
                f"[fail-loud/NF-26] SR lookback/window 必须为正，lookback={lookback} window={window}"
            )
        if size < window * 2 + 1:
            raise DataIntegrityError(
                f"[fail-loud/NF-26] SR 样本不足：len={size} < 2*window+1={window * 2 + 1}"
            )
        tail_start = max(0, size - lookback)
        last_close = float(close_arr[-1])
        supports_set: set[float] = set()
        resistances_set: set[float] = set()
        for i in range(max(tail_start, window), min(size - window, size)):
            # 压力
            seg_h = high_arr[i - window : i + window + 1]
            if high_arr[i] >= float(np.nanmax(seg_h)):
                resistances_set.add(round(float(high_arr[i]), 2))
            # 支撑
            seg_l = low_arr[i - window : i + window + 1]
            if low_arr[i] <= float(np.nanmin(seg_l)):
                supports_set.add(round(float(low_arr[i]), 2))
        supports = sorted([s for s in supports_set if s <= last_close])
        resistances = sorted([r for r in resistances_set if r >= last_close], reverse=True)
        return supports[-8:], resistances[-8:]

    # ------------------------------------------------------------------ #
    # 趋势判定
    # ------------------------------------------------------------------ #

    @staticmethod
    def trend(
        closes: Sequence[float],
        *,
        short: int = 20,
        long_period: int = 60,
    ) -> tuple[str, float]:
        """基于双均线的趋势方向 + 强度。

        Returns:
            ``(direction, strength)``
            - ``direction ∈ {'up', 'down', 'flat'}``；
            - ``strength ∈ [0, 1]``：``(last - MA_long) / MA_long`` 的绝对值按 10%
              线性映射到 [0, 1]，超出即裁剪到 1。
        """
        values = _as_float_array(closes, "TREND")
        if short <= 0 or long_period <= 0 or short >= long_period:
            raise DataIntegrityError(
                f"[fail-loud/NF-26] TREND 要求 0<short<long，实际 short={short} long={long_period}"
            )
        if len(values) < long_period:
            raise DataIntegrityError(
                f"[fail-loud/NF-26] TREND 样本不足：len={len(values)} < long={long_period}"
            )
        ma_short = IndicatorBridge.sma(values[-short:].tolist(), short)
        ma_long = IndicatorBridge.sma(values[-long_period:].tolist(), long_period)
        last_close = float(values[-1])
        # 等号判为 flat（相等意味着方向不明确，避免静默取默认）
        if ma_short > ma_long:
            direction = "up"
        elif ma_short < ma_long:
            direction = "down"
        else:
            direction = "flat"
        # 强度裁剪 [0,0.1] → [0,1]
        denom = float(ma_long) if float(ma_long) != 0 else 1e-9
        raw = abs((last_close - denom) / denom)
        strength = min(1.0, max(0.0, raw / 0.1))
        return direction, require_finite(strength, "TREND.strength")


#: 模块级便捷别名（与 MyTT 命名对齐，供外部引用）
SMA = IndicatorBridge.sma
compute_macd = IndicatorBridge.macd
compute_rsi = IndicatorBridge.rsi
compute_kdj = IndicatorBridge.kdj
compute_boll = IndicatorBridge.boll
