"""新增技术指标单元测试（BOLL/ENE/SAR/WR/BIAS/OBV/VWAP）。

口径校准参照通达信：
- BOLL 用总体标准差（ddof=0）；
- SAR 的 AF 步长 0.02 / 上限 0.2、初始方向由前两根涨跌决定；
- WR 区间 [0, 100]，收盘越接近 N 日高点值越小；
- OBV 首根为 0，累积 sign(ΔC)*VOL；
- VWAP 为当日累计 amount/vol（分时均价线口径）。
"""

from __future__ import annotations

import numpy as np
import pytest

from Kuantix.analysis.indicators import bias, boll, ene, obv, sar, vwap, wr


# --------------------------------------------------------------------- #
# BOLL
# --------------------------------------------------------------------- #
class TestBoll:
    def test_constant_series_bands_collapse(self):
        """价格恒定 → 标准差 0，上/中/下轨重合。"""
        vals = [10.0] * 30
        out = boll(vals, n=20, p=2.0)
        assert out["mid"][25] == pytest.approx(10.0)
        assert out["upper"][25] == pytest.approx(10.0)
        assert out["lower"][25] == pytest.approx(10.0)

    def test_head_none_until_window_filled(self):
        out = boll([1.0, 2.0, 3.0, 4.0], n=3)
        assert out["mid"][0] is None and out["upper"][0] is None
        assert out["mid"][1] is None  # 窗口 3 未满
        assert out["mid"][2] is not None  # 第 3 根起有效

    def test_population_std(self):
        """ddof=0：手算 [1,2,3] 的 BOLL(3, 2) 校验。"""
        out = boll([1.0, 2.0, 3.0], n=3, p=2.0)
        assert out["mid"][2] == pytest.approx(2.0)
        # 总体标准差 = sqrt(2/3) ≈ 0.8165
        assert out["upper"][2] == pytest.approx(2.0 + 2.0 * np.sqrt(2.0 / 3.0))
        assert out["lower"][2] == pytest.approx(2.0 - 2.0 * np.sqrt(2.0 / 3.0))

    def test_upper_above_mid_above_lower(self):
        vals = list(np.linspace(10.0, 12.0, 40))
        out = boll(vals, n=10, p=2.0)
        assert out["upper"][-1] > out["mid"][-1] > out["lower"][-1]


# --------------------------------------------------------------------- #
# ENE
# --------------------------------------------------------------------- #
class TestEne:
    def test_default_params(self):
        """ENE(10, 11, 9)：上轨 = 1.11*MA10，下轨 = 0.91*MA10，中轨居中。"""
        vals = [10.0] * 15
        out = ene(vals, n=10, m1=11.0, m2=9.0)
        assert out["upper"][14] == pytest.approx(10.0 * 1.11)
        assert out["lower"][14] == pytest.approx(10.0 * 0.91)
        assert out["ene"][14] == pytest.approx((10.0 * 1.11 + 10.0 * 0.91) / 2)

    def test_head_none(self):
        out = ene([1.0, 2.0], n=5)
        assert out["upper"][0] is None


# --------------------------------------------------------------------- #
# SAR
# --------------------------------------------------------------------- #
class TestSar:
    def test_uptrend_sar_below_price(self):
        """单边上涨 → SAR 持续位于价格下方（多头止损点）。"""
        highs = [10.0 + i * 0.5 for i in range(20)]
        lows = [9.5 + i * 0.5 for i in range(20)]
        out = sar(highs, lows)
        s = [v for v in out["sar"] if v is not None]
        assert len(s) == 19  # 首根无 SAR（需两根定向）
        assert all(v < lows[i + 1] for i, v in enumerate(s[1:]))

    def test_downtrend_sar_above_price(self):
        """单边下跌 → SAR 位于价格上方（空头反转点）。"""
        highs = [20.0 - i * 0.5 for i in range(20)]
        lows = [19.5 - i * 0.5 for i in range(20)]
        out = sar(highs, lows)
        s = [v for v in out["sar"] if v is not None]
        assert all(v > lows[i + 1] for i, v in enumerate(s[1:]))

    def test_short_series_all_none(self):
        out = sar([10.0], [9.0])
        assert out["sar"] == [None]


# --------------------------------------------------------------------- #
# WR
# --------------------------------------------------------------------- #
class TestWr:
    def test_close_at_high_gives_zero(self):
        """收盘 = N 日最高 → WR = 0（超买）。"""
        highs = [11.0] * 10
        lows = [9.0] * 10
        closes = [11.0] * 10
        out = wr(highs, lows, closes, windows=(6,))
        assert out["wr6"][-1] == pytest.approx(0.0)

    def test_close_at_low_gives_hundred(self):
        """收盘 = N 日最低 → WR = 100（超卖）。"""
        out = wr([11.0] * 10, [9.0] * 10, [9.0] * 10, windows=(6,))
        assert out["wr6"][-1] == pytest.approx(100.0)

    def test_two_windows(self):
        keys = wr([11.0] * 12, [9.0] * 12, [10.0] * 12, windows=(6, 10))
        assert set(keys) == {"wr6", "wr10"}


# --------------------------------------------------------------------- #
# BIAS
# --------------------------------------------------------------------- #
class TestBias:
    def test_value_matches_formula(self):
        """收盘高于 MA6 约 5% → BIAS6 ≈ 4.13（4 位小数舍入口径）。"""
        vals = [10.0] * 6 + [10.5]
        out = bias(vals, windows=(6,))
        # MA6 of last 6 = (10*5 + 10.5)/6 = 10.0833
        expected = (10.5 - 10.083333) / 10.083333 * 100
        assert out["bias6"][-1] == pytest.approx(expected, abs=1e-4)

    def test_flat_series_zero(self):
        out = bias([10.0] * 30, windows=(6, 12, 24))
        assert out["bias6"][-1] == pytest.approx(0.0)


# --------------------------------------------------------------------- #
# OBV
# --------------------------------------------------------------------- #
class TestObv:
    def test_accumulates_signed_volume(self):
        closes = [10.0, 10.5, 10.2, 10.8]
        vols = [100.0, 200.0, 150.0, 300.0]
        out = obv(closes, vols)
        assert out["obv"][0] == pytest.approx(0.0)
        assert out["obv"][1] == pytest.approx(200.0)  # 涨 +
        assert out["obv"][2] == pytest.approx(200.0 - 150.0)  # 跌 -
        assert out["obv"][3] == pytest.approx(50.0 + 300.0)  # 涨 +

    def test_flat_close_no_change(self):
        out = obv([10.0] * 5, [100.0] * 5)
        assert out["obv"][-1] == pytest.approx(0.0)


# --------------------------------------------------------------------- #
# VWAP（分时均价线）
# --------------------------------------------------------------------- #
class TestVwap:
    def test_cumulative_average(self):
        closes = [10.0, 10.0, 10.0]
        vols = [100.0, 100.0, 100.0]
        amounts = [1000.0, 1100.0, 900.0]
        out = vwap(closes, vols, amounts)
        assert out[0] == pytest.approx(10.0)
        assert out[1] == pytest.approx((1000 + 1100) / 200)
        assert out[2] == pytest.approx(3000 / 300)

    def test_first_bar_zero_volume_is_none(self):
        out = vwap([10.0, 10.0], [0.0, 100.0], [0.0, 1000.0])
        assert out[0] is None
        assert out[1] == pytest.approx(10.0)
