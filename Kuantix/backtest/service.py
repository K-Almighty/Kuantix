"""回测服务（B1–B4 业务层，经 BacktestBridge 调上游引擎，R2）。

设计
----
- **策略发现**：:meth:`list_strategies` 经 :class:`BacktestBridge` 枚举上游
  注册表（19 个预置策略 + 参数 schema），业务层不复制策略实现。
- **运行**：:meth:`run` 在后台 Job 线程内执行——
  1. 经 :meth:`_read_frame` 读数据：``local`` 读本地 vipdoc（零网络）；
     ``live`` 经 :class:`~Kuantix.adapters.quotation.QuotationFetcher` 实时拉取
     （未复权/vol=手/列格式与本地同构，回测可比）；``auto`` 本地优先、缺失转实时
     （v1.4 增量，核心回测逻辑不变，只改数据获取）；
  2. 逐标的经 bridge 调上游 ``BacktestEngine``（薄包装）；
  3. 组合净值 = 各标的归一化净值**等权平均**（聚合已算结果，不重复实现
     绩效计算；组合绩效经上游 ``PerformanceAnalyzer`` 计算）；
  4. 完整结果落 :class:`~Kuantix.backtest.store.BacktestResultStore`
     （job_id → JSON），返回轻量摘要给 Job.result_summary。
- **市场规则**：代码→交易所映射经 :class:`MarketProfile`（NF-5/R6，
  不硬编码 A 股常量）。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from Kuantix.adapters.backtest_bridge import BacktestBridge
from Kuantix.adapters.factor_bridge import L1Reader
from Kuantix.adapters.quotation import QuotationFetcher
from Kuantix.backtest.data_source import (
    fetch_live_frame,
    local_has_data,
    parse_data_source,
)
from Kuantix.backtest.store import BacktestResultStore
from Kuantix.config import Config, get_config
from Kuantix.core.fail_loud import (
    DataIntegrityError,
    MissingKeyError,
    UnknownValueError,
    require_key,
    require_non_empty,
)
from Kuantix.core.market import MarketProfile, get_market_profile

__all__ = ["BacktestService", "BacktestRunRequest", "BacktestRunResult"]

#: 单标的 K 线数量下限（上游引擎要求至少 2 根）
MIN_BARS = 2


@dataclass(frozen=True)
class BacktestRunRequest:
    """一次回测请求（B2 的领域对象）。

    Attributes:
        market: 市场码（P0 仅 ``CN``）。
        codes: 标的代码池（非空，上限由路由层校验）。
        strategy: 上游策略名。
        params: 策略参数。
        start: 起始日期（含）。
        end: 结束日期（含）。
        cash: 初始资金。
        commission: 佣金费率。
        min_commission: 单笔最低佣金。
        stamp_tax: 印花税（卖出）。
        slippage: 滑点费率。
        execution: 成交模式（``next_open`` / ``next_close``）。
        data_source: 数据源（``auto`` / ``local`` / ``live``，默认 ``auto``，v1.4）。
            ``local``：只读本地湖（现状行为）；``live``：强制实时拉取（仅单标的，
            路由层 422 拦截多标的）；``auto``：本地有数据用本地，本地无此标的才实时。
    """

    market: str = "CN"
    codes: tuple[str, ...] = ()
    strategy: str = "ma_cross"
    params: dict[str, Any] = field(default_factory=dict)
    start: dt.date = dt.date(2020, 1, 1)
    end: dt.date = dt.date(2025, 12, 31)
    cash: float = 1_000_000.0
    commission: float = 0.0003
    min_commission: float = 5.0
    stamp_tax: float = 0.001
    slippage: float = 0.0
    execution: str = "next_open"
    data_source: str = "auto"


@dataclass(frozen=True)
class BacktestRunResult:
    """一次回测的运行结果（摘要 + 完整结果句柄）。

    Attributes:
        summary: Job.result_summary 用轻量摘要。
        result: 完整结果字典（已落 store，供 B4 查询）。
    """

    summary: dict[str, Any]
    result: dict[str, Any]


class BacktestService:
    """回测服务门面（组合根注入 reader / bridge / store，测试可注入假实现）。

    Args:
        config: 配置对象；``None`` 时取全局配置。
        reader: L1 读侧；``None`` 时用 ``~/.Kuantix/vipdoc``。
        bridge: 回测桥；``None`` 时新建。
        store: 结果存储；``None`` 时用 ``config.paths.db``。
        fetcher: 在线拉取器（``data_source=live/auto 回退`` 用）；``None`` 时
            惰性构造（``shared_connection=False``，独立连接，NF-28）。
    """

    def __init__(
        self,
        config: Config | None = None,
        *,
        reader: L1Reader | None = None,
        bridge: BacktestBridge | None = None,
        store: BacktestResultStore | None = None,
        fetcher: QuotationFetcher | None = None,
    ) -> None:
        self._config = config if config is not None else get_config()
        self._reader = (
            reader
            if reader is not None
            else L1Reader.from_config(self._config, backend="auto")
        )
        self._bridge = bridge if bridge is not None else BacktestBridge()
        self._store = (
            store
            if store is not None
            else BacktestResultStore(self._config.paths.db / "backtest_results.db")
        )
        self._fetcher = fetcher

    # ------------------------------------------------------------------ #
    # 策略发现
    # ------------------------------------------------------------------ #

    def list_strategies(self) -> list[dict[str, Any]]:
        """返回上游全部预置策略 schema（name/label/description/params）。"""
        return self._bridge.list_strategies()

    # ------------------------------------------------------------------ #
    # 运行
    # ------------------------------------------------------------------ #

    def run(
        self,
        job_id: str,
        req: BacktestRunRequest,
        progress_cb: Callable[[dict[str, Any]], None] | None = None,
    ) -> BacktestRunResult:
        """执行一次回测并落完整结果到 store（后台线程内调用）。

        Args:
            job_id: 关联 Job id（完整结果以它为键落 store）。
            req: 回测请求。
            progress_cb: 进度回调（可空）。

        Returns:
            :class:`BacktestRunResult`（summary 供 Job.result_summary）。

        Raises:
            MissingKeyError: 代码池为空。
            DataIntegrityError: 全部标的读不到有效数据 / 策略名非法。
        """
        require_non_empty(req.codes, "backtest.codes")
        profile = get_market_profile(req.market)

        frames: dict[str, pd.DataFrame] = {}
        skipped: list[dict[str, Any]] = []
        total = len(req.codes)
        # O2：本地/auto 数据源优先走批量读取（一次 SQL 取全部标的 + 区间下推），
        # 替代逐只 read_daily_frame 全量读 + 内存过滤（与已优化的 loop200 同模式）。
        # live 数据源（网络拉取）无法批量，仍逐只走 _load_frame。
        batch = self._load_frames_batch(profile, req)
        for index, code in enumerate(req.codes, start=1):
            if progress_cb is not None:
                progress_cb(
                    {
                        "stage": "load",
                        "total": total,
                        "done": index - 1,
                        "current": code,
                        "percent": round((index - 1) / total * 100, 1),
                        "failed": 0,
                        "quarantined": 0,
                    }
                )
            try:
                if code in batch:
                    frame = batch[code]
                else:
                    # 批量缺失（live 源 / 测试替身无批量方法）→ 单只 fallback
                    frame = self._load_frame(profile, code, req)
            except DataIntegrityError as exc:
                skipped.append({"code": code, "reason": str(exc)})
                continue
            if len(frame) < MIN_BARS:
                skipped.append(
                    {"code": code, "reason": f"K 线不足 {MIN_BARS} 根"}
                )
                continue
            frames[code] = frame

        if not frames:
            raise DataIntegrityError(
                f"[fail-loud/NF-26] 回测标的池全部读取失败（{len(skipped)} 只被跳过），"
                f"无法运行。首个失败原因: {skipped[0]['reason'] if skipped else '无'}"
            )

        per_code: dict[str, dict[str, Any]] = {}
        for index, (code, frame) in enumerate(frames.items(), start=1):
            if progress_cb is not None:
                progress_cb(
                    {
                        "stage": "run",
                        "total": len(frames),
                        "done": index - 1,
                        "current": code,
                        "percent": round((index - 1) / len(frames) * 100, 1),
                        "failed": 0,
                        "quarantined": 0,
                    }
                )
            per_code[code] = self._run_one(frame, req)

        combined = self._combine(per_code, req)
        result = {
            "strategy": req.strategy,
            "params": dict(req.params),
            "market": req.market,
            "start_date": req.start.isoformat(),
            "end_date": req.end.isoformat(),
            "config": {
                "cash": req.cash,
                "commission": req.commission,
                "min_commission": req.min_commission,
                "stamp_tax": req.stamp_tax,
                "slippage": req.slippage,
                "execution": req.execution,
            },
            "codes": list(frames.keys()),
            "skipped": skipped,
            "per_code": per_code,
            "combined": combined,
        }
        self._store.save(job_id, result)

        combined_perf = require_key(combined, "performance", "回测组合绩效")
        combined_curve = require_key(combined, "equity_curve", "回测组合净值")
        summary = {
            "strategy": req.strategy,
            "market": req.market,
            "codes": list(frames.keys()),
            "result_count": len(frames),
            "skipped_count": len(skipped),
            "combined": {
                "total_return": combined_perf.get("total_return"),
                "annual_return": combined_perf.get("annual_return"),
                "max_drawdown": combined_perf.get("max_drawdown"),
                "sharpe": combined_perf.get("sharpe"),
                "total_trades": combined_perf.get("total_trades"),
                "win_rate": combined_perf.get("win_rate"),
                "equity_points": len(combined_curve),
            },
        }
        return BacktestRunResult(summary=summary, result=result)

    # ------------------------------------------------------------------ #
    # 结果查询
    # ------------------------------------------------------------------ #

    def get_result(self, job_id: str) -> dict[str, Any] | None:
        """按 job_id 读取完整结果；不存在返回 ``None``（路由层映射 404）。"""
        return self._store.load(job_id)

    def delete_result(self, job_id: str) -> bool:
        """删除结果；返回是否确实删除了。"""
        return self._store.delete(job_id)

    def get_kline_with_signals(
        self,
        code: str,
        market: str,
        start: dt.date,
        end: dt.date,
        strategy: str = "ma_cross",
        data_source: str = "auto",
    ) -> dict[str, Any]:
        """单标的 K 线 + 策略买卖点标注（B5，契约 §2.1b，v1.3 增量 P1，v1.4 增 data_source）。

        K 线经与 B2 同一 :meth:`_read_frame` 分支（``auto/local/live``），保证
        下钻图与回测数据源一致（D1.6）；买卖点是**信号标注**
        （``{date, price}`` 数组，非下单动作，R5），由 :meth:`BacktestBridge.signal_points`
        经策略回测成交信号序列计算。

        Args:
            code: 6 位证券代码。
            market: 市场码（路由层已做 501 门禁）。
            start / end: 日期区间（含）。
            strategy: 策略名（默认 ``ma_cross``）。
            data_source: 数据源（``auto`` / ``local`` / ``live``，默认 ``auto``，v1.4）。

        Returns:
            ``{code, market, start_date, end_date, strategy, kline,
            buy_points, sell_points}``。

        Raises:
            MissingKeyError: 代码格式非法 / 代码段无法识别 / data_source 非法（→ 400）。
            DataIntegrityError: 无日线数据 / 区间无数据 / K 线不足（路由层映射 404）。
        """
        profile = get_market_profile(market)
        try:
            exchange = profile.exchange_for_code(code)
        except UnknownValueError as exc:
            raise MissingKeyError(
                f"[fail-loud/NF-26] K 线代码非法: {code!r}（{exc}）"
            ) from exc
        frame = self._read_frame(profile, code, start, end, data_source)
        if len(frame) < MIN_BARS:
            raise DataIntegrityError(
                f"[fail-loud/NF-26] {code} K 线不足 {MIN_BARS} 根"
            )

        kline: list[dict[str, Any]] = []
        for _, row in frame.iterrows():
            kline.append(
                {
                    "date": str(pd.Timestamp(row["datetime"]).date()),
                    "open": round(float(row["open"]), 6),
                    "high": round(float(row["high"]), 6),
                    "low": round(float(row["low"]), 6),
                    "close": round(float(row["close"]), 6),
                    "vol": round(float(row["vol"]), 6),
                    "amount": round(float(row["amount"]), 6),
                }
            )
        points = self._bridge.signal_points(frame, strategy)
        return {
            "code": code,
            "market": market,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "strategy": strategy,
            "kline": kline,
            "buy_points": points["buy_points"],
            "sell_points": points["sell_points"],
        }

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def _load_frames_batch(
        self, profile: MarketProfile, req: BacktestRunRequest
    ) -> dict[str, pd.DataFrame]:
        """批量读取本地 K 线（O2）：一次 SQL 取全部标的 + 区间下推。

        ``read_daily_frames(codes, market, start_date, end_date)`` 在 SQL 层
        用索引限定区间，避免逐只全量读 + 内存过滤（200 只全历史实测
        2816ms → 766ms，3.68×）。语义与 :meth:`_read_local_frame` 一致：
        - 仅对 ``local`` / ``auto`` 数据源生效（``live`` 网络源无法批量）；
        - reader 无批量方法（测试替身）时返回空 dict，调用方走单只 fallback；
        - 缺失/损坏标的由调用方按单只路径跳过（本方法不抛错，只返回已读到的）。

        Returns:
            ``{code: DataFrame}``（已按 ``[req.start, req.end]`` 过滤）。
        """
        source = parse_data_source(req.data_source)
        if source == "live":
            return {}
        batch_method = getattr(self._reader, "read_daily_frames", None)
        if batch_method is None:
            return {}
        codes = [str(c) for c in req.codes]
        if not codes:
            return {}
        try:
            raw = batch_method(
                codes,
                req.market,
                start_date=req.start.year * 10000 + req.start.month * 100 + req.start.day,
                end_date=req.end.year * 10000 + req.end.month * 100 + req.end.day,
            )
        except Exception:  # noqa: BLE001 - 批量读失败不吞业务异常，回退逐只
            return {}
        out: dict[str, pd.DataFrame] = {}
        for code, frame in raw.items():
            if frame is None or frame.empty:
                continue
            if "datetime" not in frame.columns:
                continue  # 与 _read_local_frame 的契约校验一致，由调用方跳过
            out[str(code)] = frame
        return out

    def _load_frame(
        self, profile: MarketProfile, code: str, req: BacktestRunRequest
    ) -> pd.DataFrame:
        """读单标的 K 线并按日期范围过滤（按 ``req.data_source`` 走数据源分支）。

        核心回测逻辑零改动：本方法只决定「数据从哪来」——
        ``local`` 读本地湖；``live`` 实时拉取；``auto`` 本地优先、缺失转实时。

        Raises:
            DataIntegrityError: 数据源读取失败（fail-loud，由调用方跳过并记录）。
            MissingKeyError: data_source 取值非法。
        """
        return self._read_frame(
            profile, code, req.start, req.end, req.data_source
        )

    def _read_frame(
        self,
        profile: MarketProfile,
        code: str,
        start: dt.date,
        end: dt.date,
        data_source: str,
    ) -> pd.DataFrame:
        """数据源分支入口（设计一 A1.1：local / live / auto）。

        语义（D1.1/D1.2，NF-26）：
        - ``local`` —— 只读本地湖；文件不存在/为空/损坏 → 显式抛错；
        - ``live`` —— 强制实时拉取；失败 → :class:`DataIntegrityError` 统一包装；
        - ``auto`` —— **先探测存在性再读**：文件不存在（合法业务态）→ 转实时；
          文件存在但读失败 → 显式抛错，**绝不静默降级到 live**。

        Args:
            profile: 市场档案。
            code: 6 位证券代码。
            start / end: 日期区间（含）。
            data_source: ``auto`` / ``local`` / ``live``。

        Returns:
            过滤 ``[start, end]`` 后的 DataFrame。

        Raises:
            MissingKeyError: data_source 取值非法（→ 400）。
            DataIntegrityError: 本地读取失败 / 实时拉取失败（fail-loud）。
        """
        source = parse_data_source(data_source)
        if source == "live":
            return fetch_live_frame(self._get_fetcher(), profile, code, start, end)
        if source == "local":
            return self._read_local_frame(profile, code, start, end)
        # auto：先探测本地存在性，再决定读本地还是转实时（D1.2）
        if local_has_data(self._reader, profile, code):
            return self._read_local_frame(profile, code, start, end)
        return fetch_live_frame(self._get_fetcher(), profile, code, start, end)

    def _read_local_frame(
        self, profile: MarketProfile, code: str, start: dt.date, end: dt.date
    ) -> pd.DataFrame:
        """读本地 vipdoc 日线并按日期范围过滤（现状行为，L1Reader 零网络）。

        Raises:
            DataIntegrityError: 文件不存在/为空/缺少 datetime 列（fail-loud，
                由调用方跳过并记录）。
        """
        exchange = profile.exchange_for_code(code)
        frame = self._reader.read_daily_frame(exchange, code)
        if frame.empty:
            raise DataIntegrityError(
                f"[fail-loud/NF-26] {code} 无日线数据（vipdoc 文件为空）"
            )
        if "datetime" not in frame.columns:
            raise DataIntegrityError(
                f"[fail-loud/NF-26] {code} 日线缺少 datetime 列，上游契约异常"
            )
        dt_series = pd.to_datetime(frame["datetime"])
        start_ts = pd.Timestamp(start)
        end_ts = pd.Timestamp(end)
        mask = (dt_series >= start_ts) & (dt_series <= end_ts)
        return frame[mask].reset_index(drop=True)

    def _get_fetcher(self) -> QuotationFetcher:
        """惰性构造实时拉取器（``shared_connection=False``：回测单标的独立连接）。

        设计一 A1.3：``__init__`` 注入的 ``fetcher`` 优先；否则首次用到 live
        路径时才构造，避免普通 local 回测产生任何连接/网络开销。
        """
        if self._fetcher is None:
            from Kuantix.adapters.tdx_client import TdxClientFactory

            self._fetcher = QuotationFetcher(
                TdxClientFactory.from_config(self._config),
                shared_connection=False,
            )
        return self._fetcher

    def _run_one(
        self, frame: pd.DataFrame, req: BacktestRunRequest
    ) -> dict[str, Any]:
        """单标的经 bridge 跑上游 BacktestEngine 并返回完整归一化结果。"""
        return self._bridge.run_strategy_backtest(
            frame,
            req.strategy,
            req.params,
            cash=req.cash,
            commission=req.commission,
            min_commission=req.min_commission,
            stamp_tax=req.stamp_tax,
            slippage=req.slippage,
            execution=req.execution,
        )

    def _combine(
        self, per_code: dict[str, dict[str, Any]], req: BacktestRunRequest
    ) -> dict[str, Any]:
        """组合视图：各标的归一化净值等权平均 → 上游 PerformanceAnalyzer。

        Args:
            per_code: ``{code: 完整结果}``。
            req: 回测请求（strategy 名用于 config 字段）。

        Returns:
            ``{equity_curve: [...], performance: {...}}``。
        """
        # 每个标的：datetime → normalized total（起始=1.0）
        normalized: dict[str, dict[str, float]] = {}
        for code, result in per_code.items():
            curve = result.get("equity_curve") or []
            per: dict[str, float] = {}
            for point in curve:
                dt_key = str(point.get("datetime"))[:10]
                total = float(point.get("total") or 0.0)
                base = float(curve[0].get("total") or 1.0) if curve else 1.0
                per[dt_key] = total / base if base != 0 else 0.0
            normalized[code] = per

        # O3：向量化聚合替代 O(N×M) 嵌套循环——
        # 原逻辑对每个日期取「有该日期的标的」均值（缺失跳过），全无值日期为 0，
        # 等价于 DataFrame 转置 + mean(axis=1, skipna=True)（全 NaN 行→NaN→0）。
        if not normalized:
            equity_df = pd.DataFrame(
                columns=["datetime", "total", "drawdown", "drawdown_pct"]
            )
        else:
            norm_df = pd.DataFrame(normalized)  # index=date, columns=code
            mean = norm_df.mean(axis=1, skipna=True)
            ordered = [k for k in sorted(norm_df.index)]
            rows: list[dict[str, Any]] = []
            peak = 0.0
            for dt_key in ordered:
                value = float(mean.loc[dt_key])
                # 全 NaN（该日期无任何标的有数据）→ 0，与原 sum/len 语义一致
                total = round(value, 6) if value == value else 0.0
                peak = max(peak, total)
                rows.append(
                    {
                        "datetime": dt_key,
                        "total": total,
                        "drawdown": round(peak - total, 6),
                        "drawdown_pct": (
                            round((peak - total) / peak, 6) if peak != 0 else 0.0
                        ),
                    }
                )
            equity_df = pd.DataFrame(
                rows, columns=["datetime", "total", "drawdown", "drawdown_pct"]
            )

        empty_trades = pd.DataFrame(
            columns=["direction", "pnl", "rejected", "datetime", "cost_basis"]
        )
        performance = self._bridge.analyze_equity(equity_df, empty_trades)
        return {
            "equity_curve": rows if normalized else [],
            "performance": performance,
            "config": {"strategy": req.strategy, "combine": "equal_weight"},
        }
