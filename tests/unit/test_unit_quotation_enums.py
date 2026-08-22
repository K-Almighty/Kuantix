"""上游枚举契约断言单测（import 期 fail-loud，收口轮）。

背景：主/次版本校验（1.20.3）拦不住同版本内枚举改名（历史坑 Period.DAY →
DAILY）。:func:`Kuantix.adapters.quotation.assert_upstream_enums` 在 import 期
断言本模块实际使用的全部上游枚举成员；本文件验证该断言本身：
- 真实上游 1.20.3 齐备 → 不抛；
- 模拟缺失 → 一次性抛 DataIntegrityError（含缺失清单）。
"""
from __future__ import annotations

import pytest

from Kuantix.adapters.quotation import (
    _UPSTREAM_ENUM_MEMBERS,
    assert_upstream_enums,
)
from Kuantix.core.fail_loud import DataIntegrityError


def test_upstream_enum_members_full() -> None:
    """本模块实际使用的全部上游枚举成员应齐备（1.20.3）。"""
    assert_upstream_enums()  # 不抛错即通过


def test_upstream_enum_members_coverage_list() -> None:
    """清单覆盖六个关键成员（Period/Adjust/ExMarket/Market）。"""
    names = {f"{cls.__name__}.{member}" for cls, member in _UPSTREAM_ENUM_MEMBERS}
    assert names == {
        "Period.DAILY",
        "Adjust.NONE",
        "ExMarket.HK_MAIN_BOARD",
        "ExMarket.US_STOCK",
        "Market.SH",
        "Market.SZ",
    }


def test_assert_upstream_enums_missing_raises_once(monkeypatch) -> None:
    """模拟缺失 → 一次性抛 DataIntegrityError（含缺失清单，不逐个炸）。"""
    fake_enum = type("FakeEnum", (), {})
    monkeypatch.setattr(
        "Kuantix.adapters.quotation._UPSTREAM_ENUM_MEMBERS",
        ((fake_enum, "SH"), (fake_enum, "SZ")),
    )
    with pytest.raises(DataIntegrityError) as excinfo:
        assert_upstream_enums()
    message = str(excinfo.value)
    assert "SH" in message
    assert "SZ" in message
