"""盘后复盘模块（涨跌停分类 + 技术亮点 + 等待收盘数据）。

流程
----
盘后调度在「收盘后（默认 15:20，Asia/Shanghai，交易日）」触发：

1. :meth:`PostCloseService._wait_until_data_ready` 轮询 DataLake 是否已有
   当日收盘 L1；缺数据时每 60s 再查，最晚等至 cfg.analysis.post_close_wait_until
   （默认 16:00），仍无 → ``NotSupportedError`` fail-loud。
2. :meth:`PostCloseService.run_limit_analysis` 取全市场当日日线 →
   :class:`LimitClassifier` 逐只判定涨停/跌停 → 行业 + 类型汇总 →
   写入 ``LimitUpDownStore``。
3. :meth:`PostCloseService._scan_tech_highlights` 复用
   :class:`PreOpenService.scan_technical` 抽取今日 signals + Top10
   亮点（按信号数 + 强度排序）。
4. :meth:`PostCloseService.run_report` 把以上结果聚合成
   :class:`PostCloseReport`。
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import time
from collections import defaultdict
from typing import Any, Iterable

from Kuantix.adapters.indicator_bridge import IndicatorBridge, compute_boll
from Kuantix.analysis.pre_open import PreOpenService, _factor_map
from Kuantix.analysis.stores import LimitUpDownStore
from Kuantix.config import Config
from Kuantix.core import contracts as C
from Kuantix.core.fail_loud import (
    DataIntegrityError,
    NotSupportedError,
    require_known,
)
from Kuantix.core.market import known_markets

logger = logging.getLogger(__name__)

__all__ = ["LimitClassifier", "PostCloseService"]


#: A 股涨跌停阈值（比例）：主板 10%，科创板/创业板 20%，ST 5%。
#: T0 版本采用宽松阈值 ``change_pct >= 0.098`` 即视为涨停，避免不同板块
#: 阈值拆分的元信息依赖（后续可按 exchange + ST 名称拆分）。
_LIMIT_UP_THRESHOLD: float = 0.098
_LIMIT_DOWN_THRESHOLD: float = -0.098

#: 新股判定：距上市日 <= 60 个自然日
_NEW_LISTING_DAYS: int = 60

#: ST 摘帽回看窗口（自然日）
_ST_HISTORY_WINDOW: int = 30

#: 业绩驱动增速阈值（净利润 / 营收同比 >= 30%）
_EARNINGS_THRESHOLD: float = 0.30


class LimitClassifier:
    """规则化涨停板类型分类器（主类型按优先级严格取最早命中）。

    规则（spec §Task 7）：

    1. 新股上市：``list_date``（首根 K 线日期兜底）距今 ≤ 60 天；
    2. ST 摘帽：近 30 日名称历史含 "ST" 但当日不含（T0 缺名称历史 → 降级
       为 "unknown"，计入 reasons「名称数据缺失」，不与其他主类型混淆，
       最终主类型走后续规则；若后续也未命中 → 归「其他」，不抢业绩/概念）；
    3. 连板天数：回溯 close.pct_change ≥ 0.095 连续日数，≥ 2 时在 reasons
       追加「N 连板」；
    4. 业绩驱动：近 1 季度净利润 / 营收同比 ≥ 30%；
    5. 技术突破：当日 close 同时突破 BOLL upper + MA60；
    6. 概念炒作：连板 ≥ 2 且未命中 3/4/5 → 概念炒作（兜底概念数据未启用，
       reasons 追加「概念关键词未启用，按连板兜底判定」）；
    7. 其他：都没命中。
    """

    def __init__(self, config: Config, *, lake: Any, factor_service: Any | None = None) -> None:
        self._cfg = config
        self._lake = lake
        self._factor_service = factor_service

    # ------------------------------------------------------------------
    # 单指标判定（每步都尽量 fail-loud，避免静默瞎猜）
    # ------------------------------------------------------------------

    def _list_date(self, market: str, code: str, sec: Any) -> dt.date | None:
        """取上市日期：优先 Security.list_date，其次 lake 首根 K 线日期。"""
        explicit = getattr(sec, "list_date", None)
        if isinstance(explicit, dt.date):
            return explicit
        if isinstance(explicit, str) and explicit.strip():
            try:
                return dt.date.fromisoformat(explicit.strip())
            except ValueError:
                pass
        # 兜底：首根 K 线 date
        store = self._lake.store
        bars = store.read_daily_bars(market, code, tail=None)
        if not bars:
            return None
        first = bars[0]
        if isinstance(first, dt.date):
            return first
        d = getattr(first, "date", None)
        if isinstance(d, dt.date):
            return d
        return None

    def _continuous_up_days(self, bars: list[Any]) -> int:
        """从最后一根向前数 change_pct >= 9.5% 的连续日数。"""
        if not bars:
            return 1
        count = 0
        for b in reversed(bars):
            prev = getattr(b, "prev_close", None)
            close = float(getattr(b, "close", 0.0))
            if prev is None:
                break
            change = (close / float(prev)) - 1.0
            if change >= 0.095:
                count += 1
                continue
            break
        return max(1, count)

    def _st_removal_check(self, sec: Any, target_day: dt.date) -> tuple[bool, list[str]]:
        """ST 摘帽判定。T0 只有当日名称，缺历史 → (False, ['名称数据缺失'])。"""
        current_name = str(getattr(sec, "name", "") or "")
        has_st_now = "ST" in current_name or "st" in current_name
        # names 历史：扩展点（sec.names 或独立 name_store）
        names_hist = getattr(sec, "names", None)
        if names_hist is None or not hasattr(names_hist, "__iter__"):
            # P0 缺历史 → 规则降级，reasons 标注，但**不判定为摘帽**（避免错判）
            return False, ["名称历史数据缺失，ST摘帽规则降级未判定"]
        # 近 30 日窗口是否存在 ST 名称
        cut = target_day - dt.timedelta(days=_ST_HISTORY_WINDOW)
        had_st = False
        for record in names_hist:
            # record: {name, start_date, end_date} 或 (name, date)
            if isinstance(record, dict):
                name = str(record.get("name", "") or "")
                end_s = record.get("end_date")
                end_d = dt.date.fromisoformat(end_s) if isinstance(end_s, str) else None
                if end_d and end_d >= cut:
                    if "ST" in name or "st" in name:
                        had_st = True
            elif isinstance(record, (tuple, list)) and len(record) >= 2:
                name = str(record[0] or "")
                d_s = record[1]
                d_d = dt.date.fromisoformat(d_s) if isinstance(d_s, str) else None
                if d_d and d_d >= cut:
                    if "ST" in name or "st" in name:
                        had_st = True
        if had_st and not has_st_now:
            return True, ["近30日ST摘帽"]
        return False, []

    def _earnings_signal(self, market: str, code: str, target_day: dt.date) -> tuple[bool, list[str]]:
        """近 1 季度 净利润 / 营收 同比 ≥ 30%。"""
        if self._factor_service is None:
            return False, ["因子服务未注入，业绩驱动规则未判定"]
        factor_store = getattr(self._factor_service, "store", None)
        if factor_store is None:
            return False, ["因子服务无 store 属性，业绩驱动规则未判定"]
        date_int = target_day.year * 10000 + target_day.month * 100 + target_day.day
        values_by_code = _factor_map(factor_store, date_int=date_int, codes=[code])
        vals = values_by_code.get(code, {})
        np_g = vals.get("netprofit_yoy")
        rev_g = vals.get("revenue_yoy")
        reasons: list[str] = []
        ok = False
        if np_g is not None and float(np_g) >= _EARNINGS_THRESHOLD:
            reasons.append(f"净利润同比{float(np_g)*100:+.2f}%")
            ok = True
        if rev_g is not None and float(rev_g) >= _EARNINGS_THRESHOLD:
            reasons.append(f"营收同比{float(rev_g)*100:+.2f}%")
            ok = True
        return ok, reasons

    def _tech_breakout(self, bars: list[Any]) -> tuple[bool, list[str]]:
        """当日 close 同时突破 BOLL upper + MA60。"""
        if len(bars) < 60:
            return False, ["日线样本不足60根，技术突破规则未判定"]
        closes = [float(b.close) for b in bars]
        close_last = closes[-1]
        # MA60
        try:
            ma60 = float(IndicatorBridge.sma(closes[-60:], 60))
        except DataIntegrityError:
            return False, ["MA60计算失败，技术突破规则未判定"]
        if math.isnan(ma60):
            return False, ["MA60为NaN，技术突破规则未判定"]
        # BOLL(20, 2)
        try:
            upper, _mid, _lower = compute_boll(closes)
        except DataIntegrityError:
            return False, ["BOLL计算失败，技术突破规则未判定"]
        if not upper:
            return False, []
        upper_last = float(upper[-1])
        if math.isnan(upper_last):
            return False, []
        if close_last > upper_last and close_last > ma60:
            return True, [f"收盘价突破BOLL上轨({upper_last:.2f})+MA60({ma60:.2f})"]
        return False, []

    def _concept_hype(self, sec: Any, *, continuous_days: int, code: str) -> tuple[bool, list[str]]:
        """概念炒作：config.analysis.keywords 若提供 → 匹配；否则若连板≥2 兜底。"""
        ana_cfg = self._cfg.analysis
        concepts: dict[str, Any] = getattr(ana_cfg, "keywords", None) or {}
        if not isinstance(concepts, dict) or not concepts:
            # 关键词未启用 → 按连板兜底
            if continuous_days >= 2:
                return True, [
                    f"{continuous_days}连板",
                    "概念关键词未启用，按连板兜底判定为概念炒作",
                ]
            return False, []
        reasons: list[str] = []
        matched = False
        for concept_name, codes_or_keywords in concepts.items():
            if isinstance(codes_or_keywords, (list, tuple, set)):
                if any(str(x).strip() == code for x in codes_or_keywords):
                    matched = True
                    reasons.append(f"命中概念[{concept_name}]")
            elif isinstance(codes_or_keywords, dict):
                c_list = codes_or_keywords.get("codes") or []
                if any(str(x).strip() == code for x in c_list):
                    matched = True
                    reasons.append(f"命中概念[{concept_name}]")
        if matched and continuous_days >= 2:
            reasons.append(f"{continuous_days}连板")
        if matched:
            return True, reasons
        if continuous_days >= 2:
            return True, [
                f"{continuous_days}连板",
                "概念关键词未命中，按连板兜底判定为概念炒作",
            ]
        return False, []

    # ------------------------------------------------------------------
    # 总入口
    # ------------------------------------------------------------------

    def classify(
        self,
        market: str,
        code: str,
        target_day: dt.date,
        *,
        limit_side: str,  # 'up' | 'down'
    ) -> C.LimitEntry:
        """对一只标的做涨停板类型判定。

        Args:
            market: 市场代码（CN）。
            code: 证券代码。
            target_day: 判定基准日（当日）。
            limit_side: 'up' 表示涨停，'down' 表示跌停（跌停只做分类但不触发
                「业绩驱动 / 技术突破 / 概念炒作」逻辑，主类型退化为「新股上市」
                /「ST摘帽」/「其他」）。

        Returns:
            :class:`LimitEntry`（填充 code/name/sector/limit_type/close/
            change_pct/volume_ratio/continuous_days/reasons）。
        """
        require_known(str(market).upper(), "market", allowed=set(known_markets()))
        if str(limit_side).lower() not in {"up", "down"}:
            raise DataIntegrityError(
                f"[fail-loud/NF-26] LimitClassifier.classify 要求 limit_side∈"
                "{'up','down'}，实际 {limit_side!r}"
            )
        store = self._lake.store
        # 元信息
        securities = store.list_securities(market)
        sec_map: dict[str, Any] = {str(s.code): s for s in securities}
        sec = sec_map.get(code)
        if sec is None:
            raise DataIntegrityError(
                f"[fail-loud/NF-26] LimitClassifier: 元信息缺失 {market}/{code}"
            )
        name = str(getattr(sec, "name", "") or code)
        sector = str(getattr(sec, "sector", "") or "未知")
        # 当日日线
        bars_long = store.read_daily_bars(market, code, tail=260)
        if not bars_long:
            raise DataIntegrityError(
                f"[fail-loud/NF-26] LimitClassifier: 湖内无日线 {market}/{code}"
            )
        last = bars_long[-1]
        close = float(last.close)
        prev_close = float(getattr(last, "prev_close", 0.0) or 0.0)
        if prev_close <= 0:
            # 兜底：用 bars_long[-2]
            if len(bars_long) >= 2:
                prev_close = float(bars_long[-2].close)
        change_pct = (close / prev_close) - 1.0 if prev_close > 0 else 0.0
        # 量比：今日量 / 5日均量；缺量 → None 写入契约允许
        vol_series = [float(getattr(b, "vol", 0.0) or 0.0) for b in bars_long]
        volume_ratio: float | None = None
        if len(vol_series) >= 6 and vol_series[-1] > 0:
            ma5_vol = sum(vol_series[-6:-1]) / 5.0
            if ma5_vol > 0:
                volume_ratio = float(vol_series[-1] / ma5_vol)
        continuous_days = self._continuous_up_days(bars_long) if limit_side == "up" else 1

        # --- 6 类按优先级判定 ---
        reasons_acc: list[str] = []
        type_scores: list[tuple[C.LimitType, list[str]]] = []

        # 1. 新股上市
        list_d = self._list_date(market, code, sec)
        if list_d is not None:
            days_since = (target_day - list_d).days
            if 0 <= days_since <= _NEW_LISTING_DAYS:
                type_scores.append(
                    (C.LimitType.NEW_LISTING, [f"上市日{list_d.isoformat()}距今{days_since}天"])
                )
        # 2. ST 摘帽
        if limit_side == "up":
            is_st_removal, st_reasons = self._st_removal_check(sec, target_day)
            if is_st_removal:
                type_scores.append((C.LimitType.ST_REMOVAL, st_reasons))
            else:
                reasons_acc.extend(st_reasons)
        else:
            # 跌停：ST* 类属于正常下跌，不触发摘帽正类
            _, st_reasons = self._st_removal_check(sec, target_day)
            reasons_acc.extend(st_reasons)

        if limit_side == "up":
            # 3. 连板天数（非主类型，reasons 追加）
            if continuous_days >= 2:
                reasons_acc.append(f"{continuous_days}连板")
            # 4. 业绩驱动
            ok, earn_reasons = self._earnings_signal(market, code, target_day)
            if ok:
                type_scores.append((C.LimitType.EARNINGS_DRIVEN, earn_reasons))
            else:
                reasons_acc.extend(earn_reasons)
            # 5. 技术突破
            ok, tech_reasons = self._tech_breakout(bars_long)
            if ok:
                type_scores.append((C.LimitType.TECH_BREAKOUT, tech_reasons))
            else:
                reasons_acc.extend(tech_reasons)
            # 6. 概念炒作（优先级在业绩/技术之后）
            ok, hyp_reasons = self._concept_hype(sec, continuous_days=continuous_days, code=code)
            if ok:
                type_scores.append((C.LimitType.THEME_HYPE, hyp_reasons))

        # 按优先级取主类型
        priority_index = {t: i for i, t in enumerate(C.LIMIT_TYPE_PRIORITY)}
        primary: C.LimitType = C.LimitType.OTHER
        primary_reasons: list[str] = []
        if type_scores:
            type_scores.sort(key=lambda t: priority_index.get(t[0], 99))
            primary, primary_reasons = type_scores[0]
            # 其余命中的也追加到 reasons 作说明
            for t, r_list in type_scores[1:]:
                for r in r_list:
                    if r not in primary_reasons and r not in reasons_acc:
                        reasons_acc.append(r)
        # 组装最终 reasons：主类型原因放前，其余跟后（去重）
        seen: set[str] = set()
        final_reasons: list[str] = []
        for r in list(primary_reasons) + list(reasons_acc):
            rr = str(r).strip()
            if not rr or rr in seen:
                continue
            seen.add(rr)
            final_reasons.append(rr)
        return C.LimitEntry(
            code=code,
            name=name,
            sector=sector,
            limit_type=primary,
            close=close,
            change_pct=change_pct,
            volume_ratio=volume_ratio,
            continuous_days=continuous_days,
            reasons=tuple(final_reasons),
        )


class PostCloseService:
    """盘后复盘门面（涨跌停分析 + 技术亮点，依赖 PreOpenService 技术扫描）。"""

    def __init__(
        self,
        config: Config,
        *,
        lake: Any,
        factor_service: Any | None,
        limit_store: LimitUpDownStore,
        pre_open: PreOpenService,
        monitor_store: Any | None = None,
    ) -> None:
        self._cfg = config
        self._lake = lake
        self._factor_service = factor_service
        self._limit_store = limit_store
        self._pre_open = pre_open
        self._monitor_store = monitor_store
        self._classifier = LimitClassifier(config, lake=lake, factor_service=factor_service)

    # ------------------------------------------------------------------
    # 等待收盘数据就绪
    # ------------------------------------------------------------------

    def _market_latest_bar_date(self, market: str) -> dt.date | None:
        """取全市场任意一只股票的最新 bar date（粗略：取 lake.store 里
        security_types=股票 类型中随机 20 只，返回最大值）。
        """
        store = self._lake.store
        try:
            all_secs = store.list_securities(market)
        except AttributeError as exc:
            raise DataIntegrityError(
                f"[fail-loud/NF-26] PostCloseService 要求 lake.store.list_securities: {exc}"
            ) from exc
        sample = [s for s in all_secs if str(getattr(s, "code", "") or "").strip()]
        sample = sample[:20]
        max_d: dt.date | None = None
        for s in sample:
            bars = store.read_daily_bars(market, str(s.code), tail=1)
            if not bars:
                continue
            d = getattr(bars[-1], "date", None)
            if isinstance(d, dt.date) and (max_d is None or d > max_d):
                max_d = d
        return max_d

    def _wait_until_data_ready(
        self,
        market: str,
        target_day: dt.date,
        *,
        force: bool = False,
        poll_interval_s: float = 60.0,
    ) -> dt.date:
        """解析「生效报告日」与数据来源（不再硬超时 501）。

        语义（契合「只展示当天，数据缺失时用 tdx 实时补全」）：

        1. lake 已有 ``target_day`` → 正常返回 ``(target_day, "lake")``。
        2. lake 无 ``target_day`` → 尝试用 tdx 实时行情探测当日是否可得
           （拉取基准标的当日日线验证）。可达则触发一次 ``sync_incremental``
           补齐当日，返回 ``(target_day, "tdx_realtime")``。
        3. tdx 不可达 / 无当日 → **降级**到 lake 最新交易日，返回
           ``(lake_latest, "lake_fallback")``；报告照常展示最近可得日，
           **绝不抛 501**（fail-loud 精神：如实标注来源，而非编造/崩溃）。

        Returns:
            ``(effective_day, data_source)``；``effective_day`` 即后续分析使用的
            报告日。
        """
        if force:
            logger.info("[PostCloseService] force=True，跳过 data ready 判断")
            return target_day, "lake"

        latest = self._market_latest_bar_date(market)
        if latest is not None and latest >= target_day:
            logger.info(
                "[PostCloseService] 湖内收盘L1已就绪 market=%s latest=%s target=%s",
                market, latest, target_day,
            )
            return target_day, "lake"

        # lake 无 target_day → 用 tdx 实时探测「当日是否真实存在」（仅探测）
        logger.warning(
            "[PostCloseService] 湖内无当日数据 market=%s target=%s latest=%s；"
            "tdx 实时探测当日可达性",
            market, target_day, latest,
        )
        tdx_has_today = self._try_tdx_realtime(market, target_day)
        if tdx_has_today:
            # 当天确为交易日且 tdx 可得：后台触发增量同步补全（不阻塞请求），
            # 本次报告先尝试用 lake 当天；若 lake 仍未就绪则降级到最新可得日。
            self._trigger_background_sync(market)
            if latest is not None:
                logger.warning(
                    "[PostCloseService] tdx 确认当日可达，已后台触发同步；"
                    "本次降级展示 lake 最新交易日 %s（同步完成后刷新即当天）",
                    latest,
                )
                return latest, "tdx_realtime"
            return target_day, "tdx_realtime"

        # 降级：展示 lake 最新可得交易日（不编造、不 501）
        if latest is not None:
            logger.warning(
                "[PostCloseService] tdx 实时当日不可达（非交易日/未收盘），降级展示 "
                "lake 最新交易日 market=%s effective=%s",
                market, latest,
            )
            return latest, "lake_fallback"
        raise NotSupportedError(
            f"[fail-loud/NF-26] 盘后复盘无可用行情：lake 为空且 tdx 实时不可达 "
            f"market={market} target={target_day}。请先 `Kuantix sync increment` 或 "
            "检查 tdx 行情源连通性。"
        )

    def _try_tdx_realtime(
        self, market: str, target_day: dt.date
    ) -> bool:
        """探测 tdx 实时行情是否含「当日」数据（仅探测，绝不触发全市场同步）。

        用途：区分「非交易日 / 盘中未收盘」（tdx 也无当日）→ 应降级到
        lake 最新交易日；与「交易日但本地未同步」（tdx 有当日）→ 标记
        ``tdx_realtime`` 来源。

        关键：**不在请求路径触发 ``sync_incremental``** —— 全市场增量同步
        是 CPU/IO 密集操作，放在请求内会拖垮整个 worker 进程（GIL 争抢致
        页面与其他请求超时）。当天数据补全应交由 cron（16:30 增量同步）或
        手动 ``Kuantix sync increment`` 负责。

        Returns:
            当天数据是否可由 tdx 实时获取（``True``=可，``False``=不可/异常）。
        """
        try:
            from Kuantix.adapters.tdx_client import TdxClientFactory
            from Kuantix.adapters.quotation import QuotationFetcher

            fetcher = QuotationFetcher(TdxClientFactory.from_config(self._cfg))
            bars = fetcher.fetch_kline(market, "000001", count=5)
            if not bars:
                return False
            last_date = bars[-1].date
            available = last_date >= target_day
            logger.info(
                "[PostCloseService] tdx 实时探测 market=%s tdx_latest=%s target=%s → %s",
                market, last_date, target_day, "当日可达" if available else "当日不可达",
            )
            return available
        except Exception as exc:  # noqa: BLE001 - 防御式
            logger.warning("[PostCloseService] tdx 实时探测失败（视为不可达）：%s", exc)
            return False

    def _trigger_background_sync(self, market: str) -> None:
        """在后台线程触发一次增量同步（不阻塞当前请求）。

        仅当 lake 明显滞后（无当日）且 tdx 确认当日可达时，由调用方决定是否
        触发；用 daemon 线程避免拖垮请求，真正的补全由它异步完成。
        """
        try:
            import threading

            def _run() -> None:
                try:
                    self._lake.sync_incremental(market)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[PostCloseService] 后台增量同步失败：%s", exc)

            t = threading.Thread(target=_run, name="post-close-sync", daemon=True)
            t.start()
            logger.info("[PostCloseService] 已后台触发增量同步 market=%s（不阻塞请求）", market)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[PostCloseService] 无法启动后台同步线程：%s", exc)

    # ------------------------------------------------------------------
    # 涨跌停分析
    # ------------------------------------------------------------------

    def _change_pct(self, bar: Any) -> float:
        prev = float(getattr(bar, "prev_close", 0.0) or 0.0)
        close = float(getattr(bar, "close", 0.0))
        if prev <= 0:
            return 0.0
        return (close / prev) - 1.0

    def run_limit_analysis(
        self,
        market: str,
        target_day: dt.date,
    ) -> tuple[C.LimitUpDownSummary, list[C.LimitEntry]]:
        """遍历全市场股票 → 判定涨跌停 → 分类 + 汇总 → 写入 store。"""
        market_code = require_known(str(market).upper(), "market", allowed=set(known_markets()))
        store = self._lake.store
        securities = store.list_securities(market_code)
        entries: list[C.LimitEntry] = []
        up_count = 0
        down_count = 0
        flat_count = 0
        sector_stats: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"sector": "", "up": 0, "down": 0, "representative_codes": []}
        )
        type_counts: dict[str, int] = defaultdict(int)
        for sec in securities:
            code = str(getattr(sec, "code", "") or "").strip()
            if not code:
                continue
            if market_code == "CN" and (len(code) != 6 or not code.isdigit()):
                continue  # 仅 A 股股票
            bars = store.read_daily_bars(market_code, code, tail=1)
            if not bars:
                flat_count += 1
                continue
            last = bars[-1]
            d = getattr(last, "date", None)
            if isinstance(d, dt.date) and d < target_day:
                # 不是今日 → 不计入当日涨跌停
                flat_count += 1
                continue
            pct = self._change_pct(last)
            side: str | None = None
            if pct >= _LIMIT_UP_THRESHOLD:
                side = "up"
                up_count += 1
            elif pct <= _LIMIT_DOWN_THRESHOLD:
                side = "down"
                down_count += 1
            else:
                flat_count += 1
            if side is None:
                continue
            try:
                entry = self._classifier.classify(market_code, code, target_day, limit_side=side)
            except DataIntegrityError as exc:
                logger.warning(
                    "[PostCloseService.run_limit_analysis] 跳过 %s/%s: %s",
                    market_code, code, exc,
                )
                continue
            entries.append(entry)
            sec_obj = sec
            sector = str(getattr(sec_obj, "sector", "") or "未知")
            s_bucket = sector_stats[sector]
            s_bucket["sector"] = sector
            if side == "up":
                s_bucket["up"] += 1
                if len(s_bucket["representative_codes"]) < 5:
                    s_bucket["representative_codes"].append(code)
            else:
                s_bucket["down"] += 1
                if len(s_bucket["representative_codes"]) < 5:
                    s_bucket["representative_codes"].append(code)
            type_counts[entry.limit_type.value] += 1

        total_count = max(1, up_count + down_count + flat_count)
        up_ratio = float(up_count) / float(total_count)
        down_ratio = float(down_count) / float(total_count)
        by_sector = tuple(
            {
                "sector": s["sector"],
                "up": int(s["up"]),
                "down": int(s["down"]),
                "representative_codes": list(s["representative_codes"]),
            }
            for s in sorted(
                sector_stats.values(),
                key=lambda x: -(x["up"] + x["down"]),
            )
        )
        by_type = tuple(
            {"limit_type": k, "count": int(v)}
            for k, v in sorted(type_counts.items(), key=lambda kv: -kv[1])
        )
        summary = C.LimitUpDownSummary(
            date=target_day,
            market=market_code,
            up_count=int(up_count),
            down_count=int(down_count),
            flat_count=int(flat_count),
            total_count=int(total_count),
            up_ratio=up_ratio,
            down_ratio=down_ratio,
            by_sector=by_sector,
            by_type=by_type,
            generated_at=dt.datetime.now().astimezone(),
        )
        self._limit_store.upsert(
            market_code,
            target_day,
            entries,
            summary=summary,
            by_sector=by_sector,
            by_type=by_type,
        )
        return summary, entries

    # ------------------------------------------------------------------
    # 技术亮点复用 PreOpenService.scan_technical
    # ------------------------------------------------------------------

    def _scan_tech_highlights(
        self,
        market: str,
        target_day: dt.date,
        *,
        codes: Iterable[str] | None = None,
        top_n: int = 30,
    ) -> tuple[list[C.TechnicalAnalysis], list[dict[str, Any]]]:
        """技术扫描 → (all_tech, signals_today)。

        signals_today 格式：``[{code, name, date, signals:[...],
        trend_direction, trend_strength, support_levels,
        resistance_levels, data_source}]``。
        """
        watch, sample = self._pre_open._expand_codes(market, codes)  # noqa: SLF001 内部协作
        scan_codes = list(dict.fromkeys(watch + sample))
        logger.info(
            "[PostCloseService] 技术亮点扫描 market=%s date=%s total=%d",
            market, target_day, len(scan_codes),
        )
        all_tech = self._pre_open.scan_technical(market, scan_codes)
        # 过滤最新数据日 → 今日信号。tdx 实时补全后各标的 last_date 可能
        # 领先 lake（effective_day），故以扫描结果中的最大 last_date 为
        # 「信号基准日」，避免湖内滞后时 signals_today 全被过滤为空。
        signal_day = max(
            (t.last_date for t in all_tech),
            default=target_day,
        )
        today_signals: list[dict[str, Any]] = []
        for t in all_tech:
            if t.last_date != signal_day or not t.signals:
                continue
            today_signals.append({
                "code": t.code,
                "name": getattr(t, "name", "") or "",
                "date": t.last_date.isoformat(),
                "signals": list(t.signals),
                "trend_direction": t.trend_direction.value,
                "trend_strength": float(t.trend_strength),
                "support_levels": [round(float(x), 2) for x in t.support_levels],
                "resistance_levels": [round(float(x), 2) for x in t.resistance_levels],
                "data_source": getattr(t, "data_source", "lake"),
            })
        # Top 亮点：按信号数、强度排序
        highlights = sorted(
            all_tech,
            key=lambda t: (-len(t.signals), -float(t.trend_strength), t.code),
        )[:top_n]
        return highlights, today_signals

    # ------------------------------------------------------------------
    # 自选 PnL（粗略：按 day change_pct 估算单票浮盈；未注入仓位 → 占比返回 0）
    # ------------------------------------------------------------------

    def _compute_watchlist_pnl(
        self,
        market: str,
        target_day: dt.date,
    ) -> list[dict[str, Any]]:
        """返回 [{code,name,close,prev_close,change_pct,position_qty,weight,est_pnl}]。"""
        watch = self._pre_open._watchlist_codes(market)  # noqa: SLF001
        store = self._lake.store
        securities = store.list_securities(market)
        sec_map = {str(s.code): s for s in securities}
        # positions 若有 → 使用 qty * change
        pos_map: dict[str, Any] = {}
        if self._monitor_store is not None:
            try:
                positions = self._monitor_store.list_positions(market=market)
            except Exception:  # noqa: BLE001
                positions = []
            for p in positions:
                c = str(getattr(p, "code", "") or "").strip()
                if c:
                    pos_map[c] = p
        result: list[dict[str, Any]] = []
        for code in watch:
            bars = store.read_daily_bars(market, code, tail=2)
            if len(bars) < 2:
                # lake 缺该标的 → tdx 实时兜底（数据源优先 tdx）
                try:
                    tdx_bars = self._pre_open.fetch_tdx_daily_bars(market, code, count=5)
                    if len(tdx_bars) >= 2:
                        bars = tdx_bars[-2:]
                except Exception as exc:  # noqa: BLE001 - tdx 失败跳过该标的
                    logger.warning(
                        "[PostCloseService._compute_watchlist_pnl] %s/%s "
                        "tdx 实时兜底失败：%s", market, code, exc,
                    )
            if len(bars) < 2:
                continue
            close = float(bars[-1].close)
            prev_close = float(bars[-2].close)
            pct = (close / prev_close - 1.0) if prev_close > 0 else 0.0
            qty = 0
            if code in pos_map:
                qty = int(getattr(pos_map[code], "qty", 0) or 0)
            est_pnl = float(qty) * float(close - prev_close)
            sec = sec_map.get(code)
            name = str(getattr(sec, "name", "") or code) if sec else code
            result.append({
                "code": code,
                "name": name,
                "close": close,
                "prev_close": prev_close,
                "change_pct": pct,
                "position_qty": int(qty),
                "weight": 0.0,  # 无总资产信息，留空
                "est_pnl": float(est_pnl),
            })
        result.sort(key=lambda x: -abs(float(x["est_pnl"])))
        return result

    # ------------------------------------------------------------------
    # 报告入口
    # ------------------------------------------------------------------

    def run_report(
        self,
        market: str,
        date: dt.date | None = None,
        *,
        force: bool = False,
        codes: Iterable[str] | None = None,
    ) -> C.PostCloseReport:
        market_code = require_known(str(market).upper(), "market", allowed=set(known_markets()))
        requested_day = date if date is not None else dt.date.today()
        effective_day, data_source = self._wait_until_data_ready(
            market_code, requested_day, force=force
        )
        logger.info(
            "[PostCloseService.run_report] market=%s requested=%s effective=%s source=%s force=%s",
            market_code, requested_day, effective_day, data_source, force,
        )
        summary, _entries = self.run_limit_analysis(market_code, effective_day)
        highlights, today_signals = self._scan_tech_highlights(
            market_code, effective_day, codes=codes, top_n=30,
        )
        watch_pnl = self._compute_watchlist_pnl(market_code, effective_day)
        return C.PostCloseReport(
            date=effective_day,
            market=market_code,
            generated_at=dt.datetime.now().astimezone(),
            data_source=data_source,
            data_as_of=effective_day if data_source != "lake" else requested_day,
            limit_summary=summary.to_dict(),
            tech_highlights=tuple(t.to_dict() for t in highlights),
            signals_today=tuple(today_signals),
            watchlist_pnl=tuple(watch_pnl),
        )
