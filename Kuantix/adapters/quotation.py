"""在线行情拉取与市场路由（RD-8 / 陷阱 T2、T5 的落地点）。

RD-8：成交量单位不匹配（最容易写出静默错误的地方）
--------------------------------------------------
两侧口径**不同**，中间必须换算：

- **在线侧**：``MacClient.get_stock_kline`` 返回的 ``vol`` 单位是 **股**
  （S3 实测：``amount / close ≈ vol``，即 vol 就是股数）。
- **落盘侧**：vipdoc ``.day`` 的编码是 ``stored_uint32 = round(vol / vol_coeff)``，
  读回是 ``vol = stored_uint32 * vol_coeff``。A 股 ``vol_coeff = 0.01``，
  也就是说 **写入时 ``bar.vol`` 必须是「手」**，落盘整数才等于「股」。

若不换算直接把「股」喂进去：

- ``stored = 股 / 0.01 = 股 × 100``，对大盘股轻易突破 ``uint32`` 上限
  4294967295 → ``struct.error``（S3 步骤 2b 实测确实溢出）；
- 即便侥幸不溢出，读回也会与在线口径**差 100 倍**，且**全程不报错**。

因此本模块在**离开适配层之前**就把 ``股 → 手`` 换算掉（``÷ lot_size``，
A 股 lot_size=100，取自 :class:`~Kuantix.core.market.CNMarketProfile`，
不硬编码）。契约 :class:`~Kuantix.core.contracts.Bar` 的 ``vol`` 语义即为「手」。

陷阱 T5：A 股与港美股是两条协议
--------------------------------
- A 股：``MacClient``（7709）+ ``get_stock_kline`` + ``Market.SH/SZ``；
- 港美股：``MacExClient``（7727，需 Login）+ ``goods_kline`` + ``ExMarket``。

且**两侧 vol 单位不同**：S4 实测港股 00700 ``amount/close ≈ 20.83M 股``，
而 ``vol = 212369`` → 比值 ≈ 98，说明 ``goods_kline`` 的 vol **已经是「手」**；
同时扩展市场 ``.day`` 用 ``<IffffIIf`` 直接存 float/int，**没有系数**。
所以港美股链路 **不做 ÷100**。这个差异写死在 :data:`_ROUTE_TABLE` 里，
而不是让调用方去猜。

RD-5：L1 只存原始未复权
-----------------------
默认 ``adjust=Adjust.NONE``。复权在 L2 派生，不污染数据湖。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

import pandas as pd
from easy_tdx.mac.enums import Adjust, ExMarket, Period
from easy_tdx.models.enums import Market

from Kuantix.adapters.tdx_client import TdxClientFactory
from Kuantix.core.contracts import Bar, Quote
from Kuantix.core.fail_loud import (
    DataIntegrityError,
    NotSupportedError,
    UpstreamContractError,
    require_finite,
)
from Kuantix.core.market import MarketProfile, get_market_profile

__all__ = [
    "EX_MARKET_CODES",
    "VolUnit",
    "KlineRoute",
    "VolUnitProbe",
    "QuotationFetcher",
]


# ---------------------------------------------------------------------------
# 上游枚举契约断言（NF-1 / fail-loud）
# ---------------------------------------------------------------------------
# 主/次版本校验（1.20.3）拦不住**同版本内枚举改名**（历史坑：Period.DAY →
# DAILY）。这里在 import 期断言本模块实际使用的全部上游枚举成员：先收集缺失
# 清单再一次性抛 DataIntegrityError（不逐个炸），让上游 API 调整在首次
# import 显式炸出，而不是在 K 线拉取时静默传错参数。
_UPSTREAM_ENUM_MEMBERS: tuple[tuple[type, str], ...] = (
    (Period, "DAILY"),
    (Adjust, "NONE"),
    (ExMarket, "HK_MAIN_BOARD"),
    (ExMarket, "US_STOCK"),
    (Market, "SH"),
    (Market, "SZ"),
)


def assert_upstream_enums() -> None:
    """断言上游 easy_tdx 枚举成员齐备；缺失清单一次性抛错（fail-loud）。

    Raises:
        DataIntegrityError: 任一成员缺失（上游同版本内 API 调整）。
    """
    missing = [
        f"{enum_cls.__name__}.{member}"
        for enum_cls, member in _UPSTREAM_ENUM_MEMBERS
        if not hasattr(enum_cls, member)
    ]
    if missing:
        raise DataIntegrityError(
            "[fail-loud/NF-1] 上游 easy_tdx 枚举成员缺失（同版本内 API 调整？）："
            f"{missing}。请核对 easy_tdx==1.20.3 的 mac/enums 与 models/enums"
        )


assert_upstream_enums()

#: 扩展市场代码（**引用上游 ``ExMarket`` 枚举，不写魔数**，S4 实测可用）。
EX_MARKET_CODES: dict[str, int] = {
    "HK": int(ExMarket.HK_MAIN_BOARD),  # 31 香港主板
    "US": int(ExMarket.US_STOCK),  # 74 美股
}

#: 上游 K 线返回的必备列。缺任何一列都说明协议变了，必须 fail-loud。
_REQUIRED_KLINE_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close", "vol", "amount")

#: 上游单次 K 线请求的分页上限（S4 实测 700/页；``get_stock_kline`` 内部自动翻页）。
_PAGE_CAP: int = 700


class VolUnit(str, Enum):
    """成交量单位标记（用于路由表自文档化，不参与数值计算）。"""

    #: 股（在线 ``MacClient`` 口径）
    SHARES = "shares"
    #: 手（Kuantix ``Bar.vol`` 与 vipdoc 写入口径）
    LOTS = "lots"


@dataclass(frozen=True)
class KlineRoute:
    """一个市场的 K 线拉取路由。

    Attributes:
        market: Kuantix 市场码（``CN``/``HK``/``US``）。
        client_kind: 使用的上游客户端（``mac`` / ``mac_ex``）。
        method: 上游方法名（``get_stock_kline`` / ``goods_kline``）。
        source_vol_unit: 上游返回的 vol 单位。
        divide_by_lot_size: 是否需要 ``÷ lot_size`` 把「股」换成「手」（RD-8）。
        evidence: 该结论的实测出处，便于日后复核。
    """

    market: str
    client_kind: str
    method: str
    source_vol_unit: VolUnit
    divide_by_lot_size: bool
    evidence: str

    def to_dict(self) -> dict[str, Any]:
        """转为 JSON 安全字典。"""
        return {
            "market": self.market,
            "client_kind": self.client_kind,
            "method": self.method,
            "source_vol_unit": self.source_vol_unit.value,
            "divide_by_lot_size": self.divide_by_lot_size,
            "evidence": self.evidence,
        }


#: 各市场路由表。**vol 单位差异是实测结论，不是猜测**。
_ROUTE_TABLE: dict[str, KlineRoute] = {
    "CN": KlineRoute(
        market="CN",
        client_kind="mac",
        method="get_stock_kline",
        source_vol_unit=VolUnit.SHARES,
        divide_by_lot_size=True,
        evidence=(
            "S3 步骤2b 实测：amount/close≈vol（vol 为股）；"
            "不换算直接编码会突破 uint32 上限（RD-8/RD-9）"
        ),
    ),
    "HK": KlineRoute(
        market="HK",
        client_kind="mac_ex",
        method="goods_kline",
        source_vol_unit=VolUnit.LOTS,
        divide_by_lot_size=False,
        evidence=(
            "S4 实测 00700：amount/close≈20.83M 股，vol=212369 → 比值≈98，"
            "vol 已是手；且扩展市场 .day 为 <IffffIIf 无系数"
        ),
    ),
    "US": KlineRoute(
        market="US",
        client_kind="mac_ex",
        method="goods_kline",
        source_vol_unit=VolUnit.LOTS,
        divide_by_lot_size=False,
        evidence="S4 实测美股与港股同走 goods_kline，编码格式一致",
    ),
}


@dataclass(frozen=True)
class VolUnitProbe:
    """成交量单位一致性探针结果（诊断用，不参与写盘决策）。

    通过 ``amount / close`` 反推名义股数，再与 ``vol`` 相除得到隐含倍率：
    倍率 ≈ ``lot_size`` 说明 vol 是「股」，倍率 ≈ 1 说明 vol 是「手」。

    Attributes:
        samples: 参与统计的样本数。
        implied_ratio_median: 隐含倍率中位数。
        expected_ratio: 按路由表预期的倍率。
        consistent: 中位数是否落在预期的 [1/3, 3] 倍区间内。
        note: 人类可读结论。
    """

    samples: int
    implied_ratio_median: float
    expected_ratio: float
    consistent: bool
    note: str

    def to_dict(self) -> dict[str, Any]:
        """转为 JSON 安全字典。"""
        return {
            "samples": self.samples,
            "implied_ratio_median": self.implied_ratio_median,
            "expected_ratio": self.expected_ratio,
            "consistent": self.consistent,
            "note": self.note,
        }


class QuotationFetcher:
    """在线行情拉取器（A 股 / 港美股路由 + RD-8 单位换算）。

    Examples:
        >>> factory = TdxClientFactory.from_config()                  # doctest: +SKIP
        >>> bars = QuotationFetcher(factory).fetch_kline("CN", "600000", years=1)
        ...                                                            # doctest: +SKIP
        >>> bars[-1].vol  # 单位：手                                    # doctest: +SKIP
        483210.0
    """

    def __init__(
        self,
        factory: TdxClientFactory,
        *,
        shared_connection: bool = True,
    ) -> None:
        """初始化拉取器。

        Args:
            factory: 客户端工厂。
            shared_connection: ``True`` 用池化连接（S2 实测复用连接 0.06s/只，
                每只新建反而 0.53s/只）；``False`` 每次新建独立连接，
                用于监控链路资源隔离（NF-28）与多线程 worker。
        """
        self._factory = factory
        self._shared = bool(shared_connection)

    # ------------------------------------------------------------------ #
    # 路由
    # ------------------------------------------------------------------ #

    @staticmethod
    def route_for(market: str) -> KlineRoute:
        """返回某市场的拉取路由。

        Args:
            market: 市场码。

        Returns:
            :class:`KlineRoute`。

        Raises:
            NotSupportedError: 市场未在路由表中。
        """
        key = str(market).strip().upper()
        if key not in _ROUTE_TABLE:
            raise NotSupportedError(
                f"[fail-loud/NF-5] 未知市场 {market!r}，已知 {sorted(_ROUTE_TABLE)}"
            )
        return _ROUTE_TABLE[key]

    # ------------------------------------------------------------------ #
    # K 线
    # ------------------------------------------------------------------ #

    def fetch_kline(
        self,
        market: str,
        code: str,
        years: int = 10,
        *,
        exchange: str | None = None,
        count: int | None = None,
        adjust: Adjust = Adjust.NONE,
    ) -> list[Bar]:
        """拉取日线并转成 Kuantix 契约（对应设计文档 ``fetch_kline``）。

        Args:
            market: 市场码（``CN``/``HK``/``US``）。
            code: 证券代码（不含交易所前缀）。
            years: 回溯年数；与 ``count`` 二选一，``count`` 优先。
            exchange: A 股交易所前缀（``sh``/``sz``）；``None`` 时按代码段推断。
            count: 直接指定条数。
            adjust: 复权方式，默认 ``NONE``（RD-5：L1 只存原始未复权）。

        Returns:
            :class:`~Kuantix.core.contracts.Bar` 列表（时间升序，``vol`` 单位为**手**）。

        Raises:
            NotSupportedError: 市场未实现（HK/US 的 MarketProfile 尚未落地）。
            UpstreamContractError: 上游返回结构与预期不符。
            DataIntegrityError: 数据自相矛盾（由 ``Bar.__post_init__`` 校验）。
        """
        route = self.route_for(market)
        # 关键：先取 MarketProfile。HK/US 的占位实现会在这里抛 NotSupportedError，
        # 保证"未实现的市场"绝不会静默走到写盘链路（NF-5/NF-7，无降级）。
        profile = get_market_profile(route.market)
        bar_count = self._resolve_count(years=years, count=count, profile=profile)

        if route.market == "CN":
            frame = self._fetch_cn_frame(
                code=code, exchange=exchange, count=bar_count, adjust=adjust
            )
        else:
            frame = self._fetch_ex_frame(
                market=route.market, code=code, count=bar_count, adjust=adjust
            )

        divisor = self._vol_divisor(route, profile)
        return self._frame_to_bars(
            frame,
            context=f"{route.market}:{code}",
            vol_divisor=divisor,
        )

    def probe_vol_unit(
        self,
        market: str,
        code: str,
        *,
        count: int = 60,
        exchange: str | None = None,
    ) -> VolUnitProbe:
        """探测上游 vol 的真实单位（诊断用，验证 RD-8 结论是否仍成立）。

        用 ``amount / close`` 反推名义股数，与原始 ``vol`` 相除得到隐含倍率。

        Args:
            market: 市场码。
            code: 证券代码。
            count: 采样条数。
            exchange: A 股交易所前缀。

        Returns:
            :class:`VolUnitProbe`。

        Raises:
            NotSupportedError: 市场未实现。
            UpstreamContractError: 上游返回缺列。
        """
        route = self.route_for(market)
        profile = get_market_profile(route.market)
        if route.market == "CN":
            frame = self._fetch_cn_frame(
                code=code, exchange=exchange, count=count, adjust=Adjust.NONE
            )
        else:
            frame = self._fetch_ex_frame(
                market=route.market, code=code, count=count, adjust=Adjust.NONE
            )
        self._assert_columns(frame, context=f"{route.market}:{code}")
        expected = float(profile.lot_size) if route.divide_by_lot_size else 1.0
        ratios: list[float] = []
        for close, vol, amount in zip(
            frame["close"].tolist(), frame["vol"].tolist(), frame["amount"].tolist()
        ):
            close_f, vol_f, amount_f = float(close), float(vol), float(amount)
            if close_f <= 0 or vol_f <= 0 or amount_f <= 0:
                continue
            ratios.append((amount_f / close_f) / vol_f)
        if not ratios:
            raise UpstreamContractError(
                f"[fail-loud/NF-1] {route.market}:{code} 无有效样本可探测 vol 单位"
            )
        ratios.sort()
        median = ratios[len(ratios) // 2]
        consistent = (expected / 3.0) <= median <= (expected * 3.0)
        return VolUnitProbe(
            samples=len(ratios),
            implied_ratio_median=round(median, 4),
            expected_ratio=expected,
            consistent=consistent,
            note=(
                f"隐含倍率中位数 {median:.2f}（期望≈{expected:g}）。"
                f"{'与路由表一致' if consistent else '与路由表不一致，RD-8 结论需复核！'}"
                f" 依据：{route.evidence}"
            ),
        )

    # ------------------------------------------------------------------ #
    # 实时报价
    # ------------------------------------------------------------------ #

    def fetch_quotes(self, market: str, codes: Sequence[tuple[str, str]]) -> list[Quote]:
        """批量拉取实时报价（监控链路用，NF-28 建议配 ``shared_connection=False``）。

        Args:
            market: 市场码；目前仅 ``CN``。
            codes: ``[(exchange, code), ...]``，如 ``[("sh", "600000")]``。
                上游单次上限 80 只，本方法自动分批。

        Returns:
            :class:`~Kuantix.core.contracts.Quote` 列表。

        Raises:
            NotSupportedError: 非 CN 市场。
            UpstreamContractError: 上游返回缺少必要字段。
        """
        route = self.route_for(market)
        get_market_profile(route.market)
        if route.market != "CN":
            raise NotSupportedError(
                f"[fail-loud/NF-5] 实时报价目前仅支持 CN 市场，收到 {market!r}"
            )
        pairs: list[tuple[int, str]] = []
        exchanges: list[str] = []
        for exchange, code in codes:
            pairs.append((self._cn_market_int(exchange, code), str(code)))
            exchanges.append(str(exchange).strip().lower())
        if not pairs:
            return []

        profile = get_market_profile(route.market)
        lot_size = self._vol_divisor(route, profile)
        client = self._mac_client()
        quotes: list[Quote] = []
        now = dt.datetime.now()
        for offset in range(0, len(pairs), 80):
            batch = pairs[offset : offset + 80]
            frame = client.get_stock_quotes(stocks=batch)
            if not isinstance(frame, pd.DataFrame):
                raise UpstreamContractError(
                    f"[fail-loud/NF-1] get_stock_quotes 返回 {type(frame).__name__}，"
                    f"期望 DataFrame"
                )
            quotes.extend(self._frame_to_quotes(frame, now=now, vol_divisor=lot_size))
        return quotes

    # ------------------------------------------------------------------ #
    # 内部：上游调用
    # ------------------------------------------------------------------ #

    def _mac_client(self) -> Any:
        """取 A 股行情客户端（按 ``shared_connection`` 决定是否池化）。"""
        if self._shared:
            return self._factory.get_mac_client()
        return self._factory.new_mac_client()

    def _mac_ex_client(self) -> Any:
        """取扩展市场客户端（按 ``shared_connection`` 决定是否池化）。"""
        if self._shared:
            return self._factory.get_mac_ex_client()
        return self._factory.new_mac_ex_client()

    def _fetch_cn_frame(
        self,
        *,
        code: str,
        exchange: str | None,
        count: int,
        adjust: Adjust,
    ) -> pd.DataFrame:
        """A 股链路：``MacClient.get_stock_kline``（7709）。"""
        market_int = self._cn_market_int(exchange, code)
        client = self._mac_client()
        frame = client.get_stock_kline(
            market=market_int,
            code=str(code),
            period=Period.DAILY,
            start=0,
            count=int(count),
            adjust=adjust,
        )
        if not isinstance(frame, pd.DataFrame):
            raise UpstreamContractError(
                f"[fail-loud/NF-1] get_stock_kline({code}) 返回 "
                f"{type(frame).__name__}，期望 DataFrame"
            )
        return frame

    def _fetch_ex_frame(
        self,
        *,
        market: str,
        code: str,
        count: int,
        adjust: Adjust,
    ) -> pd.DataFrame:
        """港美股链路：``MacExClient.goods_kline``（7727，需 Login 握手）。

        单次上限 700 条（S4 实测），超过则按 ``start`` 逐页回溯拼接。
        """
        ex_market = EX_MARKET_CODES[market]
        client = self._mac_ex_client()
        frames: list[pd.DataFrame] = []
        remaining = int(count)
        start = 0
        while remaining > 0:
            page = min(remaining, _PAGE_CAP)
            frame = client.goods_kline(
                market=ex_market,
                code=str(code),
                period=Period.DAILY,
                start=start,
                count=page,
                adjust=adjust,
            )
            if not isinstance(frame, pd.DataFrame):
                raise UpstreamContractError(
                    f"[fail-loud/NF-1] goods_kline({market}:{code}) 返回 "
                    f"{type(frame).__name__}，期望 DataFrame"
                )
            if frame.empty:
                break
            frames.append(frame)
            got = len(frame)
            start += got
            remaining -= got
            if got < page:
                break
        if not frames:
            return pd.DataFrame(columns=list(_REQUIRED_KLINE_COLUMNS))
        # goods_kline 的 start 越大越早，拼接后需按时间升序还原
        merged = pd.concat(frames[::-1], ignore_index=True)
        return merged

    # ------------------------------------------------------------------ #
    # 内部：解析与换算
    # ------------------------------------------------------------------ #

    @staticmethod
    def _cn_market_int(exchange: str | None, code: str) -> int:
        """A 股交易所前缀 → 上游 ``Market`` 整数。

        Args:
            exchange: ``sh`` / ``sz``；``None`` 时按代码段推断。
            code: 证券代码。

        Returns:
            ``Market.SH``(1) 或 ``Market.SZ``(0)。

        Raises:
            DataIntegrityError: 无法确定交易所（**绝不猜默认值**，NF-26）。
        """
        if exchange is not None:
            key = str(exchange).strip().lower()
            if key == "sh":
                return int(Market.SH)
            if key == "sz":
                return int(Market.SZ)
            raise DataIntegrityError(
                f"[fail-loud/NF-26] 不支持的交易所前缀 {exchange!r}（仅 sh/sz）。"
                f"北交所 bj 不在 P0 范围，且会导致上游系数判定为 UNKNOWN"
            )
        head = str(code).strip()[:2]
        if head in ("60", "68", "51", "50", "52", "53", "55", "56", "58", "88", "99", "01"):
            return int(Market.SH)
        if head in ("00", "30", "39", "15", "16", "17", "18", "12", "13"):
            return int(Market.SZ)
        raise DataIntegrityError(
            f"[fail-loud/NF-26] 无法从代码 {code!r} 推断交易所，请显式传 exchange。"
            f"静默猜测会导致连错市场并拉到空数据或错标的"
        )

    @staticmethod
    def _resolve_count(*, years: int, count: int | None, profile: MarketProfile) -> int:
        """把"回溯年数"换算成条数。

        Args:
            years: 回溯年数。
            count: 显式条数（优先）。
            profile: 市场画像（提供 ``trading_days_per_year``）。

        Returns:
            请求条数。

        Raises:
            DataIntegrityError: 参数非正。
        """
        if count is not None:
            if count <= 0:
                raise DataIntegrityError(
                    f"[fail-loud/NF-26] count 必须为正整数，实际 {count!r}"
                )
            return int(count)
        if years <= 0:
            raise DataIntegrityError(
                f"[fail-loud/NF-26] years 必须为正整数，实际 {years!r}"
            )
        per_year = int(profile.trading_days_per_year)
        if per_year <= 0:
            raise DataIntegrityError(
                f"[fail-loud/NF-5] {profile.market} 市场未定义 trading_days_per_year，"
                f"无法把 years 换算成条数"
            )
        # +20 缓冲：闰年 / 临时调休导致的年内交易日波动
        return int(years) * per_year + 20

    @staticmethod
    def _vol_divisor(route: KlineRoute, profile: MarketProfile) -> float:
        """计算 vol 换算除数（RD-8 的唯一落点）。

        Args:
            route: 市场路由。
            profile: 市场画像。

        Returns:
            除数；``1.0`` 表示无需换算。

        Raises:
            DataIntegrityError: 需要换算却拿不到合法的 ``lot_size``。
        """
        if not route.divide_by_lot_size:
            return 1.0
        lot_size = int(profile.lot_size)
        if lot_size <= 0:
            raise DataIntegrityError(
                f"[fail-loud/RD-8] {route.market} 市场 lot_size={lot_size} 非法，"
                f"无法把在线「股」换算成 vipdoc「手」。"
                f"不换算会导致落盘量错 100 倍甚至 uint32 溢出，故拒绝继续"
            )
        return float(lot_size)

    @staticmethod
    def _assert_columns(frame: pd.DataFrame, *, context: str) -> None:
        """校验 K 线 DataFrame 含全部必备列。

        Args:
            frame: 上游返回。
            context: 错误上下文。

        Raises:
            UpstreamContractError: 缺列。
        """
        missing = [c for c in _REQUIRED_KLINE_COLUMNS if c not in frame.columns]
        if missing:
            raise UpstreamContractError(
                f"[fail-loud/NF-1] {context} K 线缺少列 {missing}，"
                f"实际列={list(frame.columns)}，上游协议可能已变更"
            )

    @staticmethod
    def _extract_dates(frame: pd.DataFrame, *, context: str) -> list[dt.date]:
        """从 DataFrame 抽取交易日序列。

        Args:
            frame: 上游返回。
            context: 错误上下文。

        Returns:
            ``datetime.date`` 列表。

        Raises:
            UpstreamContractError: 找不到日期来源。
        """
        if isinstance(frame.index, pd.DatetimeIndex):
            series = pd.Series(frame.index)
        elif "datetime" in frame.columns:
            series = pd.to_datetime(frame["datetime"])
        elif "date" in frame.columns:
            series = pd.to_datetime(frame["date"])
        else:
            raise UpstreamContractError(
                f"[fail-loud/NF-1] {context} K 线找不到日期列（既非 DatetimeIndex，"
                f"也无 datetime/date 列），实际列={list(frame.columns)}"
            )
        return [pd.Timestamp(v).date() for v in series.tolist()]

    def _frame_to_bars(
        self,
        frame: pd.DataFrame,
        *,
        context: str,
        vol_divisor: float,
    ) -> list[Bar]:
        """把上游 DataFrame 转成 :class:`Bar` 列表，**在此完成 RD-8 换算**。

        Args:
            frame: 上游返回。
            context: 错误上下文。
            vol_divisor: vol 除数（A 股 100.0：股→手；港美股 1.0）。

        Returns:
            按日期升序的 Bar 列表。

        Raises:
            UpstreamContractError: 缺列或缺日期。
            DataIntegrityError: 单根 K 线自相矛盾。
        """
        if frame.empty:
            return []
        self._assert_columns(frame, context=context)
        dates = self._extract_dates(frame, context=context)
        if len(dates) != len(frame):
            raise UpstreamContractError(
                f"[fail-loud/NF-1] {context} 日期数({len(dates)})与行数({len(frame)})不一致"
            )

        opens = frame["open"].tolist()
        highs = frame["high"].tolist()
        lows = frame["low"].tolist()
        closes = frame["close"].tolist()
        vols = frame["vol"].tolist()
        amounts = frame["amount"].tolist()

        bars: list[Bar] = []
        for i, date in enumerate(dates):
            ctx = f"{context}@{date.isoformat()}"
            raw_vol = require_finite(vols[i], f"{ctx}.vol")
            # ---------------- RD-8 唯一换算点：股 → 手 ----------------
            lots = raw_vol / vol_divisor
            bars.append(
                Bar(
                    date=date,
                    open=require_finite(opens[i], f"{ctx}.open"),
                    high=require_finite(highs[i], f"{ctx}.high"),
                    low=require_finite(lows[i], f"{ctx}.low"),
                    close=require_finite(closes[i], f"{ctx}.close"),
                    vol=lots,
                    amount=require_finite(amounts[i], f"{ctx}.amount"),
                )
            )
        bars.sort(key=lambda b: b.date)
        self._assert_strictly_increasing(bars, context=context)
        return bars

    @staticmethod
    def _assert_strictly_increasing(bars: Iterable[Bar], *, context: str) -> None:
        """校验日期严格递增（重复日期会破坏 vipdoc 去重依据）。

        Args:
            bars: Bar 序列。
            context: 错误上下文。

        Raises:
            DataIntegrityError: 出现重复日期。
        """
        seen: set[dt.date] = set()
        for bar in bars:
            if bar.date in seen:
                raise DataIntegrityError(
                    f"[fail-loud/NF-26] {context} 出现重复交易日 {bar.date}，"
                    f"上游分页拼接可能有重叠，拒绝写入以免污染 L1"
                )
            seen.add(bar.date)

    @staticmethod
    def _pick_field(row: Mapping[str, Any], candidates: Sequence[str], context: str) -> Any:
        """从报价行里按候选名取字段（**取不到就报错，不给默认值**）。

        Args:
            row: 一行报价。
            candidates: 候选字段名。
            context: 错误上下文。

        Returns:
            字段值。

        Raises:
            UpstreamContractError: 所有候选名都不存在。
        """
        for name in candidates:
            if name in row:
                return row[name]
        raise UpstreamContractError(
            f"[fail-loud/NF-1] {context} 报价缺少字段 {list(candidates)}，"
            f"实际字段={sorted(row)}，上游协议可能已变更"
        )

    def _frame_to_quotes(
        self,
        frame: pd.DataFrame,
        *,
        now: dt.datetime,
        vol_divisor: float,
    ) -> list[Quote]:
        """把报价 DataFrame 转成 :class:`Quote` 列表。

        上游 ``get_stock_quotes`` 的列由协议字段动态决定，这里用候选名解析，
        解析不到直接报错（NF-26：不猜、不填默认值）。

        Args:
            frame: 上游返回。
            now: 快照时间。
            vol_divisor: vol 除数（RD-8：A 股在线为股，契约为手，故 ÷100）。

        Returns:
            Quote 列表。

        Raises:
            UpstreamContractError: 缺少必要字段。
        """
        quotes: list[Quote] = []
        for record in frame.to_dict(orient="records"):
            row: Mapping[str, Any] = record
            code = str(self._pick_field(row, ("code",), "quote")).strip()
            ctx = f"quote:{code}"
            last = float(self._pick_field(row, ("price", "close", "last"), ctx))
            prev_close = float(
                self._pick_field(row, ("last_close", "pre_close", "prev_close"), ctx)
            )
            vol_shares = float(self._pick_field(row, ("vol", "volume"), ctx))
            amount = float(self._pick_field(row, ("amount", "turnover"), ctx))
            if prev_close <= 0:
                raise UpstreamContractError(
                    f"[fail-loud/NF-26] {ctx} 昨收={prev_close}，无法计算涨跌幅；"
                    f"填 0 会让监控规则误判，故拒绝"
                )
            # 契约 §1.4/§3.5：change_pct 一律为**小数比例**（0.05 = 5%），
            # 与 core/contracts.py Quote.change_pct 的 docstring 口径一致。
            # 全链路（适配层 → monitor → api 层）只有这一种口径，禁止百分数。
            change_pct = (last / prev_close) - 1.0
            quotes.append(
                Quote(
                    code=code,
                    market="CN",
                    last=last,
                    prev_close=prev_close,
                    change_pct=change_pct,
                    # RD-8 同样适用于报价：在线 vol 为股，契约 Quote.vol 为手
                    vol=vol_shares / vol_divisor,
                    amount=amount,
                    ts=now,
                )
            )
        return quotes
