"""个股详情服务（多周期 K 线 + 技术指标 + 核心数据，通达信风格）。

本服务是「个股详情页」的后端支柱，独立于回测契约（B5），直接复用
:class:`~Kuantix.data.market_store.MarketStore` 的日线 / 分钟线读取能力：

- **日 / 周 / 月 / 季 / 年 K**：以日线为基础重采样（本地一定有日线，D1）；
- **分时 / 5日 / 15分 / 30分 / 60分 K**：优先读分钟库（``read_minute_bars``），
  本地无分钟数据时回退 tdx 实时拉取并标注 ``data_source='tdx_realtime'``，
  仍不可得时返回空序列并置 ``available=False``，**绝不静默编造**（fail-loud，
  R4）。

复权（RD-5：本地 lake 只存原始未复权）：``qfq``（前复权）/ ``hfq``（后复权）
仅对日基周期有意义，且只能走 tdx 在线拉取（本地无除权除息因子表）；
``none``（不复权）走本地 lake 优先 + tdx 深历史回退。

换手率：本地 ``daily_bars`` 仅含 ``vol/amount``，流通股本属证券主数据；
若 :class:`MarketStore` 暴露 ``free_float_shares`` 则直接计算，否则退化为
「成交量 / 估算股本」并标注 ``turnover_estimated``，避免误导（D3）。

周期枚举（与前端 ``Period`` 一致）
-------------------------------
``min1``(当日分时) / ``min5``(5日1分钟) / ``min15``(15分钟) / ``min30``(30分钟)
/ ``min60``(60分钟) / ``day``(日K) / ``week``(周K) / ``month``(月K)
/ ``quarter``(季K) / ``year``(年K)。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Any

import pandas as pd

from Kuantix.analysis.indicators import (
    INDICATOR_NAMES,
    bias,
    boll,
    ene,
    kdj,
    ma,
    macd,
    obv,
    rsi,
    sar,
    vwap,
    wr,
)
from Kuantix.core.fail_loud import DataIntegrityError, MissingKeyError
from Kuantix.core.market import get_market_profile
from Kuantix.data.market_store import MarketStore, MinuteBar

__all__ = ["StockDetailService", "PERIODS", "ADJUSTS", "DEFAULT_LIMIT"]


#: 支持的周期键（前端 Period 联合类型）。
PERIODS = (
    "min1",
    "min5",
    "min15",
    "min30",
    "min60",
    "day",
    "week",
    "month",
    "quarter",
    "year",
)

#: 支持的复权方式（none=不复权 / qfq=前复权 / hfq=后复权）。
ADJUSTS = ("none", "qfq", "hfq")

#: 分钟周期集合（走分钟库 / tdx 分钟链路）。
_MINUTE_PERIODS = frozenset({"min1", "min5", "min15", "min30", "min60"})

#: 分钟周期 → tdx 分钟档位（min5 实为「5 日 1 分钟」，拉 1 分钟档）。
_TDX_MINUTE_MAP: dict[str, int] = {
    "min1": 1,
    "min5": 1,
    "min15": 15,
    "min30": 30,
    "min60": 60,
}

#: 分钟周期 tdx 回退拉取根数（1 分钟/交易日在 A 股 ≈ 240 根）。
_TDX_MINUTE_COUNTS: dict[str, int] = {
    "min1": 480,
    "min5": 1200,
    "min15": 960,
    "min30": 480,
    "min60": 240,
}

#: 默认返回 bar 数量上限（防止一次拉全历史撑爆前端）。
DEFAULT_LIMIT = 500


def _require_code(code: str, market: str) -> None:
    """校验代码形态（复用市场档案的交易所判定，非法 → 400 语义）。"""
    profile = get_market_profile(market)
    try:
        profile.exchange_for_code(code)
    except Exception as exc:  # noqa: BLE001 - 统一转 MissingKeyError
        raise MissingKeyError(
            f"[fail-loud] 个股代码非法: {code!r}（{exc}）"
        ) from exc


def _resample_daily(
    frame: pd.DataFrame, rule: str, limit: int
) -> pd.DataFrame:
    """对日线 DataFrame 按 ``rule`` 重采样（week/month/quarter/year）。

    重采样聚合：open=首、close=末、high=最大、low=最小、vol/amount=求和。

    与 ``pandas.resample`` 的差异（通达信口径）：
    - 周桶按**周五对齐**（W-FRI），而非默认 W-SUN——避免周五交易日的
      周K横轴显示成周日；
    - 桶标签取**桶内最后一个实际交易日**（节假日停牌时显示节前最后
      交易日），不显示自然周末/月末等非交易日。

    返回升序、截断到最近 ``limit`` 根。
    """
    df = frame.set_index("datetime").sort_index()
    if rule == "W-FRI":
        keys = df.index + pd.to_timedelta((4 - df.index.weekday) % 7, unit="D")
    elif rule == "ME":
        keys = df.index.to_period("M").to_timestamp("M")
    elif rule == "QE":
        keys = df.index.to_period("Q").to_timestamp("Q")
    elif rule == "YE":
        keys = df.index.to_period("Y").to_timestamp("Y")
    else:
        raise ValueError(f"不支持的重采样周期: {rule!r}")
    agg = {
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "vol": "sum",
        "amount": "sum",
    }
    grouped = df.groupby(keys)
    rs = grouped.agg(agg)
    # 桶标签 = 桶内最后交易日（groupby 迭代序与 agg 行序一致，均为键升序）
    rs.index = pd.DatetimeIndex([sub.index.max() for _, sub in grouped])
    rs = rs.dropna(subset=["close"])
    rs = rs.reset_index().rename(columns={"index": "datetime"})
    if limit and len(rs) > limit:
        rs = rs.tail(limit)
    return rs.reset_index(drop=True)


class StockDetailService:
    """个股详情：多周期 K 线 + 指标 + 核心数据。

    数据源策略：lake（market.db）优先；本地无该标的日线时**自动回退**
    tdx 实时行情拉取并正常展示（响应标注 ``data_source='tdx_realtime'``），
    而非直接抛 ``DataIntegrityError``——顶部搜索框搜任意代码都应能进详情页。
    tdx 也不可得时才 fail-loud 报错。

    Args:
        store: 行情主存储；``None`` 时用默认工厂构造。
        config: 全局配置（用于构造 tdx 客户端工厂）；``None`` 时无 tdx 回退。
    """

    def __init__(self, store: MarketStore | None = None, config: Any | None = None) -> None:
        self._store = store if store is not None else MarketStore()
        self._cfg = config
        self._tdx_fetcher: Any | None = None
        self._name_cache: dict[str, str] = {}

    # ------------------------------------------------------------------ #
    # tdx 实时回退
    # ------------------------------------------------------------------ #
    def _get_fetcher(self) -> Any:
        """惰性构造 QuotationFetcher（日线/分钟/盘口共用同一连接池）。"""
        if self._tdx_fetcher is None:
            from Kuantix.adapters.quotation import QuotationFetcher
            from Kuantix.adapters.tdx_client import TdxClientFactory

            if self._cfg is None:
                raise DataIntegrityError(
                    "[fail-loud] StockDetailService 未注入 config，无法回退 tdx 实时行情"
                )
            self._tdx_fetcher = QuotationFetcher(TdxClientFactory.from_config(self._cfg))
        return self._tdx_fetcher

    def _fetch_tdx_bars(
        self, market: str, code: str, count: int, *, adjust: str = "none"
    ) -> list[Any]:
        """经 QuotationFetcher 从 tdx 实时拉日线；失败抛原始异常由调用方处理。

        Args:
            adjust: 复权方式（none/qfq/hfq → 上游 ``Adjust`` 枚举）。
        """
        from easy_tdx.mac.enums import Adjust

        enum = {"none": Adjust.NONE, "qfq": Adjust.QFQ, "hfq": Adjust.HFQ}[adjust]
        return self._get_fetcher().fetch_kline(market, code, count=count, adjust=enum)

    def _fetch_tdx_minute_bars(
        self, market: str, code: str, period: str, count: int
    ) -> list[Any]:
        """经 QuotationFetcher 从 tdx 实时拉分钟线；失败抛原始异常由调用方处理。

        ``period`` 为详情页周期键；min1/min5 拉 1 分钟档（分时/5日），
        min15/min30/min60 直接拉对应周期档（无需本地聚合）。
        """
        return self._get_fetcher().fetch_minute_kline(
            market,
            code,
            period_minutes=_TDX_MINUTE_MAP[period],
            count=count,
        )

    def _security_name(self, market: str, code: str) -> str:
        """取证券名称（A 股行取 lake；非 A 股行回退 tdx 个股优先真名）。

        securities 表主键 ``(market, code)`` 无交易所列：000001-000999 区间
        沪指（sh000001 上证指数）与深股（sz000001 平安银行）冲突，指数行
        后写入会覆盖股票行。详情页按个股优先语义，检测到非 A 股行时经
        tdx 取个股真名（进程缓存），不可用时退回 lake 名称。
        """
        row: dict[str, Any] | None = None
        try:
            row = self._store.security_row(code, market)
        except Exception:  # noqa: BLE001 - 旧测试替身无该方法时走 security_name
            row = None
        if row is not None:
            if str(row.get("security_type") or "").endswith("_A_STOCK"):
                return str(row.get("name") or "").strip()
            tdx_name = self._tdx_stock_name(market, code)
            return tdx_name or str(row.get("name") or "").strip()
        try:
            name = self._store.security_name(code, market)
            return str(name or "").strip()
        except Exception:  # noqa: BLE001 - 名称缺失不影响行情展示
            return ""

    def _tdx_stock_name(self, market: str, code: str) -> str:
        """tdx 个股优先名称（进程级缓存；失败/无 tdx 返回空串）。"""
        key = f"{market}:{code}"
        cached = self._name_cache.get(key)
        if cached is not None:
            return cached
        try:
            book = self._get_fetcher().fetch_order_book(market, code)
            name = str(book.get("name") or "").strip()
        except Exception:  # noqa: BLE001 - tdx 不可用时回退 lake 名称
            name = ""
        self._name_cache[key] = name
        return name

    def _daily_quote(
        self, market: str, code: str, daily_bars: Sequence[Any] | None = None
    ) -> dict[str, Any] | None:
        """最新交易日行情快照（与请求周期无关，供前端顶部报价区展示）。

        周/月/年K的最后一根是跨期聚合值（昨收=上期收盘、涨跌=跨期对比），
        直接当日内行情展示会出现「单日 -27%」这类误导数字；顶部行情必须
        始终是最新交易日口径。日线序列优先复用调用方已取数据；不足时本地
        读尾 120 根（同时探测残缺），本地残缺（<120 根，market.db 未同步
        时的测试残留）再 tdx 回退；均不可得返回 None（前端退化为旧逻辑）。
        """
        bars: list[Any] = list(daily_bars) if daily_bars is not None else []
        if len(bars) < 2:
            try:
                # tail=120：取 quote 候选的同时探测本地是否残缺（<120 根
                # 说明 market.db 未同步，可能是测试残留假数据）
                bars = list(self._store.read_daily_bars(market, code, tail=120))
            except Exception:  # noqa: BLE001 - quote 缺失不影响 K 线展示
                bars = []
            if len(bars) < 120:
                try:
                    tdx_bars = list(self._fetch_tdx_bars(market, code, count=5))
                except Exception:  # noqa: BLE001
                    tdx_bars = []
                # 本地残缺时 tdx 更可信（与日线回退同策略：多者胜出）
                if len(tdx_bars) > len(bars):
                    bars = tdx_bars
        if len(bars) < 2:
            return None
        last, prev = bars[-1], bars[-2]
        close, prev_close = float(last.close), float(prev.close)
        float_shares = self._free_float_shares(market, code)
        return {
            "date": last.date.isoformat(),
            "open": float(last.open),
            "high": float(last.high),
            "low": float(last.low),
            "close": close,
            "prev_close": prev_close,
            "change": round(close - prev_close, 6),
            "change_pct": round((close - prev_close) / prev_close, 6)
            if prev_close
            else None,
            "vol": float(last.vol),
            "amount": float(last.amount),
            "turnover": round(float(last.vol) / float_shares, 6)
            if float_shares
            else 0.0,
        }

    def _listing_date(self, market: str, code: str) -> str | None:
        """取上市日期（lake 日线首根日期；缺失返回 None，前端按默认区间处理）。

        经 :meth:`MarketStore.first_daily_date` 的 O(1) ``MIN(date)`` 聚合
        查询获取，避免为取首根而把该标的全部历史读进内存。

        本地无日线（如纯 tdx 回退标的）时返回 None，由前端退化到固定区间，
        不编造上市日期（fail-loud/不误导）。
        """
        getter = getattr(self._store, "first_daily_date", None)
        if getter is None:
            return None
        try:
            first = getter(market, code)
        except Exception:  # noqa: BLE001 - 上市日期缺失不影响行情展示
            return None
        if first is None:
            return None
        s = str(int(first))
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"

    # ------------------------------------------------------------------ #
    # 主入口
    # ------------------------------------------------------------------ #
    def get_detail(
        self,
        code: str,
        market: str = "CN",
        period: str = "day",
        *,
        limit: int = DEFAULT_LIMIT,
        indicators: Sequence[str] | None = None,
        adjust: str = "none",
        ma_windows: Sequence[int] = (5, 10, 20, 60),
    ) -> dict[str, Any]:
        """取个股某周期的 K 线 + 指标 + 核心数据。

        Args:
            code: 6 位证券代码。
            market: 市场码（默认 ``CN``）。
            period: 周期键（见 :data:`PERIODS`）。
            limit: 返回 bar 上限。
            indicators: 需要计算的指标子集（默认全算）；非法名被忽略。
            adjust: 复权方式（``none``/``qfq``/``hfq``；仅日基周期生效，
                分钟周期为当日近期数据，无复权意义）。
            ma_windows: MA 均线窗口自定义（通达信参数面板语义）。

        Returns:
            ``{code, market, period, adjust, available, turnover_estimated,
            bars, indicators}``。
            ``bars`` 为 ``[{date, open, high, low, close, vol, amount,
            turnover}]``；``indicators`` 为各指标序列（键对应指标名）。
        """
        if period not in PERIODS:
            raise MissingKeyError(
                f"[fail-loud] 未知周期: {period!r}（支持 {PERIODS}）"
            )
        if adjust not in ADJUSTS:
            raise MissingKeyError(
                f"[fail-loud] 未知复权方式: {adjust!r}（支持 {ADJUSTS}）"
            )
        _require_code(code, market)

        if indicators is None:
            indicators = list(INDICATOR_NAMES)
        indicators = [i for i in indicators if i in INDICATOR_NAMES]
        windows = sorted({int(w) for w in ma_windows if 1 <= int(w) <= 500})

        if period in _MINUTE_PERIODS:
            return self._detail_minute(code, market, period, limit, indicators)
        return self._detail_daily(
            code, market, period, limit, indicators,
            adjust=adjust, ma_windows=windows,
        )

    # ------------------------------------------------------------------ #
    # 日 / 周 / 月 / 季 / 年
    # ------------------------------------------------------------------ #
    def _detail_daily(
        self,
        code: str,
        market: str,
        period: str,
        limit: int,
        indicators: Sequence[str],
        *,
        adjust: str = "none",
        ma_windows: Sequence[int] = (5, 10, 20, 60),
    ) -> dict[str, Any]:
        # 复权数据本地没有（RD-5：lake 只存未复权），只能 tdx 在线拉取。
        # 拉不到直接报错——静默降级回未复权会让用户误以为在前复权视图。
        data_source = "lake"
        if adjust != "none":
            try:
                bars = self._fetch_tdx_bars(
                    market, code, count=5000, adjust=adjust
                )
            except Exception as exc:  # noqa: BLE001
                raise DataIntegrityError(
                    f"[fail-loud] {code} {adjust} 复权数据拉取失败：{exc}"
                ) from exc
            data_source = "tdx_realtime"
        elif period == "day":
            # 日K只需末尾 limit 根 + 指标预热（MA60/MACD(26,9)/RSI24 约需
            # 80 根历史）；SQL 反向取尾，避免全历史数千根读入内存
            bars = self._store.read_daily_bars(
                market, code, tail=int(limit) + 80
            )
        else:
            bars = self._store.read_daily_bars(market, code)
        if adjust == "none":
            # 本地数据不足（market.db 未全量同步）→ tdx 回退拉深历史：
            # day 需 limit+80 根（指标预热）；周/月/季/年重采样需足够历史
            # （120 根 ≈ 半年，否则年K仅 1 根、指标全空）。
            # tdx 比本地更全则采用并标注来源；不足时沿用本地（少好于无）。
            min_needed = int(limit) + 80 if period == "day" else 120
            if len(bars) < min_needed:
                try:
                    tdx_bars = self._fetch_tdx_bars(market, code, count=5000)
                except Exception as exc:  # noqa: BLE001 - 本地有数据时容忍回退失败
                    if not bars:
                        raise DataIntegrityError(
                            f"[fail-loud] {code} 无日线数据（market.db 未同步），"
                            f"且 tdx 实时回退失败：{exc}"
                        ) from exc
                    tdx_bars = []
                if len(tdx_bars) > len(bars):
                    bars = tdx_bars
                    data_source = "tdx_realtime"
                elif not bars and not tdx_bars:
                    raise DataIntegrityError(
                        f"[fail-loud] {code} 无日线数据（market.db 未同步，"
                        "tdx 实时亦无返回——请确认代码是否有效）"
                    )
        frame = self._bars_to_frame(bars)
        # 防御上游重复写入（同一 date 多条相同记录），按日期去重保留末值
        frame = frame.drop_duplicates(subset=["datetime"], keep="last").reset_index(drop=True)
        if period == "day":
            rs = frame
        elif period == "week":
            rs = _resample_daily(frame, "W-FRI", limit)
        elif period == "month":
            rs = _resample_daily(frame, "ME", limit)
        elif period == "quarter":
            rs = _resample_daily(frame, "QE", limit)
        else:  # year
            rs = _resample_daily(frame, "YE", limit)

        if limit and len(rs) > limit:
            rs = rs.tail(limit)
        rs = rs.reset_index(drop=True)
        out_bars = self._frame_to_bars(rs, market, code)
        if data_source == "tdx_realtime":
            # tdx 深历史比本地元信息可信：上市日期取回退数据首根
            # （本地 lake 未同步时 first_daily_date 同样残缺）
            listing_date = bars[0].date.isoformat()
        else:
            listing_date = self._listing_date(market, code)
        # 顶部行情快照取自日线原始序列（bars 即日线，非重采样结果）
        quote = self._daily_quote(market, code, daily_bars=bars)
        return self._assemble(
            code, market, period, out_bars, indicators,
            available=True, data_source=data_source, listing_date=listing_date,
            quote=quote, adjust=adjust, ma_windows=ma_windows,
        )

    # ------------------------------------------------------------------ #
    # 分钟（分时 / 5日 / 15分 / 30分 / 60分）
    # ------------------------------------------------------------------ #
    def _detail_minute(
        self,
        code: str,
        market: str,
        period: str,
        limit: int,
        indicators: Sequence[str],
    ) -> dict[str, Any]:
        # min1（当日分时）：最近一个交易日；min5：最近 5 个自然日 1 分钟；
        # min15/min30/min60：60 天窗口读 1 分钟线后按桶聚合（tdx 回退时
        # 直接拉对应档位，无需聚合）。
        today = dt.date.today()
        if period == "min1":
            start = _date_int(today - dt.timedelta(days=10))
            rows = self._store.read_minute_bars(
                market, code, start_date=start
            )
            # 只保留最近一个交易日（容忍周末/节假日回看）
            if rows:
                last_date = max(r.date for r in rows)
                rows = [r for r in rows if r.date == last_date]
        elif period == "min5":
            start = _date_int(today - dt.timedelta(days=5))
            rows = self._store.read_minute_bars(
                market, code, start_date=start
            )
        else:
            start = _date_int(today - dt.timedelta(days=60))
            rows = self._store.read_minute_bars(
                market, code, start_date=start
            )
            rows = self._aggregate_minute(rows, f"{period[3:]}min")

        data_source = "lake"
        if not rows:
            # 本地无分钟数据 → tdx 实时回退（与日线回退同策略：拉到就正常
            # 展示并标注来源；拉不到才标记 unavailable，绝不编造）。
            try:
                tdx_rows = self._fetch_tdx_minute_bars(
                    market, code, period, _TDX_MINUTE_COUNTS[period]
                )
            except Exception as exc:  # noqa: BLE001 - 回退失败 → 显式 unavailable
                return self._minute_unavailable(
                    code, market, period, indicators,
                    message=(
                        "本地无分钟级数据（market.db 未同步），"
                        f"tdx 实时回退失败：{exc}"
                    ),
                )
            if not tdx_rows:
                return self._minute_unavailable(
                    code, market, period, indicators,
                    message=(
                        "本地无分钟级数据（market.db 未同步，"
                        "tdx 实时亦无返回——请确认代码是否有效）"
                    ),
                )
            rows = tdx_rows
            data_source = "tdx_realtime"
            if period == "min1":
                # 当日分时：同样只保留最近交易日
                last_date = max(r.date for r in rows)
                rows = [r for r in rows if r.date == last_date]

        frame = self._minute_to_frame(rows)
        if limit and len(frame) > limit:
            frame = frame.tail(limit)
        frame = frame.reset_index(drop=True)
        out_bars = self._frame_to_bars(frame, market, code)
        listing_date = self._listing_date(market, code)
        quote = self._daily_quote(market, code)
        return self._assemble(
            code, market, period, out_bars, indicators, available=True,
            data_source=data_source, listing_date=listing_date, quote=quote,
        )

    def _minute_unavailable(
        self,
        code: str,
        market: str,
        period: str,
        indicators: Sequence[str],
        *,
        message: str,
    ) -> dict[str, Any]:
        """构造分钟周期「无数据」响应（available=False + 提示，不编造）。"""
        return {
            "code": code,
            "market": market,
            "period": period,
            "adjust": "none",
            "available": False,
            "turnover_estimated": False,
            "listing_date": self._listing_date(market, code),
            "bars": [],
            "indicators": {i: {} for i in indicators},
            "quote": self._daily_quote(market, code),
            "message": message,
        }

    # ------------------------------------------------------------------ #
    # 实时面板（盘口 / 逐笔 / 资金流 / 快照；tdx 在线直连）
    # ------------------------------------------------------------------ #
    def get_order_book(
        self, code: str, market: str = "CN"
    ) -> dict[str, Any]:
        """五档盘口 + 实时快照（含换手/量比/PE/股本等基本面字段）。

        Raises:
            DataIntegrityError: 未注入 config（无 tdx 链路）或上游拉取失败。
        """
        _require_code(code, market)
        return self._get_fetcher().fetch_order_book(market, code)

    def get_transactions(
        self, code: str, market: str = "CN", *, count: int = 300
    ) -> dict[str, Any]:
        """逐笔成交（当日，时间升序；bs: 0=买 1=卖 2=中性）。"""
        _require_code(code, market)
        return self._get_fetcher().fetch_transactions(market, code, count=count)

    def get_capital_flow(
        self, code: str, market: str = "CN"
    ) -> dict[str, Any]:
        """资金流向（今日主力/散户 + 5 日大中小单净额，单位元）。"""
        _require_code(code, market)
        return self._get_fetcher().fetch_capital_flow(market, code)

    def get_quotes(
        self, codes: Sequence[str], market: str = "CN"
    ) -> list[dict[str, Any]]:
        """批量实时报价（自选股侧栏；tdx 批量接口，单批上限 80 只）。

        Args:
            codes: 证券代码列表（≤80 只；超出自动分批）。
            market: 市场码（仅 CN）。

        Returns:
            ``[{code, price, prev_close, change, change_pct, vol, amount}]``；
            拉不到的代码不出现在结果里（前端按缺失处理，不编造）。
        """
        from Kuantix.adapters.quotation import QuotationFetcher

        fetcher = self._get_fetcher()
        pairs: list[tuple[str, str]] = []
        for c in codes:
            exchange = QuotationFetcher.cn_stock_exchange(str(c))
            if exchange is not None:
                pairs.append((exchange, str(c)))
        if not pairs:
            return []
        quotes = fetcher.fetch_quotes(market, pairs)
        return [
            {
                "code": q.code,
                "price": q.last,
                "prev_close": q.prev_close,
                "change": round(q.last - q.prev_close, 6),
                "change_pct": q.change_pct,
                "vol": q.vol,
                "amount": q.amount,
            }
            for q in quotes
        ]

    def _assemble(
        self,
        code: str,
        market: str,
        period: str,
        bars: list[dict[str, Any]],
        indicators: Sequence[str],
        available: bool,
        data_source: str = "lake",
        listing_date: str | None = None,
        quote: dict[str, Any] | None = None,
        adjust: str = "none",
        ma_windows: Sequence[int] = (5, 10, 20, 60),
    ) -> dict[str, Any]:
        closes = [b["close"] for b in bars]
        highs = [b["high"] for b in bars]
        lows = [b["low"] for b in bars]
        vols = [b["vol"] for b in bars]
        amounts = [b["amount"] for b in bars]
        ind: dict[str, Any] = {}
        if "ma" in indicators:
            for w in ma_windows:
                ind[f"ma{w}"] = ma(closes, int(w))
        if "boll" in indicators:
            ind["boll"] = boll(closes)
        if "ene" in indicators:
            ind["ene"] = ene(closes)
        if "sar" in indicators:
            ind["sar"] = sar(highs, lows)
        if "macd" in indicators:
            ind["macd"] = macd(closes)
        if "kdj" in indicators:
            ind["kdj"] = kdj(highs, lows, closes)
        if "rsi" in indicators:
            ind["rsi"] = rsi(closes)
        if "wr" in indicators:
            ind["wr"] = wr(highs, lows, closes)
        if "bias" in indicators:
            ind["bias"] = bias(closes)
        if "obv" in indicators:
            ind["obv"] = obv(closes, vols)
        if "vwap" in indicators or period == "min1":
            # 分时均价线：当日累计口径（cumsum(amount)/cumsum(vol)）
            ind["vwap"] = vwap(closes, vols, amounts)

        # 换手率：日/周/月/季/年线才有意义（分钟线 vol 即真实成交量）。
        # 无流通股本时不编造（vol/1e6 是毫无意义的相对量），置 0 并标
        # turnover_estimated=True，由前端显示「--」，符合 fail-loud/不误导。
        turnover_estimated = False
        if period in ("day", "week", "month", "quarter", "year"):
            float_shares = self._free_float_shares(market, code)
            if float_shares:
                for b in bars:
                    b["turnover"] = round(b["vol"] / float_shares, 6)
            else:
                turnover_estimated = True
                for b in bars:
                    b["turnover"] = 0.0

        return {
            "code": code,
            "name": self._security_name(market, code),
            "market": market,
            "period": period,
            "adjust": adjust,
            "available": available,
            "data_source": data_source,
            "turnover_estimated": turnover_estimated,
            "listing_date": listing_date,
            "quote": quote,
            "bars": bars,
            "indicators": ind,
        }

    def _free_float_shares(self, market: str, code: str) -> float | None:
        """尝试取流通股本（无则 None，由调用方退化为估算）。"""
        getter = getattr(self._store, "free_float_shares", None)
        if getter is None:
            return None
        try:
            return getter(market, code)
        except Exception:  # noqa: BLE001 - 缺失即估算
            return None

    @staticmethod
    def _bars_to_frame(bars: list) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {
                    "datetime": pd.Timestamp(b.date),
                    "open": float(b.open),
                    "high": float(b.high),
                    "low": float(b.low),
                    "close": float(b.close),
                    "vol": float(b.vol),
                    "amount": float(b.amount),
                }
                for b in bars
            ]
        )

    @staticmethod
    def _minute_to_frame(rows: list[MinuteBar]) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {
                    "datetime": pd.Timestamp(
                        int(r.date // 10000),
                        int(r.date // 100 % 100),
                        int(r.date % 100),
                        int(r.time // 100),
                        int(r.time % 100),
                    ),
                    "open": float(r.open),
                    "high": float(r.high),
                    "low": float(r.low),
                    "close": float(r.close),
                    "vol": float(r.vol),
                    "amount": float(r.amount),
                }
                for r in rows
            ]
        )

    @staticmethod
    def _frame_to_bars(frame: pd.DataFrame, market: str, code: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for _, row in frame.iterrows():
            ts = pd.Timestamp(row["datetime"])
            out.append(
                {
                    "datetime": ts.strftime("%Y-%m-%d %H:%M")
                    if ts.hour or ts.minute
                    else ts.strftime("%Y-%m-%d"),
                    "date": ts.strftime("%Y-%m-%d"),
                    "open": round(float(row["open"]), 6),
                    "high": round(float(row["high"]), 6),
                    "low": round(float(row["low"]), 6),
                    "close": round(float(row["close"]), 6),
                    "vol": round(float(row["vol"]), 6),
                    "amount": round(float(row["amount"]), 6),
                    "turnover": 0.0,
                }
            )
        return out

    @staticmethod
    def _aggregate_minute(
        rows: list[MinuteBar], bucket: str
    ) -> list[MinuteBar]:
        """按分钟桶聚合 1 分钟线（open=首、close=末、high=max、low=min）。

        bucket 支持 ``15min`` / ``30min`` / ``60min``（其他值原样返回，不聚合）。
        桶边界按「当日累计分钟数对齐」：如 09:35 → 15min 桶 09:30、
        13:47 → 60min 桶 13:00（交易时段自然分桶，不复用小时取整）。
        """
        if bucket not in ("15min", "30min", "60min") or not rows:
            return rows
        step = {"15min": 15, "30min": 30, "60min": 60}[bucket]
        buckets: dict[tuple[int, int], list[MinuteBar]] = {}
        for r in rows:
            minutes = (r.time // 100) * 60 + (r.time % 100)
            bucket_start = (minutes // step) * step
            key = (r.date, bucket_start)
            buckets.setdefault(key, []).append(r)
        out: list[MinuteBar] = []
        for (date, start_min), grp in sorted(buckets.items()):
            out.append(
                MinuteBar(
                    market=grp[0].market,
                    code=grp[0].code,
                    date=date,
                    time=(start_min // 60) * 100 + (start_min % 60),
                    open=grp[0].open,
                    high=max(g.high for g in grp),
                    low=min(g.low for g in grp),
                    close=grp[-1].close,
                    vol=sum(g.vol for g in grp),
                    amount=sum(g.amount for g in grp),
                )
            )
        return out


def _date_int(d: dt.date) -> int:
    return d.year * 10000 + d.month * 100 + d.day
