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

import numpy as np
import pandas as pd

from Kuantix.analysis.indicators import INDICATOR_NAMES, kdj, macd, ma, rsi
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
    返回升序、截断到最近 ``limit`` 根。
    """
    df = frame.set_index("datetime").sort_index()
    agg = {
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "vol": "sum",
        "amount": "sum",
    }
    rs = df.resample(rule).agg(agg).dropna(subset=["close"])
    rs = rs.reset_index().rename(columns={"index": "datetime"})
    # 重采样后 datetime 是 Period/Timestamp，统一为日期
    rs["datetime"] = pd.to_datetime(rs["datetime"])
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

    def _security_name(self, market: str, code: str) -> str:
        """取证券名称（lake 元信息；缺失返回空串，前端兜底显示代码）。"""
        try:
            name = self._store.security_name(code, market)
            return str(name or "").strip()
        except Exception:  # noqa: BLE001 - 名称缺失不影响行情展示
            return ""

    def _listing_date(self, market: str, code: str) -> str | None:
        """取上市日期（lake 日线首根日期；缺失返回 None，前端按默认区间处理）。

        本地无日线（如纯 tdx 回退标的）时返回 None，由前端退化到固定区间，
        不编造上市日期（fail-loud/不误导）。
        """
        try:
            bars = self._store.read_daily_bars(market, code)
            if bars:
                return str(bars[0].date)
        except Exception:  # noqa: BLE001 - 上市日期缺失不影响行情展示
            pass
        return None

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
        bars = self._store.read_daily_bars(market, code)
        data_source = "lake"
        if not bars:
            # 本地无日线 → 回退 tdx 实时拉取（顶部搜索任意代码可进详情页），
            # 成功则正常展示并标注来源；仍拿不到才 fail-loud。
            fetch_count = max(int(limit), 120)
            try:
                tdx_bars = self._fetch_tdx_bars(market, code, count=fetch_count)
            except Exception as exc:  # noqa: BLE001 - 回退失败回到原报错
                raise DataIntegrityError(
                    f"[fail-loud] {code} 无日线数据（market.db 未同步），"
                    f"且 tdx 实时回退失败：{exc}"
                ) from exc
            if not tdx_bars:
                raise DataIntegrityError(
                    f"[fail-loud] {code} 无日线数据（market.db 未同步，"
                    "tdx 实时亦无返回——请确认代码是否有效）"
                )
            bars = tdx_bars
            data_source = "tdx_realtime"
        frame = self._bars_to_frame(bars)
        # 防御上游重复写入（同一 date 多条相同记录），按日期去重保留末值
        frame = frame.drop_duplicates(subset=["datetime"], keep="last").reset_index(drop=True)
        if period == "day":
            rs = frame
        elif period == "week":
            rs = _resample_daily(frame, "W", limit)
        elif period == "month":
            rs = _resample_daily(frame, "ME", limit)
        else:  # year
            rs = _resample_daily(frame, "YE", limit)

        if limit and len(rs) > limit:
            rs = rs.tail(limit)
        rs = rs.reset_index(drop=True)
        out_bars = self._frame_to_bars(rs, market, code)
        listing_date = self._listing_date(market, code)
        return self._assemble(
            code, market, period, out_bars, indicators,
            available=True, data_source=data_source, listing_date=listing_date,
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

        if not rows:
            # 本地无分钟数据：明确标记，前端提示，不编造
            listing_date = self._listing_date(market, code)
            return {
                "code": code,
                "market": market,
                "period": period,
                "available": False,
                "turnover_estimated": False,
                "listing_date": listing_date,
                "bars": [],
                "indicators": {i: {} for i in indicators},
                "message": "本地无分钟级数据，请先执行 data sync --minute",
            }

        frame = self._minute_to_frame(rows)
        if limit and len(frame) > limit:
            frame = frame.tail(limit)
        frame = frame.reset_index(drop=True)
        out_bars = self._frame_to_bars(frame, market, code)
        listing_date = self._listing_date(market, code)
        return self._assemble(
            code, market, period, out_bars, indicators, available=True,
            listing_date=listing_date,
        )

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
        """
        if bucket not in ("15min", "60min") or not rows:
            return rows
        step = 15 if bucket == "15min" else 60
        buckets: dict[tuple[int, int], list[MinuteBar]] = {}
        for r in rows:
            hh = r.time // 100
            bucket_start = (hh // step) * step
            key = (r.date, bucket_start)
            buckets.setdefault(key, []).append(r)
        out: list[MinuteBar] = []
        for (date, hh), grp in sorted(buckets.items()):
            out.append(
                MinuteBar(
                    market=grp[0].market,
                    code=grp[0].code,
                    date=date,
                    time=hh * 100,
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
