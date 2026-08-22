"""T02 单测⑤（NF-27 白盒）：写→回读数值一致，覆盖 A股/ETF/指数/债券 ≥4 类。

验收台 ``test_acc_write_readback.py`` 走 ``write_daily`` + ``read_daily_bars``
黑盒逐字段比对；本单测从**内部实现**验证：
- ``_to_security_bar`` 契约转换正确（vol 单位=手，RD-8）；
- ``_check_bounds`` 对每类证券的系数预检不越界；
- 写后回读（``_verify_daily_tail``）价格容差 < 0.001。
"""
from __future__ import annotations

import datetime as dt

import pytest

from Kuantix.adapters.coefficients import CoefficientResolver
from Kuantix.adapters.vipdoc_writer import VipdocWriter
from Kuantix.core.contracts import Bar

PRICE_TOL = 1e-3

# (文件名, exchange, code, 期望类型)
_CASES = [
    ("sh600000.day", "sh", "600000", "SH_A_STOCK"),
    ("sh510300.day", "sh", "510300", "SH_FUND"),
    ("sh000001.day", "sh", "000001", "SH_INDEX"),
    ("sh010107.day", "sh", "010107", "SH_BOND"),
]


def _make_bar(close: float = 10.2, day: int = 1) -> Bar:
    return Bar(
        date=dt.date(2024, 1, day),
        open=close - 0.2,
        high=close + 0.3,
        low=close - 0.4,
        close=close,
        vol=1_000_000.0,
        amount=10_200_000.0,
    )


def test_to_security_bar_vol_is_lots() -> None:
    """白盒：_to_security_bar 的 vol 保持「手」语义（RD-8），不擅自换算。"""
    writer = VipdocWriter(".")
    bar = _make_bar()
    sec_bar = writer._to_security_bar(bar)  # noqa: SLF001 - 白盒单测
    assert sec_bar.vol == bar.vol
    assert sec_bar.year == 2024
    assert sec_bar.month == 1
    assert sec_bar.day == 1
    assert sec_bar.close == bar.close


def test_check_bounds_for_all_four_types() -> None:
    """白盒：四类证券按各自系数预检，编码不越界且 price_headroom 有限。"""
    writer = VipdocWriter(".")
    for fname, _exchange, _code, sec_type in _CASES:
        coeff = CoefficientResolver().resolve(fname)
        assert coeff.security_type == sec_type
        bound = writer._check_bounds(  # noqa: SLF001 - 白盒单测
            [_make_bar(day=1)], coeff, context=fname
        )
        assert bound.bars == 1
        assert bound.max_encoded_price <= 4294967295
        assert bound.vol_headroom > 1.0


def test_verify_daily_tail_price_within_tolerance(tmp_path) -> None:
    """白盒：_verify_daily_tail 回读末尾 N 条，价格偏差 < 0.001（NF-27）。"""
    from easy_tdx.offline.daily_bar import read_daily_bars

    for fname, exchange, code, _sec_type in _CASES:
        writer = VipdocWriter(tmp_path)
        target = tmp_path / fname
        bars = [_make_bar(close=10.2, day=1), _make_bar(close=10.8, day=2)]
        writer.write_daily(bars, exchange, code, path=target)

        coeff = CoefficientResolver().resolve(fname)
        verified, max_diff = writer._verify_daily_tail(  # noqa: SLF001 - 白盒单测
            target, bars, coeff
        )
        assert verified == len(bars)
        assert max_diff < PRICE_TOL, f"{fname} 回读价格偏差超容差: {max_diff}"

        read = read_daily_bars(target)
        assert len(read) == len(bars)
        assert abs(read[-1].close - 10.8) < PRICE_TOL
