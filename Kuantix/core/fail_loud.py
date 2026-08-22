"""fail-loud 工具集（NF-26，全项目第一原则）。

设计原则
--------
一切不确定 —— UNKNOWN 证券类型、市场未实现、系数缺失、因子列缺失、
插件加载失败、配置缺省、数据异常 —— 一律**显式报错 + 跳过 + 记隔离区**，
绝不静默兜底。

因此全库禁止两种写法：

1. ``dict.get(key, 默认值)`` 承载业务语义
   （典型反例：上游 ``daily_bar.py:89`` 的
   ``_SECURITY_COEFFICIENTS.get(sec_type, (0.01, 0.01))`` ——
   UNKNOWN 会被静默按 A 股系数解码，价格错 10 倍且不报错）。
   替代写法：:func:`require_key`。

2. ``try/except: pass`` 吞异常。
   替代写法：显式捕获 → 记录隔离区 → 继续；或直接向上抛。

本模块不依赖 Kuantix 任何其他模块，可被全项目安全 import（无循环依赖）。
"""

from __future__ import annotations

import math
from collections.abc import Container, Iterable, Mapping
from typing import Any, NoReturn, TypeVar

__all__ = [
    "KuantixError",
    "FailLoudError",
    "UnknownValueError",
    "MissingKeyError",
    "MissingConfigError",
    "DataIntegrityError",
    "NotSupportedError",
    "UpstreamContractError",
    "require_known",
    "reject_unknown",
    "require_key",
    "require_attr",
    "require_finite",
    "require_non_empty",
    "require_in_range",
]

T = TypeVar("T")


# ---------------------------------------------------------------------------
# 异常体系
# ---------------------------------------------------------------------------


class KuantixError(Exception):
    """Kuantix 所有自定义异常的根。"""


class FailLoudError(KuantixError):
    """fail-loud 家族根异常：表示"本可以静默兜底，但我们选择报错"。"""


class UnknownValueError(FailLoudError):
    """取值不在已知集合内（如证券类型 UNKNOWN、市场代码未注册）。"""


class MissingKeyError(FailLoudError):
    """映射中缺少必需的键（替代 ``dict.get(k, 默认)``）。"""


class MissingConfigError(FailLoudError):
    """配置项缺失或为空（NF-16）。"""


class DataIntegrityError(FailLoudError):
    """数据完整性校验失败（NaN/越界/回读不一致，NF-27）。"""


class NotSupportedError(FailLoudError, NotImplementedError):
    """能力尚未实现且**拒绝静默降级**（如 HK/US MarketProfile，NF-7）。

    同时继承 :class:`NotImplementedError`，因此调用方既可以按
    ``except NotImplementedError`` 的通用契约捕获（设计文档 U3 的要求），
    也可以按 ``except FailLoudError`` 统一归口到隔离区。
    """


class UpstreamContractError(FailLoudError):
    """上游 easy-tdx 的结构与 Kuantix 的假设不符（版本漂移早发现）。"""


# ---------------------------------------------------------------------------
# 核心工具
# ---------------------------------------------------------------------------


def _fmt_allowed(allowed: Iterable[Any] | None, limit: int = 20) -> str:
    """把允许集合格式化成可读字符串（超长截断）。"""
    if allowed is None:
        return ""
    try:
        items = sorted(str(a) for a in allowed)
    except TypeError:
        items = [str(a) for a in allowed]
    if len(items) > limit:
        shown = ", ".join(items[:limit])
        return f"；允许取值（前 {limit} 个，共 {len(items)}）: [{shown}, ...]"
    return f"；允许取值: [{', '.join(items)}]"


def require_known(
    value: T,
    context: str,
    *,
    allowed: Container[Any] | None = None,
    unknown_sentinels: Iterable[Any] = ("UNKNOWN", "unknown", "", None),
) -> T:
    """断言 ``value`` 是"已知"的，否则显式抛 :class:`UnknownValueError`。

    这是 fail-loud 的主入口，用于替代 ``dict.get(k, 默认值)`` 与
    ``value or 默认值`` 之类的静默兜底。

    Args:
        value: 待校验的取值。
        context: 出错时写入异常消息的业务上下文，必须能定位到具体标的/文件/字段。
        allowed: 可选的白名单容器（支持 ``in`` 运算）。给出时 ``value`` 必须命中。
        unknown_sentinels: 视为"未知"的哨兵值集合，默认覆盖 ``UNKNOWN``/空串/None。

    Returns:
        原样返回 ``value``（便于链式使用）。

    Raises:
        UnknownValueError: ``value`` 命中哨兵值，或不在 ``allowed`` 中。

    Examples:
        >>> require_known("SH_A_STOCK", "系数判定", allowed={"SH_A_STOCK"})
        'SH_A_STOCK'
        >>> require_known("UNKNOWN", "系数判定")
        Traceback (most recent call last):
        ...
        Kuantix.core.fail_loud.UnknownValueError: ...
    """
    sentinels = list(unknown_sentinels)
    for sentinel in sentinels:
        if value is sentinel or value == sentinel:
            raise UnknownValueError(
                f"[fail-loud/NF-26] {context}：取值为未知哨兵 {value!r}，"
                f"拒绝静默兜底，请显式处理（跳过 + 记隔离区）"
            )
    if allowed is not None and value not in allowed:
        raise UnknownValueError(
            f"[fail-loud/NF-26] {context}：取值 {value!r} 不在已知集合内"
            f"{_fmt_allowed(allowed if isinstance(allowed, Iterable) else None)}"
        )
    return value


def reject_unknown(value: Any, context: str, *, reason: str = "") -> NoReturn:
    """无条件拒绝一个未知/不可信取值，抛 :class:`UnknownValueError`。

    与 :func:`require_known` 的区别：调用方已经自行判定了"这就是未知"，
    此函数只负责统一异常类型与消息格式，保证隔离区记录口径一致。

    Args:
        value: 被拒绝的取值。
        context: 业务上下文（标的代码 / 文件名 / 字段名）。
        reason: 补充原因说明。

    Raises:
        UnknownValueError: 总是抛出。
    """
    suffix = f"；原因: {reason}" if reason else ""
    raise UnknownValueError(
        f"[fail-loud/NF-26] {context}：拒绝未知取值 {value!r}，"
        f"不允许按默认值继续{suffix}"
    )


def require_key(mapping: Mapping[Any, T], key: Any, context: str) -> T:
    """从映射中取值，键缺失时显式报错（**替代 ``dict.get(k, 默认)``**）。

    Args:
        mapping: 源映射。
        key: 键。
        context: 业务上下文。

    Returns:
        ``mapping[key]``。

    Raises:
        MissingKeyError: 键不存在。
    """
    if key not in mapping:
        raise MissingKeyError(
            f"[fail-loud/NF-26] {context}：缺少必需键 {key!r}"
            f"{_fmt_allowed(mapping.keys())}"
        )
    return mapping[key]


def require_attr(obj: Any, name: str, context: str) -> Any:
    """取对象属性，缺失时显式报错（用于校验上游对象契约）。

    Args:
        obj: 目标对象。
        name: 属性名。
        context: 业务上下文。

    Returns:
        属性值。

    Raises:
        UpstreamContractError: 属性不存在。
    """
    if not hasattr(obj, name):
        raise UpstreamContractError(
            f"[fail-loud/NF-26] {context}：对象 {type(obj).__name__} 缺少属性 {name!r}，"
            f"上游契约可能已变更"
        )
    return getattr(obj, name)


def require_finite(value: float, context: str) -> float:
    """断言浮点数有限（非 NaN / ±Inf），否则报错（NF-12/NF-27）。

    Args:
        value: 待校验数值。
        context: 业务上下文。

    Returns:
        转为 ``float`` 的原值。

    Raises:
        DataIntegrityError: 值非数值类型或非有限。
    """
    try:
        num = float(value)
    except (TypeError, ValueError) as exc:
        raise DataIntegrityError(
            f"[fail-loud/NF-26] {context}：取值 {value!r} 无法转为浮点数"
        ) from exc
    if math.isnan(num) or math.isinf(num):
        raise DataIntegrityError(
            f"[fail-loud/NF-26] {context}：取值为非有限数 {num!r}（NaN/Inf 禁止入库）"
        )
    return num


def require_non_empty(seq: Any, context: str) -> Any:
    """断言序列/容器非空。

    Args:
        seq: 待校验容器。
        context: 业务上下文。

    Returns:
        原容器。

    Raises:
        MissingConfigError: 容器为空或 None。
    """
    if seq is None or len(seq) == 0:
        raise MissingConfigError(f"[fail-loud/NF-26] {context}：内容为空，拒绝以空值继续")
    return seq


def require_in_range(
    value: float,
    context: str,
    *,
    minimum: float,
    maximum: float,
) -> float:
    """断言数值落在闭区间 ``[minimum, maximum]`` 内（如 uint32 上界，RD-9）。

    Args:
        value: 待校验数值。
        context: 业务上下文。
        minimum: 下界（含）。
        maximum: 上界（含）。

    Returns:
        原值。

    Raises:
        DataIntegrityError: 越界。禁止截断 / 取模 / 静默溢出。
    """
    num = require_finite(value, context)
    if num < minimum or num > maximum:
        raise DataIntegrityError(
            f"[fail-loud/NF-26] {context}：取值 {num!r} 越界 "
            f"[{minimum}, {maximum}]，拒绝截断/取模/静默溢出"
        )
    return num
