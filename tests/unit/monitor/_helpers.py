"""monitor 单测共享工具（tests/unit/conftest.py 已把项目根加入 sys.path）。"""

from __future__ import annotations

import datetime as dt
from typing import Any

from Kuantix.core.contracts import Quote


def make_quote(
    code: str = "600519",
    market: str = "CN",
    last: float = 1610.0,
    prev_close: float = 1500.0,
    change_pct: float | None = None,
    ts: dt.datetime | None = None,
) -> Quote:
    """构造一条测试报价（change_pct 缺省按 last/prev_close 反推小数比例）。"""
    ratio = change_pct if change_pct is not None else (last / prev_close) - 1.0
    return Quote(
        code=code,
        market=market,
        last=last,
        prev_close=prev_close,
        change_pct=ratio,
        vol=100.0,
        amount=last * 1000.0,
        ts=ts if ts is not None else dt.datetime(2026, 8, 1, 10, 30, 0),
    )


def alert_dict(alert: Any) -> dict[str, Any]:
    """把 Alert 转成 dict（兼容核心 DTO 的 to_dict）。"""
    return alert.to_dict()
