"""验收标准⑤（T02 / NF-25 核心回归）：写→回读数值一致，覆盖 A股/ETF/指数/债券 ≥4 类。

独立验证手段：每类走完整闭环——
    构造 bars → VipdocWriter 写入 vipdoc → easy_tdx read_daily_bars 回读
    → 逐字段比对（价格容差 < 0.001）

四类系数不同，必须逐类验证（上游 _SECURITY_COEFFICIENTS 实测值）：
    A股   sh600000  (0.01, 0.01)
    基金  sh510300  (0.001, 1.0)
    指数  sh000001  (0.01, 1.0)
    债券  sh010107  (0.001, 1.0)

**特别验证 ETF（负面控制）**：先用**错系数**故意写一遍，确认回读价格会偏差
~×0.10（证明本验收用例能测出错价）；再用正确系数跑正式断言。若用例连错价都测
不出来，说明用例本身有问题——这正是 lead 强调的「先故意错、确认能抓、再正式断言」。

API 口径（按工程师真实实现校准，2026-08-01）：
- Kuantix 内部 K 线为 ``Kuantix.core.contracts.Bar``（``date: dt.date``，``vol`` 单位=手）；
- ``VipdocWriter(root).write_daily(bars, exchange, code, *, path=...)``；
- 上游读侧 ``easy_tdx.offline.daily_bar.read_daily_bars``。
"""
from __future__ import annotations

import datetime as dt

import pytest

from _acc_common import import_optional

PRICE_TOL = 1e-3

# (文件名, exchange, code, 期望系数)
_CASES = [
    ("sh600000.day", "sh", "600000", (0.01, 0.01)),   # A 股
    ("sh510300.day", "sh", "510300", (0.001, 1.0)),   # 基金/ETF
    ("sh000001.day", "sh", "000001", (0.01, 1.0)),    # 指数
    ("sh010107.day", "sh", "010107", (0.001, 1.0)),   # 债券
]


def _make_bar(close: float = 10.2) -> "Bar":
    from Kuantix.core.contracts import Bar

    return Bar(
        date=dt.date(2024, 1, 1),
        open=10.0, high=10.5, low=9.8, close=close,
        vol=1_000_000.0, amount=10_200_000.0,
    )


def test_write_readback_consistent_across_four_types(tmp_path):
    VW = import_optional("Kuantix.adapters.vipdoc_writer")
    from easy_tdx.offline.daily_bar import read_daily_bars

    writer = VW.VipdocWriter(tmp_path)

    for fname, exchange, code, (pc, vc) in _CASES:
        target = tmp_path / fname
        report = writer.write_daily([_make_bar()], exchange, code, path=target)
        assert report.security_type in {"SH_A_STOCK", "SH_FUND", "SH_INDEX", "SH_BOND"}
        assert (report.price_coeff, report.vol_coeff) == (pc, vc), (
            f"{fname} 写入系数不符：期望 {(pc, vc)}，实际 "
            f"{(report.price_coeff, report.vol_coeff)}"
        )

        read = read_daily_bars(target)
        assert len(read) == 1, f"{fname} 回读条数应为 1，实际 {len(read)}"
        assert abs(read[0].close - 10.2) < PRICE_TOL, (
            f"{fname} 回读价格偏差超容差：写 10.2 读 {read[0].close}"
        )
        assert abs(read[0].open - 10.0) < PRICE_TOL
        assert abs(read[0].high - 10.5) < PRICE_TOL
        assert abs(read[0].low - 9.8) < PRICE_TOL


def test_etf_wrong_coeff_detectable(tmp_path):
    """负面控制：故意用 A 股系数写 ETF，回读价格应偏差 ~×0.10，证明用例能抓错价。"""
    import_optional("Kuantix.adapters.vipdoc_writer")  # 整文件以 VipdocWriter 落地为门禁
    from easy_tdx.models.bar import SecurityBar
    from easy_tdx.offline.daily_bar import read_daily_bars
    from easy_tdx.offline.write_daily import sync_daily_bars_from_security_bars

    path = tmp_path / "sh510300.day"  # ETF 文件名
    bars = [
        SecurityBar(
            open=10.0, close=10.2, high=10.5, low=9.8,
            vol=1_000_000.0, amount=10_200_000.0,
            year=2024, month=1, day=1, hour=0, minute=0,
        )
    ]
    # 故意传 A 股系数 (0.01, 0.01) 给 ETF 文件 —— 复现 T1 陷阱
    sync_daily_bars_from_security_bars(path, bars, 0.01, 0.01)
    read = read_daily_bars(path)
    ratio = read[0].close / bars[0].close
    assert abs(ratio - 0.1) < 0.01, (
        f"ETF 用错系数应读出 ~×0.10 偏差，实际 ×{ratio:.3f}；"
        "若此断言失败说明用例无法检测错价"
    )
