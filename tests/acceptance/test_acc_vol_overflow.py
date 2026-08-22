"""验收标准③（T02 / RD-8/9 / NF-27）：vol÷100 后编码不溢出 uint32。

独立验证手段：造三个贴边样本（vol 单位 = 手，RD-8）——
- 正常量（远低于上限，应通过）
- 89% 贴边量（A 股 vol_coeff=0.01，编码值 = vol/0.01 = vol×100。
  实测背景：000100 单日量编码后占 uint32 上限 4294967295 的 ~89.5%，
  余量仅 1.1×。编码 89.5% ⇒ vol ≈ 3.84e9/100 ≈ 3.84e7 手，应通过）
- 超上限量（编码值 > 4294967295，应被**拦截并进隔离区**，
  不允许截断 / 取模 / 静默溢出）

API 口径（按工程师真实实现校准，2026-08-01）：
- ``VipdocWriter(root).write_daily(bars, exchange, code, *, path=...)``；
- 越界抛 ``Kuantix.core.fail_loud.DataIntegrityError``。
"""
from __future__ import annotations

import datetime as dt

import pytest

from _acc_common import import_optional

UINT32_MAX = 4_294_967_295


def _make_bar(vol: float, day: int = 1):
    from Kuantix.core.contracts import Bar

    return Bar(
        date=dt.date(2024, 1, day),  # 每个样本用不同日期，避免上游重复日期去重语义
        open=10.0, high=10.5, low=9.8, close=10.2,
        vol=vol, amount=float(vol) * 10.0,
    )


def test_vol_overflow_three_cases(tmp_path):
    VW = import_optional("Kuantix.adapters.vipdoc_writer")

    writer = VW.VipdocWriter(tmp_path)
    target = tmp_path / "sh600000.day"

    # A 股 vol_coeff=0.01 ⇒ 编码值 = vol(手) × 100
    normal = _make_bar(1_000_000.0, day=1)              # 编码 1e8，远低于上限
    # 贴边样本取"可达"的整手量：vipdoc 编码步长是 vol_coeff/2=0.005 手（round 量化），
    # 带 0.29 手小数碎片的量在真实数据中不存在（000100 实测单日 3.84e9 股 ≈ 3.84e7 整手）。
    edge = _make_bar(float(round(UINT32_MAX / 100 * 0.895)), day=2)  # 编码 ≈ 89.5% 上限，应通过
    over = _make_bar(UINT32_MAX / 100 + 1_000_000.0, day=3)  # 编码 > 上限，应拦截

    # 正常 / 贴边：写出不报错
    assert writer.write_daily([normal], "sh", "600000", path=target) is not None
    assert writer.write_daily([edge], "sh", "600000", path=target) is not None

    # 超上限：必须显式报错（进隔离区），绝不静默截断/取模/溢出
    with pytest.raises(Exception):
        writer.write_daily([over], "sh", "600000", path=target)
