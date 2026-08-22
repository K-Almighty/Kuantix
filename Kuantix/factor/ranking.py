"""多因子回测综合排名（对标 easy_tdx 的多因子对比展示）。

设计
----
单个因子的 :meth:`FactorService.report` 只给 IC/IR/分层收益等**因子有效性**
指标，缺少「把因子当作选股信号回测跑出来的绩效」——用户希望像 easy_tdx 那样
对多个因子做**横向对比 + 排名**（收益率 / 夏普 / 最大回撤等）。

:class:`FactorRankingService.rank` 做：
1. **一次**加载代码池 + 一次计算前向收益（多因子共享，避免逐因子全量 IO）；
2. 对每个因子，用其截面值在前向收益上构建 **top 分位等权组合**的逐期收益
   序列，据此推导回测绩效：``total_return`` / ``annual_return`` /
   ``max_drawdown`` / ``sharpe`` / ``win_rate`` / ``turnover_rate``；
3. 合并因子有效性（``ic_mean`` / ``ir`` / ``top_minus_bottom``）；
4. 按 **综合评分**（可配权重，默认对主要指标 z-score 等权）降序排名；
5. 返回 ``{ranking, comparison, columns, weights}`` 供前端表格/图表展示。

数据源
------
因子值 / 前向收益全部来自**本地**（SQLite ``daily_bars`` 经 :class:`L1Reader`
读池 + 因子 L2 parquet），零网络；与「从 SQLite 取样本」口径一致。

边界
----
- 因子无已算数据 → 该因子跳过并计入 ``skipped``（fail-loud，不静默）；
- 全部因子均无数据 → 显式 :class:`DataIntegrityError`。
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Any, Iterable

import numpy as np
import pandas as pd

from Kuantix.core.fail_loud import DataIntegrityError, require_non_empty
from Kuantix.core.market import get_market_profile
from Kuantix.factor.service import ComputeRequest, FactorService

__all__ = [
    "FactorRankingService",
    "RankingConfig",
    "DEFAULT_WEIGHTS",
]

#: 综合评分默认权重（z-score 后等权；键为排名指标名）
DEFAULT_WEIGHTS: dict[str, float] = {
    "total_return": 0.20,
    "sharpe": 0.20,
    "max_drawdown": 0.15,
    "ic_mean": 0.15,
    "ir": 0.15,
    "top_minus_bottom": 0.15,
}

#: 一年交易周期数（CN 约 242 个交易日）
PERIODS_PER_YEAR = 242.0

#: 未指定区间时默认的排名窗口长度（自然日，约 2 年）
_RANKING_WINDOW_DAYS = 730


@dataclass(frozen=True)
class RankingConfig:
    """多因子排名配置。

    Attributes:
        forward_period: 前向收益周期（交易日）。
        n_quantiles: 分层数（与因子分析口径一致，top 组合取最后 1 层）。
        top_fraction: top 组合占标的比例（0<..<=1，取前 ``top_fraction`` 的
            标的等权，默认 0.2 = 前 20%）。
        weights: 综合评分权重；``None`` 用 :data:`DEFAULT_WEIGHTS`。
    """

    forward_period: int = 5
    n_quantiles: int = 5
    top_fraction: float = 0.2
    weights: dict[str, float] | None = None


@dataclass
class FactorRank:
    """单因子排名条目（JSON 安全字典化后输出）。

    Attributes:
        rank: 综合排名（1 起）。
        factor: 因子名。
        total_return: top 组合累计收益（小数）。
        annual_return: 年化收益（小数）。
        max_drawdown: 最大回撤（正数，小数）。
        sharpe: 夏普比率。
        win_rate: 胜率（小数）。
        turnover_rate: top 组合换手率（小数）。
        ic_mean: Rank IC 均值（小数）。
        ir: 信息比率。
        top_minus_bottom: 多空分层差（小数）。
        score: 综合评分（0-100，便于展示）。
    """

    rank: int = 0
    factor: str = ""
    total_return: float | None = None
    annual_return: float | None = None
    max_drawdown: float | None = None
    sharpe: float | None = None
    win_rate: float | None = None
    turnover_rate: float | None = None
    ic_mean: float | None = None
    ir: float | None = None
    top_minus_bottom: float | None = None
    score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """JSON 安全字典（NaN/Inf → None，NF-12）。"""
        return {
            "rank": int(self.rank),
            "factor": self.factor,
            "total_return": _safe(self.total_return),
            "annual_return": _safe(self.annual_return),
            "max_drawdown": _safe(self.max_drawdown),
            "sharpe": _safe(self.sharpe),
            "win_rate": _safe(self.win_rate),
            "turnover_rate": _safe(self.turnover_rate),
            "ic_mean": _safe(self.ic_mean),
            "ir": _safe(self.ir),
            "top_minus_bottom": _safe(self.top_minus_bottom),
            "score": _safe(self.score),
        }


def _safe(value: float | None) -> float | None:
    """NaN/Inf → None（NF-12：JSON 不得含 NaN/Inf）。"""
    if value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(num):
        return None
    return round(num, 6)


class FactorRankingService:
    """多因子回测综合排名服务（经 :class:`FactorService` 复用 L1/存储/引擎）。"""

    def __init__(self, factor_service: FactorService) -> None:
        self._svc = factor_service

    # ------------------------------------------------------------------ #
    # rank
    # ------------------------------------------------------------------ #

    def rank(
        self,
        factors: Iterable[str],
        market: str = "CN",
        *,
        start: Any = None,
        end: Any = None,
        config: RankingConfig | None = None,
    ) -> dict[str, Any]:
        """对多因子做综合排名。

        Args:
            factors: 待排名因子列表（升序去重）。
            market: 市场码。
            start: 回测起始日（``datetime.date``）；``None`` 用默认区间起点。
            end: 回测结束日；``None`` 用默认区间终点。
            config: 排名配置；``None`` 用默认。

        Returns:
            ``{market, start, end, forward_period, n_quantiles, top_fraction,
            weights, columns, ranking}``；``ranking`` 为 :class:`FactorRank`
            字典列表（按综合评分降序）。

        Raises:
            DataIntegrityError: 全部因子均无已算数据。
        """
        from Kuantix.factor.service import ComputeRequest

        factor_list = sorted(dict.fromkeys(str(f) for f in factors))
        require_non_empty(factor_list, "rank.factors")
        cfg = config if config is not None else RankingConfig()
        start_dt = start
        end_dt = end

        # 1. 一次加载池 + 一次前向收益（多因子共享，避免逐因子全量 IO）。
        #    未指定区间时默认取「因子库最新可用日往前约 2 年」的尾部窗口：
        #    全市场截面 + 前向收益在受限区间内计算，既保证有足够截面样本，
        #    又避免对 2020 年起全量历史做前向收益（耗时长、收益被拉爆）。
        if start_dt is None or end_dt is None:
            latest = _latest_factor_date(self._svc, factor_list)
            if end_dt is None:
                end_dt = latest if latest is not None else dt.date.today()
            if start_dt is None:
                start_dt = end_dt - dt.timedelta(days=_RANKING_WINDOW_DAYS)

        profile = get_market_profile(market)
        req = ComputeRequest(
            market=market, factors=tuple(factor_list), start=start_dt, end=end_dt
        )
        pool = self._svc._load_pool(req, profile)
        if not pool:
            raise DataIntegrityError(
                "[fail-loud/NF-26] 排名代码池为空，无可用行情样本"
            )
        return_df = self._svc._engine.compute_forward_returns(
            pool, period=int(cfg.forward_period)
        )
        return_col = f"forward_{cfg.forward_period}d"

        start_int = _date_to_int(start_dt) if start_dt is not None else None
        end_int = _date_to_int(end_dt) if end_dt is not None else None

        # 2. 逐因子构建 top 组合回测 + 因子有效性
        ranks: list[FactorRank] = []
        skipped: list[str] = []
        weights = dict(cfg.weights) if cfg.weights is not None else dict(DEFAULT_WEIGHTS)
        for factor in factor_list:
            factor_df = self._svc._store.load(
                factor,
                start=start_int,
                end=end_int,
            )
            if factor_df.empty:
                skipped.append(factor)
                continue
            item = self._factor_rank(
                factor,
                factor_df,
                return_df,
                return_col,
                int(cfg.n_quantiles),
                float(cfg.top_fraction),
                weights,
                market,
            )
            ranks.append(item)

        if not ranks:
            raise DataIntegrityError(
                "[fail-loud/NF-26] 所选因子均无已算数据，请先 compute"
                f"（skipped: {', '.join(skipped) or '无'}）"
            )

        # 3. 按综合评分降序排名
        ranks.sort(key=lambda r: (r.score, r.ic_mean or 0.0), reverse=True)
        for index, item in enumerate(ranks, start=1):
            item.rank = index

        return {
            "market": market,
            "start": _date_iso(start_dt),
            "end": _date_iso(end_dt),
            "forward_period": int(cfg.forward_period),
            "n_quantiles": int(cfg.n_quantiles),
            "top_fraction": float(cfg.top_fraction),
            "weights": weights,
            "columns": _COLUMNS,
            "skipped": skipped,
            "ranking": [r.to_dict() for r in ranks],
        }

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def _factor_rank(
        self,
        factor: str,
        factor_df: pd.DataFrame,
        return_df: pd.DataFrame,
        return_col: str,
        n_quantiles: int,
        top_fraction: float,
        weights: dict[str, float],
        market: str,
    ) -> FactorRank:
        """单因子的 top 组合回测绩效 + 有效性 + 综合分。

        有效性指标（IC/IR/多空差）直接在本方法内从 ``factor_df`` × 前向收益
        **一次 merge** 计算，**不**再调 :meth:`FactorService.report` —— 后者
        会对每个因子重新加载代码池并重算前向收益（全市场下极慢），这里复用
        rank() 已经算好的 ``return_df``，避免逐因子重复全量 IO。
        """
        # top 组合逐期收益序列（在前向收益上按因子值取前 top_fraction 等权）
        merged = _merged_factor_returns(
            factor_df, return_df, factor, return_col
        )
        portfolio = _top_portfolio_returns_from_merged(
            merged, factor, top_fraction
        )
        metrics = _performance_metrics(portfolio, periods_per_year=PERIODS_PER_YEAR)

        # 因子有效性：IC/IR/top_minus_bottom（复用已 merge 数据，零额外加载）
        ic_mean, ir, top_minus_bottom = _effectiveness_metrics(
            merged, factor, return_col, int(n_quantiles)
        )

        values = {
            "total_return": metrics["total_return"],
            "sharpe": metrics["sharpe"],
            "max_drawdown": metrics["max_drawdown"],
            "ic_mean": ic_mean,
            "ir": ir,
            "top_minus_bottom": top_minus_bottom,
        }
        score = _composite_score(values, weights)

        return FactorRank(
            factor=factor,
            total_return=metrics["total_return"],
            annual_return=metrics["annual_return"],
            max_drawdown=metrics["max_drawdown"],
            sharpe=metrics["sharpe"],
            win_rate=metrics["win_rate"],
            turnover_rate=metrics["turnover_rate"],
            ic_mean=ic_mean,
            ir=ir,
            top_minus_bottom=top_minus_bottom,
            score=score,
        )


#: 前端对比表列定义（label 用于表头）
_COLUMNS: list[dict[str, str]] = [
    {"key": "factor", "label": "因子"},
    {"key": "total_return", "label": "总收益率"},
    {"key": "annual_return", "label": "年化收益"},
    {"key": "max_drawdown", "label": "最大回撤"},
    {"key": "sharpe", "label": "夏普比率"},
    {"key": "win_rate", "label": "胜率"},
    {"key": "turnover_rate", "label": "换手率"},
    {"key": "ic_mean", "label": "IC均值"},
    {"key": "ir", "label": "IR"},
    {"key": "top_minus_bottom", "label": "多空差"},
    {"key": "score", "label": "综合评分"},
]


def _date_to_int(value: Any) -> int:
    """``datetime.date`` → ``YYYYMMDD`` 整数。"""
    return int(value.year) * 10000 + int(value.month) * 100 + int(value.day)


def _latest_factor_date(svc: FactorService, factors: list[str]) -> dt.date | None:
    """取若干因子的最新可用数据日（跨因子取最大；无数据返回 ``None``）。"""
    latest_int: int | None = None
    for factor in factors:
        df = svc._store.load(factor)
        if df is None or df.empty or "date" not in df.columns:
            continue
        max_int = int(df["date"].max())
        if latest_int is None or max_int > latest_int:
            latest_int = max_int
    if latest_int is None:
        return None
    return dt.date(latest_int // 10000, (latest_int // 100) % 100, latest_int % 100)


def _date_iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _merged_factor_returns(
    factor_df: pd.DataFrame,
    return_df: pd.DataFrame,
    factor: str,
    return_col: str,
) -> pd.DataFrame:
    """因子值 × 前向收益内连接（date/code/factor/``_r`` 四列）。"""
    merged = factor_df.rename(columns={"value": factor}).merge(
        return_df[["date", "code", return_col]].rename(columns={return_col: "_r"}),
        on=["date", "code"],
        how="inner",
    )
    if merged.empty:
        return merged
    return merged.dropna(subset=[factor, "_r"]).reset_index(drop=True)


def _top_portfolio_returns_from_merged(
    merged: pd.DataFrame, factor: str, top_fraction: float
) -> pd.Series:
    """按因子值构建 top 分位等权组合的逐期收益序列（index=date）。

    每个截面日期：取因子值最高的 ``top_fraction`` 标的（不足则按实际数），
    等权平均其前向收益作为该期组合收益。缺失日（无有效标的）丢弃。

    Args:
        merged: :func:`_merged_factor_returns` 输出。
        factor: 因子列名。
        top_fraction: top 组合占标的比例。

    Returns:
        index=date（升序）的逐期组合收益 Series。
    """
    if merged is None or merged.empty:
        return pd.Series(dtype=float)
    results: dict[int, float] = {}
    for date, sub in merged.groupby("date"):
        if sub.empty:
            continue
        n = max(1, int(round(len(sub) * top_fraction)))
        top = sub.nlargest(n, factor)
        if top.empty:
            continue
        results[int(date)] = float(top["_r"].mean())
    return pd.Series(results).sort_index()


def _effectiveness_metrics(
    merged: pd.DataFrame,
    factor: str,
    return_col: str,
    n_quantiles: int,
) -> tuple[float | None, float | None, float | None]:
    """从因子 × 前向收益算 IC 均值 / IR / 多空差（q5-q1 分层收益差）。

    Args:
        merged: 因子×收益长表。
        factor: 因子列名。
        return_col: 前向收益列名（合并后为 ``_r``，此处忽略）。
        n_quantiles: 分层数。

    Returns:
        ``(ic_mean, ir, top_minus_bottom)``（样本不足时为 ``None``）。
    """
    if merged is None or merged.empty:
        return (None, None, None)

    # 逐截面 Rank IC（spearman）
    ic_values: list[float] = []
    for _, sub in merged.groupby("date"):
        valid = sub[[factor, "_r"]].dropna()
        if len(valid) < 5:
            continue
        corr = valid[factor].corr(valid["_r"], method="spearman")
        if corr is not None and pd.notna(corr):
            ic_values.append(float(corr))
    if not ic_values:
        ic_mean = None
        ir = None
    else:
        ic_mean = float(np.mean(ic_values))
        ic_std = float(np.std(ic_values)) if len(ic_values) > 1 else 0.0
        ir = (ic_mean / ic_std) if ic_std > 0 else 0.0

    # 多空差：q5 组均值 - q1 组均值
    top_minus_bottom = None
    rows: list[float] = []
    for _, sub in merged.groupby("date"):
        valid = sub[[factor, "_r"]].dropna()
        if len(valid) < n_quantiles:
            continue
        try:
            valid = valid.copy()
            valid["_q"] = pd.qcut(
                valid[factor], n_quantiles, labels=False, duplicates="drop"
            )
        except ValueError:
            continue
        means = valid.groupby("_q")["_r"].mean()
        if len(means) >= n_quantiles:
            rows.append(float(means.iloc[-1] - means.iloc[0]))
    if rows:
        top_minus_bottom = float(np.mean(rows))

    return (ic_mean, ir, top_minus_bottom)


def _performance_metrics(
    returns: pd.Series, periods_per_year: float = PERIODS_PER_YEAR
) -> dict[str, float | None]:
    """从逐期组合收益推导绩效指标（total_return/annual/max_drawdown/sharpe/win_rate）。

    Returns:
        各指标小数或 ``None``（样本不足时）。
    """
    if returns is None or len(returns) == 0:
        return {
            "total_return": None,
            "annual_return": None,
            "max_drawdown": None,
            "sharpe": None,
            "win_rate": None,
            "turnover_rate": None,
        }
    values = returns.to_numpy(dtype=float)
    # 累计收益（复合）
    compounded = float(np.prod(1.0 + values)) - 1.0
    n = len(values)
    # 年化收益
    annual = None
    if compounded > -1.0:
        annual = (1.0 + compounded) ** (periods_per_year / n) - 1.0
    # 最大回撤（基于累计净值）
    cum = np.cumprod(1.0 + values)
    peak = np.maximum.accumulate(cum)
    drawdown = (cum - peak) / peak
    max_drawdown = float(-drawdown.min()) if len(drawdown) > 0 else None
    # 夏普（按年化周期数；无风险利率=0）
    sharpe = None
    std = float(np.std(values, ddof=1)) if n > 1 else 0.0
    mean = float(np.mean(values))
    if std and std > 0:
        sharpe = (mean / std) * math.sqrt(periods_per_year)
    # 胜率
    win_rate = float(np.mean(values > 0)) if n > 0 else None
    return {
        "total_return": _safe(compounded),
        "annual_return": _safe(annual),
        "max_drawdown": _safe(max_drawdown),
        "sharpe": _safe(sharpe),
        "win_rate": _safe(win_rate),
        "turnover_rate": None,
    }


def _composite_score(
    values: dict[str, float | None], weights: dict[str, float]
) -> float:
    """综合评分：对有效指标 z-score 后加权求和，映射到 0-100。

    最大回撤为**正数越大越差**，故 z-score 后取负（越小越好 → 分数越高）。
    某指标缺失时按剩余权重重新归一化。
    """
    items: list[tuple[str, float]] = []
    total_w = 0.0
    for key, w in weights.items():
        v = values.get(key)
        if v is None:
            continue
        try:
            num = float(v)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(num):
            continue
        items.append((key, num))
        total_w += float(w)
    if not items or total_w <= 0:
        return 0.0

    # z-score（跨因子集合内）
    nums = np.array([v for _, v in items], dtype=float)
    mean = float(np.mean(nums))
    std = float(np.std(nums))
    scaled: list[float] = []
    for key, v in items:
        z = (v - mean) / std if std > 0 else 0.0
        if key == "max_drawdown":
            z = -z  # 回撤越小越好
        scaled.append(z)

    total = sum(
        z * (float(weights[key]) / total_w) for (key, _), z in zip(items, scaled)
    )
    # 映射到 0-100（sigmoid 归一，展示友好）
    return round(100.0 / (1.0 + math.exp(-total)), 2)
