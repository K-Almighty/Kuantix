"""vipdoc 价格/量系数解析（NF-25 红线，陷阱 T1/T2 的落地点）。

背景：上游的静默兜底陷阱
------------------------
``easy_tdx/offline/daily_bar.py`` 的读侧逻辑是：

.. code-block:: python

    # daily_bar.py:88-89
    sec_type = _detect_security_type(filepath.name)
    price_coeff, vol_coeff = _SECURITY_COEFFICIENTS.get(sec_type, (0.01, 0.01))
    #                                                   ^^^^^^^^^^^^^^^^^^^^^^
    # UNKNOWN 的兜底默认值恰好等于 A 股系数 → 静默按 A 股解码

``_detect_security_type`` 的 docstring 声称"无法识别返回 UNKNOWN 而非默认
深市 A 股，避免误判"，但紧接着的 ``.get(sec_type, (0.01, 0.01))`` 又把
UNKNOWN 兜回了 A 股系数。后果：

- 基金/ETF（正确系数 0.001）被按 0.01 解码 → **价格错 10 倍**；
- 指数（正确量系数 1.0）被按 0.01 解码 → **成交量错 100 倍**；
- 北交所 ``bj`` 前缀同时绕开 ``sh``/``sz`` 双分支 → 必然走进这条路；
- 全程**不报任何错**。

本模块的职责
------------
1. **只 import、绝不复制**上游系数表（NF-25）。Kuantix 代码库中不得存在
   ``_SECURITY_COEFFICIENTS`` 的任何副本——有专门的静态测试守这条线。
2. 自己做 ``in`` 判定，命中不了就 :func:`~Kuantix.core.fail_loud.reject_unknown`，
   **绝不使用 ``.get(key, 默认值)`` 分支**（NF-26）。
3. 校验上游表结构，版本漂移时立刻报错而不是错价（NF-1）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

import easy_tdx

# --- 唯一允许的上游私有符号引用（NF-1 的显式例外，NF-25 强制要求）-----------
# 复制一份系数表到 Kuantix 代码库是被明令禁止的：那样上游修表后我们会
# 静默错价。这里刻意 import 私有名，让版本漂移在 import 期就炸出来。
from easy_tdx.offline.daily_bar import (  # noqa: PLC2701 - NF-25 强制 import 引用
    _SECURITY_COEFFICIENTS,
    _detect_security_type,
)

from Kuantix import UPSTREAM_EASY_TDX_VERSION
from Kuantix.core.fail_loud import (
    UpstreamContractError,
    reject_unknown,
    require_finite,
)

__all__ = [
    "UNKNOWN_SECURITY_TYPE",
    "Coefficients",
    "CoefficientResolver",
    "upstream_coefficient_table",
    "known_security_types",
    "detect_security_type",
    "resolve_coefficients",
    "assert_upstream_version",
]

#: 上游用于表示"无法识别"的哨兵值
UNKNOWN_SECURITY_TYPE = "UNKNOWN"


def assert_upstream_version(strict: bool = False) -> str:
    """校验实际安装的 easy-tdx 版本与锁定版本一致。

    Args:
        strict: ``True`` 时版本不一致直接抛错；``False`` 时仅在**主次版本**
            不一致时抛错（补丁号差异放行）。

    Returns:
        实际安装的版本号。

    Raises:
        UpstreamContractError: 版本不匹配。系数表/文件格式属于上游私有实现，
            版本漂移可能悄悄改变编解码口径（NF-1/NF-25）。
    """
    actual = str(getattr(easy_tdx, "__version__", "")).strip()
    if not actual:
        raise UpstreamContractError(
            "[fail-loud/NF-1] 无法读取 easy_tdx.__version__，上游包可能损坏"
        )
    if actual == UPSTREAM_EASY_TDX_VERSION:
        return actual
    if strict:
        raise UpstreamContractError(
            f"[fail-loud/NF-1] easy-tdx 版本不匹配：期望 {UPSTREAM_EASY_TDX_VERSION}，"
            f"实际 {actual}"
        )
    expected_mm = UPSTREAM_EASY_TDX_VERSION.split(".")[:2]
    actual_mm = actual.split(".")[:2]
    if expected_mm != actual_mm:
        raise UpstreamContractError(
            f"[fail-loud/NF-1] easy-tdx 主次版本不匹配：期望 "
            f"{'.'.join(expected_mm)}.x，实际 {actual}。"
            f"vipdoc 编解码口径可能已变更，拒绝继续以免静默错价"
        )
    return actual


def _validate_upstream_table(table: object) -> Mapping[str, tuple[float, float]]:
    """校验上游系数表的结构符合预期（import 期执行一次）。

    Args:
        table: 上游 ``_SECURITY_COEFFICIENTS`` 对象。

    Returns:
        校验通过的映射。

    Raises:
        UpstreamContractError: 结构与预期不符。
    """
    if not isinstance(table, dict) or not table:
        raise UpstreamContractError(
            "[fail-loud/NF-25] 上游 _SECURITY_COEFFICIENTS 不是非空 dict，"
            f"实际 {type(table).__name__}"
        )
    for key, value in table.items():
        if not isinstance(key, str):
            raise UpstreamContractError(
                f"[fail-loud/NF-25] 上游系数表的键必须是 str，实际 {key!r}"
            )
        if not (isinstance(value, tuple) and len(value) == 2):
            raise UpstreamContractError(
                f"[fail-loud/NF-25] 上游系数表 {key!r} 的取值必须是 (price, vol) 二元组，"
                f"实际 {value!r}"
            )
        price_coeff, vol_coeff = value
        for name, coeff in (("price_coeff", price_coeff), ("vol_coeff", vol_coeff)):
            num = require_finite(coeff, f"上游系数表 {key}.{name}")
            if num <= 0:
                raise UpstreamContractError(
                    f"[fail-loud/NF-25] 上游系数表 {key}.{name} 必须为正数，实际 {num!r}"
                )
    if UNKNOWN_SECURITY_TYPE in table:
        raise UpstreamContractError(
            "[fail-loud/NF-25] 上游系数表出现了 UNKNOWN 键，"
            "Kuantix 的 UNKNOWN 拒绝逻辑失效，需重新评估"
        )
    return table


# import 期即校验；上游一旦改结构，这里立刻炸，而不是等到写盘错价。
_VALIDATED_TABLE = _validate_upstream_table(_SECURITY_COEFFICIENTS)

#: 上游系数表的只读视图（禁止任何写操作）
_READONLY_TABLE: Mapping[str, tuple[float, float]] = MappingProxyType(_VALIDATED_TABLE)


def upstream_coefficient_table() -> Mapping[str, tuple[float, float]]:
    """返回上游系数表的**只读视图**。

    这是全项目获取系数表的唯一入口。返回的是 :class:`MappingProxyType`，
    对同一份上游 dict 的只读投影 —— 既保证"不复制"（NF-25），
    又防止任何代码误改上游状态（NF-1）。

    Returns:
        ``{security_type: (price_coeff, vol_coeff)}`` 只读映射。
    """
    return _READONLY_TABLE


def known_security_types() -> frozenset[str]:
    """返回上游已知的证券类型集合（不含 ``UNKNOWN``）。"""
    return frozenset(_READONLY_TABLE)


def detect_security_type(filename: str | Path) -> str:
    """按 vipdoc 文件名判定证券类型（**直接委托上游判定函数**）。

    刻意不在 Kuantix 侧重写判定规则：写侧必须与上游读侧
    ``read_daily_bars`` 使用完全相同的判定逻辑，否则写读系数不一致
    就会错价（RD-1）。

    Args:
        filename: vipdoc 文件名或完整路径，如 ``sh600000.day``。

    Returns:
        证券类型字符串；无法识别时返回 ``"UNKNOWN"``（由调用方拒绝）。
    """
    return str(_detect_security_type(Path(filename).name))


@dataclass(frozen=True)
class Coefficients:
    """一组已确认可用的价格/量系数。

    只能由 :func:`resolve_coefficients` 构造 —— 能拿到本对象，
    就意味着证券类型已通过"在上游表内"的校验。

    Attributes:
        security_type: 上游口径的证券类型，如 ``SH_A_STOCK``。
        price_coeff: 价格系数（``stored_int = round(price / price_coeff)``）。
        vol_coeff: 量系数（``stored_int = round(vol_手 / vol_coeff)``）。
        source_filename: 判定所依据的文件名（便于隔离区溯源）。
    """

    security_type: str
    price_coeff: float
    vol_coeff: float
    source_filename: str

    def as_tuple(self) -> tuple[float, float]:
        """返回 ``(price_coeff, vol_coeff)``，用于直接喂给上游写入函数。"""
        return (self.price_coeff, self.vol_coeff)

    def to_dict(self) -> dict[str, object]:
        """转为 JSON 安全字典。"""
        return {
            "security_type": self.security_type,
            "price_coeff": self.price_coeff,
            "vol_coeff": self.vol_coeff,
            "source_filename": self.source_filename,
        }


def resolve_coefficients(filename: str | Path) -> Coefficients:
    """按 vipdoc 文件名解析系数，**未知类型显式拒绝**（NF-25/NF-26）。

    与上游 ``daily_bar.py:89`` 的关键差异：

    .. code-block:: python

        # 上游（危险）：UNKNOWN 静默兜底为 A 股系数
        price, vol = _SECURITY_COEFFICIENTS.get(sec_type, (0.01, 0.01))

        # Kuantix（本函数）：先判定、后直接下标，不存在兜底分支
        if sec_type not in table:
            reject_unknown(...)
        price, vol = table[sec_type]

    Args:
        filename: vipdoc 文件名或路径，如 ``sh600000.day`` / ``bj430047.day``。

    Returns:
        :class:`Coefficients`。

    Raises:
        UnknownValueError: 证券类型为 ``UNKNOWN`` 或不在上游表内。
            调用方必须捕获并写入隔离区，**不得**改用默认系数继续。
    """
    name = Path(filename).name
    security_type = detect_security_type(name)
    table = upstream_coefficient_table()
    if security_type == UNKNOWN_SECURITY_TYPE or security_type not in table:
        reject_unknown(
            security_type,
            f"vipdoc 系数判定 file={name!r}",
            reason=(
                "上游 _detect_security_type 无法识别该代码段"
                f"（已知类型 {sorted(table)}）。"
                "按上游 daily_bar.py:89 的 .get(sec_type, (0.01, 0.01)) 兜底会被"
                "静默当作 A 股解码，导致基金价格错 10 倍、指数成交量错 100 倍，"
                "故 Kuantix 在此显式拒绝并要求入隔离区（NF-25/NF-26）"
            ),
        )
    # 直接下标：无默认值分支，键不存在会抛 KeyError（上面已保证存在）
    price_coeff, vol_coeff = table[security_type]
    return Coefficients(
        security_type=security_type,
        price_coeff=float(price_coeff),
        vol_coeff=float(vol_coeff),
        source_filename=name,
    )


class CoefficientResolver:
    """系数解析器（对应设计文档的 ``CoefficientResolver``）。

    无状态包装器，便于依赖注入与测试替身。

    Examples:
        >>> CoefficientResolver().detect_coeff("sh600000.day")
        (0.01, 0.01)
        >>> CoefficientResolver().resolve("sh510300.day").security_type
        'SH_FUND'
    """

    def resolve(self, filename: str | Path) -> Coefficients:
        """解析系数，未知类型抛 ``UnknownValueError``。

        Args:
            filename: vipdoc 文件名或路径。

        Returns:
            :class:`Coefficients`。

        Raises:
            UnknownValueError: 证券类型未知。
        """
        return resolve_coefficients(filename)

    def detect_coeff(self, filename: str | Path) -> tuple[float, float]:
        """返回 ``(price_coeff, vol_coeff)`` 二元组。

        Args:
            filename: vipdoc 文件名或路径。

        Returns:
            系数二元组。

        Raises:
            UnknownValueError: 证券类型未知。
        """
        return self.resolve(filename).as_tuple()

    def detect_type(self, filename: str | Path) -> str:
        """返回上游判定的证券类型（可能是 ``UNKNOWN``，由调用方判断）。"""
        return detect_security_type(filename)

    def is_known(self, filename: str | Path) -> bool:
        """判断文件名对应的证券类型是否可安全解码。"""
        return detect_security_type(filename) in upstream_coefficient_table()
