"""组合回测 / 多策略组合回测服务（P1–P3 / S5 业务层）。

设计
----
- **组合回测（P1）**：:class:`PortfolioService` —— 1 策略 × N 标的，总资金
  分仓（``total_cash / N``），经 :class:`BacktestBridge.run_portfolio_backtest`
  直调上游 ``PortfolioBacktestEngine``（R2 合规，金额求和 D-8 原生语义）。
- **多策略回测（S5）**：:class:`MultiStrategyService` —— N 策略 × 各自标的，
  总资金 1/N 均分，经 :meth:`BacktestBridge.run_multi_strategy` 直调上游
  ``MultiStrategyEngine``。
- **数据源**：K 线一律经 :class:`L1Reader` 读本地 vipdoc（零网络，数据源独立），
  日期过滤与 :class:`~Kuantix.backtest.service.BacktestService._load_frame` 同模式
  （抽为模块级 :func:`_load_frame` 共享）。
- **市场规则**：代码→交易所映射经 :class:`MarketProfile`（R6，不硬编码）。
- **结果落库**：完整结果以 ``job_id`` 为键落
  :class:`~Kuantix.backtest.store.BacktestResultStore`（与 B4 同库同 schema，
  互不冲突），``action`` 区分 ``portfolio`` / ``multi``。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from Kuantix.adapters.backtest_bridge import BacktestBridge
from Kuantix.adapters.factor_bridge import L1Reader
from Kuantix.backtest.store import BacktestResultStore
from Kuantix.config import Config, get_config
from Kuantix.core.fail_loud import DataIntegrityError, MissingKeyError, require_key
from Kuantix.core.market import MarketProfile, get_market_profile

__all__ = [
    "PortfolioService",
    "MultiStrategyService",
    "PortfolioRunRequest",
    "MultiStrategyItem",
    "MultiStrategyRunRequest",
    "MIN_BARS",
]

#: 单标的 K 线数量下限（与 BacktestService 对齐）
MIN_BARS = 2


def _load_frame(
    reader: L1Reader,
    profile: MarketProfile,
    code: str,
    start: dt.date,
    end: dt.date,
) -> pd.DataFrame:
    """读单标的 L1 日线并按日期范围过滤（共享工具，与 BacktestService 同模式）。

    Raises:
        DataIntegrityError: 文件不存在/为空/缺少 datetime 列（fail-loud，
            由调用方跳过并记录）。
    """
    exchange = profile.exchange_for_code(code)
    frame = reader.read_daily_frame(exchange, code)
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


@dataclass(frozen=True)
class PortfolioRunRequest:
    """组合回测请求（P1 领域对象，字段与 B2 对齐，语义为总资金分仓）。

    Attributes:
        market: 市场码（P0 仅 ``CN``）。
        codes: 组合标的池（非空，上限由路由层校验）。
        strategy: 单一策略名（组合 = 1 策略 × N 标的）。
        params: 策略参数。
        start: 起始日期（含）。
        end: 结束日期（含）。
        cash: 组合总资金（按 N 均分）。
        commission / min_commission / stamp_tax / slippage / execution: 成本配置。
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


@dataclass(frozen=True)
class MultiStrategyItem:
    """多策略槽位（S5 items 元素）。

    Attributes:
        strategy: 上游策略名。
        label: 策略展示名（拼结果 key ``{label}@{symbol}``）。
        code: 该策略跑的标的代码（6 位）。
        params: 策略参数。
    """

    strategy: str
    label: str
    code: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MultiStrategyRunRequest:
    """多策略组合回测请求（S5 领域对象，总资金 1/N 均分到各槽位）。"""

    market: str = "CN"
    items: tuple[MultiStrategyItem, ...] = ()
    cash: float = 1_000_000.0
    commission: float = 0.0003
    min_commission: float = 5.0
    stamp_tax: float = 0.001
    slippage: float = 0.0
    execution: str = "next_open"
    start: dt.date = dt.date(2020, 1, 1)
    end: dt.date = dt.date(2025, 12, 31)


class PortfolioService:
    """组合回测服务门面（P1/P3；测试可注入假 reader/bridge/store）。"""

    def __init__(
        self,
        config: Config | None = None,
        *,
        reader: L1Reader | None = None,
        bridge: BacktestBridge | None = None,
        store: BacktestResultStore | None = None,
    ) -> None:
        self._config = config if config is not None else get_config()
        # 数据源：装配 SQLite 主存储（auto 后端，SQLite 优先 + 镜像兜底），
        # 与「从 SQLite 取样本」口径一致。
        self._reader = (
            reader
            if reader is not None
            else L1Reader.from_config(self._config)
        )
        self._bridge = bridge if bridge is not None else BacktestBridge()
        self._store = (
            store
            if store is not None
            else BacktestResultStore(self._config.paths.db / "backtest_results.db")
        )

    # ------------------------------------------------------------------ #
    # 运行
    # ------------------------------------------------------------------ #

    def run(
        self,
        job_id: str,
        req: PortfolioRunRequest,
        progress_cb: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """执行一次组合回测并落完整结果到 store（后台线程内调用）。

        Returns:
            Job.result_summary 用轻量摘要（``{strategy, codes, result_count,
            skipped_count, total: {...}}``）。

        Raises:
            MissingKeyError: 标的池为空。
            DataIntegrityError: 全部标的读不到有效数据 / 策略名非法。
        """
        if not req.codes:
            raise MissingKeyError("[fail-loud/NF-26] 组合回测标的池代码数组为空")
        profile = get_market_profile(req.market)

        frames: dict[str, pd.DataFrame] = {}
        skipped: list[dict[str, Any]] = []
        total = len(req.codes)
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
                frame = _load_frame(self._reader, profile, code, req.start, req.end)
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
                f"[fail-loud/NF-26] 组合回测标的池全部读取失败"
                f"（{len(skipped)} 只被跳过），无法运行。"
                f"首个失败原因: {skipped[0]['reason'] if skipped else '无'}"
            )

        stocks = [
            (code, profile.exchange_for_code(code), frame)
            for code, frame in frames.items()
        ]
        result = self._bridge.run_portfolio_backtest(
            stocks,
            req.strategy,
            req.params,
            req.cash,
            commission=req.commission,
            min_commission=req.min_commission,
            stamp_tax=req.stamp_tax,
            slippage=req.slippage,
            execution=req.execution,
        )
        result["strategy"] = req.strategy
        result["params"] = dict(req.params)
        result["market"] = req.market
        result["start_date"] = req.start.isoformat()
        result["end_date"] = req.end.isoformat()
        result["config"] = {
            "cash": req.cash,
            "commission": req.commission,
            "min_commission": req.min_commission,
            "stamp_tax": req.stamp_tax,
            "slippage": req.slippage,
            "execution": req.execution,
            "allocation": "equal",
        }
        result["codes"] = list(frames.keys())
        result["skipped"] = skipped
        self._store.save(job_id, result)

        total_perf = require_key(
            result, "total_performance", "PortfolioResult.total_performance"
        )
        combined_equity = require_key(
            result, "combined_equity", "PortfolioResult.combined_equity"
        )
        summary = {
            "strategy": req.strategy,
            "market": req.market,
            "codes": list(frames.keys()),
            "result_count": len(frames),
            "skipped_count": len(skipped),
            "total": {
                "total_return": total_perf.get("total_return"),
                "annual_return": total_perf.get("annual_return"),
                "total_stocks": total_perf.get("total_stocks"),
                "total_cash": total_perf.get("total_cash"),
                "combined_points": len(combined_equity),
            },
        }
        return summary

    # ------------------------------------------------------------------ #
    # 结果查询
    # ------------------------------------------------------------------ #

    def get_result(self, job_id: str) -> dict[str, Any] | None:
        """按 job_id 读取完整结果；不存在返回 ``None``（路由层映射 404）。"""
        return self._store.load(job_id)


class MultiStrategyService:
    """多策略组合回测服务门面（S5；测试可注入假 reader/bridge/store）。"""

    def __init__(
        self,
        config: Config | None = None,
        *,
        reader: L1Reader | None = None,
        bridge: BacktestBridge | None = None,
        store: BacktestResultStore | None = None,
    ) -> None:
        self._config = config if config is not None else get_config()
        # 数据源：装配 SQLite 主存储（auto 后端，SQLite 优先 + 镜像兜底），
        # 与「从 SQLite 取样本」口径一致。
        self._reader = (
            reader
            if reader is not None
            else L1Reader.from_config(self._config)
        )
        self._bridge = bridge if bridge is not None else BacktestBridge()
        self._store = (
            store
            if store is not None
            else BacktestResultStore(self._config.paths.db / "backtest_results.db")
        )

    # ------------------------------------------------------------------ #
    # 运行
    # ------------------------------------------------------------------ #

    def run(
        self,
        job_id: str,
        req: MultiStrategyRunRequest,
        progress_cb: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """执行一次多策略组合回测并落完整结果到 store（后台线程内调用）。

        Returns:
            Job.result_summary 用轻量摘要。

        Raises:
            MissingKeyError: 槽位列表为空。
            DataIntegrityError: 全部槽位读不到有效数据 / 策略名非法。
        """
        if not req.items:
            raise MissingKeyError("[fail-loud/NF-26] 多策略回测 items 为空")
        profile = get_market_profile(req.market)

        slots: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        total = len(req.items)
        for index, item in enumerate(req.items, start=1):
            if progress_cb is not None:
                progress_cb(
                    {
                        "stage": "load",
                        "total": total,
                        "done": index - 1,
                        "current": item.code,
                        "percent": round((index - 1) / total * 100, 1),
                        "failed": 0,
                        "quarantined": 0,
                    }
                )
            try:
                frame = _load_frame(
                    self._reader, profile, item.code, req.start, req.end
                )
            except DataIntegrityError as exc:
                skipped.append(
                    {"label": item.label, "code": item.code, "reason": str(exc)}
                )
                continue
            if len(frame) < MIN_BARS:
                skipped.append(
                    {
                        "label": item.label,
                        "code": item.code,
                        "reason": f"K 线不足 {MIN_BARS} 根",
                    }
                )
                continue
            exchange = profile.exchange_for_code(item.code)
            slots.append(
                {
                    "label": item.label,
                    "symbol": f"{exchange.upper()}:{item.code}",
                    "strategy_name": item.strategy,
                    "params": dict(item.params),
                    "df": frame,
                }
            )

        if not slots:
            raise DataIntegrityError(
                f"[fail-loud/NF-26] 多策略回测全部槽位读取失败"
                f"（{len(skipped)} 个被跳过），无法运行。"
                f"首个失败原因: {skipped[0]['reason'] if skipped else '无'}"
            )

        result = self._bridge.run_multi_strategy(
            slots,
            req.cash,
            commission=req.commission,
            min_commission=req.min_commission,
            stamp_tax=req.stamp_tax,
            slippage=req.slippage,
            execution=req.execution,
        )
        result["market"] = req.market
        result["start_date"] = req.start.isoformat()
        result["end_date"] = req.end.isoformat()
        result["config"] = {
            "cash": req.cash,
            "commission": req.commission,
            "min_commission": req.min_commission,
            "stamp_tax": req.stamp_tax,
            "slippage": req.slippage,
            "execution": req.execution,
            "allocation": "equal",
        }
        result["items"] = [
            {
                "strategy": item.strategy,
                "label": item.label,
                "code": item.code,
                "params": dict(item.params),
            }
            for item in req.items
        ]
        result["skipped"] = skipped
        self._store.save(job_id, result)

        total_perf = require_key(
            result, "total_performance", "MultiStrategyResult.total_performance"
        )
        combined_equity = require_key(
            result, "combined_equity", "MultiStrategyResult.combined_equity"
        )
        summary = {
            "action": "multi",
            "market": req.market,
            "items": [
                {"strategy": item.strategy, "label": item.label, "code": item.code}
                for item in req.items
            ],
            "result_count": len(slots),
            "skipped_count": len(skipped),
            "total": {
                "total_return": total_perf.get("total_return"),
                "annual_return": total_perf.get("annual_return"),
                "total_stocks": total_perf.get("total_stocks"),
                "total_cash": total_perf.get("total_cash"),
                "combined_points": len(combined_equity),
            },
        }
        return summary

    # ------------------------------------------------------------------ #
    # 结果查询
    # ------------------------------------------------------------------ #

    def get_result(self, job_id: str) -> dict[str, Any] | None:
        """按 job_id 读取完整结果；不存在返回 ``None``（路由层映射 404）。"""
        return self._store.load(job_id)
