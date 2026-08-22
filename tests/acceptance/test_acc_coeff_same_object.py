"""验收标准①（T02 / NF-25）：系数为 import 引用，代码库无副本。

独立验证手段（与红线 R1 的 AST 层互补）：在**运行时**断言
``Kuantix.adapters.coefficients`` 拿到的系数表与上游
``easy_tdx.offline.daily_bar._SECURITY_COEFFICIENTS`` **是同一批值对象**
（用 ``is`` 判 identity，不是内容相等）。

为什么用 `is` 而不是 `==`：NF-25 的雷是「复制一份系数表」——上游哪天补上
北交所/新 ETF 代码段，这份副本就变成错的且照样不报错。若工程师改成
「复制成新 dict / 重新构造元组」，值对象 identity 会断，本用例红灯。
`==` 看不出这种复制，必须用 `is`。
"""
from __future__ import annotations

from _acc_common import import_optional


def test_coeff_table_shares_upstream_value_objects():
    C = import_optional("Kuantix.adapters.coefficients")
    from easy_tdx.offline.daily_bar import _SECURITY_COEFFICIENTS

    table = C.upstream_coefficient_table()
    assert table is not None

    # 键集合一致
    assert set(table) == set(_SECURITY_COEFFICIENTS), "系数表键集合应与上游一致"

    # 关键：每个系数值元组必须与上游是**同一个对象**（identity），证明无副本
    mismatched = [
        k for k in _SECURITY_COEFFICIENTS
        if table[k] is not _SECURITY_COEFFICIENTS[k]
    ]
    assert not mismatched, (
        f"系数值被复制/重新构造（非同一对象），违反 NF-25：{mismatched}。"
        "上游改表后这份副本会静默错价"
    )


def test_coeff_table_has_all_upstream_types():
    C = import_optional("Kuantix.adapters.coefficients")
    from easy_tdx.offline.daily_bar import _SECURITY_COEFFICIENTS

    table = C.upstream_coefficient_table()
    # 上游共 10 种证券类型 → 4 种系数值对；覆盖不全等于漏类型
    assert len(table) >= 10, f"上游系数表应有 ≥10 种证券类型，实际 {len(table)}"
    assert len(table) == len(_SECURITY_COEFFICIENTS)
