"""验收标准②（T02 / NF-26）：UNKNOWN 证券类型写入被显式拒绝。

独立验证手段：用**北交所 `bj` 前缀代码**（`bj430047` / `bj830799`）构造用例。
这是实测出来的真实绕过路径——`bj` 前缀同时绕过上游 `_detect_security_type`
的 sh/sz 双分支落到 `UNKNOWN`，而 `UNKNOWN` 的兜底值恰好是 A 股系数
``(0.01, 0.01)``，于是静默按 A 股解码。这是整个项目的**头号数据损坏风险**。

断言 ``resolve_coefficients`` 对 `bj` 前缀**抛 `UnknownValueError`**
（不返回 A 股系数）——即「不认识的主题立刻报错」，而非静默错价。
同时反向确认 ETF 走基金系数 ``(0.001, 1.0)``，证明已落地代码没有把 ETF 当 A 股。
"""
from __future__ import annotations

import pytest

from _acc_common import import_optional


def test_bj_prefix_rejected_as_unknown():
    C = import_optional("Kuantix.adapters.coefficients")
    from Kuantix.core.fail_loud import UnknownValueError

    for code in ("bj430047.day", "bj830799.day"):
        with pytest.raises(UnknownValueError):
            C.resolve_coefficients(code)


def test_known_a_share_and_etf_coeffs_correct():
    C = import_optional("Kuantix.adapters.coefficients")

    # 正常 A 股：A 股系数
    a = C.resolve_coefficients("sh600000.day")
    assert a.security_type == "SH_A_STOCK"
    assert (a.price_coeff, a.vol_coeff) == (0.01, 0.01)

    # ETF：必须走基金系数，绝不能静默按 A 股（这正是 bj 陷阱会犯的错）
    etf = C.resolve_coefficients("sh510300.day")
    assert etf.security_type == "SH_FUND"
    assert (etf.price_coeff, etf.vol_coeff) == (0.001, 1.0)
