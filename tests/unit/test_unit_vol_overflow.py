"""T02 单测③（RD-8/RD-9 白盒）：vol÷100 后编码不溢出 uint32。

验收台 ``test_acc_vol_overflow.py`` 走 ``write_daily`` 黑盒；
本单测直接调用 ``VipdocWriter._check_bounds`` 白盒验证：
- 正常量：编码远低于上限；
- 89% 贴边量：编码 ≈ 0.895×uint32 上限，余量约 1.12×，应通过；
- 超上限量：编码 > uint32 上限，应抛 ``DataIntegrityError``（不截断/不取模）。
"""
from __future__ import annotations

import datetime as dt

import pytest

from Kuantix.adapters.coefficients import CoefficientResolver
from Kuantix.adapters.vipdoc_writer import UINT32_MAX, VipdocWriter
from Kuantix.core.contracts import Bar
from Kuantix.core.fail_loud import DataIntegrityError


def _bar(vol_lots: float, day: int) -> Bar:
    return Bar(
        date=dt.date(2024, 1, day),
        open=10.0,
        high=10.5,
        low=9.8,
        close=10.2,
        vol=vol_lots,
        amount=float(vol_lots) * 10.0,
    )


def test_check_bounds_normal_and_edge_pass() -> None:
    """白盒：正常量 / 89% 贴边量编码均不越界。"""
    writer = VipdocWriter(".")
    coeff = CoefficientResolver().resolve("sh600000.day")
    assert coeff.vol_coeff == 0.01  # A 股：编码值 = vol(手) × 100

    normal = writer._check_bounds(  # noqa: SLF001 - 白盒单测
        [_bar(1_000_000.0, day=1)], coeff, context="sh600000.day"
    )
    assert normal.max_encoded_vol < UINT32_MAX
    assert normal.vol_headroom > 1.0

    # 89% 贴边：编码 ≈ 0.895 × uint32 上限（实测 000100 的余量只有 ~1.12×）
    edge_vol = round(UINT32_MAX / 100 * 0.895)
    edge = writer._check_bounds(  # noqa: SLF001 - 白盒单测
        [_bar(edge_vol, day=2)], coeff, context="sh600000.day"
    )
    assert edge.max_encoded_vol <= UINT32_MAX
    assert 1.0 < edge.vol_headroom < 1.2


def test_check_bounds_over_limit_raises() -> None:
    """白盒：编码值超过 uint32 上限，必须抛 DataIntegrityError。"""
    writer = VipdocWriter(".")
    coeff = CoefficientResolver().resolve("sh600000.day")
    over_vol = UINT32_MAX / 100 + 1_000_000.0
    with pytest.raises(DataIntegrityError):
        writer._check_bounds(  # noqa: SLF001 - 白盒单测
            [_bar(over_vol, day=3)], coeff, context="sh600000.day"
        )


def test_write_daily_over_limit_rejected(tmp_path) -> None:
    """白盒→黑盒收敛：write_daily 对超上限量同样拒绝（整只标的进隔离区）。"""
    writer = VipdocWriter(tmp_path)
    over_vol = UINT32_MAX / 100 + 1_000_000.0
    with pytest.raises(DataIntegrityError):
        writer.write_daily(
            [_bar(over_vol, day=3)], "sh", "600000", path=tmp_path / "sh600000.day"
        )


def test_ex_daily_bounds_uint32_vol() -> None:
    """白盒：扩展市场（港美股）vol 直存 uint32，同样预检。"""
    writer = VipdocWriter(".")
    ex_bars = [_bar(1_000_000.0, day=1)]
    bound = writer._check_ex_bounds(ex_bars, context="31#00700.day")  # noqa: SLF001
    assert bound.max_encoded_vol <= UINT32_MAX
    assert bound.vol_headroom > 1.0
