"""参数寻优服务（O1–O3 业务层，经 BacktestBridge 调上游 ParamGridOptimizer，R2）。

设计
----
- **运行（O1）**：:class:`OptimizeService` —— 单标的（1 code）× 1-2 参数网格
  （笛卡尔积 ≤200，路由层 400 预校验 + 桥内 DataIntegrityError 兜底），
  经 :meth:`BacktestBridge.run_optimize` 直调上游 ``ParamGridOptimizer``
  （R2 合规，results/best/heatmap 原生结构）。
- **数据源**：K 线一律经 :class:`L1Reader` 读本地 vipdoc（零网络，数据源独立），
  日期过滤复用 :func:`~Kuantix.backtest.portfolio_service._load_frame` 共享工具。
- **市场规则**：代码→交易所映射经 :class:`MarketProfile`（R6，不硬编码）。
- **结果落库**：完整结果以 ``job_id`` 为键落
  :class:`~Kuantix.backtest.store.BacktestResultStore`（与 B4/P3 同库同 schema，
  互不冲突），``action=optimize`` 区分。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from Kuantix.adapters.backtest_bridge import BacktestBridge
from Kuantix.adapters.factor_bridge import L1Reader
from Kuantix.backtest.portfolio_service import MIN_BARS, _load_frame
from Kuantix.backtest.store import BacktestResultStore
from Kuantix.config import Config, get_config
from Kuantix.core.fail_loud import (
    DataIntegrityError,
    MissingKeyError,
    require_key,
)

__all__ = [
    "OptimizeService",
    "OptimizeRunRequest",
    "OptimizeAllRunRequest",
    "MAX_GRID_POINTS",
    "MAX_GRID_PARAMS",
]

#: 网格点（笛卡尔积）上限（对齐上游 optimizer.MAX_GRID_POINTS，O1 契约）
MAX_GRID_POINTS = 200

#: 寻优参数个数上限（1-2，草案 §2.3）
MAX_GRID_PARAMS = 2


def grid_size(param_grid: dict[str, list[Any]]) -> int:
    """计算参数网格的笛卡尔积大小（O1 路由层 400 预校验用）。"""
    size = 1
    for values in param_grid.values():
        size *= len(values)
    return size


@dataclass(frozen=True)
class OptimizeRunRequest:
    """参数寻优请求（O1 领域对象，单标的 × 1-2 参数网格）。

    Attributes:
        market: 市场码（P1 仅 ``CN``）。
        code: 单标的代码（6 位，非空）。
        strategy: 上游策略名。
        param_grid: 参数取值网格（1-2 个参数，笛卡尔积 ≤200）。
        start / end: 回测区间（含）。
        cash: 初始资金（每个网格点共用）。
        commission / min_commission / stamp_tax / slippage / execution: 成本配置。
    """

    market: str = "CN"
    code: str = ""
    strategy: str = "ma_cross"
    param_grid: dict[str, list[Any]] = field(default_factory=dict)
    start: dt.date = dt.date(2020, 1, 1)
    end: dt.date = dt.date(2025, 12, 31)
    cash: float = 1_000_000.0
    commission: float = 0.0003
    min_commission: float = 5.0
    stamp_tax: float = 0.001
    slippage: float = 0.0
    execution: str = "next_open"


@dataclass(frozen=True)
class OptimizeAllRunRequest:
    """一键寻优所有策略请求（对标 easy_tdx ``optimize-all``）。

    在**单个标的上**，对所有**已注册**策略的**预设参数网格**（见
    ``easy_tdx.backtest.strategies.presets.STRATEGY_PRESETS``）依次做网格寻优，
    取各策略最优点汇总成全局策略排名（``ranking``/``best``/``per_strategy``）。
    ``workers`` ≥2 时跨进程并行（CPU-bound，线程无加速）。

    Attributes:
        market: 市场码（仅 ``CN``）。
        code: 单标的代码（6 位，非空）。
        start / end: 回测区间（含）。
        cash: 每个网格点共用初始资金。
        commission / min_commission / stamp_tax / slippage / execution: 成本配置。
        workers: 并行进程数（0/1=串行，2+=ProcessPoolExecutor）。
    """

    market: str = "CN"
    code: str = ""
    start: dt.date = dt.date(2020, 1, 1)
    end: dt.date = dt.date(2025, 12, 31)
    cash: float = 1_000_000.0
    commission: float = 0.0003
    min_commission: float = 5.0
    stamp_tax: float = 0.001
    slippage: float = 0.0
    execution: str = "next_open"
    workers: int = 0


class OptimizeService:
    """参数寻优服务门面（O1/O3；测试可注入假 reader/bridge/store）。"""

    def __init__(
        self,
        config: Config | None = None,
        *,
        reader: L1Reader | None = None,
        bridge: BacktestBridge | None = None,
        store: BacktestResultStore | None = None,
    ) -> None:
        self._config = config if config is not None else get_config()
        # 数据源：经 ``L1Reader.from_config`` 装配 SQLite 主存储（auto 后端，
        # SQLite 优先、vipdoc 镜像兜底），与 factor/搜索「从 SQLite 取样本」
        # 口径一致 —— 不复用旧单参数构造（退化纯镜像、读 vipdoc 文件）。
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
        req: OptimizeRunRequest,
        progress_cb: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """执行一次参数寻优并落完整结果到 store（后台线程内调用）。

        Returns:
            Job.result_summary 用轻量摘要（``{action, market, code, strategy,
            grid_size, param_names, result_count, best}``）。

        Raises:
            MissingKeyError: code 为空 / param_grid 为空。
            DataIntegrityError: K 线读取失败 / 网格超限 / 策略名非法
                （全部失败 → job failed 422，fail-loud）。
        """
        if not req.code.strip():
            raise MissingKeyError("[fail-loud/NF-26] 寻优 code 为空")
        if not isinstance(req.param_grid, dict) or not req.param_grid:
            raise MissingKeyError("[fail-loud/NF-26] 寻优 param_grid 为空")
        size = grid_size(req.param_grid)
        if size > MAX_GRID_POINTS:
            raise DataIntegrityError(
                f"[fail-loud/NF-26] 网格大小 {size} 超过上限 {MAX_GRID_POINTS}"
            )

        from Kuantix.core.market import get_market_profile

        profile = get_market_profile(req.market)
        code = req.code.strip()
        if progress_cb is not None:
            progress_cb(
                {
                    "stage": "load",
                    "total": 1,
                    "done": 0,
                    "current": code,
                    "percent": 0.0,
                    "failed": 0,
                    "quarantined": 0,
                }
            )
        frame = _load_frame(self._reader, profile, code, req.start, req.end)
        if len(frame) < MIN_BARS:
            raise DataIntegrityError(
                f"[fail-loud/NF-26] {code} K 线不足 {MIN_BARS} 根"
            )
        if progress_cb is not None:
            progress_cb(
                {
                    "stage": "optimize",
                    "total": size,
                    "done": 0,
                    "current": code,
                    "percent": 0.0,
                    "failed": 0,
                    "quarantined": 0,
                }
            )

        result = self._bridge.run_optimize(
            frame,
            req.strategy,
            req.param_grid,
            cash=req.cash,
            commission=req.commission,
            min_commission=req.min_commission,
            stamp_tax=req.stamp_tax,
            slippage=req.slippage,
            execution=req.execution,
        )
        result["market"] = req.market
        result["code"] = code
        result["start_date"] = req.start.isoformat()
        result["end_date"] = req.end.isoformat()
        result["config"] = {
            "cash": req.cash,
            "commission": req.commission,
            "min_commission": req.min_commission,
            "stamp_tax": req.stamp_tax,
            "slippage": req.slippage,
            "execution": req.execution,
        }
        result["grid_size"] = size
        self._store.save(job_id, result)

        best = require_key(result, "best", "OptimizeResult.best")
        param_names = require_key(result, "param_names", "OptimizeResult.param_names")
        summary = {
            "action": "optimize",
            "market": req.market,
            "code": code,
            "strategy": req.strategy,
            "grid_size": size,
            "param_names": list(param_names),
            "result_count": len(require_key(result, "results", "OptimizeResult.results")),
            "best": best,
        }
        return summary

    # ------------------------------------------------------------------ #
    # 一键寻优所有策略（对标 easy_tdx optimize-all）
    # ------------------------------------------------------------------ #

    def run_all(
        self,
        job_id: str,
        req: OptimizeAllRunRequest,
        progress_cb: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """对所有已注册策略的预设网格逐策略寻优，汇总全局排名并落库。

        Returns:
            Job.result_summary 用轻量摘要（``{action, market, code, strategy,
            total_strategies, total_grid_points, ranked_count, best}``）。

        Raises:
            MissingKeyError: code 为空。
            DataIntegrityError: K 线不足 / 无策略可寻优。
        """
        if not req.code.strip():
            raise MissingKeyError("[fail-loud/NF-26] 寻优 code 为空")

        from Kuantix.core.market import get_market_profile

        profile = get_market_profile(req.market)
        code = req.code.strip()
        if progress_cb is not None:
            progress_cb(
                {
                    "stage": "load",
                    "total": 1,
                    "done": 0,
                    "current": code,
                    "percent": 0.0,
                    "failed": 0,
                    "quarantined": 0,
                }
            )
        frame = _load_frame(self._reader, profile, code, req.start, req.end)
        if len(frame) < MIN_BARS:
            raise DataIntegrityError(
                f"[fail-loud/NF-26] {code} K 线不足 {MIN_BARS} 根"
            )

        # 枚举已注册策略的预设网格（R2：经 BacktestBridge 访问 easy_tdx）
        presets = self._bridge.list_strategy_presets()
        jobs: list[tuple[str, dict[str, list[Any]]]] = []
        labels: dict[str, str] = {}
        for strategy_name, entry in presets.items():
            labels[strategy_name] = entry["label"]
            jobs.append((strategy_name, entry["grid"]))
        if not jobs:
            raise DataIntegrityError(
                "[fail-loud/NF-26] 无已注册策略的预设网格可寻优"
            )

        if progress_cb is not None:
            progress_cb(
                {
                    "stage": "optimize-all",
                    "total": len(jobs),
                    "done": 0,
                    "current": code,
                    "percent": 0.0,
                    "failed": 0,
                    "quarantined": 0,
                }
            )

        raw_results = self._run_all_strategies(
            frame,
            jobs,
            req,
        )
        # 组装全局排名（按 total_return 降序）
        ranking: list[dict[str, Any]] = []
        per_strategy: dict[str, dict[str, Any]] = {}
        total_grid = 0
        for res in raw_results:
            strategy_name = str(res["strategy"])
            entry = {
                "strategy": strategy_name,
                "strategy_label": labels[strategy_name],
                "params": res["params"],
                "total_return": res["total_return"],
                "annual_return": res.get("annual_return"),
                "sharpe": res["sharpe"],
                "max_drawdown": res["max_drawdown"],
                "total_trades": res["total_trades"],
                "win_rate": res["win_rate"],
                "profit_factor": res["profit_factor"],
                "grid_points": res["grid_points"],
            }
            ranking.append(entry)
            per_strategy[strategy_name] = entry
            total_grid += int(res["grid_points"])

        ranking.sort(key=lambda r: r["total_return"], reverse=True)
        best = ranking[0] if ranking else None

        result: dict[str, Any] = {
            "market": req.market,
            "code": code,
            "start_date": req.start.isoformat(),
            "end_date": req.end.isoformat(),
            "ranking": ranking,
            "best": best,
            "per_strategy": per_strategy,
            "total_strategies": len(jobs),
            "total_grid_points": total_grid,
        }
        self._store.save(job_id, result)

        summary = {
            "action": "optimize-all",
            "market": req.market,
            "code": code,
            "strategy": "all",
            "total_strategies": len(jobs),
            "ranked_count": len(ranking),
            "total_grid_points": total_grid,
            "best": best,
        }
        return summary

    def _run_all_strategies(
        self,
        frame: pd.DataFrame,
        jobs: list[tuple[str, dict[str, list[Any]]]],
        req: OptimizeAllRunRequest,
    ) -> list[dict[str, Any]]:
        """逐策略跑网格寻优取最优点；``workers``≥2 用 ProcessPool 并行。

        单策略无有效结果（best 为 None）则跳过，不中断整组。
        """
        # 串行（默认）
        if not req.workers or req.workers < 2:
            results: list[dict[str, Any]] = []
            for name, grid in jobs:
                res = _optimize_one_strategy_summary(
                    self._bridge, frame, name, grid, req
                )
                if res is not None:
                    results.append(res)
            return results

        # 进程池并行：桥实例不可 pickle，改为在子进程内自建桥（同 SQLite 数据源）
        import concurrent.futures

        results: list[dict[str, Any]] = []
        with concurrent.futures.ProcessPoolExecutor(
            max_workers=int(req.workers)
        ) as executor:
            futures = {
                executor.submit(
                    _optimize_one_strategy_process,
                    frame,
                    name,
                    grid,
                    req,
                ): name
                for name, grid in jobs
            }
            for future in concurrent.futures.as_completed(futures):
                res = future.result()
                if res is not None:
                    results.append(res)
        return results

    # ------------------------------------------------------------------ #
    # 结果查询
    # ------------------------------------------------------------------ #

    def get_result(self, job_id: str) -> dict[str, Any] | None:
        """按 job_id 读取完整寻优结果；不存在返回 ``None``（路由层映射 404）。"""
        return self._store.load(job_id)

    def delete_result(self, job_id: str) -> bool:
        """按 job_id 删除完整寻优结果；不存在返回 ``False``。"""
        return self._store.delete(job_id)


# --------------------------------------------------------------------------- #
# 模块级辅助（供 run_all 串行 / 进程池复用）
# --------------------------------------------------------------------------- #


def _optimize_one_strategy_summary(
    bridge: BacktestBridge,
    frame: pd.DataFrame,
    strategy_name: str,
    grid: dict[str, list[Any]],
    req: OptimizeAllRunRequest,
) -> dict[str, Any] | None:
    """跑单个策略的网格寻优，返回其最优点摘要；无有效结果返回 ``None``。"""
    try:
        payload = bridge.run_optimize(
            frame,
            strategy_name,
            grid,
            cash=req.cash,
            commission=req.commission,
            min_commission=req.min_commission,
            stamp_tax=req.stamp_tax,
            slippage=req.slippage,
            execution=req.execution,
        )
    except (DataIntegrityError, ValueError):
        return None
    best = payload.get("best")
    if best is None:
        return None
    return {
        "strategy": strategy_name,
        "params": best.get("params"),
        "total_return": best.get("total_return"),
        "annual_return": best.get("annual_return"),
        "sharpe": best.get("sharpe"),
        "max_drawdown": best.get("max_drawdown"),
        "total_trades": best.get("total_trades"),
        "win_rate": best.get("win_rate"),
        "profit_factor": best.get("profit_factor"),
        "grid_points": len(payload.get("results") or []),
    }


def _optimize_one_strategy_process(
    frame: pd.DataFrame,
    strategy_name: str,
    grid: dict[str, list[Any]],
    req: OptimizeAllRunRequest,
) -> dict[str, Any] | None:
    """进程池 worker：子进程内自建 BacktestBridge（复用 SQLite 数据源），跑单策略寻优。

    必须是模块级顶层函数（ProcessPoolExecutor spawn 可 pickle）。``req`` 为
    frozen dataclass 可安全跨进程传递；桥在子进程内新建，规避不可 pickle。
    """
    from Kuantix.adapters.backtest_bridge import BacktestBridge

    bridge = BacktestBridge()
    return _optimize_one_strategy_summary(bridge, frame, strategy_name, grid, req)
