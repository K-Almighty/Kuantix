"""上游 Backtest / Rebalance / SignalScanner / Chanlun / StrengthRanker 薄包装。

Kuantix 侧**不重复实现**回测/技术分析逻辑，只做参数收敛与结果归一化
（NF-1：所有上游调用收敛在适配层）。业务层（screen/ backtest/）只能经本桥访问。
"""

from __future__ import annotations

from typing import Any

import pandas as pd
from easy_tdx.backtest.engine import BacktestEngine
from easy_tdx.backtest.multi_strategy_engine import (
    MultiStrategyEngine,
    StrategySlot,
)
from easy_tdx.backtest.optimizer import ParamGridOptimizer
from easy_tdx.backtest.portfolio_engine import PortfolioBacktestEngine, StockData
from easy_tdx.backtest.strategies import get_registry
from easy_tdx.backtest.strategy import Strategy
from easy_tdx.chanlun import ChanlunAnalyser, ChanlunResult
from easy_tdx.portfolio.optimizer import EqualWeightOptimizer
from easy_tdx.portfolio.rebalance import RebalanceEngine
from easy_tdx.screen.scanner import ScanResult, SignalScanner
from easy_tdx.screen.strength import STRENGTH_PRESETS, StrengthRanker, StrengthResult

from Kuantix.core.fail_loud import (
    DataIntegrityError,
    UpstreamContractError,
    require_key,
    require_known,
)

__all__ = [
    "Strategy",
    "ChanlunAnalyser",
    "ChanlunConfig",
    "BacktestBridge",
]


class BacktestBridge:
    """回测 / 调仓 / 扫描 / 缠论 / 强势排名的统一薄包装。"""

    # ------------------------------------------------------------------ #
    # 回测
    # ------------------------------------------------------------------ #

    def list_strategies(self) -> list[dict[str, Any]]:
        """枚举上游全部预置策略（name/label/description/params schema）。

        Returns:
            策略 schema 列表（供前端策略下拉 + 参数表单动态渲染）。
        """
        entries = get_registry().all()
        return [e.to_schema() for e in entries]

    def list_strategy_presets(self) -> dict[str, dict[str, Any]]:
        """枚举已注册策略的预设参数网格（对标 easy_tdx ``optimize-all``）。

        返回 ``{策略名: {label, grid}}``，其中 ``grid`` 为 ``{参数名: [候选值]}``
        —— 供一键寻优所有策略（O4）逐策略用预设网格做寻优。仅含已注册策略，
        未登记的预设自动跳过（与 easy_tdx ``_run_optimize_all`` 一致）。

        Returns:
            ``{strategy_name: {label: str, grid: {param: [values]}}}``。
        """
        from easy_tdx.backtest.strategies.presets import STRATEGY_PRESETS

        registry = get_registry()
        out: dict[str, dict[str, Any]] = {}
        for strategy_name, grid in STRATEGY_PRESETS.items():
            if strategy_name not in registry.names():
                continue
            out[strategy_name] = {
                "label": registry.get(strategy_name).label,
                "grid": dict(grid),
            }
        return out

    def analyze_equity(
        self,
        equity_curve: pd.DataFrame,
        trades: pd.DataFrame,
        *,
        risk_free_rate: float = 0.03,
    ) -> dict[str, Any]:
        """对（组合）资金曲线调用上游 PerformanceAnalyzer 计算绩效指标。

        供多标的等权净值聚合后计算组合绩效（薄包装，不重复实现绩效逻辑）。

        Args:
            equity_curve: 资金曲线 DataFrame（须含 ``total`` / ``drawdown`` 列）。
            trades: 成交记录 DataFrame（须含 ``direction`` / ``pnl`` / ``rejected``
                列；组合视图无成交时传空表）。
            risk_free_rate: 无风险利率（默认 3%）。

        Returns:
            19 项绩效指标（JSON 安全）。
        """
        from easy_tdx.backtest.performance import PerformanceAnalyzer

        analyzer = PerformanceAnalyzer(
            equity_curve=equity_curve,
            trades=trades,
            risk_free_rate=risk_free_rate,
        )
        metrics = analyzer.compute()
        return {str(k): self._clean_value(v) for k, v in metrics.items()}

    def run_strategy_backtest(
        self,
        df: pd.DataFrame,
        strategy_name: str,
        params: dict[str, Any] | None = None,
        *,
        cash: float = 1_000_000.0,
        commission: float = 0.0003,
        min_commission: float = 5.0,
        stamp_tax: float = 0.001,
        slippage: float = 0.0,
        execution: str = "next_open",
    ) -> dict[str, Any]:
        """按策略名 + 参数跑单标的回测，返回**完整**归一化结果。

        Args:
            df: 单标的日线 DataFrame（含 datetime/open/high/low/close/vol/amount）。
            strategy_name: 上游注册策略名（见 :meth:`list_strategies`）。
            params: 策略参数；缺失取默认值。
            cash: 初始资金。
            commission: 佣金费率。
            min_commission: 单笔最低佣金。
            stamp_tax: 印花税（卖出）。
            slippage: 滑点费率。
            execution: 成交模式（``next_open`` / ``next_close``）。

        Returns:
            ``{performance, equity_curve, trades, positions, config, diagnostic}``，
            全 JSON 安全（NaN/Inf → None，numpy → 原生）。

        Raises:
            DataIntegrityError: 策略名不存在（上游 KeyError 归一化）。
        """
        try:
            entry = get_registry().get(strategy_name)
        except KeyError as exc:
            raise DataIntegrityError(
                f"[fail-loud/NF-1] 未知回测策略: {strategy_name!r}（{exc}）"
            ) from exc
        strategy = entry.build(params or {})
        engine = BacktestEngine(
            strategy=strategy,
            cash=cash,
            commission=commission,
            min_commission=min_commission,
            stamp_tax=stamp_tax,
            slippage=slippage,
            execution=execution,
        )
        result = engine.run(df)
        return self._serialize_result(result)

    def run_backtest(
        self,
        df: pd.DataFrame,
        strategy_cls: type[Strategy],
        *,
        cash: float = 100_000.0,
    ) -> dict[str, Any]:
        """单标的回测，归一化输出绩效指标（兼容旧调用方）。

        Args:
            df: 单标的日线 DataFrame。
            strategy_cls: 上游 Strategy 子类。
            cash: 初始资金。

        Returns:
            ``{total_return, annual_return, max_drawdown, sharpe, trades}``。
        """
        engine = BacktestEngine(strategy_cls(), cash=cash)
        result = engine.run(df)
        perf = result.performance
        trades = getattr(result, "trades", None)
        trade_count = 0 if trades is None else len(trades)
        return {
            "total_return": self._perf(perf, "total_return"),
            "annual_return": self._perf(perf, "annual_return"),
            "max_drawdown": self._perf(perf, "max_drawdown"),
            "sharpe": self._perf(perf, "sharpe"),
            "trades": int(trade_count),
        }

    def run_portfolio_backtest(
        self,
        stocks: list[tuple[str, str, pd.DataFrame]],
        strategy_name: str,
        params: dict[str, Any] | None = None,
        total_cash: float = 1_000_000.0,
        *,
        commission: float = 0.0003,
        min_commission: float = 5.0,
        stamp_tax: float = 0.001,
        slippage: float = 0.0,
        execution: str = "next_open",
    ) -> dict[str, Any]:
        """组合回测：1 策略 × N 标的，总资金均分到各标的（D-8 金额求和）。

        桥内直调上游 :class:`PortfolioBacktestEngine`（R2 合规）——资金分仓、
        各标的独立回测、组合净值按日期并集 ffill 对齐后**金额求和**、回撤
        计算全部由上游原生实现，适配层只做结果归一化（NF-12）。

        Args:
            stocks: ``(code, exchange, df)`` 元组列表。``exchange`` 为
                vipdoc 目录前缀（``sh``/``sz``/``bj``，由 MarketProfile 解析），
                ``df`` 为含 datetime/open/high/low/close/vol/amount 的日线。
            strategy_name: 上游注册策略名（见 :meth:`list_strategies`）。
            params: 策略参数；缺失取默认值。
            total_cash: 组合总资金（按 N 均分到各标的）。
            commission: 佣金费率。
            min_commission: 单笔最低佣金。
            stamp_tax: 印花税（卖出）。
            slippage: 滑点费率。
            execution: 成交模式（``next_open`` / ``next_close``）。

        Returns:
            ``{total_performance, individual_results, equity_allocation,
            combined_equity}`` 全部 JSON 安全。``individual_results`` /
            ``equity_allocation`` 的 key 归一化为 6 位 code（草案 P3 DTO 口径；
            上游原生 key 为 ``{exchange}{code}`` 如 ``SH600519``）。

        Raises:
            DataIntegrityError: 策略名不存在 / 标的数据为空。
            UpstreamContractError: 上游返回结构异常（key 无法映射回标的代码）。
        """
        if not stocks:
            raise DataIntegrityError(
                "[fail-loud/NF-26] run_portfolio_backtest 标的列表为空"
            )
        try:
            entry = get_registry().get(strategy_name)
        except KeyError as exc:
            raise DataIntegrityError(
                f"[fail-loud/NF-1] 未知回测策略: {strategy_name!r}（{exc}）"
            ) from exc
        strategy = entry.build(params or {})

        stock_datas: list[StockData] = []
        code_by_native: dict[str, str] = {}
        for code, exchange, frame in stocks:
            exchange_code = str(exchange).strip().lower()
            native_key = f"{exchange_code.upper()}{code}"
            code_by_native[native_key] = str(code)
            stock_datas.append(
                StockData(code=str(code), market=exchange_code.upper(), df=frame)
            )

        engine = PortfolioBacktestEngine(
            strategy=strategy,
            stocks=stock_datas,
            total_cash=total_cash,
            allocation="equal",
            commission=commission,
            min_commission=min_commission,
            stamp_tax=stamp_tax,
            slippage=slippage,
            execution=execution,
        )
        payload = self._serialize_result(engine.run())

        individual_raw = require_key(
            payload, "individual_results", "PortfolioResult.individual_results"
        )
        allocation_raw = require_key(
            payload, "equity_allocation", "PortfolioResult.equity_allocation"
        )
        combined_raw = require_key(
            payload, "combined_equity", "PortfolioResult.combined_equity"
        )
        individual: dict[str, Any] = {}
        allocation: dict[str, float] = {}
        for native_key, value in individual_raw.items():
            code = code_by_native.get(native_key)
            if code is None:
                raise UpstreamContractError(
                    f"[fail-loud/NF-1] PortfolioResult 键 {native_key!r} "
                    f"无法映射到标的代码，上游契约可能已变更"
                )
            individual[code] = value
        for native_key, value in allocation_raw.items():
            code = code_by_native.get(native_key)
            if code is None:
                raise UpstreamContractError(
                    f"[fail-loud/NF-1] PortfolioResult 资金分配键 {native_key!r} "
                    f"无法映射到标的代码，上游契约可能已变更"
                )
            allocation[code] = value
        return {
            "total_performance": require_key(
                payload, "total_performance", "PortfolioResult.total_performance"
            ),
            "individual_results": individual,
            "equity_allocation": allocation,
            "combined_equity": self._normalize_equity_dates(combined_raw),
        }

    def run_multi_strategy(
        self,
        slots: list[dict[str, Any]],
        total_cash: float = 1_000_000.0,
        *,
        commission: float = 0.0003,
        min_commission: float = 5.0,
        stamp_tax: float = 0.001,
        slippage: float = 0.0,
        execution: str = "next_open",
    ) -> dict[str, Any]:
        """多策略组合回测：N 策略 × 各自标的，总资金 1/N 均分（D-8 金额求和）。

        桥内直调上游 :class:`MultiStrategyEngine`（R2 合规）。每个槽位独立
        回测，组合净值按日期并集 ffill 对齐后**金额求和**；``individual_results``
        的 key 天然为 ``"{label}@{symbol}"``（如 ``双均线交叉@SH:600519``）。

        Args:
            slots: 策略槽位列表，每个元素为
                ``{label, symbol, strategy_name, params, df}``：
                - ``label`` —— 策略展示名（拼 key）；
                - ``symbol`` —— 标的完整标识（如 ``SH:600519``，仅展示）；
                - ``strategy_name`` —— 上游注册策略名；
                - ``params`` —— 策略参数（可空）；
                - ``df`` —— 该标的日线 DataFrame。
            total_cash: 组合总资金（按 N 均分到各槽位）。
            commission / min_commission / stamp_tax / slippage / execution:
                与 :meth:`run_strategy_backtest` 同口径。

        Returns:
            ``{total_performance, individual_results, equity_allocation,
            combined_equity}`` 全部 JSON 安全。

        Raises:
            DataIntegrityError: 槽位列表为空 / 策略名不存在。
            UpstreamContractError: 上游返回结构异常。
        """
        if not slots:
            raise DataIntegrityError(
                "[fail-loud/NF-26] run_multi_strategy 策略槽位列表为空"
            )
        strategy_slots: list[StrategySlot] = []
        for slot in slots:
            label = require_key(slot, "label", "multi-strategy slot")
            symbol = require_key(slot, "symbol", "multi-strategy slot")
            strategy_name = require_key(
                slot, "strategy_name", "multi-strategy slot"
            )
            df = require_key(slot, "df", "multi-strategy slot")
            try:
                entry = get_registry().get(str(strategy_name))
            except KeyError as exc:
                raise DataIntegrityError(
                    f"[fail-loud/NF-1] 未知回测策略: {strategy_name!r}（{exc}）"
                ) from exc
            raw_params = slot.get("params")
            params = dict(raw_params) if isinstance(raw_params, dict) else {}
            strategy_slots.append(
                StrategySlot(
                    label=str(label),
                    symbol=str(symbol),
                    strategy=entry.build(params),
                    df=df,
                )
            )

        engine = MultiStrategyEngine(
            strategies=strategy_slots,
            total_cash=total_cash,
            commission=commission,
            min_commission=min_commission,
            stamp_tax=stamp_tax,
            slippage=slippage,
            execution=execution,
        )
        payload = self._serialize_result(engine.run())
        return {
            "total_performance": require_key(
                payload, "total_performance", "MultiStrategyResult.total_performance"
            ),
            "individual_results": require_key(
                payload, "individual_results", "MultiStrategyResult.individual_results"
            ),
            "equity_allocation": require_key(
                payload, "equity_allocation", "MultiStrategyResult.equity_allocation"
            ),
            "combined_equity": self._normalize_equity_dates(
                require_key(
                    payload, "combined_equity", "MultiStrategyResult.combined_equity"
                )
            ),
        }

    def run_optimize(
        self,
        df: pd.DataFrame,
        strategy_name: str,
        param_grid: dict[str, list[Any]],
        *,
        cash: float = 1_000_000.0,
        commission: float = 0.0003,
        min_commission: float = 5.0,
        stamp_tax: float = 0.001,
        slippage: float = 0.0,
        execution: str = "next_open",
        max_grid_points: int = 200,
    ) -> dict[str, Any]:
        """单标的参数网格寻优（D-4 / O1–O3）。

        桥内直调上游 :class:`ParamGridOptimizer`（R2 合规）——笛卡尔积遍历
        1-2 个参数的取值组合，每点独立回测后按 ``total_return`` 降序，输出
        ``{strategy, param_names, results, best, heatmap}``（结构见草案 §2.3
        O3 DTO）。

        Args:
            df: 单标的日线 DataFrame（含 datetime/open/high/low/close/vol/amount）。
            strategy_name: 上游注册策略名。
            param_grid: 参数取值网格 ``{param_name: [values...]}``（1-2 个参数）。
            cash: 初始资金（每个网格点共用）。
            commission / min_commission / stamp_tax / slippage / execution:
                与 :meth:`run_strategy_backtest` 同口径。
            max_grid_points: 网格点（笛卡尔积）上限，默认 200（对齐上游
                ``optimizer.MAX_GRID_POINTS``）。

        Returns:
            ``{strategy, param_names, results, best, heatmap}`` 全 JSON 安全
            （NaN/Inf → None，numpy → 原生）。

        Raises:
            DataIntegrityError: 网格为空 / 参数数超出 1-2 / 网格点超限 /
                策略名不存在（fail-loud，路由层对网格超限以 400 预校验）。
            UpstreamContractError: 上游返回结构缺关键字段。
        """
        if not isinstance(param_grid, dict) or not param_grid:
            raise DataIntegrityError(
                "[fail-loud/NF-26] run_optimize param_grid 为空"
            )
        if len(param_grid) > 2:
            raise DataIntegrityError(
                f"[fail-loud/NF-26] run_optimize 最多支持 2 个寻优参数，"
                f"实际 {len(param_grid)} 个"
            )
        size = 1
        for name, values in param_grid.items():
            if not isinstance(values, (list, tuple)) or len(values) == 0:
                raise DataIntegrityError(
                    f"[fail-loud/NF-26] run_optimize 参数 {name!r} 的取值列表为空"
                )
            size *= len(values)
        if size > max_grid_points:
            raise DataIntegrityError(
                f"[fail-loud/NF-26] 网格大小 {size} 超过上限 {max_grid_points}，"
                f"拒绝组合爆炸（笛卡尔积 {list(param_grid.keys())}）"
            )
        try:
            entry = get_registry().get(strategy_name)
        except KeyError as exc:
            raise DataIntegrityError(
                f"[fail-loud/NF-1] 未知回测策略: {strategy_name!r}（{exc}）"
            ) from exc
        # 校验策略可构造（寻优前置，避免网格全失败后才暴露）
        entry.build({})

        optimizer = ParamGridOptimizer(
            strategy_name=strategy_name,
            param_grid=param_grid,
            df=df,
            cash=cash,
            commission=commission,
            min_commission=min_commission,
            stamp_tax=stamp_tax,
            slippage=slippage,
            execution=execution,
        )
        payload = self._serialize_result(optimizer.run())
        for key in ("strategy", "param_names", "results", "best", "heatmap"):
            require_key(payload, key, f"OptimizeResult.{key}")
        return payload

    def signal_points(
        self,
        df: pd.DataFrame,
        strategy_name: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """单标的策略买卖点标注（B5，K 线图叠加用）。

        买卖点是**信号标注**（数据结构，非下单动作，R5 允许）：跑一遍策略
        回测，把成交信号序列按方向拆成 ``buy_points`` / ``sell_points``
        （``{date, price}``）。与上游 :class:`SignalScanner` 的单标的信号
        检测同源（Scanner 对单标的即取该策略回测的末笔信号；这里返回完整
        信号序列）。

        Args:
            df: 单标的日线 DataFrame（含 datetime 列）。
            strategy_name: 上游注册策略名。
            params: 策略参数；缺失取默认值。

        Returns:
            ``{"buy_points": [{date, price}], "sell_points": [{date, price}]}``，
            ``date`` 为 ``YYYY-MM-DD``，``price`` 为成交价（JSON 安全）。

        Raises:
            DataIntegrityError: 策略名不存在 / 回测结果缺 trades 字段。
        """
        result = self.run_strategy_backtest(df, strategy_name, params)
        trades = require_key(result, "trades", "BacktestResult.trades")
        buy_points: list[dict[str, Any]] = []
        sell_points: list[dict[str, Any]] = []
        for trade in trades:
            if not isinstance(trade, dict):
                raise UpstreamContractError(
                    f"[fail-loud/NF-1] 回测成交行应为 dict，实际为 "
                    f"{type(trade).__name__}，上游契约可能已变更"
                )
            direction = str(require_key(trade, "direction", "Trade.direction"))
            point: dict[str, Any] = {
                "date": self._normalize_trade_date(
                    require_key(trade, "datetime", "Trade.datetime")
                ),
                "price": self._clean_value(
                    require_key(trade, "price", "Trade.price")
                ),
            }
            if direction == "BUY":
                buy_points.append(point)
            elif direction == "SELL":
                sell_points.append(point)
            else:
                raise UpstreamContractError(
                    f"[fail-loud/NF-1] Trade.direction 取值非法: {direction!r}，"
                    f"上游契约可能已变更"
                )
        return {"buy_points": buy_points, "sell_points": sell_points}

    @staticmethod
    def _normalize_trade_date(raw: Any) -> str:
        """把成交日期规整为 ``YYYY-MM-DD``（契约 §1.7）。

        上游 ``Trade.datetime`` 为 ``YYYYMMDD`` 整数（如 ``20240101``）；
        ``_clean_value`` 会保留整数原样，这里统一转日期字符串。
        """
        if isinstance(raw, int) and not isinstance(raw, bool):
            text = str(raw)
            if len(text) == 8 and text.isdigit():
                return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
            return text
        text = str(raw)
        return text[:10] if len(text) >= 10 else text

    @staticmethod
    def _normalize_equity_dates(rows: list[Any]) -> list[dict[str, Any]]:
        """把组合净值曲线的 datetime 规整为 ``YYYY-MM-DD``（契约 §1.7）。

        上游 ``PortfolioResult.to_dict`` 的 ``combined_equity`` 里 datetime 是
        完整时间戳（``2020-01-02T00:00:00``）；契约口径为日期字符串，这里在
        适配层统一截断到日期部分（与 B4 组合净值曲线一致）。
        """
        out: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                raise UpstreamContractError(
                    f"[fail-loud/NF-1] 组合净值行应为 dict，实际为 "
                    f"{type(row).__name__}，上游契约可能已变更"
                )
            item: dict[str, Any] = {}
            for key, value in row.items():
                if key == "datetime":
                    item[key] = str(value)[:10]
                else:
                    item[key] = value
            out.append(item)
        return out

    @staticmethod
    def _perf(perf: dict[str, Any], key: str) -> float:
        """从绩效字典取浮点指标；缺失时显式报错而非静默默认 0（NF-26）。"""
        if key not in perf:
            raise DataIntegrityError(
                f"[fail-loud/NF-1] 回测绩效缺少指标 {key!r}，上游契约可能已变更"
            )
        return round(float(perf[key]), 6)

    def run_rebalance(
        self,
        data: dict[str, pd.DataFrame],
        factor_name: str,
        *,
        n_stocks: int = 50,
        rebalance_freq: str = "M",
        cash: float = 1_000_000.0,
    ) -> dict[str, Any]:
        """多期调仓回测（RebalanceEngine 包装）。"""
        require_known(
            rebalance_freq, "调仓频率", allowed={"W", "M", "Q"}
        )
        engine = RebalanceEngine(
            optimizer=EqualWeightOptimizer(),
            factor_name=factor_name,
            n_stocks=int(n_stocks),
            rebalance_freq=rebalance_freq,
            cash=cash,
        )
        all_dates = pd.concat(
            [d["datetime"] for d in data.values() if "datetime" in d.columns],
            ignore_index=True,
        )
        if all_dates.empty:
            return {"rebalance_dates": 0, "total_return": 0.0}
        start = int(pd.Timestamp(all_dates.min()).strftime("%Y%m%d"))
        end = int(pd.Timestamp(all_dates.max()).strftime("%Y%m%d"))
        result = engine.run(data, start_date=start, end_date=end)
        dates = getattr(result, "rebalance_dates", [])
        return {
            "rebalance_dates": len(dates),
            "total_return": round(
                float(getattr(result, "total_return", 0.0) or 0.0), 6
            ),
        }

    # ------------------------------------------------------------------ #
    # 信号扫描 / 强势排名
    # ------------------------------------------------------------------ #

    @staticmethod
    def _serialize_result(result: Any) -> dict[str, Any]:
        """把上游 BacktestResult 清洗为纯 JSON 兼容字典（NF-12）。

        递归处理 performance / equity_curve / trades / positions / config：
        numpy scalar → Python 原生、NaN/Inf → None、datetime → ISO 字符串。
        上游 ``BacktestResult.to_dict()`` 只把 DataFrame 转 records，
        不做 NaN/类型清洗——这里在适配层补齐，业务层拿到即可直接落库/渲染。
        """
        if hasattr(result, "to_dict"):
            result = result.to_dict()
        return {str(k): BacktestBridge._clean_value(v) for k, v in result.items()}

    @staticmethod
    def _clean_value(value: Any) -> Any:
        """递归清洗值为 JSON 安全形态（numpy / NaN / datetime）。"""
        if value is None or isinstance(value, (str, bool, int)):
            return value
        if isinstance(value, float):
            if value != value or value in (float("inf"), float("-inf")):
                return None
            return round(float(value), 6)
        if isinstance(value, dict):
            return {
                str(k): BacktestBridge._clean_value(v) for k, v in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [BacktestBridge._clean_value(v) for v in value]
        # numpy scalar / pandas Timestamp 等
        if hasattr(value, "item"):
            return BacktestBridge._clean_value(value.item())
        if hasattr(value, "isoformat"):
            try:
                return value.isoformat()
            except (TypeError, ValueError):
                return str(value)
        return str(value)

    def scan_signals(
        self,
        strategy_cls: type[Strategy],
        vipdoc_path: str,
        *,
        universe: str = "all",
        workers: int = 0,
    ) -> list[dict[str, Any]]:
        """全市场信号扫描（SignalScanner 包装，纯离线读 .day）。"""
        scanner = SignalScanner(strategy_cls=strategy_cls, vipdoc_path=vipdoc_path)
        results: list[ScanResult] = scanner.scan(universe=universe, workers=workers)
        return [
            {
                "code": r.code,
                "market": str(r.market).lower(),
                "signal_date": int(r.signal_date),
                "last_close": round(float(r.last_close), 6),
            }
            for r in results
        ]

    def strength_rank(
        self,
        vipdoc_path: str,
        *,
        preset: str = "balanced",
        top_n: int = 50,
        universe: str = "all",
    ) -> list[dict[str, Any]]:
        """全市场强势股排名（StrengthRanker 包装，纯离线读 .day）。"""
        require_known(preset, "强势预设", allowed=set(STRENGTH_PRESETS))
        ranker = StrengthRanker(vipdoc_path=vipdoc_path, preset=preset)
        results: list[StrengthResult] = ranker.rank(universe=universe, top_n=top_n)
        return [
            {
                "rank": int(r.rank),
                "code": r.code,
                "market": str(r.market).lower(),
                "last_close": round(float(r.last_close), 6),
                "strength": round(float(r.strength), 6),
                "ret_5": round(float(r.ret_5), 6),
                "ret_20": round(float(r.ret_20), 6),
                "ret_60": round(float(r.ret_60), 6),
            }
            for r in results
        ]

    # ------------------------------------------------------------------ #
    # 缠论
    # ------------------------------------------------------------------ #

    def chanlun_analyze(
        self, df: pd.DataFrame, *, code: str = "000000"
    ) -> dict[str, Any]:
        """缠论分析单标的日线，输出摘要（ChanlunAnalyser 包装）。"""
        analyser = ChanlunAnalyser(code=code, frequency="DAILY")
        result: ChanlunResult = analyser.process_klines(df)
        summary = result.to_dict()
        latest_is_buy = False
        mmds = summary.get("mmds")
        if mmds is None:
            mmds = []
        if mmds:
            latest_entry = mmds[-1]
            latest_type = str(latest_entry["type"]) if "type" in latest_entry else ""
            latest_is_buy = "buy" in latest_type
        return {
            "bi_count": int(summary["bi_count"]) if "bi_count" in summary else 0,
            "zs_count": int(summary["zs_count"]) if "zs_count" in summary else 0,
            "mmd_count": int(summary["mmd_count"]) if "mmd_count" in summary else 0,
            "latest_is_buy": latest_is_buy,
        }
