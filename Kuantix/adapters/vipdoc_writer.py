"""vipdoc 落盘（RD-1 / RD-2 / RD-8 / RD-9 / NF-25 / NF-27 的集中落地点）。

这是全项目**数据正确性风险最高**的一个文件。四道闸门缺一不可：

闸门 1 — 系数按文件名判定，UNKNOWN 显式拒绝（RD-1 / NF-25，陷阱 T1）
--------------------------------------------------------------------
写侧必须与上游读侧 ``read_daily_bars`` 用**同一张系数表、同一套判定规则**，
否则写读口径不一致直接错价。系数从
:func:`Kuantix.adapters.coefficients.resolve_coefficients` 取——它 import 上游
``_SECURITY_COEFFICIENTS``（禁止复制），并在类型判定为 ``UNKNOWN`` 时
:func:`~Kuantix.core.fail_loud.reject_unknown`，而不是像上游
``daily_bar.py:89`` 那样 ``.get(sec_type, (0.01, 0.01))`` 静默兜回 A 股系数。

闸门 2 — vol 单位（RD-8，陷阱 T2）
----------------------------------
入参 :class:`~Kuantix.core.contracts.Bar` 的 ``vol`` 语义是**手**，
换算发生在 :mod:`Kuantix.adapters.quotation`。本模块**再校验一次**：
``手`` 经 ``/vol_coeff`` 编码后应落在 uint32 内；若调用方误传了「股」，
放大 100 倍后必然被闸门 3 拦下，而不是静默截断。

闸门 3 — uint32 上界（RD-9，陷阱 T3）
-------------------------------------
``.day`` 的 ``<IIIIIfII`` 里日期/OHLC/vol 全是 ``uint32``。S3 实测：
即便正确 ÷100，极端大盘股（如 000100）的编码值也能达到 uint32 上限的
**89.5%**，安全余量只有 **1.12 倍**。``struct.pack`` 对越界会抛
``struct.error``，但 Kuantix 不能等到写了一半才炸——本模块在**写入前**
逐条预检，越界即整只标的拒绝并入隔离区，**绝不截断 / 取模 / 静默溢出**。

闸门 4 — fsync + 写后回读（RD-2 / NF-27）
-----------------------------------------
- ``append_daily_bars`` 上游**已有** ``flush + fsync``（write_daily.py:164-165）；
- ``append_ex_daily_bars`` / ``append_5min_bars`` 上游**没有** fsync
  （write_ex_daily.py:78、write_min_bar.py:109），本模块**自补**；
- 三者上游都**没有 fsync 父目录**——新建文件的目录项可能丢，本模块统一自补；
- 写完回读末尾 N 条与源数据比对（价格容差 <0.001），不一致即抛
  :class:`~Kuantix.core.fail_loud.DataIntegrityError` 供调用方入隔离区。
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

log = logging.getLogger(__name__)

from easy_tdx.models.bar import SecurityBar
from easy_tdx.offline.daily_bar import read_daily_bars
from easy_tdx.offline.ex_daily_bar import ExDailyBar, read_ex_daily_bars
from easy_tdx.offline.min_bar import read_5min_bars
from easy_tdx.offline.write_daily import get_last_bar_date, sync_daily_bars_from_security_bars
from easy_tdx.offline.write_ex_daily import sync_ex_daily_bars
from easy_tdx.offline.write_min_bar import append_5min_bars

from Kuantix.adapters.coefficients import Coefficients, CoefficientResolver
from Kuantix.core.contracts import Bar, QuarantineEntry
from Kuantix.core.fail_loud import (
    DataIntegrityError,
    UnknownValueError,
    require_finite,
    require_in_range,
)

__all__ = [
    "UINT32_MAX",
    "UINT32_MIN",
    "FLOAT32_MAX",
    "BoundCheck",
    "WriteReport",
    "VipdocWriter",
    "SqliteBarWriter",
    "check_daily_bounds",
]

#: ``.day`` 记录中 ``<I`` 字段的取值上界。
UINT32_MAX: int = 4294967295
#: ``.day`` 记录中 ``<I`` 字段的取值下界。
UINT32_MIN: int = 0
#: ``.day`` 记录中 ``amount`` 为 float32，超过此值 struct 会溢出成 inf。
FLOAT32_MAX: float = 3.4028234663852886e38

#: 上游 ``.day`` 单条记录字节数（``struct.Struct("<IIIIIfII").size``）。


def check_daily_bounds(
    bars: Sequence[Bar],
    coeff: Coefficients,
    *,
    context: str,
) -> BoundCheck:
    """A 股日线的 uint32 上界预检（RD-9，**VipdocWriter 与 SqliteBarWriter 共用**）。

    编码口径与上游 ``encode_daily_bar`` **逐字对齐**::

        date_int = year*10000 + month*100 + day        # uint32
        price_int = round(price / price_coeff)          # uint32 × 4
        vol_int   = round(vol_手 / vol_coeff)           # uint32
        amount    = float32

    SQLite 后端没有文件损坏，但**保留值域校验**（设计 08：四道闸门语义迁移）：
    若数据按 .day 编码口径会越界，说明源头数据可疑（如把「股」当「手」），
    必须在写库前拦下，保证镜像/主存储可互转。

    Args:
        bars: 待写入 K 线（已按日期升序）。
        coeff: 系数。
        context: 错误上下文（文件名）。

    Returns:
        :class:`BoundCheck`。

    Raises:
        DataIntegrityError: 任一字段越界。**整只标的拒绝**，不截断不取模。
    """
    max_price_int = 0
    max_vol_int = 0
    worst_date: dt.date | None = None

    for bar in bars:
        ctx = f"{context}@{bar.date.isoformat()}"
        require_in_range(
            float(bar.date_int),
            f"{ctx}.date_int",
            minimum=float(UINT32_MIN),
            maximum=float(UINT32_MAX),
        )
        for name in ("open", "high", "low", "close"):
            price = require_finite(getattr(bar, name), f"{ctx}.{name}")
            encoded = int(round(price / coeff.price_coeff))
            require_in_range(
                float(encoded),
                (
                    f"{ctx}.{name} 编码值（price={price} / price_coeff="
                    f"{coeff.price_coeff}，security_type={coeff.security_type}）"
                ),
                minimum=float(UINT32_MIN),
                maximum=float(UINT32_MAX),
            )
            if encoded > max_price_int:
                max_price_int = encoded
                worst_date = bar.date

        vol_lots = require_finite(bar.vol, f"{ctx}.vol")
        encoded_vol = int(round(vol_lots / coeff.vol_coeff))
        require_in_range(
            float(encoded_vol),
            (
                f"{ctx}.vol 编码值（vol={vol_lots} 手 / vol_coeff={coeff.vol_coeff} "
                f"= {encoded_vol}，uint32 上限 {UINT32_MAX}）。"
                f"RD-8 提示：若此处超限约 100 倍，多半是把在线的「股」当成「手」传进来了，"
                f"应先 ÷{100} 换算；RD-9 提示：极端大盘股正常换算后余量也只有约 1.12 倍"
            ),
            minimum=float(UINT32_MIN),
            maximum=float(UINT32_MAX),
        )
        if encoded_vol > max_vol_int:
            max_vol_int = encoded_vol

        amount = require_finite(bar.amount, f"{ctx}.amount")
        if abs(amount) > FLOAT32_MAX:
            raise DataIntegrityError(
                f"[fail-loud/RD-9] {ctx}.amount={amount} 超出 float32 表示范围 "
                f"±{FLOAT32_MAX:.3e}，写入会变成 inf"
            )

    return BoundCheck(
        bars=len(bars),
        max_encoded_price=max_price_int,
        max_encoded_vol=max_vol_int,
        price_headroom=(UINT32_MAX / max_price_int) if max_price_int > 0 else float("inf"),
        vol_headroom=(UINT32_MAX / max_vol_int) if max_vol_int > 0 else float("inf"),
        worst_date=worst_date,
    )
DAILY_RECORD_SIZE: int = 32
MIN5_RECORD_SIZE: int = 32  # 5 分钟线单条记录 32 字节（date/time/open/high/low/close/amount/vol）


@dataclass(frozen=True)
class BoundCheck:
    """一次 uint32 上界预检的结果（RD-9）。

    Attributes:
        bars: 预检的记录数。
        max_encoded_price: 编码后最大的价格整数。
        max_encoded_vol: 编码后最大的量整数。
        price_headroom: ``UINT32_MAX / max_encoded_price``，越接近 1 越危险。
        vol_headroom: ``UINT32_MAX / max_encoded_vol``。
        worst_date: 触发最大值的交易日。
    """

    bars: int
    max_encoded_price: int
    max_encoded_vol: int
    price_headroom: float
    vol_headroom: float
    worst_date: dt.date | None

    def to_dict(self) -> dict[str, Any]:
        """转为 JSON 安全字典。"""
        return {
            "bars": self.bars,
            "max_encoded_price": self.max_encoded_price,
            "max_encoded_vol": self.max_encoded_vol,
            "price_headroom": round(self.price_headroom, 4),
            "vol_headroom": round(self.vol_headroom, 4),
            "worst_date": self.worst_date.isoformat() if self.worst_date else None,
            "uint32_max": UINT32_MAX,
        }


@dataclass(frozen=True)
class WriteReport:
    """一次落盘的完整报告。

    Attributes:
        path: 目标文件。
        written: 实际写入条数（上游会按日期去重，重复日不重写）。
        supplied: 调用方提供的条数。
        security_type: 判定出的证券类型。
        price_coeff: 使用的价格系数。
        vol_coeff: 使用的量系数。
        verified: 回读比对的条数。
        max_price_diff: 回读价格最大偏差。
        bound_check: uint32 预检结果。
        fsynced: 是否执行了自补 fsync（daily 由上游负责，仍会 fsync 目录）。
    """

    path: Path
    written: int
    supplied: int
    security_type: str
    price_coeff: float
    vol_coeff: float
    verified: int
    max_price_diff: float
    bound_check: BoundCheck | None = None
    fsynced: bool = True
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """转为 JSON 安全字典。"""
        return {
            "path": str(self.path),
            "written": self.written,
            "supplied": self.supplied,
            "security_type": self.security_type,
            "price_coeff": self.price_coeff,
            "vol_coeff": self.vol_coeff,
            "verified": self.verified,
            "max_price_diff": self.max_price_diff,
            "bound_check": self.bound_check.to_dict() if self.bound_check else None,
            "fsynced": self.fsynced,
            "warnings": list(self.warnings),
        }


class VipdocWriter:
    """vipdoc 落盘器（系数判定 + uint32 上界 + fsync + 回读校验）。

    目录布局与通达信原生一致，可被上游 ``SignalScanner(vipdoc_path=...)``
    与 ``read_daily_bars`` 直接读取（PRD 数据层验收 ⑧）::

        ~/.Kuantix/vipdoc/
        ├── sh/lday/sh600000.day
        ├── sz/lday/sz000001.day
        └── ds/       # 扩展市场（港股/美股）

    Examples:
        >>> writer = VipdocWriter(Path.home() / ".Kuantix" / "vipdoc")  # doctest: +SKIP
        >>> report = writer.write_daily(bars, "sh", "600000")          # doctest: +SKIP
        >>> report.written                                              # doctest: +SKIP
        2400
    """

    def __init__(
        self,
        vipdoc_root: Path | str,
        *,
        resolver: CoefficientResolver | None = None,
        verify_tail_bars: int = 5,
        verify_price_tolerance: float = 0.001,
    ) -> None:
        """初始化落盘器。

        Args:
            vipdoc_root: vipdoc 根目录（如 ``~/.Kuantix/vipdoc``）。
            resolver: 系数解析器；``None`` 时新建默认实例。
            verify_tail_bars: 写后回读比对的末尾条数（NF-27）；``0`` 关闭校验
                （**不推荐**，仅供性能压测）。
            verify_price_tolerance: 价格比对容差，默认 0.001。

        Raises:
            DataIntegrityError: 参数非法。
        """
        if verify_tail_bars < 0:
            raise DataIntegrityError(
                f"[fail-loud/NF-27] verify_tail_bars 不能为负，实际 {verify_tail_bars!r}"
            )
        if verify_price_tolerance <= 0:
            raise DataIntegrityError(
                f"[fail-loud/NF-27] verify_price_tolerance 必须为正数，"
                f"实际 {verify_price_tolerance!r}"
            )
        self._root = Path(vipdoc_root).expanduser()
        self._resolver = resolver if resolver is not None else CoefficientResolver()
        self._tail = int(verify_tail_bars)
        self._tolerance = float(verify_price_tolerance)

    # ------------------------------------------------------------------ #
    # 路径
    # ------------------------------------------------------------------ #

    @property
    def root(self) -> Path:
        """vipdoc 根目录。"""
        return self._root

    def daily_path(self, exchange: str, code: str) -> Path:
        """A 股日线文件路径 ``<root>/<exchange>/lday/<exchange><code>.day``。

        Args:
            exchange: ``sh`` / ``sz``。
            code: 证券代码。

        Returns:
            文件路径（目录可能尚未创建）。

        Raises:
            DataIntegrityError: 交易所前缀不在 ``sh``/``sz``（``bj`` 会被上游
                判定为 UNKNOWN，不允许进入写盘链路）。
        """
        key = str(exchange).strip().lower()
        if key not in ("sh", "sz"):
            raise DataIntegrityError(
                f"[fail-loud/NF-25] 不支持的交易所前缀 {exchange!r}（仅 sh/sz）。"
                f"bj 前缀会同时绕过上游 _detect_security_type 的 sh/sz 两个分支，"
                f"必然判定 UNKNOWN 并被静默按 A 股系数解码"
            )
        return self._root / key / "lday" / f"{key}{str(code).strip()}.day"

    def ex_daily_path(self, market_code: int, code: str) -> Path:
        """扩展市场日线文件路径 ``<root>/ds/<market>#<code>.day``。

        Args:
            market_code: 上游 ``ExMarket`` 值（港股主板 31、美股 74）。
            code: 证券代码。

        Returns:
            文件路径。
        """
        return self._root / "ds" / f"{int(market_code)}#{str(code).strip()}.day"

    def min5_path(self, exchange: str, code: str) -> Path:
        """5 分钟线文件路径 ``<root>/<exchange>/minline/<exchange><code>.5``。

        Args:
            exchange: ``sh`` / ``sz``。
            code: 证券代码。

        Returns:
            文件路径。
        """
        key = str(exchange).strip().lower()
        if key not in ("sh", "sz"):
            raise DataIntegrityError(
                f"[fail-loud/NF-25] 不支持的交易所前缀 {exchange!r}（仅 sh/sz）"
            )
        return self._root / key / "minline" / f"{key}{str(code).strip()}.5"

    # ------------------------------------------------------------------ #
    # A 股日线（主链路）
    # ------------------------------------------------------------------ #

    def write_daily(
        self,
        bars: Sequence[Bar],
        exchange: str,
        code: str,
        *,
        path: Path | None = None,
    ) -> WriteReport:
        """写 A 股日线到 ``.day``（四道闸门全开）。

        Args:
            bars: 待写入 K 线，``vol`` 单位必须是**手**（RD-8）。
            exchange: ``sh`` / ``sz``。
            code: 证券代码。
            path: 覆盖默认路径（测试用）。

        Returns:
            :class:`WriteReport`。

        Raises:
            UnknownValueError: 证券类型判定为 UNKNOWN（闸门 1，调用方须入隔离区）。
            DataIntegrityError: uint32 越界（闸门 3）或回读不一致（闸门 4）。
        """
        target = path if path is not None else self.daily_path(exchange, code)
        # ---- 闸门 1：系数按文件名判定，UNKNOWN 直接抛 UnknownValueError ----
        coeff = self._resolver.resolve(target.name)

        supplied = len(bars)
        if supplied == 0:
            return WriteReport(
                path=target,
                written=0,
                supplied=0,
                security_type=coeff.security_type,
                price_coeff=coeff.price_coeff,
                vol_coeff=coeff.vol_coeff,
                verified=0,
                max_price_diff=0.0,
                bound_check=None,
                warnings=["调用方提供了 0 根 K 线，未做任何写入"],
            )

        ordered = sorted(bars, key=lambda b: b.date)
        # ---- 闸门 3：写入前逐条 uint32 预检，越界整只拒绝 ----
        bound = self._check_bounds(ordered, coeff, context=target.name)

        target.parent.mkdir(parents=True, exist_ok=True)
        security_bars = [self._to_security_bar(b) for b in ordered]

        # 上游 append_daily_bars 内部已有 flush + fsync（write_daily.py:164）
        written = int(
            sync_daily_bars_from_security_bars(
                target, security_bars, coeff.price_coeff, coeff.vol_coeff
            )
        )
        # ---- 闸门 4a：上游只 fsync 了文件，没 fsync 目录项 ----
        self._fsync_dir(target.parent)

        # ---- 闸门 4b：写后回读比对 ----
        verified, max_diff = self._verify_daily_tail(target, ordered, coeff)
        self._assert_file_aligned(target, DAILY_RECORD_SIZE)

        return WriteReport(
            path=target,
            written=written,
            supplied=supplied,
            security_type=coeff.security_type,
            price_coeff=coeff.price_coeff,
            vol_coeff=coeff.vol_coeff,
            verified=verified,
            max_price_diff=max_diff,
            bound_check=bound,
            fsynced=True,
        )

    def last_bar_date(self, exchange: str, code: str, *, path: Path | None = None) -> int | None:
        """读取已落盘文件的最后交易日（增量同步用）。

        Args:
            exchange: ``sh`` / ``sz``。
            code: 证券代码。
            path: 覆盖默认路径。

        Returns:
            ``YYYYMMDD`` 整数；文件不存在返回 ``None``。
        """
        target = path if path is not None else self.daily_path(exchange, code)
        return get_last_bar_date(target)

    # ------------------------------------------------------------------ #
    # 扩展市场日线（港股/美股）
    # ------------------------------------------------------------------ #

    def write_ex_daily(
        self,
        bars: Sequence[Bar],
        market_code: int,
        code: str,
        *,
        path: Path | None = None,
        settlement: float = 0.0,
    ) -> WriteReport:
        """写扩展市场日线到 ``ds/<market>#<code>.day``（无系数，自补 fsync）。

        扩展市场记录格式 ``<IffffIIf``：OHLC 为 float32 直存、无系数换算，
        但 ``amount``/``vol`` 仍是 ``uint32``，闸门 3 照样要过。

        Args:
            bars: 待写入 K 线（``vol`` 单位为手，港美股上游本就返回手）。
            market_code: 上游 ``ExMarket`` 值。
            code: 证券代码。
            path: 覆盖默认路径。
            settlement: 结算价（股票无结算价，默认 0）。

        Returns:
            :class:`WriteReport`。

        Raises:
            DataIntegrityError: uint32 越界或回读不一致。
        """
        target = path if path is not None else self.ex_daily_path(market_code, code)
        supplied = len(bars)
        if supplied == 0:
            return WriteReport(
                path=target,
                written=0,
                supplied=0,
                security_type=f"EX_{int(market_code)}",
                price_coeff=1.0,
                vol_coeff=1.0,
                verified=0,
                max_price_diff=0.0,
                warnings=["调用方提供了 0 根 K 线，未做任何写入"],
            )

        ordered = sorted(bars, key=lambda b: b.date)
        bound = self._check_ex_bounds(ordered, context=target.name)

        target.parent.mkdir(parents=True, exist_ok=True)
        ex_bars = [self._to_ex_daily_bar(b, settlement=settlement) for b in ordered]
        written = int(sync_ex_daily_bars(target, ex_bars))

        # ---- RD-2：上游 append_ex_daily_bars 没有 fsync，这里自补 ----
        self._fsync_file(target)
        self._fsync_dir(target.parent)

        verified, max_diff = self._verify_ex_tail(target, ordered)
        return WriteReport(
            path=target,
            written=written,
            supplied=supplied,
            security_type=f"EX_{int(market_code)}",
            price_coeff=1.0,
            vol_coeff=1.0,
            verified=verified,
            max_price_diff=max_diff,
            bound_check=bound,
            fsynced=True,
        )

    # ------------------------------------------------------------------ #
    # 5 分钟线
    # ------------------------------------------------------------------ #

    def write_min5(
        self,
        bars: Sequence[SecurityBar],
        exchange: str,
        code: str,
        *,
        path: Path | None = None,
    ) -> WriteReport:
        """写 5 分钟线到 ``.5``（上游无 fsync，这里自补，RD-2）。

        Args:
            bars: 上游 ``SecurityBar`` 列表（含 hour/minute）。
            exchange: ``sh`` / ``sz``。
            code: 证券代码。
            path: 覆盖默认路径。

        Returns:
            :class:`WriteReport`（分钟线不做价格回读比对，只校验条数与对齐）。

        Raises:
            DataIntegrityError: 回读条数为 0 却声称写入了数据。
        """
        target = path if path is not None else self.min5_path(exchange, code)
        supplied = len(bars)
        if supplied == 0:
            return WriteReport(
                path=target,
                written=0,
                supplied=0,
                security_type="MIN5",
                price_coeff=1.0,
                vol_coeff=1.0,
                verified=0,
                max_price_diff=0.0,
                warnings=["调用方提供了 0 根分钟线，未做任何写入"],
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        # ---- RD-2 崩溃恢复：追加前先修复残尾，避免上次崩溃半条记录污染 ----
        self._repair_tail(target, MIN5_RECORD_SIZE)
        written = int(append_5min_bars(target, list(bars)))

        # ---- RD-2：上游 append_5min_bars 没有 fsync，这里自补 ----
        self._fsync_file(target)
        self._fsync_dir(target.parent)

        readback = read_5min_bars(target)
        if written > 0 and not readback:
            raise DataIntegrityError(
                f"[fail-loud/NF-27] {target.name} 声称写入 {written} 条 5 分钟线，"
                f"回读却为空，落盘失败"
            )
        # ---- RD-2 崩溃恢复：写入后断言对齐，残留半条记录即视为损坏 ----
        self._assert_file_aligned(target, MIN5_RECORD_SIZE)
        return WriteReport(
            path=target,
            written=written,
            supplied=supplied,
            security_type="MIN5",
            price_coeff=1.0,
            vol_coeff=1.0,
            verified=len(readback),
            max_price_diff=0.0,
            fsynced=True,
        )

    def min5_partition_path(self, exchange: str, code: str, key: str) -> Path:
        """分区 5 分钟线路径 ``<root>/<exchange>/minline/<exchange><code>/<key>.5``。

        按月（``2026-08``）或按周（``2026-W31``）分区，避免单文件过大。

        Args:
            exchange: ``sh`` / ``sz``。
            code: 证券代码。
            key: 分区键（由 :meth:`_min5_partition_key` 生成）。

        Returns:
            文件路径。
        """
        ex = str(exchange).strip().lower()
        if ex not in ("sh", "sz"):
            raise DataIntegrityError(
                f"[fail-loud/NF-25] 不支持的交易所前缀 {exchange!r}（仅 sh/sz）"
            )
        return self._root / ex / "minline" / f"{ex}{str(code).strip()}" / f"{key}.5"

    @staticmethod
    def _min5_partition_key(partition: str, date_int: int) -> str:
        """把 ``yyyymmdd`` 整型日期映射为分区键。

        Args:
            partition: ``"month"``（默认）→ ``YYYY-MM``；``"week"`` → ``YYYY-Www``。
            date_int: 形如 ``20260802`` 的整型日期。

        Returns:
            分区键字符串。
        """
        s = str(int(date_int))
        y, m, d = int(s[:4]), int(s[4:6]), int(s[6:8])
        if partition == "week":
            import datetime as _dt

            wk = _dt.date(y, m, d).isocalendar()[1]
            return f"{y}-W{wk:02d}"
        return f"{y}-{m:02d}"

    def write_min5_partitioned(
        self,
        bars: Sequence[SecurityBar],
        exchange: str,
        code: str,
        *,
        partition: str = "month",
    ) -> list[WriteReport]:
        """写 5 分钟线（按月/周分区，避免单文件过大）。

        将 ``bars`` 按日期分组到不同分区文件，逐个调用 :meth:`write_min5`
        （已含 fsync + 残尾修复 + 对齐校验）。

        Args:
            bars: 上游 ``SecurityBar`` 列表。
            exchange: ``sh`` / ``sz``。
            code: 证券代码。
            partition: ``"month"`` 或 ``"week"``。

        Returns:
            每个分区的 :class:`WriteReport` 列表（按分区键排序）。
        """
        if not bars:
            return []
        groups: dict[str, list] = {}
        for b in bars:
            key = self._min5_partition_key(partition, int(b.date))
            groups.setdefault(key, []).append(b)
        reports: list[WriteReport] = []
        for key in sorted(groups):
            p = self.min5_partition_path(exchange, code, key)
            reports.append(
                self.write_min5(groups[key], exchange, code, path=p)
            )
        return reports

    # ------------------------------------------------------------------ #
    # 隔离区辅助
    # ------------------------------------------------------------------ #

    def quarantine_entry_for(
        self,
        *,
        code: str,
        market: str,
        error: Exception,
        occurred_at: dt.datetime | None = None,
    ) -> QuarantineEntry:
        """把一次写入失败包装成隔离区条目（NF-27）。

        Args:
            code: 证券代码。
            market: 市场码。
            error: 触发失败的异常。
            occurred_at: 发生时间；``None`` 取当前时间。

        Returns:
            :class:`~Kuantix.core.contracts.QuarantineEntry`。
        """
        moment = occurred_at if occurred_at is not None else dt.datetime.now()
        if isinstance(error, UnknownValueError):
            reason = "UNKNOWN_SECURITY_TYPE"
        elif isinstance(error, DataIntegrityError):
            reason = "DATA_INTEGRITY"
        else:
            reason = type(error).__name__
        return QuarantineEntry(
            code=str(code),
            market=str(market),
            reason=reason,
            detail=str(error),
            occurred_at=moment,
            last_try=moment,
        )

    # ------------------------------------------------------------------ #
    # 闸门 3：uint32 预检
    # ------------------------------------------------------------------ #

    def _check_bounds(
        self,
        bars: Sequence[Bar],
        coeff: Coefficients,
        *,
        context: str,
    ) -> BoundCheck:
        """A 股日线的 uint32 上界预检（RD-9）。

        委托模块级 :func:`check_daily_bounds`，与 :class:`SqliteBarWriter`
        共用同一套值域判定（四道闸门语义跨后端一致）。
        """
        return check_daily_bounds(bars, coeff, context=context)

    def _check_ex_bounds(self, bars: Sequence[Bar], *, context: str) -> BoundCheck:
        """扩展市场的上界预检：OHLC 为 float32，``amount``/``vol`` 仍是 uint32。

        Args:
            bars: 待写入 K 线。
            context: 错误上下文。

        Returns:
            :class:`BoundCheck`（``max_encoded_price`` 恒为 0，扩展市场无价格整数化）。

        Raises:
            DataIntegrityError: 越界。
        """
        max_vol_int = 0
        worst_date: dt.date | None = None
        for bar in bars:
            ctx = f"{context}@{bar.date.isoformat()}"
            require_in_range(
                float(bar.date_int),
                f"{ctx}.date_int",
                minimum=float(UINT32_MIN),
                maximum=float(UINT32_MAX),
            )
            for name in ("open", "high", "low", "close"):
                price = require_finite(getattr(bar, name), f"{ctx}.{name}")
                if abs(price) > FLOAT32_MAX:
                    raise DataIntegrityError(
                        f"[fail-loud/RD-9] {ctx}.{name}={price} 超出 float32 范围"
                    )
            vol_int = int(round(require_finite(bar.vol, f"{ctx}.vol")))
            require_in_range(
                float(vol_int),
                f"{ctx}.vol 编码值（扩展市场直存整数，uint32 上限 {UINT32_MAX}）",
                minimum=float(UINT32_MIN),
                maximum=float(UINT32_MAX),
            )
            amount_int = int(round(require_finite(bar.amount, f"{ctx}.amount")))
            require_in_range(
                float(amount_int),
                f"{ctx}.amount 编码值（扩展市场 amount 为 uint32）",
                minimum=float(UINT32_MIN),
                maximum=float(UINT32_MAX),
            )
            if vol_int > max_vol_int:
                max_vol_int = vol_int
                worst_date = bar.date
        return BoundCheck(
            bars=len(bars),
            max_encoded_price=0,
            max_encoded_vol=max_vol_int,
            price_headroom=float("inf"),
            vol_headroom=(UINT32_MAX / max_vol_int) if max_vol_int > 0 else float("inf"),
            worst_date=worst_date,
        )

    # ------------------------------------------------------------------ #
    # 闸门 4：fsync + 回读
    # ------------------------------------------------------------------ #

    @staticmethod
    def _fsync_file(path: Path) -> None:
        """对文件补一次 ``fsync``（上游 ex/min 写入路径缺失，RD-2）。

        以 ``r+b`` 打开而非 ``rb``：部分平台对只读 fd 的 fsync 语义不保证。

        Args:
            path: 目标文件。
        """
        if not path.is_file():
            return
        with path.open("r+b") as handle:
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _fsync_dir(directory: Path) -> None:
        """对父目录补 ``fsync``，确保新建文件的目录项落盘（RD-2）。

        Windows 不支持对目录 ``open``，该平台跳过——这是平台能力差异而非
        静默降级，故在 ``OSError`` 时明确忽略并只针对目录场景。

        Args:
            directory: 目标目录。
        """
        if not directory.is_dir():
            return
        try:
            fd = os.open(directory, os.O_RDONLY)
        except OSError:
            # Windows：目录不可 open，无法 fsync 目录项。POSIX 上不会走到这里。
            return
        try:
            os.fsync(fd)
        except OSError:
            # 某些文件系统（如部分网络盘）对目录 fsync 返回 EINVAL，属平台限制
            return
        finally:
            os.close(fd)

    def _verify_daily_tail(
        self,
        path: Path,
        source: Sequence[Bar],
        coeff: Coefficients,
    ) -> tuple[int, float]:
        """回读末尾 N 条与源数据比对（NF-27）。

        Args:
            path: 已写入的 ``.day``。
            source: 源 Bar 列表（升序）。
            coeff: 使用的系数。

        Returns:
            ``(比对条数, 价格最大偏差)``。

        Raises:
            DataIntegrityError: 文件读不出、日期错位或价格偏差超容差。
        """
        if self._tail == 0:
            return (0, 0.0)
        readback = read_daily_bars(path)
        if not readback:
            raise DataIntegrityError(
                f"[fail-loud/NF-27] {path.name} 写入后回读为空，落盘失败"
            )
        n = min(self._tail, len(source), len(readback))
        expected = list(source)[-n:]
        actual = readback[-n:]

        max_diff = 0.0
        for exp, act in zip(expected, actual):
            act_date = dt.date(int(act.year), int(act.month), int(act.day))
            if act_date != exp.date:
                raise DataIntegrityError(
                    f"[fail-loud/NF-27] {path.name} 回读日期错位：期望 {exp.date}，"
                    f"实际 {act_date}。文件可能被其它进程并发写入或残留脏尾"
                )
            for name in ("open", "high", "low", "close"):
                diff = abs(float(getattr(act, name)) - float(getattr(exp, name)))
                max_diff = max(max_diff, diff)
                if diff >= self._tolerance:
                    raise DataIntegrityError(
                        f"[fail-loud/NF-27] {path.name}@{exp.date} {name} 回读不一致："
                        f"写入 {getattr(exp, name)}，回读 {getattr(act, name)}，"
                        f"偏差 {diff:.6f} ≥ 容差 {self._tolerance}。"
                        f"security_type={coeff.security_type}，"
                        f"price_coeff={coeff.price_coeff}（系数误判会导致 10 倍错价，RD-1）"
                    )
            # 量的比对用相对误差：round 到整数编码必然带来 <1 个 vol_coeff 的量化误差
            exp_vol = float(exp.vol)
            act_vol = float(act.vol)
            allowed_vol_diff = max(coeff.vol_coeff, abs(exp_vol) * 1e-6)
            if abs(act_vol - exp_vol) > allowed_vol_diff:
                raise DataIntegrityError(
                    f"[fail-loud/NF-27] {path.name}@{exp.date} vol 回读不一致："
                    f"写入 {exp_vol} 手，回读 {act_vol} 手，"
                    f"vol_coeff={coeff.vol_coeff}。"
                    f"差 100 倍通常意味着 RD-8 的「股/手」换算被漏做"
                )
        return (n, max_diff)

    def _verify_ex_tail(self, path: Path, source: Sequence[Bar]) -> tuple[int, float]:
        """扩展市场回读比对。

        Args:
            path: 已写入的 ``.day``。
            source: 源 Bar 列表（升序）。

        Returns:
            ``(比对条数, 价格最大偏差)``。

        Raises:
            DataIntegrityError: 回读为空、日期错位或价格偏差超容差。
        """
        if self._tail == 0:
            return (0, 0.0)
        readback = read_ex_daily_bars(path)
        if not readback:
            raise DataIntegrityError(
                f"[fail-loud/NF-27] {path.name} 写入后回读为空，落盘失败"
            )
        n = min(self._tail, len(source), len(readback))
        expected = list(source)[-n:]
        actual = readback[-n:]
        max_diff = 0.0
        for exp, act in zip(expected, actual):
            act_date = dt.date(int(act.year), int(act.month), int(act.day))
            if act_date != exp.date:
                raise DataIntegrityError(
                    f"[fail-loud/NF-27] {path.name} 回读日期错位：期望 {exp.date}，"
                    f"实际 {act_date}"
                )
            for name in ("open", "high", "low", "close"):
                exp_val = float(getattr(exp, name))
                act_val = float(getattr(act, name))
                # 扩展市场 OHLC 为 float32，存在固有精度损失，用相对容差
                tolerance = max(self._tolerance, abs(exp_val) * 1e-6)
                diff = abs(act_val - exp_val)
                max_diff = max(max_diff, diff)
                if diff > tolerance:
                    raise DataIntegrityError(
                        f"[fail-loud/NF-27] {path.name}@{exp.date} {name} 回读不一致："
                        f"写入 {exp_val}，回读 {act_val}，偏差 {diff:.6f} > 容差 {tolerance:.6f}"
                    )
        return (n, max_diff)

    @staticmethod
    def _assert_file_aligned(path: Path, record_size: int) -> None:
        """断言文件大小是记录长度的整数倍（残尾意味着写入中途崩溃）。

        Args:
            path: 目标文件。
            record_size: 单条记录字节数。

        Raises:
            DataIntegrityError: 存在半条残尾记录。
        """
        if not path.is_file():
            raise DataIntegrityError(f"[fail-loud/NF-27] {path} 写入后文件不存在")
        size = path.stat().st_size
        remainder = size % record_size
        if remainder != 0:
            raise DataIntegrityError(
                f"[fail-loud/NF-27] {path.name} 大小 {size} 不是 {record_size} 的整数倍，"
                f"尾部残留 {remainder} 字节半条记录，文件已损坏"
            )

    @staticmethod
    def _repair_tail(path: Path, record_size: int) -> bool:
        """崩溃恢复 —— 截断到最后一个完整记录边界，清除残尾半条记录。

        与 :meth:`_assert_file_aligned` 的「发现即报错」不同，本方法主动修复：
        写入追加前先调用，可消除上次崩溃遗留的半条记录，避免污染后续数据。
        对标日线写入的崩溃恢复机制（RD-2）。

        Args:
            path: 目标文件。
            record_size: 单条记录字节数。

        Returns:
            是否发生了截断（``True`` 表示发现并清除了残尾）。
        """
        if not path.is_file():
            return False
        size = path.stat().st_size
        if size == 0:
            return False
        aligned = (size // record_size) * record_size
        if aligned == size:
            return False
        with open(path, "r+b") as f:
            f.truncate(aligned)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        VipdocWriter._fsync_dir(path.parent)
        log.warning(
            "[崩溃恢复] %s 截断残尾至 %d 字节（原 %d）", path, aligned, size
        )
        return True

    # ------------------------------------------------------------------ #
    # 契约转换
    # ------------------------------------------------------------------ #

    @staticmethod
    def _to_security_bar(bar: Bar) -> SecurityBar:
        """:class:`Bar` → 上游 :class:`SecurityBar`。

        .. note::
            上游 ``SecurityBar.vol`` 的字段注释写的是「股」，但
            ``encode_daily_bar`` 实际做的是 ``round(bar.vol / vol_coeff)``，
            而 ``read_daily_bars`` 又 ``× vol_coeff`` 读回。A 股 ``vol_coeff=0.01``
            时，只有传入「手」才能让落盘整数等于「股」、读回等于「手」。
            这里传的是**手**（RD-8），与 :class:`Bar` 的契约一致。

        Args:
            bar: Kuantix 契约 K 线。

        Returns:
            上游 SecurityBar。
        """
        return SecurityBar(
            open=float(bar.open),
            close=float(bar.close),
            high=float(bar.high),
            low=float(bar.low),
            vol=float(bar.vol),
            amount=float(bar.amount),
            year=bar.date.year,
            month=bar.date.month,
            day=bar.date.day,
            hour=0,
            minute=0,
        )

    @staticmethod
    def _to_ex_daily_bar(bar: Bar, *, settlement: float = 0.0) -> ExDailyBar:
        """:class:`Bar` → 上游 :class:`ExDailyBar`（扩展市场，无系数）。

        Args:
            bar: Kuantix 契约 K 线。
            settlement: 结算价（股票无结算价）。

        Returns:
            上游 ExDailyBar。
        """
        return ExDailyBar(
            open=float(bar.open),
            high=float(bar.high),
            low=float(bar.low),
            close=float(bar.close),
            amount=int(round(bar.amount)),
            vol=int(round(bar.vol)),
            settlement=float(settlement),
            hk_stock_amount=0.0,
            year=bar.date.year,
            month=bar.date.month,
            day=bar.date.day,
        )


# ---------------------------------------------------------------------------
# SQLite 日线落盘器（设计文档 08：T03 写侧）
# ---------------------------------------------------------------------------


#: 交易所前缀 → 市场码（SQLite daily_bars 的 market 列）。
_EXCHANGE_TO_MARKET_SQLITE: dict[str, str] = {
    "sh": "CN",
    "sz": "CN",
    "hk": "HK",
    "us": "US",
}


class SqliteBarWriter:
    """SQLite 日线落盘器（四道闸门语义迁移，T03）。

    与 :class:`VipdocWriter` 保持**同一套四道闸门语义**：

    闸门 1 — 系数按文件名判定，UNKNOWN 显式拒绝（RD-1 / NF-25）
        ``SqliteBarWriter`` 也走 :func:`resolve_coefficients`，未知类型
        直接抛 :class:`~Kuantix.core.fail_loud.UnknownValueError` 入隔离区。
    闸门 2/3 — 值域预检（RD-8 / RD-9）
        SQLite 无文件损坏，但**保留值域校验**：数据按 .day 编码口径
        （date/OHLC/vol/amount ∈ uint32）预检，越界整只拒绝 —— 保证
        主存储与镜像可互转、源头单位错误（股/手混淆）在写库前被拦下。
    闸门 4 — 写后回读（NF-27）
        SQLite 无 fsync 语义，但保留**回读末尾 N 条比对**，价格容差
        默认 0.001；不一致即抛 :class:`DataIntegrityError`。

    Args:
        store: :class:`~Kuantix.data.market_store.MarketStore`（行情主存储）。
        resolver: 系数解析器；``None`` 时新建默认实例。
        verify_tail_bars: 写后回读比对的末尾条数（NF-27）；``0`` 关闭。
        verify_price_tolerance: 回读价格容差，默认 0.001。

    Examples:
        >>> writer = SqliteBarWriter(MarketStore(tmp_path))  # doctest: +SKIP
        >>> report = writer.write_daily(bars, "sh", "600000")  # doctest: +SKIP
        >>> report.written  # doctest: +SKIP
        2400
    """

    def __init__(
        self,
        store: Any,
        *,
        resolver: CoefficientResolver | None = None,
        verify_tail_bars: int = 5,
        verify_price_tolerance: float = 0.001,
    ) -> None:
        if verify_tail_bars < 0:
            raise DataIntegrityError(
                f"[fail-loud/NF-27] verify_tail_bars 不能为负，实际 {verify_tail_bars!r}"
            )
        if verify_price_tolerance <= 0:
            raise DataIntegrityError(
                f"[fail-loud/NF-27] verify_price_tolerance 必须为正数，"
                f"实际 {verify_price_tolerance!r}"
            )
        self._store = store
        self._resolver = resolver if resolver is not None else CoefficientResolver()
        self._tail = int(verify_tail_bars)
        self._tolerance = float(verify_price_tolerance)

    # ------------------------------------------------------------------ #
    # 公开接口
    # ------------------------------------------------------------------ #

    @property
    def store(self) -> Any:
        """关联的行情主存储。"""
        return self._store

    @staticmethod
    def _market_for_exchange(exchange: str) -> str:
        """交易所前缀 → 市场码（SQLite market 列）。

        Args:
            exchange: ``sh`` / ``sz`` / ``hk`` / ``us``。

        Raises:
            DataIntegrityError: 未知交易所前缀。
        """
        key = str(exchange).strip().lower()
        if key not in _EXCHANGE_TO_MARKET_SQLITE:
            raise DataIntegrityError(
                f"[fail-loud/NF-26] 未知交易所前缀 {exchange!r}，无法映射市场码"
            )
        return _EXCHANGE_TO_MARKET_SQLITE[key]

    def write_daily(
        self,
        bars: Sequence[Bar],
        exchange: str,
        code: str,
    ) -> WriteReport:
        """写 A 股日线到 SQLite（四道闸门语义迁移）。

        Args:
            bars: 待写入 K 线，``vol`` 单位必须是**手**（RD-8）。
            exchange: ``sh`` / ``sz``。
            code: 证券代码。

        Returns:
            :class:`WriteReport`（``path`` 指向 market.db）。

        Raises:
            UnknownValueError: 证券类型判定为 UNKNOWN（闸门 1）。
            DataIntegrityError: 值域越界（闸门 2/3）或回读不一致（闸门 4）。
        """
        # ---- 闸门 1：系数按文件名判定，UNKNOWN 直接抛 UnknownValueError ----
        filename = f"{str(exchange).strip().lower()}{str(code).strip()}.day"
        coeff = self._resolver.resolve(filename)
        market = self._market_for_exchange(exchange)

        supplied = len(bars)
        if supplied == 0:
            return WriteReport(
                path=self._store.db_path,
                written=0,
                supplied=0,
                security_type=coeff.security_type,
                price_coeff=coeff.price_coeff,
                vol_coeff=coeff.vol_coeff,
                verified=0,
                max_price_diff=0.0,
                bound_check=None,
                warnings=["调用方提供了 0 根 K 线，未做任何写入"],
            )

        ordered = sorted(bars, key=lambda b: b.date)
        # ---- 闸门 2/3：写入前逐条值域预检（.day 编码口径），越界整只拒绝 ----
        bound = check_daily_bounds(ordered, coeff, context=filename)

        written = self._store.write_daily_bars(market, code, ordered)

        # ---- 闸门 4：写后回读末尾 N 条比对（NF-27）----
        verified, max_diff = self._verify_daily_tail(market, code, ordered, filename)

        return WriteReport(
            path=self._store.db_path,
            written=written,
            supplied=supplied,
            security_type=coeff.security_type,
            price_coeff=coeff.price_coeff,
            vol_coeff=coeff.vol_coeff,
            verified=verified,
            max_price_diff=max_diff,
            bound_check=bound,
            fsynced=True,
        )

    def last_bar_date(self, exchange: str, code: str) -> int | None:
        """读取 SQLite 中该标的的最后交易日（增量同步用）。

        Args:
            exchange: ``sh`` / ``sz``。
            code: 证券代码。

        Returns:
            ``YYYYMMDD`` 整数；无数据返回 ``None``。
        """
        market = self._market_for_exchange(exchange)
        return self._store.last_bar_date(market, code)

    def quarantine_entry_for(
        self,
        *,
        code: str,
        market: str,
        error: Exception,
        occurred_at: dt.datetime | None = None,
    ) -> QuarantineEntry:
        """把一次写入失败包装成隔离区条目（NF-27，与 VipdocWriter 同口径）。"""
        moment = occurred_at if occurred_at is not None else dt.datetime.now()
        if isinstance(error, UnknownValueError):
            reason = "UNKNOWN_SECURITY_TYPE"
        elif isinstance(error, DataIntegrityError):
            reason = "DATA_INTEGRITY"
        else:
            reason = type(error).__name__
        return QuarantineEntry(
            code=str(code),
            market=str(market),
            reason=reason,
            detail=str(error),
            occurred_at=moment,
            last_try=moment,
        )

    # ------------------------------------------------------------------ #
    # 闸门 4：回读
    # ------------------------------------------------------------------ #

    def _verify_daily_tail(
        self,
        market: str,
        code: str,
        source: Sequence[Bar],
        filename: str,
    ) -> tuple[int, float]:
        """回读末尾 N 条与源数据比对（NF-27，SQLite 后端）。

        Args:
            market: 市场码。
            code: 证券代码。
            source: 源 Bar 列表（升序）。
            filename: 系数判定文件名（错误上下文）。

        Returns:
            ``(比对条数, 价格最大偏差)``。

        Raises:
            DataIntegrityError: 回读为空 / 日期错位 / 价格偏差超容差。
        """
        if self._tail == 0:
            return (0, 0.0)
        # 只回读末尾 tail 条（NF-27 只比对尾部）：原实现读全历史，
        # 全量同步 17798 只 × 每只数千根 = 数量级的读放大。
        readback = self._store.read_daily_bars(market, code, tail=self._tail)
        if not readback:
            raise DataIntegrityError(
                f"[fail-loud/NF-27] {filename} 写入后回读为空，落盘失败"
            )
        n = min(self._tail, len(source), len(readback))
        if n == 0:
            return (0, 0.0)
        max_diff = 0.0
        for src_bar, dst_bar in zip(source[-n:], readback[-n:]):
            if src_bar.date != dst_bar.date:
                raise DataIntegrityError(
                    f"[fail-loud/NF-27] {filename} 回读日期错位: "
                    f"期望 {src_bar.date}，实际 {dst_bar.date}"
                )
            for name in ("open", "high", "low", "close"):
                diff = abs(float(getattr(src_bar, name)) - float(getattr(dst_bar, name)))
                if diff > max_diff:
                    max_diff = diff
            if max_diff > self._tolerance:
                raise DataIntegrityError(
                    f"[fail-loud/NF-27] {filename} 回读价格偏差 {max_diff} 超容差 "
                    f"{self._tolerance}"
                )
        return (n, max_diff)
