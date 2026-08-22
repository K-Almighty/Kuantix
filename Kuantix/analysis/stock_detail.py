"""个股详情服务（多周期 K 线 + 技术指标 + 核心数据，通达信风格）。

本服务是「个股详情页」的后端支柱，独立于回测契约（B5），直接复用
:class:`~Kuantix.data.market_store.MarketStore` 的日线 / 分钟线读取能力：

- **日 / 周 / 月 / 年 K**：以日线为基础重采样（本地一定有日线，D1）；
- **5 日 / 15 分钟 K**：优先读分钟库（``read_minute_bars``），本地无分钟
  数据时该周期返回空序列并置 ``available=False``，由前端提示「本地无分钟数据」，
  **绝不静默编造**（fail-loud 精神，R4）。

换手率：本地 ``daily_bars`` 仅含 ``vol/amount``，流通股本属证券主数据；
若 :class:`MarketStore` 暴露 ``free_float_shares`` 则直接计算，否则退化为
「成交量 / 估算股本」并标注 ``turnover_estimated``，避免误导（D3）。

周期枚举（与前端 ``Period`` 一致）
-------------------------------
``day``(日K) / ``week``(周K) / ``month``(月K) / ``year``(年K) /
``min5``(5日1分钟) / ``min15``(15分钟)。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Any

import pandas as pd

from Kuantix.analysis.indicators import INDICATOR_NAMES, kdj, ma, macd, rsi
from Kuantix.core.fail_loud import DataIntegrityError, MissingKeyError
from Kuantix.core.market import get_market_profile
from Kuantix.data.market_store import MarketStore, MinuteBar

__all__ = ["StockDetailService", "PERIODS", "DEFAULT_LIMIT"]


#: 支持的周期键（前端 Period 联合类型）。
PERIODS = ("day", "week", "month", "year", "min5", "min15", "min60")

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
    """对日线 DataFrame 按 ``rule`` 重采样（week/month/year）。

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

    # ------------------------------------------------------------------ #
    # tdx 实时回退
    # ------------------------------------------------------------------ #
    def _fetch_tdx_bars(self, market: str, code: str, count: int) -> list[Any]:
        """经 QuotationFetcher 从 tdx 实时拉日线；失败抛原始异常由调用方处理。"""
        if self._tdx_fetcher is None:
            from Kuantix.adapters.quotation import QuotationFetcher
            from Kuantix.adapters.tdx_client import TdxClientFactory

            if self._cfg is None:
                raise DataIntegrityError(
                    "[fail-loud] StockDetailService 未注入 config，无法回退 tdx 实时行情"
                )
            self._tdx_fetcher = QuotationFetcher(TdxClientFactory.from_config(self._cfg))
        return self._tdx_fetcher.fetch_kline(market, code, count=count)

    def _fetch_tdx_minute_bars(
        self, market: str, code: str, period: str, count: int
    ) -> list[Any]:
        """经 QuotationFetcher 从 tdx 实时拉分钟线；失败抛原始异常由调用方处理。

        ``period`` 为详情页周期键（min5/min15/min60）：min5 拉 1 分钟线
        （分时），min15/min60 直接拉对应周期档（无需本地聚合）。
        """
        if self._tdx_fetcher is None:
            from Kuantix.adapters.quotation import QuotationFetcher
            from Kuantix.adapters.tdx_client import TdxClientFactory

            if self._cfg is None:
                raise DataIntegrityError(
                    "[fail-loud] StockDetailService 未注入 config，无法回退 tdx 分钟行情"
                )
            self._tdx_fetcher = QuotationFetcher(TdxClientFactory.from_config(self._cfg))
        minutes = {"min5": 1, "min15": 15, "min60": 60}[period]
        return self._tdx_fetcher.fetch_minute_kline(
            market, code, period_minutes=minutes, count=count
        )

    def _security_name(self, market: str, code: str) -> str:
        """取证券名称（lake 元信息；缺失返回空串，前端兜底显示代码）。"""
        try:
            name = self._store.security_name(code, market)
            return str(name or "").strip()
        except Exception:  # noqa: BLE001 - 名称缺失不影响行情展示
            return ""

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
    ) -> dict[str, Any]:
        """取个股某周期的 K 线 + 指标 + 核心数据。

        Args:
            code: 6 位证券代码。
            market: 市场码（默认 ``CN``）。
            period: 周期键（见 :data:`PERIODS`）。
            limit: 返回 bar 上限。
            indicators: 需要计算的指标子集（默认全算）；非法名被忽略。

        Returns:
            ``{code, market, period, available, turnover_estimated,
            bars, indicators}``。
            ``bars`` 为 ``[{date, open, high, low, close, vol, amount,
            turnover}]``；``indicators`` 为各指标序列（键对应指标名）。
        """
        if period not in PERIODS:
            raise MissingKeyError(
                f"[fail-loud] 未知周期: {period!r}（支持 {PERIODS}）"
            )
        _require_code(code, market)

        if indicators is None:
            indicators = list(INDICATOR_NAMES)
        indicators = [i for i in indicators if i in INDICATOR_NAMES]

        if period in ("min5", "min15", "min60"):
            return self._detail_minute(code, market, period, limit, indicators)
        return self._detail_daily(code, market, period, limit, indicators)

    # ------------------------------------------------------------------ #
    # 日 / 周 / 月 / 年
    # ------------------------------------------------------------------ #
    def _detail_daily(
        self,
        code: str,
        market: str,
        period: str,
        limit: int,
        indicators: Sequence[str],
    ) -> dict[str, Any]:
        if period == "day":
            # 日K只需末尾 limit 根 + 指标预热（MA60/MACD(26,9)/RSI24 约需
            # 80 根历史）；SQL 反向取尾，避免全历史数千根读入内存
            bars = self._store.read_daily_bars(
                market, code, tail=int(limit) + 80
            )
        else:
            bars = self._store.read_daily_bars(market, code)
        data_source = "lake"
        # 本地数据不足（market.db 未全量同步）→ tdx 回退拉深历史：
        # day 需 limit+80 根（指标预热）；周/月/年重采样需足够历史
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
            quote=quote,
        )

    # ------------------------------------------------------------------ #
    # 分钟（5 日 1 分钟 / 15 分钟）
    # ------------------------------------------------------------------ #
    def _detail_minute(
        self,
        code: str,
        market: str,
        period: str,
        limit: int,
        indicators: Sequence[str],
    ) -> dict[str, Any]:
        # 15 分钟 / 60 分钟：按交易日聚合（60 天窗口保证足够数量）；
        # 5 日：最近 5 个自然日 1 分钟线
        today = dt.date.today()
        if period == "min15":
            start = _date_int(today - dt.timedelta(days=60))
            rows = self._store.read_minute_bars(
                market, code, start_date=start
            )
            # 聚合为 15 分钟：同日期 + 15 分钟桶
            rows = self._aggregate_minute(rows, "15min")
        elif period == "min60":
            start = _date_int(today - dt.timedelta(days=60))
            rows = self._store.read_minute_bars(
                market, code, start_date=start
            )
            # 聚合为 60 分钟：同日期 + 60 分钟桶
            rows = self._aggregate_minute(rows, "60min")
        else:  # min5（5 日 1 分钟）
            start = _date_int(today - dt.timedelta(days=5))
            rows = self._store.read_minute_bars(
                market, code, start_date=start
            )

        data_source = "lake"
        if not rows:
            # 本地无分钟数据 → tdx 实时回退（与日线回退同策略：拉到就正常
            # 展示并标注来源；拉不到才标记 unavailable，绝不编造）。
            # min5 拉 1200 根 1 分钟（≈5 个交易日）；min15/min60 直接拉
            # 对应周期档（60 天窗口），无需再走本地聚合。
            counts = {"min5": 1200, "min15": 960, "min60": 240}
            try:
                tdx_rows = self._fetch_tdx_minute_bars(
                    market, code, period, counts[period]
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
            "available": False,
            "turnover_estimated": False,
            "listing_date": self._listing_date(market, code),
            "bars": [],
            "indicators": {i: {} for i in indicators},
            "quote": self._daily_quote(market, code),
            "message": message,
        }

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
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
    ) -> dict[str, Any]:
        closes = [b["close"] for b in bars]
        highs = [b["high"] for b in bars]
        lows = [b["low"] for b in bars]
        ind: dict[str, Any] = {}
        if "ma" in indicators:
            ind["ma5"] = ma(closes, 5)
            ind["ma10"] = ma(closes, 10)
            ind["ma20"] = ma(closes, 20)
            ind["ma60"] = ma(closes, 60)
        if "macd" in indicators:
            ind["macd"] = macd(closes)
        if "kdj" in indicators:
            ind["kdj"] = kdj(highs, lows, closes)
        if "rsi" in indicators:
            ind["rsi"] = rsi(closes)

        # 换手率：日/周/月/年线才有意义（分钟线 vol 即真实成交量）。
        # 无流通股本时不编造（vol/1e6 是毫无意义的相对量），置 0 并标
        # turnover_estimated=True，由前端显示「--」，符合 fail-loud/不误导。
        turnover_estimated = False
        if period in ("day", "week", "month", "year"):
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

        bucket 支持 ``15min`` / ``60min``（其他值原样返回，不聚合）。
        桶边界按「当日累计分钟数对齐」：如 09:35 → 15min 桶 09:30、
        13:47 → 60min 桶 13:00（交易时段自然分桶，不复用小时取整）。
        """
        if bucket not in ("15min", "60min") or not rows:
            return rows
        step = 15 if bucket == "15min" else 60
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
