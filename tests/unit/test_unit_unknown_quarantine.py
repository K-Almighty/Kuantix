"""T02 单测②（NF-25/NF-26 白盒）：UNKNOWN 证券类型写入被显式拒绝并入隔离区。

验收台 ``test_acc_unknown_rejected.py`` 用北交所 ``bj`` 前缀做黑盒断言；
本单测从**模块内部**验证（白盒）：

- 枚举阶段 ``_absorb_page`` 就把 UNKNOWN 类型拒掉并产出隔离区条目；
- 写盘阶段 ``VipdocWriter.write_daily`` 对 ``bj`` 文件抛 ``UnknownValueError``；
- ``quarantine_entry_for`` 能按异常类型映射出 ``UNKNOWN_SECURITY_TYPE`` 原因。
"""
from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from Kuantix.adapters.coefficients import CoefficientResolver, detect_security_type
from Kuantix.adapters.universe import UniverseEnumerator
from Kuantix.adapters.vipdoc_writer import VipdocWriter
from Kuantix.core.contracts import Bar
from Kuantix.core.fail_loud import UnknownValueError


class _FakeTdxFactory:
    """白盒替身：只提供 new_tdx_client（枚举内部用），不触网。"""

    def __init__(self, frame: pd.DataFrame) -> None:
        self._frame = frame
        self.created_total = 0

    def new_tdx_client(self, host: str | None = None):
        self.created_total += 1
        return _FakeTdxClient(self._frame)


class _FakeTdxClient:
    def __init__(self, frame: pd.DataFrame) -> None:
        self._frame = frame
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def get_security_count(self, _market) -> int:
        return len(self._frame)

    def get_security_list(self, _market, _start) -> pd.DataFrame:
        return self._frame.copy()


def test_detect_bj_prefix_is_unknown() -> None:
    """白盒：bj 前缀被上游判定为 UNKNOWN（这正是被拒的根源）。"""
    assert detect_security_type("bj430047.day") == "UNKNOWN"
    assert detect_security_type("bj830799.day") == "UNKNOWN"


def test_resolve_coefficients_bj_raises() -> None:
    """白盒：resolve_coefficients 对 bj 直接抛 UnknownValueError。"""
    resolver = CoefficientResolver()
    with pytest.raises(UnknownValueError):
        resolver.resolve("bj430047.day")
    assert resolver.is_known("bj430047.day") is False


def test_absorb_page_rejects_unknown_to_quarantine() -> None:
    """白盒：枚举解析单页时，UNKNOWN（bj 前缀）被放入 rejected 隔离清单。"""
    frame = pd.DataFrame(
        {
            "code": ["430047"],
            "name": ["北交测试"],
        }
    )
    enum = UniverseEnumerator(_FakeTdxFactory(frame))
    securities: list = []
    rejected: list = []
    enum._absorb_page(  # noqa: SLF001 - 白盒单测
        frame,
        exchange="bj",
        allowed=frozenset({"SH_A_STOCK", "SZ_A_STOCK"}),
        securities=securities,
        rejected=rejected,
    )
    assert len(rejected) == 1
    entry = rejected[0]
    assert entry.code == "430047"
    assert entry.reason == "UNKNOWN_SECURITY_TYPE"
    assert "UNKNOWN" in entry.detail
    # bj 前缀不会进入可写股票池
    assert securities == []

    # 对照：sh 前缀的 A 股正常进入股票池，不产生隔离
    good_frame = pd.DataFrame(
        {"code": ["600000"], "name": ["浦发银行"]}
    )
    securities2: list = []
    rejected2: list = []
    enum._absorb_page(  # noqa: SLF001 - 白盒单测
        good_frame,
        exchange="sh",
        allowed=frozenset({"SH_A_STOCK", "SZ_A_STOCK"}),
        securities=securities2,
        rejected=rejected2,
    )
    assert len(securities2) == 1
    assert securities2[0].code == "600000"
    assert rejected2 == []


def test_write_daily_bj_rejected(tmp_path) -> None:
    """白盒：VipdocWriter 对 bj 文件写盘抛异常（显式拒绝，不静默按 A 股系数）。

    注意：``daily_path`` 在路径层就用 ``DataIntegrityError`` 拒绝 bj；
    若绕过路径层（自定义 path），系数解析仍会抛 ``UnknownValueError``。
    """
    writer = VipdocWriter(tmp_path)
    bars = [
        Bar(
            date=dt.date(2024, 1, 2),
            open=1.0,
            high=1.0,
            low=1.0,
            close=1.0,
            vol=100.0,
            amount=100.0,
        )
    ]
    # 路径层：DataIntegrityError（bj 不在 sh/sz）
    with pytest.raises(Exception):
        writer.write_daily(bars, "bj", "430047")

    # 系数解析层：绕过路径层后，UnknownValueError
    from Kuantix.adapters.coefficients import resolve_coefficients

    with pytest.raises(UnknownValueError):
        resolve_coefficients("bj430047.day")


def test_quarantine_entry_reason_mapping() -> None:
    """白盒：quarantine_entry_for 把 UnknownValueError 映射为 UNKNOWN_SECURITY_TYPE。"""
    writer = VipdocWriter(".")
    err = UnknownValueError("[fail-loud/NF-26] 测试未知类型")
    entry = writer.quarantine_entry_for(code="430047", market="CN", error=err)
    assert entry.reason == "UNKNOWN_SECURITY_TYPE"
    assert entry.code == "430047"
    assert entry.market == "CN"
    assert entry.attempts == 1
