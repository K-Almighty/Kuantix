"""适配层 change_pct 口径回归测试（team-lead 裁决 1）。

裁决：``QuotationFetcher.fetch_quotes`` 的 ``Quote.change_pct`` 必须为
**小数比例**（0.05 = 5%，契约 §1.4/§3.5），适配层根因修复后全链路只有一种口径。

本测试锁定适配层根源：即使未来有人在 feed 边界做换算/透传失误，
这条测试也能第一时间暴露口径漂移（百分数回归）。
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from Kuantix.adapters.quotation import QuotationFetcher


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "code": "600519",
                "price": 1545.6,
                "last_close": 1680.0,
                "vol": 10000.0,
                "amount": 15456000.0,
            }
        ]
    )


def test_quotation_fetcher_change_pct_is_ratio():
    """fetch_quotes 的 change_pct 是小数比例（-0.08），不是百分数（-8.0）。"""
    fetcher = object.__new__(QuotationFetcher)  # 仅调用静态/内部解析，不触网
    now = dt.datetime(2026, 8, 1, 10, 30, 0)
    quotes = QuotationFetcher._frame_to_quotes(
        fetcher,  # type: ignore[arg-type]
        _frame(),
        now=now,
        vol_divisor=100.0,
    )
    assert len(quotes) == 1
    quote = quotes[0]
    # 契约 §1.4/§3.5：小数比例，0.05 = 5%；-0.08 = -8%
    assert quote.change_pct == pytest.approx(-0.08)
    assert quote.change_pct == pytest.approx((1545.6 / 1680.0) - 1.0)
    assert quote.code == "600519"
    assert quote.last == 1545.6
    assert quote.prev_close == 1680.0
