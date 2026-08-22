"""T02 单测①（NF-25 白盒）：系数为上游 import 引用，无本地副本。

验收台 ``test_acc_coeff_same_object.py`` 从「值对象 identity」角度验证；
本单测从**模块内部实现**角度验证（白盒）：

- ``adapters/coefficients.py`` 的 ``_VALIDATED_TABLE`` 必须与上游
  ``easy_tdx.offline.daily_bar._SECURITY_COEFFICIENTS`` **是同一个对象**；
- ``upstream_coefficient_table()`` 返回的只读视图包裹的仍是同一个 dict；
- 修改上游 dict 会立刻反映到 Kuantix 视角（证明不是复制）。
"""
from __future__ import annotations

import pytest

from easy_tdx.offline.daily_bar import _SECURITY_COEFFICIENTS

from Kuantix.adapters import coefficients as C


def test_validated_table_is_upstream_dict_identity() -> None:
    """白盒：_VALIDATED_TABLE 与上游 _SECURITY_COEFFICIENTS 同一对象。"""
    assert C._VALIDATED_TABLE is _SECURITY_COEFFICIENTS, (
        "NF-25 要求从上游 import 引用（同一对象），而不是复制成新 dict。"
        "若这里红灯，说明有人把系数表复制进了 Kuantix 代码库"
    )


def test_readonly_view_is_mappingproxy_over_same_dict() -> None:
    """白盒：只读视图是 MappingProxyType，且其底层与 _VALIDATED_TABLE 同一对象。

    注意：MappingProxyType 没有公开的 ``_mapping`` 属性，因此这里用
    ``type(view) is MappingProxyType`` + ``view is C._READONLY_TABLE`` 断言，
    并结合 ``test_validated_table_is_upstream_dict_identity`` 的 identity 判断，
    共同证明「读侧拿到的就是上游那一份」。
    """
    from types import MappingProxyType

    view = C.upstream_coefficient_table()
    assert type(view) is MappingProxyType
    assert view is C._READONLY_TABLE
    # 只读视图的每一项与上游表对应项是同一值对象（NF-25）
    for key in _SECURITY_COEFFICIENTS:
        assert view[key] is _SECURITY_COEFFICIENTS[key]


def test_upstream_mutation_propagates() -> None:
    """白盒：往上游 dict 里加一个键，Kuantix 视角立刻可见（无副本的铁证）。

    注意：上游表是运行时可变 dict（NF-25 只要求我们 import 引用，
    不要求我们冻结上游）。测试结束后必须把键删掉，保持上游状态不变。
    """
    probe_key = "SH_PROBE_UNIT"
    assert probe_key not in _SECURITY_COEFFICIENTS
    _SECURITY_COEFFICIENTS[probe_key] = (0.5, 0.5)
    try:
        assert probe_key in C.upstream_coefficient_table()
        assert C.upstream_coefficient_table()[probe_key] == (0.5, 0.5)
    finally:
        del _SECURITY_COEFFICIENTS[probe_key]


def test_resolved_coeff_reads_same_upstream_entries() -> None:
    """白盒：解析出的系数值就是上游表对应键的值（不是本地另一份）。"""
    for filename, sec_type in [
        ("sh600000.day", "SH_A_STOCK"),
        ("sh510300.day", "SH_FUND"),
        ("sh000001.day", "SH_INDEX"),
    ]:
        resolved = C.resolve_coefficients(filename)
        assert resolved.security_type == sec_type
        assert resolved.as_tuple() == tuple(_SECURITY_COEFFICIENTS[sec_type])


def test_known_security_types_from_upstream() -> None:
    """白盒：known_security_types() 与上游表键集合一致。"""
    assert C.known_security_types() == frozenset(_SECURITY_COEFFICIENTS)
