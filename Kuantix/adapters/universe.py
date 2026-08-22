"""证券清单枚举（RD-10 / 陷阱 T4 的落地点）。

RD-10：``get_security_list`` 的连接复用陷阱
-------------------------------------------
S2 spike 实测（``spikes/results/S2_throughput.json`` → ``A0_security_list_conn_trap``）：

===================  ==========================
调用方式              每页耗时
===================  ==========================
复用同一 TdxClient    0.05s / **15.21s** / 15.22s
每页新建 TdxClient    0.05s / 0.03s / 0.04s
===================  ==========================

服务端对**同一条连接**的第 2 次 ``get_security_list`` 不响应，客户端要等到
15s read timeout 才重连重试。52 页 × 15.2s ≈ **13 分钟**，而每页新连接
总耗时 **5.9s**（S2 实测），差距 **512 倍**。

因此本模块的铁律：**每页新建一条 ``TdxClient`` 连接，用完立刻关闭**。

其它红线
--------
- **禁止 ``get_security_list_all()``**（NF-1/NF-20）：上游该方法会把结果缓存
  写进 ``~/.easy_tdx/``，违反"上游目录只读"。
- **UNKNOWN 显式拒绝**（NF-25/NF-26）：文件名判定不出证券类型的标的，
  绝不放进股票池——它们一旦进入写盘链路就会被上游
  ``.get(sec_type, (0.01, 0.01))`` 静默按 A 股系数解码。这里直接剔除并
  产出 :class:`~Kuantix.core.contracts.QuarantineEntry`，由调用方入隔离区（NF-27）。
- **北交所（BJ）不在 P0 范围**：上游 ``get_security_list_all`` 自己都注明
  ``Market.BJ`` 长期超时；且 ``bj`` 前缀会同时绕过 ``sh``/``sz`` 两个分支，
  必然判定 UNKNOWN。这里显式不枚举 BJ，而不是"枚举了再静默丢弃"。
"""

from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

import pandas as pd
from easy_tdx.models.enums import Market

from Kuantix.adapters.coefficients import (
    UNKNOWN_SECURITY_TYPE,
    detect_security_type,
    known_security_types,
)
from Kuantix.adapters.tdx_client import TdxClientFactory
from Kuantix.core.contracts import QuarantineEntry, Security
from Kuantix.core.fail_loud import (
    DataIntegrityError,
    NotSupportedError,
    UpstreamContractError,
)

__all__ = [
    "CN_EXCHANGE_BY_MARKET",
    "A_SHARE_TYPES",
    "EnumerationStats",
    "EnumerationResult",
    "UniverseEnumerator",
]

#: 上游 ``Market`` 枚举 → vipdoc 交易所前缀。**只含 P0 支持的两个市场**。
CN_EXCHANGE_BY_MARKET: dict[int, str] = {
    int(Market.SH): "sh",
    int(Market.SZ): "sz",
}

#: A 股（含创业板/科创板）在上游系数表中的类型名。
A_SHARE_TYPES: frozenset[str] = frozenset({"SH_A_STOCK", "SZ_A_STOCK"})

#: 上游 ``get_security_list`` 每页固定条数。
DEFAULT_PAGE_SIZE: int = 1000


@dataclass(frozen=True)
class EnumerationStats:
    """一次枚举的性能与质量统计。

    Attributes:
        pages: 实际请求的页数。
        seconds: 总耗时（秒）。
        rows: 服务端返回的原始行数。
        accepted: 通过类型校验、进入股票池的标的数。
        rejected: 因 UNKNOWN 类型被拒绝的标的数。
        connections: 创建的 TdxClient 连接数（应 == pages + 市场数）。
        per_page_seconds: 平均每页耗时——RD-10 的验收指标，必须 <1s。
        counts_by_market: 各市场服务端上报的证券总数。
    """

    pages: int
    seconds: float
    rows: int
    accepted: int
    rejected: int
    connections: int
    per_page_seconds: float
    counts_by_market: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """转为 JSON 安全字典。"""
        return {
            "pages": self.pages,
            "seconds": self.seconds,
            "rows": self.rows,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "connections": self.connections,
            "per_page_seconds": self.per_page_seconds,
            "counts_by_market": dict(self.counts_by_market),
        }


@dataclass(frozen=True)
class EnumerationResult:
    """枚举结果：可用股票池 + 被拒清单 + 统计。

    Attributes:
        securities: 类型已确认、可安全写盘的标的列表。
        rejected: UNKNOWN 类型被拒绝的标的（应由调用方写入隔离区，NF-27）。
        stats: 性能与质量统计。
    """

    securities: list[Security]
    rejected: list[QuarantineEntry]
    stats: EnumerationStats

    def to_dict(self) -> dict[str, Any]:
        """转为 JSON 安全字典。"""
        return {
            "securities": [s.to_dict() for s in self.securities],
            "rejected": [q.to_dict() for q in self.rejected],
            "stats": self.stats.to_dict(),
        }


class UniverseEnumerator:
    """全市场证券清单枚举器（每页新连接，RD-10）。

    Examples:
        >>> factory = TdxClientFactory.from_config()      # doctest: +SKIP
        >>> result = UniverseEnumerator(factory).enumerate_all()  # doctest: +SKIP
        >>> len(result.securities)                        # doctest: +SKIP
        5209
    """

    def __init__(
        self,
        factory: TdxClientFactory,
        *,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_pages_per_market: int = 200,
        sleep_between_pages: float = 0.0,
    ) -> None:
        """初始化枚举器。

        Args:
            factory: 客户端工厂（提供 :meth:`~TdxClientFactory.new_tdx_client`）。
            page_size: 每页条数（上游固定 1000，用于翻页与终止判定）。
            max_pages_per_market: 单市场最大页数上限，防止服务端异常导致死循环。
            sleep_between_pages: 每页之间的休眠秒数（限速，NF-24）。

        Raises:
            DataIntegrityError: 参数非法。
        """
        if page_size <= 0:
            raise DataIntegrityError(
                f"[fail-loud/NF-26] page_size 必须为正整数，实际 {page_size!r}"
            )
        if max_pages_per_market <= 0:
            raise DataIntegrityError(
                f"[fail-loud/NF-26] max_pages_per_market 必须为正整数，"
                f"实际 {max_pages_per_market!r}"
            )
        if sleep_between_pages < 0:
            raise DataIntegrityError(
                f"[fail-loud/NF-26] sleep_between_pages 不能为负，"
                f"实际 {sleep_between_pages!r}"
            )
        self._factory = factory
        self._page_size = int(page_size)
        self._max_pages = int(max_pages_per_market)
        self._sleep = float(sleep_between_pages)

    # ------------------------------------------------------------------ #
    # 公开接口
    # ------------------------------------------------------------------ #

    def enumerate(
        self,
        market: str = "CN",
        *,
        security_types: Iterable[str] | None = None,
        progress: Callable[[str, int, int], None] | None = None,
    ) -> list[Security]:
        """枚举指定市场的证券清单（对应设计文档 ``enumerate() list~Security~``）。

        Args:
            market: 市场代码；P0 仅支持 ``"CN"``。
            security_types: 只保留这些上游类型（如 ``{"SH_A_STOCK","SZ_A_STOCK"}``）；
                ``None`` 表示保留所有**已知**类型。
            progress: 进度回调 ``(exchange, done_rows, total_rows)``。

        Returns:
            :class:`~Kuantix.core.contracts.Security` 列表。

        Raises:
            NotSupportedError: 市场不是 ``CN``（港美股清单走 ``goods_list``，P1）。
        """
        return self.enumerate_full(
            market, security_types=security_types, progress=progress
        ).securities

    def enumerate_all(
        self,
        *,
        progress: Callable[[str, int, int], None] | None = None,
    ) -> EnumerationResult:
        """枚举 A 股全市场（所有已知类型：股票/ETF/指数/债券）。

        Args:
            progress: 进度回调。

        Returns:
            :class:`EnumerationResult`。
        """
        return self.enumerate_full("CN", security_types=None, progress=progress)

    def enumerate_a_shares(
        self,
        *,
        progress: Callable[[str, int, int], None] | None = None,
    ) -> EnumerationResult:
        """只枚举 A 股股票（``SH_A_STOCK`` / ``SZ_A_STOCK``）。

        Args:
            progress: 进度回调。

        Returns:
            :class:`EnumerationResult`。
        """
        return self.enumerate_full("CN", security_types=A_SHARE_TYPES, progress=progress)

    def enumerate_full(
        self,
        market: str = "CN",
        *,
        security_types: Iterable[str] | None = None,
        progress: Callable[[str, int, int], None] | None = None,
    ) -> EnumerationResult:
        """枚举并返回完整结果（含被拒清单与统计）。

        Args:
            market: 市场代码；P0 仅支持 ``"CN"``。
            security_types: 类型白名单；``None`` 表示所有已知类型。
            progress: 进度回调 ``(exchange, done_rows, total_rows)``。

        Returns:
            :class:`EnumerationResult`。

        Raises:
            NotSupportedError: 市场不是 ``CN``。
            DataIntegrityError: 类型白名单中含上游未知类型。
        """
        normalized = str(market).strip().upper()
        if normalized != "CN":
            raise NotSupportedError(
                f"[fail-loud/NF-5] UniverseEnumerator 目前仅支持 CN 市场，"
                f"收到 {market!r}。港股/美股清单需走 MacExClient.goods_list "
                f"（S4 已验证可用，但 HK/US MarketProfile 尚未实现，属 P1 范围）；"
                f"这里显式拒绝，不做静默降级"
            )
        allowed = self._resolve_allowed_types(security_types)

        started = time.perf_counter()
        conn_before = self._factory.created_total
        securities: list[Security] = []
        rejected: list[QuarantineEntry] = []
        counts: dict[str, int] = {}
        pages = 0
        rows = 0

        for market_code, exchange in sorted(CN_EXCHANGE_BY_MARKET.items()):
            total = self._fetch_count(market_code, exchange)
            counts[exchange] = total
            done = 0
            page_index = 0
            while done < total:
                if page_index >= self._max_pages:
                    raise UpstreamContractError(
                        f"[fail-loud/NF-1] {exchange} 证券清单翻页超过上限 "
                        f"{self._max_pages} 页仍未取完（total={total}，已取 {done}），"
                        f"疑似上游分页语义变更，拒绝继续以免死循环"
                    )
                start = page_index * self._page_size
                frame = self._fetch_page(market_code, exchange, start)
                pages += 1
                page_index += 1
                page_rows = len(frame)
                if page_rows == 0:
                    # 服务端提前结束：上报总数与实际可翻页数不一致，属正常收敛
                    break
                rows += page_rows
                self._absorb_page(
                    frame,
                    exchange=exchange,
                    allowed=allowed,
                    securities=securities,
                    rejected=rejected,
                )
                done += page_rows
                if progress is not None:
                    progress(exchange, done, total)
                if self._sleep > 0:
                    time.sleep(self._sleep)

        elapsed = time.perf_counter() - started
        connections = self._factory.created_total - conn_before
        stats = EnumerationStats(
            pages=pages,
            seconds=round(elapsed, 4),
            rows=rows,
            accepted=len(securities),
            rejected=len(rejected),
            connections=connections,
            per_page_seconds=round(elapsed / pages, 4) if pages else 0.0,
            counts_by_market=counts,
        )
        return EnumerationResult(securities=securities, rejected=rejected, stats=stats)

    # ------------------------------------------------------------------ #
    # 内部：单页拉取（RD-10 核心）
    # ------------------------------------------------------------------ #

    def _fetch_count(self, market_code: int, exchange: str) -> int:
        """取某市场证券总数（**独立新连接**）。

        Args:
            market_code: 上游 ``Market`` 值。
            exchange: ``sh`` / ``sz``。

        Returns:
            证券总数。

        Raises:
            UpstreamContractError: 上游返回非正整数。
        """
        client = self._factory.new_tdx_client()
        try:
            raw = client.get_security_count(Market(market_code))
        finally:
            client.close()
        total = int(raw)
        if total <= 0:
            raise UpstreamContractError(
                f"[fail-loud/NF-1] {exchange} 证券总数为 {total}，上游返回异常，"
                f"拒绝以空股票池继续"
            )
        return total

    def _fetch_page(self, market_code: int, exchange: str, start: int) -> pd.DataFrame:
        """拉取一页证券清单——**每页新建连接、用完即关**（RD-10）。

        这是整个模块的命门：绝不能改成复用连接，否则第 2 页起每页 15.2s。

        Args:
            market_code: 上游 ``Market`` 值。
            exchange: ``sh`` / ``sz``。
            start: 分页偏移。

        Returns:
            上游返回的 DataFrame。

        Raises:
            UpstreamContractError: 返回类型不是 DataFrame 或缺少 ``code`` 列。
        """
        client = self._factory.new_tdx_client()
        try:
            frame = client.get_security_list(Market(market_code), start)
        finally:
            # 必须立刻关闭：连接不关会被服务端认作"复用"，触发 15s 陷阱
            client.close()
        if not isinstance(frame, pd.DataFrame):
            raise UpstreamContractError(
                f"[fail-loud/NF-1] {exchange} 第 {start} 页返回类型异常："
                f"期望 DataFrame，实际 {type(frame).__name__}"
            )
        if len(frame) > 0 and "code" not in frame.columns:
            raise UpstreamContractError(
                f"[fail-loud/NF-1] {exchange} 证券清单缺少 code 列，"
                f"实际列={list(frame.columns)}，上游协议可能已变更"
            )
        return frame

    # ------------------------------------------------------------------ #
    # 内部：单页解析（UNKNOWN 显式拒绝）
    # ------------------------------------------------------------------ #

    def _absorb_page(
        self,
        frame: pd.DataFrame,
        *,
        exchange: str,
        allowed: frozenset[str],
        securities: list[Security],
        rejected: list[QuarantineEntry],
    ) -> None:
        """把一页 DataFrame 解析成 Security，UNKNOWN 类型转入被拒清单。

        Args:
            frame: 上游返回的一页数据。
            exchange: ``sh`` / ``sz``。
            allowed: 类型白名单。
            securities: 输出——通过校验的标的（原地追加）。
            rejected: 输出——被拒标的（原地追加）。
        """
        now = dt.datetime.now()
        codes = [str(c).strip() for c in frame["code"].tolist()]
        if "name" in frame.columns:
            names = [str(n).strip() for n in frame["name"].tolist()]
        else:
            names = [""] * len(codes)
        for code, name in zip(codes, names):
            if not code:
                continue
            filename = f"{exchange}{code}.day"
            security_type = detect_security_type(filename)
            if security_type == UNKNOWN_SECURITY_TYPE:
                rejected.append(
                    QuarantineEntry(
                        code=code,
                        market="CN",
                        reason="UNKNOWN_SECURITY_TYPE",
                        detail=(
                            f"上游 _detect_security_type({filename!r}) 判定为 UNKNOWN。"
                            f"若放行，上游 daily_bar.py:89 的 "
                            f".get(sec_type, (0.01, 0.01)) 会静默按 A 股系数解码，"
                            f"导致基金价格错 10 倍 / 指数成交量错 100 倍（NF-25）"
                        ),
                        occurred_at=now,
                        last_try=now,
                    )
                )
                continue
            if security_type not in allowed:
                continue
            securities.append(
                Security(
                    code=code,
                    exchange=exchange,
                    market="CN",
                    security_type=security_type,
                    name=name,
                )
            )

    @staticmethod
    def _resolve_allowed_types(security_types: Iterable[str] | None) -> frozenset[str]:
        """把类型白名单规范化，并校验每项都在上游系数表内。

        Args:
            security_types: 白名单；``None`` 表示所有已知类型。

        Returns:
            规范化后的白名单。

        Raises:
            DataIntegrityError: 白名单里出现上游不认识的类型。
        """
        known = known_security_types()
        if security_types is None:
            return known
        requested = frozenset(str(t).strip().upper() for t in security_types)
        if not requested:
            raise DataIntegrityError(
                "[fail-loud/NF-26] security_types 白名单为空集，"
                "会枚举出空股票池；如需全部类型请传 None"
            )
        unknown = requested - known
        if unknown:
            raise DataIntegrityError(
                f"[fail-loud/NF-25] security_types 含上游系数表不存在的类型："
                f"{sorted(unknown)}，已知类型={sorted(known)}"
            )
        return requested


def group_by_exchange(securities: Sequence[Security]) -> dict[str, list[Security]]:
    """按交易所前缀分组（便于按目录批量写盘）。

    Args:
        securities: 标的列表。

    Returns:
        ``{"sh": [...], "sz": [...]}``。
    """
    grouped: dict[str, list[Security]] = {}
    for sec in securities:
        grouped.setdefault(sec.exchange, []).append(sec)
    return grouped
