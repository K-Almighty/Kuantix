"""选股条件过滤器（技术 / 缠论，T04）。

:class:`ScreenFilter` 提供两个过滤器：
- :meth:`tech_filter` —— 技术面条件（均线多头 / 放量 / 价格区间）；
- :meth:`chanlun_filter` —— 缠论条件（买卖点，经上游
  :class:`~easy_tdx.chanlun.ChanlunAnalyser`）。

上游组件经 :mod:`Kuantix.adapters.backtest_bridge` 间接访问（NF-1/R2）。
条件参数从 ``cond`` 字典传入，**不硬编码**市场常量（NF-5）：
阈值如均线周期、量比、价格区间都由调用方给出。
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from Kuantix.adapters.backtest_bridge import BacktestBridge
from Kuantix.core.fail_loud import DataIntegrityError, require_known

__all__ = ["ScreenFilter"]


class ScreenFilter:
    """选股条件过滤器。

    Args:
        bridge: 上游回测/技术组件桥；``None`` 时新建默认实例。
    """

    def __init__(self, bridge: BacktestBridge | None = None) -> None:
        self._bridge = bridge if bridge is not None else BacktestBridge()

    def tech_filter(self, df: pd.DataFrame, cond: dict[str, Any]) -> bool:
        """技术面条件过滤。

        Args:
            df: 单标的日线 DataFrame（含 close/high/low/vol）。
            cond: 条件字典，支持键：
                - ``ma_fast`` / ``ma_slow``：快慢均线周期，要求 fast > slow；
                - ``min_close`` / ``max_close``：收盘价区间；
                - ``min_vol_ratio``：量比下限（当日量 / 20 日均量）。

        Returns:
            是否通过过滤。

        Raises:
            Kuantix.core.fail_loud.UnknownValueError: 条件键未知。
        """
        if not cond:
            return True
        allowed = {"ma_fast", "ma_slow", "min_close", "max_close", "min_vol_ratio"}
        for key in cond:
            require_known(key, "tech_filter 条件键", allowed=allowed)

        close = df["close"].astype(float)
        if "ma_fast" in cond and "ma_slow" in cond:
            fast = int(cond["ma_fast"])
            slow = int(cond["ma_slow"])
            ma_fast = close.rolling(fast).mean().iloc[-1]
            ma_slow = close.rolling(slow).mean().iloc[-1]
            if pd.isna(ma_fast) or pd.isna(ma_slow) or ma_fast <= ma_slow:
                return False

        if "min_close" in cond:
            if close.iloc[-1] < float(cond["min_close"]):
                return False
        if "max_close" in cond:
            if close.iloc[-1] > float(cond["max_close"]):
                return False
        if "min_vol_ratio" in cond:
            vol = df["vol"].astype(float)
            avg20 = vol.rolling(20).mean().iloc[-1]
            if pd.isna(avg20) or avg20 <= 0:
                return False
            ratio = vol.iloc[-1] / avg20
            if ratio < float(cond["min_vol_ratio"]):
                return False
        return True

    def chanlun_filter(self, df: pd.DataFrame, cond: dict[str, Any]) -> bool:
        """缠论条件过滤（经上游 ChanlunAnalyser）。

        Args:
            df: 单标的日线 DataFrame（含 open/high/low/close/vol）。
            cond: 条件字典，支持键：
                - ``require_buy_point``：要求最近出现买点（MMD 类型含 buy）。

        Returns:
            是否通过过滤。

        Raises:
            Kuantix.core.fail_loud.UnknownValueError: 条件键未知。
        """
        if not cond:
            return True
        allowed = {"require_buy_point"}
        for key in cond:
            require_known(key, "chanlun_filter 条件键", allowed=allowed)

        if "require_buy_point" in cond:
            summary = self._bridge.chanlun_analyze(df)
            if "latest_is_buy" not in summary:
                raise DataIntegrityError(
                    "[fail-loud/NF-1] 缠论分析结果缺少 latest_is_buy，上游契约可能已变更"
                )
            return bool(summary["latest_is_buy"])
        return True
