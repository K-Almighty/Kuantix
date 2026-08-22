"""统一响应信封（NF-9）与数值安全序列化（NF-12）。

所有 CLI / REST 出口统一返回 ``{code, message, data, meta}``；
``meta`` 固定含 ``generated_at / data_date / market / elapsed_ms / version``。

数值安全（NF-12）：
- JSON 中禁止出现 ``NaN`` / ``Infinity`` / ``-Infinity``，一律序列化为 ``null``；
- 浮点数保留 6 位小数。

注意：这里的 ``NaN → null`` **只作用于输出序列化**，不是业务兜底。
业务链路上的 NaN 由 :func:`Kuantix.core.fail_loud.require_finite` 显式拦截。
"""

from __future__ import annotations

import datetime as dt
import json
import math
import time
from dataclasses import dataclass, field, is_dataclass
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

__all__ = [
    "FLOAT_PRECISION",
    "CODE_OK",
    "CODE_INVALID_ARGUMENT",
    "CODE_NOT_FOUND",
    "CODE_DATA_ERROR",
    "CODE_NOT_IMPLEMENTED",
    "CODE_INTERNAL_ERROR",
    "Meta",
    "Envelope",
    "sanitize",
    "Timer",
]

#: 浮点输出精度（NF-12）
FLOAT_PRECISION = 6

#: 业务状态码（0 = 成功，其余为分类错误码）
CODE_OK = 0
CODE_INVALID_ARGUMENT = 400
CODE_NOT_FOUND = 404
CODE_DATA_ERROR = 422
CODE_NOT_IMPLEMENTED = 501
CODE_INTERNAL_ERROR = 500


def _round_float(value: float) -> float | None:
    """把浮点数规整为 6 位小数；非有限数返回 ``None``（NF-12）。"""
    if math.isnan(value) or math.isinf(value):
        return None
    return round(value, FLOAT_PRECISION)


def sanitize(value: Any) -> Any:
    """递归清洗任意对象，产出 JSON 安全的纯 Python 结构（NF-12）。

    转换规则：

    ==============================  ==========================================
    输入                            输出
    ==============================  ==========================================
    ``float`` NaN / ±Inf            ``None``
    其他 ``float`` / ``Decimal``    ``round(x, 6)``
    ``datetime`` / ``date``         ISO-8601 字符串
    ``Enum``                        ``member.value``
    ``Path``                        ``str(path)``
    ``set`` / ``frozenset``         已排序的 ``list``
    ``dataclass``                   ``dict``（递归）
    带 ``to_dict()`` 的对象         ``obj.to_dict()``（递归）
    ``bytes``                       ``latin-1`` 十六进制字符串
    ==============================  ==========================================

    Args:
        value: 任意待清洗对象。

    Returns:
        JSON 可序列化的对象。
    """
    # bool 必须先于 int 判断（bool 是 int 的子类）
    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, float):
        return _round_float(value)
    if isinstance(value, Decimal):
        return _round_float(float(value))
    if isinstance(value, Enum):
        return sanitize(value.value)
    if isinstance(value, dt.datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, dict):
        return {str(k): sanitize(v) for k, v in value.items()}
    if isinstance(value, (set, frozenset)):
        return [sanitize(v) for v in sorted(value, key=str)]
    if isinstance(value, (list, tuple)):
        return [sanitize(v) for v in value]
    if is_dataclass(value) and not isinstance(value, type):
        # 不用 dataclasses.asdict：它会 deepcopy 且无法处理自定义 to_dict
        return {
            f.name: sanitize(getattr(value, f.name))
            for f in value.__dataclass_fields__.values()  # type: ignore[attr-defined]
            if not f.name.startswith("_")
        }
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return sanitize(to_dict())
    # pandas / numpy 标量的兜底：优先走 item()，失败则字符串化
    item = getattr(value, "item", None)
    if callable(item):
        return sanitize(item())
    return str(value)


@dataclass(frozen=True)
class Meta:
    """信封元数据（NF-9）。

    Attributes:
        generated_at: 响应生成时刻（本地时区，秒级 ISO-8601）。
        data_date: 数据基准日 ``YYYY-MM-DD``；无数据基准时为 ``None``。
        market: 市场代码（``CN`` / ``HK`` / ``US``），贯穿全链路（NF-6）。
        elapsed_ms: 服务端处理耗时（毫秒）。
        version: Kuantix 版本号。
    """

    market: str
    version: str
    generated_at: dt.datetime = field(default_factory=lambda: dt.datetime.now().astimezone())
    data_date: str | None = None
    elapsed_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        """转为 JSON 安全字典。"""
        return {
            "generated_at": self.generated_at.isoformat(timespec="seconds"),
            "data_date": self.data_date,
            "market": self.market,
            "elapsed_ms": int(self.elapsed_ms),
            "version": self.version,
        }


@dataclass(frozen=True)
class Envelope:
    """统一响应信封 ``{code, message, data, meta}``（NF-9）。

    Attributes:
        code: 业务状态码，``0`` 表示成功。
        message: 人类可读消息，成功时为 ``"ok"``。
        data: 载荷，可为任意可 :func:`sanitize` 的结构。
        meta: 元数据。
    """

    code: int
    message: str
    data: Any
    meta: Meta

    @classmethod
    def ok(
        cls,
        data: Any,
        *,
        market: str,
        version: str,
        elapsed_ms: int = 0,
        data_date: str | None = None,
        message: str = "ok",
    ) -> Envelope:
        """构造成功信封。

        Args:
            data: 载荷。
            market: 市场代码。
            version: Kuantix 版本。
            elapsed_ms: 处理耗时（毫秒）。
            data_date: 数据基准日。
            message: 提示消息。

        Returns:
            ``code=0`` 的 :class:`Envelope`。
        """
        return cls(
            code=CODE_OK,
            message=message,
            data=data,
            meta=Meta(
                market=market,
                version=version,
                elapsed_ms=elapsed_ms,
                data_date=data_date,
            ),
        )

    @classmethod
    def fail(
        cls,
        code: int,
        message: str,
        *,
        market: str,
        version: str,
        elapsed_ms: int = 0,
        data: Any = None,
        data_date: str | None = None,
    ) -> Envelope:
        """构造失败信封。

        Args:
            code: 非零业务状态码。
            message: 错误消息（应包含足以定位的上下文）。
            market: 市场代码。
            version: Kuantix 版本。
            elapsed_ms: 处理耗时（毫秒）。
            data: 可选的错误明细载荷（如隔离区条目）。
            data_date: 数据基准日。

        Returns:
            非零 ``code`` 的 :class:`Envelope`。

        Raises:
            ValueError: ``code`` 为 0（失败信封不允许用成功码）。
        """
        if code == CODE_OK:
            raise ValueError("Envelope.fail 不允许使用 code=0，请改用 Envelope.ok")
        return cls(
            code=code,
            message=message,
            data=data,
            meta=Meta(
                market=market,
                version=version,
                elapsed_ms=elapsed_ms,
                data_date=data_date,
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        """转为完全 JSON 安全的字典（已按 NF-12 清洗）。"""
        return {
            "code": int(self.code),
            "message": self.message,
            "data": sanitize(self.data),
            "meta": self.meta.to_dict(),
        }

    def to_json(self, *, indent: int | None = None) -> str:
        """序列化为 JSON 字符串。

        使用 ``allow_nan=False`` 做最后一道防线：若 :func:`sanitize` 漏掉了
        某个 NaN/Inf，这里会直接抛 ``ValueError`` 而不是输出非法 JSON（NF-12）。

        Args:
            indent: 缩进空格数，``None`` 表示紧凑输出。

        Returns:
            JSON 字符串。
        """
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            indent=indent,
            allow_nan=False,
        )

    @property
    def is_ok(self) -> bool:
        """是否为成功信封。"""
        return self.code == CODE_OK


class Timer:
    """上下文管理器：测量耗时并给出 ``elapsed_ms``（供 :class:`Meta` 使用）。

    Examples:
        >>> with Timer() as t:
        ...     _ = sum(range(10))
        >>> t.elapsed_ms >= 0
        True
    """

    def __init__(self) -> None:
        self._start: float = 0.0
        self._end: float | None = None

    def __enter__(self) -> Timer:
        self._start = time.perf_counter()
        self._end = None
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        self._end = time.perf_counter()
        return False  # 不吞异常（NF-26）

    @property
    def elapsed_ms(self) -> int:
        """已用毫秒数（未退出上下文时返回至今耗时）。"""
        end = self._end if self._end is not None else time.perf_counter()
        return int(round((end - self._start) * 1000))
