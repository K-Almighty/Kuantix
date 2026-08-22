"""盘前分析服务（消息面 + 基本面画像 + 技术面扫描）。

流程
----
盘前调度在「开盘前（默认 08:30，Asia/Shanghai，交易日）」触发：

1. :meth:`collect_news` 调 provider 抓取 → 关键词过滤 → importance 打分
   → 写入 :class:`NewsStore`；
2. :meth:`build_fundamental_profiles` 从 factor_store 取 PE / PB / ROE /
   营收增速等因子 → 行业地位 + 公告摘要 → 生成 FundamentalProfile → 写
   入 :class:`FundamentalStore`（缺因子 → fail-loud 指引用户先做因子计算）；
3. :meth:`scan_technical` 并发 worker 从 lake 读取 260/60 根 K 线，调用
   :mod:`Kuantix.adapters.indicator_bridge` 计算 MACD / RSI / KDJ / BOLL /
   支撑压力位 / 趋势 → 生成技术信号；
4. :meth:`run_report` 把以上三个子模块聚合成 :class:`PreOpenReport`。
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import random
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterable

from Kuantix.adapters.indicator_bridge import IndicatorBridge, compute_boll, compute_kdj, compute_macd, compute_rsi
from Kuantix.adapters.news_provider import NewsProvider
from Kuantix.analysis.stores import FundamentalStore, NewsStore
from Kuantix.config import AnalysisConfig, Config
from Kuantix.core import contracts as C
from Kuantix.core.fail_loud import (
    DataIntegrityError,
    NotSupportedError,
    require_known,
)
from Kuantix.core.market import known_markets

logger = logging.getLogger(__name__)

__all__ = ["PreOpenService"]


#: Security 表中的 A 股股票类型（与市场枚举保持口径一致，作为 list_securities
#: 的过滤）。若上游 security_type 命名漂移，需在这里同步扩展，但默认兜底为
#: 「没有类型过滤就全拿，然后代码 6 位数字过滤」。
_CN_STOCK_SECURITY_TYPES: tuple[str, ...] = (
    "SH_A_STOCK", "SZ_A_STOCK", "BJ_A_STOCK",
)


def _grade_profile(
    *,
    market_cap: float,
    pe: float | None,
    roe: float | None,
    net_profit_growth: float | None,
    debt_ratio: float | None,
    dividend_yield: float | None,
) -> C.FundamentalGrade:
    """简单 5 维加权评分 → A/B/C/D（缺因子就降档，避免静默默认）。

    分数（满分 100）：
      - 市场规模（越大越稳）：>=1e12 → 25；>=5e11 → 18；>=1e11 → 12；否则 5。
      - PE（估值）：None → 0；0<PE≤20 → 20；20<PE≤40 → 12；PE>80 或 PE<0 → 2；else 8。
      - ROE：None → 0；≥15% → 25；10-15% → 18；5-10% → 10；<0 → 2；else 5。
      - 净利润增速：None → 0；≥30% → 20；10-30% → 14；0-10% → 6；<0 → 2；else 3。
      - 股息率：None → 0；≥4% → 10；2-4% → 6；0-2% → 2；else 0。
    """
    score = 0
    # 市场规模
    if market_cap >= 1e12:
        score += 25
    elif market_cap >= 5e11:
        score += 18
    elif market_cap >= 1e11:
        score += 12
    else:
        score += 5
    # PE
    if pe is None or pe != pe:  # NaN
        score += 0
    elif 0 < pe <= 20:
        score += 20
    elif 20 < pe <= 40:
        score += 12
    elif pe > 80 or pe < 0:
        score += 2
    else:
        score += 8
    # ROE
    if roe is None:
        score += 0
    elif roe >= 0.15:
        score += 25
    elif roe >= 0.10:
        score += 18
    elif roe >= 0.05:
        score += 10
    elif roe < 0:
        score += 2
    else:
        score += 5
    # 净利润增速
    if net_profit_growth is None:
        score += 0
    elif net_profit_growth >= 0.30:
        score += 20
    elif net_profit_growth >= 0.10:
        score += 14
    elif net_profit_growth >= 0:
        score += 6
    else:
        score += 2
    # 股息率
    if dividend_yield is None:
        score += 0
    elif dividend_yield >= 0.04:
        score += 10
    elif dividend_yield >= 0.02:
        score += 6
    elif dividend_yield > 0:
        score += 2
    if score >= 80:
        return C.FundamentalGrade.A
    if score >= 60:
        return C.FundamentalGrade.B
    if score >= 40:
        return C.FundamentalGrade.C
    return C.FundamentalGrade.D


def _factor_map(
    factor_store: Any,
    *,
    date_int: int,
    codes: Iterable[str],
) -> dict[str, dict[str, float]]:
    """读取截面（每码取最新），返回 {code: {factor_name: value}}。

    没有对应因子（不存在 / 没计算）对应键缺失；调用方按缺值判定 fail-loud。
    """
    frames: dict[str, Any] = {}
    for name in ("pe_ttm", "pb", "roe_ttm", "revenue_yoy", "netprofit_yoy",
                 "debt_ratio", "dividend_yield"):
        try:
            df = factor_store.load_latest_per_code(name, as_of=date_int)
        except DataIntegrityError:
            # 因子不存在 → 跳过，不抛
            continue
        if df is None or len(df) == 0:
            continue
        frames[name] = df
    code_set = set(str(c) for c in codes)
    result: dict[str, dict[str, float]] = {}
    for name, df in frames.items():
        for _, row in df.iterrows():
            code = str(row["code"])
            if code not in code_set:
                continue
            val = float(row["value"])
            if val != val:  # NaN 跳过（不写入，None 语义）
                continue
            result.setdefault(code, {})[name] = val
    return result


class PreOpenService:
    """盘前分析门面（单市场构造，线程安全）。"""

    def __init__(
        self,
        config: Config,
        *,
        lake: Any,
        factor_service: Any,
        news_store: NewsStore,
        fundamental_store: FundamentalStore,
        news_provider: NewsProvider,
        monitor_store: Any | None = None,
    ) -> None:
        self._cfg: Config = config
        self._analysis_cfg: AnalysisConfig = config.analysis
        self._lake = lake
        self._factor_service = factor_service
        self._news_store = news_store
        self._fundamental_store = fundamental_store
        self._news_provider = news_provider
        self._monitor_store = monitor_store
        self._tdx_fetcher: Any | None = None
        self._tdx_lock = threading.Lock()

    # -------------------------------------------------------------- #
    # tdx 实时行情（优先/补全数据源）
    # -------------------------------------------------------------- #

    def _get_tdx_fetcher(self) -> Any:
        """懒加载 QuotationFetcher（线程安全单例；不可用时抛错由调用方降级）。"""
        if self._tdx_fetcher is None:
            with self._tdx_lock:
                if self._tdx_fetcher is None:
                    from Kuantix.adapters.quotation import QuotationFetcher
                    from Kuantix.adapters.tdx_client import TdxClientFactory

                    self._tdx_fetcher = QuotationFetcher(
                        TdxClientFactory.from_config(self._cfg)
                    )
        return self._tdx_fetcher

    def fetch_tdx_daily_bars(
        self, market: str, code: str, count: int = 260
    ) -> list[Any]:
        """从 tdx 实时拉取日线（时间升序 Bar 列表）；失败时抛原始异常。

        供技术扫描 / 自选 PnL / 盘后亮点复用；调用方自行 try/except 降级
        到 lake（fail-loud 精神：来源如实标注，不静默编造）。
        """
        return self._get_tdx_fetcher().fetch_kline(market, code, count=count)

    def _tdx_latest_date(self, market: str) -> dt.date | None:
        """探测 tdx 实时行情的最新交易日（基准标的 000001，仅 5 根）。

        tdx 不可达 / 异常 → ``None``（调用方降级为 lake-only，不阻塞扫描）。
        """
        try:
            bars = self.fetch_tdx_daily_bars(market, "000001", count=5)
            if bars:
                return bars[-1].date
        except Exception as exc:  # noqa: BLE001 - 探测失败不致命
            logger.warning(
                "[PreOpenService] tdx 实时探测失败（降级 lake-only）：%s", exc
            )
        return None

    def _security_names(self, market: str) -> dict[str, str]:
        """取 {code: name} 名称映射（lake 元信息；缺失的 code 不入映射）。"""
        try:
            securities = self._lake.store.list_securities(market)
        except Exception:  # noqa: BLE001 - 名称缺失不阻塞扫描
            return {}
        return {
            str(s.code).strip(): str(getattr(s, "name", "") or "").strip()
            for s in securities
            if str(getattr(s, "code", "") or "").strip()
        }

    # -------------------------------------------------------------- #
    # 自选 & 代码池
    # -------------------------------------------------------------- #

    def _watchlist_codes(self, market: str) -> list[str]:
        store = self._monitor_store
        if store is None:
            return []
        items = store.list_watch(market=market)
        return [str(i.code) for i in items]

    def _all_stock_codes(self, market: str) -> list[str]:
        """从 lake.store 的 securities 表取股票（A 股类型过滤 + 代码 6 位数字兜底）。"""
        sec_types = list(_CN_STOCK_SECURITY_TYPES) if str(market).upper() == "CN" else None
        try:
            securities = self._lake.store.list_securities(market, security_types=sec_types)
        except AttributeError as exc:  # 没有 list_securities → fail-loud 指引
            raise DataIntegrityError(
                "[fail-loud/NF-26] PreOpenService 要求 Lake 暴露 .store.list_securities，"
                f"当前 lake.store 没有：{exc}"
            ) from exc
        codes: list[str] = []
        for s in securities:
            code = str(s.code).strip()
            if str(market).upper() == "CN":
                if len(code) != 6 or not code.isdigit():
                    continue
            codes.append(code)
        return list(dict.fromkeys(codes))

    def _expand_codes(self, market: str, codes: Iterable[str] | None) -> tuple[list[str], list[str]]:
        """返回 (watchlist_codes, scan_sample_codes)；scan_sample = watchlist ∪ 全量随机抽样。"""
        watch = self._watchlist_codes(market)
        watch_set = set(watch)
        explicit = [str(c).strip() for c in (codes or []) if str(c).strip()]
        if explicit:
            # 用户显式传 codes → 不做抽样，全量扫描
            return sorted(set(watch + explicit)), []
        all_codes = self._all_stock_codes(market)
        all_codes = [c for c in all_codes if c not in watch_set]
        sample_size = int(self._analysis_cfg.scan_sample_size)
        if sample_size <= 0:
            return watch, all_codes
        needed = max(0, sample_size - len(watch))
        if needed >= len(all_codes):
            return watch, all_codes
        rng = random.Random(hash((market, sample_size)) % (2**32))
        return watch, rng.sample(all_codes, needed)

    # -------------------------------------------------------------- #
    # 消息面收集
    # -------------------------------------------------------------- #

    def collect_news(
        self,
        market: str,
        date: dt.date,
        *,
        keywords: Iterable[str] | None = None,
    ) -> list[C.NewsItem]:
        market_code = require_known(str(market).upper(), "market", allowed=set(known_markets()))
        items = self._news_provider.fetch(market_code, date, keywords=keywords)
        if items:
            self._news_store.upsert(market_code, date, items)
        return items

    # -------------------------------------------------------------- #
    # 基本面画像
    # -------------------------------------------------------------- #

    def build_fundamental_profiles(
        self,
        market: str,
        codes: Iterable[str],
        *,
        date: dt.date | None = None,
    ) -> list[C.FundamentalProfile]:
        """基于 factor_store 截面 + lake 的证券信息构造基本面画像。

        Raises:
            DataIntegrityError: 任一标的 market_cap / name / sector 三个必填字段缺。
        """
        target_day = date if date is not None else dt.date.today()
        market_code = require_known(str(market).upper(), "market", allowed=set(known_markets()))
        code_list = [str(c).strip() for c in codes if str(c).strip()]
        if not code_list:
            return []
        store = self._lake.store
        securities = store.list_securities(market_code)
        sec_map: dict[str, Any] = {str(s.code): s for s in securities}
        factor_store = getattr(self._factor_service, "store", None)
        if factor_store is None:
            raise NotSupportedError(
                "[fail-loud/NF-26] PreOpenService.build_fundamental_profiles"
                " 需要 factor_service.store。请先执行 `Kuantix factor compute`（README 量化工作流），"
                "再运行基本面画像。"
            )
        date_int = target_day.year * 10000 + target_day.month * 100 + target_day.day
        values_by_code = _factor_map(factor_store, date_int=date_int, codes=code_list)

        profiles: list[C.FundamentalProfile] = []
        for code in code_list:
            sec = sec_map.get(code)
            if sec is None:
                raise DataIntegrityError(
                    f"[fail-loud/NF-26] 基本面画像缺失证券元信息: {market_code}/{code}"
                    "（请先 `Kuantix securities update`）"
                )
            name = str(getattr(sec, "name", "") or "").strip()
            sector = str(getattr(sec, "sector", "") or "").strip()
            industry = str(getattr(sec, "industry", "") or "").strip()
            if not name or not sector:
                raise DataIntegrityError(
                    f"[fail-loud/NF-26] {market_code}/{code} 的 name/sector 为空，请补充 securities 元信息"
                )
            vals = values_by_code.get(code, {})
            pe = vals.get("pe_ttm")
            pb = vals.get("pb")
            roe = vals.get("roe_ttm")
            revenue_growth = vals.get("revenue_yoy")
            net_profit_growth = vals.get("netprofit_yoy")
            debt_ratio = vals.get("debt_ratio")
            dividend_yield = vals.get("dividend_yield")
            # market_cap：若因子库有 cap 因子使用之，否则兜底用 close * 总股本 = close
            # * 1（A股通常 1 = 1 股，股本缺失时退化成 NaN，这里做 fail-loud）。
            # 策略：用 lake 读最近 close，乘以 universe.security 的股本（若有），
            # 没有股本 → fail-loud 指引。
            bars = store.read_daily_bars(market_code, code, tail=1)
            if not bars:
                raise DataIntegrityError(
                    f"[fail-loud/NF-26] {market_code}/{code} 湖内无日线（无法计算市值）。"
                    "请先 `Kuantix data sync`。"
                )
            last_close = float(bars[-1].close)
            shares = getattr(sec, "total_shares", None)
            if shares is None:
                shares = getattr(sec, "shares", None)
            if shares is None or float(shares) <= 0:
                raise DataIntegrityError(
                    f"[fail-loud/NF-26] {market_code}/{code} 缺少 total_shares（securities 元信息）。"
                    "请先 `Kuantix securities update` 并保证字段齐全。"
                )
            market_cap = last_close * float(shares)
            grade = _grade_profile(
                market_cap=market_cap, pe=pe, roe=roe,
                net_profit_growth=net_profit_growth, debt_ratio=debt_ratio,
                dividend_yield=dividend_yield,
            )
            summary: list[str] = []
            if revenue_growth is not None:
                summary.append(f"营收同比 {revenue_growth*100:+.2f}%")
            if net_profit_growth is not None:
                summary.append(f"净利润同比 {net_profit_growth*100:+.2f}%")
            if roe is not None:
                summary.append(f"ROE(TTM) {roe*100:.2f}%")
            if pe is not None:
                summary.append(f"PE(TTM) {pe:.2f}")
            if pb is not None:
                summary.append(f"PB {pb:.2f}")
            if dividend_yield is not None:
                summary.append(f"股息率 {dividend_yield*100:.2f}%")
            if debt_ratio is not None:
                summary.append(f"资产负债率 {debt_ratio*100:.2f}%")
            latest_announcements: tuple[str, ...] = ()  # T0 先空；T5 后续接入公告 provider 可补
            profiles.append(C.FundamentalProfile(
                code=code, name=name, market=market_code, sector=sector, industry=industry,
                market_cap=market_cap, pe=pe, pb=pb, roe=roe,
                revenue_growth=revenue_growth, net_profit_growth=net_profit_growth,
                debt_ratio=debt_ratio, dividend_yield=dividend_yield,
                latest_announcements=latest_announcements, grade=grade,
                summary_lines=tuple(summary),
            ))
        self._fundamental_store.upsert(market_code, target_day, profiles)
        return profiles

    # -------------------------------------------------------------- #
    # 技术面扫描
    # -------------------------------------------------------------- #

    def _technical_signals(
        self,
        *,
        closes: list[float],
        dif: list[float], dea: list[float], hist: list[float],
        rsi: list[float],
        k: list[float], d: list[float], j: list[float],
        upper: list[float], mid: list[float], lower: list[float],
        ma_values: dict[int, float | None],
    ) -> list[str]:
        """计算信号字典命中（字符串集合，按指标顺序拼接）。"""
        signals: list[str] = []
        if len(hist) >= 2 and not (math.isnan(hist[-2]) or math.isnan(hist[-1])):
            if hist[-2] <= 0 < hist[-1]:
                signals.append("MACD金叉")
            elif hist[-2] >= 0 > hist[-1]:
                signals.append("MACD死叉")
        if len(rsi) >= 1:
            last_rsi = rsi[-1]
            if not math.isnan(last_rsi):
                if last_rsi >= 80:
                    signals.append("RSI超买")
                elif last_rsi <= 20:
                    signals.append("RSI超卖")
        if len(j) >= 1:
            last_j = j[-1]
            if not math.isnan(last_j):
                if last_j > 100:
                    signals.append("KDJ超买")
                elif last_j < 0:
                    signals.append("KDJ超卖")
        if len(closes) >= 1 and len(upper) == len(closes) == len(lower):
            close_last = closes[-1]
            upper_last = upper[-1]
            lower_last = lower[-1]
            if not math.isnan(upper_last) and not math.isnan(lower_last):
                if close_last > upper_last:
                    signals.append("突破布林上轨")
                elif close_last < lower_last:
                    signals.append("跌破布林下轨")
        for period in (20, 60):
            ma = ma_values.get(period)
            if ma is None:
                continue
            close_last = closes[-1]
            if len(closes) >= 2:
                prev_close = closes[-2]
                # 回踩 MA 支撑：prev 接近或跌破 MA，今日反弹在上方
                if prev_close <= ma <= close_last:
                    signals.append(f"回踩MA{period}获支撑")
        # 趋势切换：MACD 柱子由负转正 / 柱子由正转负的额外描述（上面已做）。
        return signals

    def _scan_one(
        self,
        market: str,
        code: str,
        *,
        tdx_latest: dt.date | None = None,
        names: dict[str, str] | None = None,
    ) -> C.TechnicalAnalysis:
        store = self._lake.store
        bars = store.read_daily_bars(market, code, tail=260)
        data_source = "lake"
        # 数据源优先级：tdx 实时 > lake。
        # 触发 tdx 补全的条件：lake 无该标的日线，或 lake 落后于 tdx 最新
        # 交易日（个股停牌等合法滞后由 fetch 结果自然兜底，不额外判断）。
        stale = (not bars) or (
            tdx_latest is not None and bars[-1].date < tdx_latest
        )
        if stale and tdx_latest is not None:
            try:
                tdx_bars = self.fetch_tdx_daily_bars(market, code, count=260)
                if tdx_bars:
                    bars = tdx_bars
                    data_source = "tdx_realtime"
                    logger.info(
                        "[PreOpenService] %s/%s 使用 tdx 实时日线（lake 缺失/滞后）",
                        market, code,
                    )
            except Exception as exc:  # noqa: BLE001 - tdx 失败降级 lake
                logger.warning(
                    "[PreOpenService] %s/%s tdx 实时拉取失败，降级 lake：%s",
                    market, code, exc,
                )
        if not bars:
            raise DataIntegrityError(
                f"[fail-loud/NF-26] TechnicalAnalysis 缺少日线: {market}/{code}"
                "（lake 与 tdx 实时均无数据）"
            )
        name = (names or {}).get(code)
        name = name.strip() if isinstance(name, str) else ""
        closes = [float(b.close) for b in bars]
        highs = [float(b.high) for b in bars]
        lows = [float(b.low) for b in bars]
        last_date = bars[-1].date
        # MA 所有周期
        ma_values: dict[int, float | None] = {}
        for p in (5, 10, 20, 60, 120, 250):
            if len(closes) >= p:
                try:
                    ma_values[p] = float(IndicatorBridge.sma(closes[-p:], p))
                except DataIntegrityError:
                    ma_values[p] = None
            else:
                ma_values[p] = None
        # 指标（样本不足时，指标返回 None 数组或 None）
        macd_last = {"dif": None, "dea": None, "hist": None}
        try:
            dif, dea, hist = compute_macd(closes)
            macd_last = {
                "dif": None if math.isnan(dif[-1]) else float(dif[-1]),
                "dea": None if math.isnan(dea[-1]) else float(dea[-1]),
                "hist": None if math.isnan(hist[-1]) else float(hist[-1]),
            }
        except DataIntegrityError:
            dif, dea, hist = [], [], []
        rsi_last = None
        rsi_seq: list[float] = []
        try:
            rsi_seq = compute_rsi(closes)
            rsi_last = None if math.isnan(rsi_seq[-1]) else float(rsi_seq[-1])
        except DataIntegrityError:
            rsi_seq = []
        kdj_last = {"k": None, "d": None, "j": None}
        k_seq = d_seq = j_seq = []
        try:
            k_seq, d_seq, j_seq = compute_kdj(closes, highs, lows)
            def _lv(xs):
                v = xs[-1]
                return None if math.isnan(v) else float(v)
            kdj_last = {"k": _lv(k_seq), "d": _lv(d_seq), "j": _lv(j_seq)}
        except DataIntegrityError:
            pass
        boll_last = {"upper": None, "mid": None, "lower": None}
        upper_seq = mid_seq = lower_seq = []
        try:
            upper_seq, mid_seq, lower_seq = compute_boll(closes)
            def _lv2(xs):
                v = xs[-1]
                return None if math.isnan(v) else float(v)
            boll_last = {"upper": _lv2(upper_seq), "mid": _lv2(mid_seq), "lower": _lv2(lower_seq)}
        except DataIntegrityError:
            pass
        # 趋势 + 支撑压力
        try:
            direction, strength = IndicatorBridge.trend(closes, short=20, long_period=60)
        except DataIntegrityError:
            direction, strength = C.TrendDirection.FLAT, 0.0
        supports: list[float] = []
        resistances: list[float] = []
        try:
            supports, resistances = IndicatorBridge.support_resistance(
                closes, highs, lows, lookback=60, window=5,
            )
        except DataIntegrityError:
            supports, resistances = [], []
        signals = self._technical_signals(
            closes=closes,
            dif=dif, dea=dea, hist=hist, rsi=rsi_seq,
            k=k_seq, d=d_seq, j=j_seq,
            upper=upper_seq, mid=mid_seq, lower=lower_seq,
            ma_values=ma_values,
        )
        return C.TechnicalAnalysis(
            code=code, last_date=last_date,
            ma5=ma_values[5], ma10=ma_values[10], ma20=ma_values[20],
            ma60=ma_values[60], ma120=ma_values[120], ma250=ma_values[250],
            macd_dif_last=macd_last["dif"], macd_dea_last=macd_last["dea"],
            macd_hist_last=macd_last["hist"], rsi_last=rsi_last,
            kdj_k_last=kdj_last["k"], kdj_d_last=kdj_last["d"], kdj_j_last=kdj_last["j"],
            boll_upper_last=boll_last["upper"], boll_mid_last=boll_last["mid"],
            boll_lower_last=boll_last["lower"],
            trend_direction=direction, trend_strength=float(strength),
            support_levels=tuple(float(s) for s in supports),
            resistance_levels=tuple(float(r) for r in resistances),
            signals=tuple(signals),
            name=name,
            data_source=data_source,
        )

    def scan_technical(
        self,
        market: str,
        codes: Iterable[str],
        *,
        workers: int | None = None,
    ) -> list[C.TechnicalAnalysis]:
        """并发技术扫描；workers 默认配置 scan_workers（1-64）。

        数据源策略（与报告口径一致）：**tdx 实时优先**——扫描前探测 tdx
        最新交易日 ``tdx_latest``；lake 无数据或落后于 ``tdx_latest`` 的
        个股改用 tdx 实时日线；tdx 不可达时整体降级 lake-only（如实标注
        ``data_source``，不静默编造）。
        """
        market_code = require_known(str(market).upper(), "market", allowed=set(known_markets()))
        code_list = [str(c).strip() for c in codes if str(c).strip()]
        if not code_list:
            return []
        w = int(workers) if workers is not None else int(self._analysis_cfg.scan_workers)
        if w <= 0:
            w = 1
        tdx_latest = self._tdx_latest_date(market_code)
        names = self._security_names(market_code)
        results: list[C.TechnicalAnalysis] = []
        if w == 1 or len(code_list) == 1:
            for code in code_list:
                results.append(
                    self._scan_one(
                        market_code, code, tdx_latest=tdx_latest, names=names
                    )
                )
            return results
        with ThreadPoolExecutor(max_workers=w) as pool:
            futures = {
                pool.submit(
                    self._scan_one, market_code, c,
                    tdx_latest=tdx_latest, names=names,
                ): c
                for c in code_list
            }
            for fut in as_completed(futures):
                code = futures[fut]
                try:
                    results.append(fut.result())
                except Exception as exc:  # noqa: BLE001 - 单标的错误不拖垮整批
                    logger.warning(
                        "[PreOpenService.scan_technical] %s/%s 技术扫描失败: %s: %s",
                        market_code, code, type(exc).__name__, exc,
                    )
        # 按 code 稳定排序，避免前端渲染抖动
        results.sort(key=lambda t: t.code)
        return results

    # -------------------------------------------------------------- #
    # 报告聚合
    # -------------------------------------------------------------- #

    def run_report(
        self,
        market: str,
        date: dt.date | None = None,
        *,
        codes: Iterable[str] | None = None,
    ) -> C.PreOpenReport:
        market_code = require_known(str(market).upper(), "market", allowed=set(known_markets()))
        target_day = date if date is not None else dt.date.today()
        watch, sample = self._expand_codes(market_code, codes)
        scan_codes = list(dict.fromkeys(watch + sample))
        logger.info(
            "[PreOpenService.run_report] market=%s date=%s watch=%d sample=%d total=%d",
            market_code, target_day, len(watch), len(sample), len(scan_codes),
        )

        # 1) 消息面收集
        news_items = self.collect_news(market_code, target_day)
        by_cat: dict[str, int] = {}
        for ni in news_items:
            by_cat[ni.category.value] = by_cat.get(ni.category.value, 0) + 1
        top_news = sorted(
            news_items,
            key=lambda x: (-int(x.importance), x.publish_ts),
        )[:20]
        news_summary = {
            "total": int(len(news_items)),
            "by_category": [{"category": k, "count": v} for k, v in sorted(by_cat.items())],
            "top_news": [n.to_dict() for n in top_news],
        }

        # 2) 自选基本面画像
        watch_profiles: list[dict[str, Any]] = []
        if watch:
            profiles = self.build_fundamental_profiles(market_code, watch, date=target_day)
            # 按 grade 升序 + market_cap 降序
            profiles.sort(
                key=lambda p: (p.grade.value, -float(p.market_cap))
            )
            watch_profiles = [p.to_dict() for p in profiles]

        # 3) 扫描技术面，取 Top 30（按信号数 + 强度排序）
        scan_tech = self.scan_technical(market_code, scan_codes)
        scan_tech.sort(
            key=lambda t: (-len(t.signals), -float(t.trend_strength), t.code)
        )
        top_scan = [t.to_dict() for t in scan_tech[:30]]

        return C.PreOpenReport(
            date=target_day, market=market_code,
            generated_at=dt.datetime.now().astimezone(),
            news_feed_summary=news_summary,
            watchlist_profiles=tuple(watch_profiles),
            broad_market_scan_top=tuple(top_scan),
        )
