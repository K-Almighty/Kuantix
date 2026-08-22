"""选股服务（T04）。

流程：加载模型权重 → 从因子库读取最新截面 → 综合打分 → 技术/缠论过滤
→ TopN → 落盘 SQLite + JSON/CSV（同花顺兼容、GBK）。

跨模块解耦（NF-3）
------------------
本模块**不 import** ``Kuantix.factor`` / ``Kuantix.data``：
- 因子值读取通过构造器注入的 ``store``（鸭子类型：需提供
  ``load(factor, date=None, code=None) -> DataFrame`` 与 ``list_factors()``）；
- 合成器通过构造器注入的 ``combiner``（鸭子类型：需提供
  ``combine(values, method, weights) -> Series``；``None`` 时用模块内
  等权兜底，不依赖 factor 包）；
- 模型加载通过注入的 ``model_loader: Callable[[str], ModelHandle]``
  （CLI/API 层把 :meth:`FactorService.load_model` 接进来）。

市场规则经 :class:`~Kuantix.core.market.MarketProfile`（NF-5）。
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import secrets
import sqlite3
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from Kuantix.adapters.factor_bridge import L1Reader
from Kuantix.config import Config, get_config
from Kuantix.core.contracts import ModelHandle, ScreenResult
from Kuantix.core.db import connect_sqlite
from Kuantix.core.fail_loud import (
    DataIntegrityError,
    MissingConfigError,
    NotSupportedError,
    require_non_empty,
)
from Kuantix.core.market import MarketProfile, get_market_profile
from Kuantix.screen.filters import ScreenFilter

__all__ = ["ScreenService", "ScreenRequest", "ScreenBatchResult"]

#: 选股批量读取的分批大小（一次 SQL ``IN (...)`` 的代码数）。
#: SQLite 默认变量上限 999，取 500 兼顾往返次数与语句复杂度。
_READ_CHUNK = 500

#: ``min_vol_ratio`` 条件使用的均量窗口（与 ScreenFilter 内实现一致）。
_VOL_RATIO_WINDOW = 20

_SCREEN_SCHEMA = """
CREATE TABLE IF NOT EXISTS screen_result (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT,
    created_at TEXT NOT NULL,
    market TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    score REAL NOT NULL,
    sub_scores TEXT NOT NULL,
    conditions TEXT NOT NULL,
    price REAL NOT NULL,
    as_of TEXT NOT NULL
)
"""

_SCREEN_BATCH_SCHEMA = """
CREATE TABLE IF NOT EXISTS screen_batch (
    batch_id TEXT PRIMARY KEY,
    market TEXT NOT NULL,
    model TEXT,
    top_n INTEGER NOT NULL,
    filters TEXT NOT NULL,
    combine TEXT NOT NULL,
    status TEXT NOT NULL,
    result_count INTEGER NOT NULL DEFAULT 0,
    excluded_count INTEGER NOT NULL DEFAULT 0,
    as_of TEXT NOT NULL,
    created_at TEXT NOT NULL,
    elapsed_ms INTEGER NOT NULL DEFAULT 0
)
"""

#: 合成方法白名单（与 factor.combiner 保持一致，由调用方传入的 combiner 支持）
_SCREEN_COMBINE_METHODS: tuple[str, ...] = ("equal", "ic", "ir")


class _EqualWeightCombiner:
    """模块内等权合成兜底（NF-3：screen 不依赖 factor 包）。

    仅支持 ``equal``；``ic`` / ``ir`` 需要调用方注入真正的 combiner。
    """

    def combine(
        self,
        values: pd.DataFrame,
        method: str,
        weights: dict[str, float] | None = None,
    ) -> pd.Series:
        if method != "equal":
            raise NotSupportedError(
                f"[fail-loud/NF-3] screen 内置合成器仅支持 equal；"
                f"{method} 需注入 factor 层的 combiner"
            )
        if values.empty:
            return pd.Series(dtype=float)
        n = float(len(values.columns))
        z = values.apply(self._zscore, axis=0)
        return (z.sum(axis=1) / n).sort_values(ascending=False)

    @staticmethod
    def _zscore(series: pd.Series) -> pd.Series:
        std = series.std(ddof=0)
        if std is None or std == 0 or pd.isna(std):
            return pd.Series(0.0, index=series.index)
        return (series - series.mean()) / std


@dataclass(frozen=True)
class ScreenRequest:
    """一次选股请求。

    Attributes:
        market: 市场码。
        model_name: 模型名；``None`` 用等权合成因子库全部因子。
        factors: 因子白名单；``None`` 用模型权重或因子库全部。
        top_n: 返回前 N 名。
        tech_cond: 技术过滤条件（传 :class:`ScreenFilter` 的条件键）。
        chanlun_cond: 缠论过滤条件。
        as_of: 数据基准日；``None`` 用最新可用日。
    """

    market: str = "CN"
    model_name: str | None = None
    factors: tuple[str, ...] | None = None
    top_n: int = 50
    tech_cond: dict[str, Any] = field(default_factory=dict)
    chanlun_cond: dict[str, Any] = field(default_factory=dict)
    as_of: dt.date | None = None


@dataclass(frozen=True)
class ScreenBatchResult:
    """一次批量选股的产出（含批次元数据与结果列表，S2–S6 用）。

    Attributes:
        batch_id: 批次 id（``batch_...``）。
        market: 市场码。
        model_name: 模型名（等权时为 ``None``）。
        top_n: 输出上限。
        filters: 请求条件回显（ScreenRunRequest.filters 原样）。
        combine: 条件组合方式（``and`` / ``or``）。
        status: 批次状态（本实现同步落库后恒为 ``done``）。
        result_count: 命中数。
        excluded_count: 被过滤剔除的标的数（NF-26/NF-27 显式计数）。
        as_of: 数据基准日。
        created_at: 创建时刻。
        elapsed_ms: 运行耗时。
        results: 命中结果（已按评分降序、截断 top_n）。
    """

    batch_id: str
    market: str
    model_name: str | None
    top_n: int
    filters: list[dict[str, Any]]
    combine: str
    status: str
    result_count: int
    excluded_count: int
    as_of: dt.date
    created_at: dt.datetime
    elapsed_ms: int
    results: list[ScreenResult]


class ScreenService:
    """选股服务门面。

    Args:
        config: 配置对象；``None`` 时取全局配置。
        store: 因子存储（鸭子类型：``load`` / ``list_factors``）。
        model_loader: 模型加载器 ``(name) -> ModelHandle``；``None`` 时
            用等权（不加载模型）。
        reader: L1 读侧（过滤条件用）；``None`` 时用 ``~/.Kuantix/vipdoc``。
        combiner: 因子合成器；``None`` 时新建。
        filter_: 条件过滤器；``None`` 时新建。
        profile: 市场档案；``None`` 时按市场取。
    """

    def __init__(
        self,
        config: Config | None = None,
        *,
        store: Any = None,
        model_loader: Callable[[str], ModelHandle] | None = None,
        reader: L1Reader | None = None,
        combiner: Any | None = None,
        filter_: ScreenFilter | None = None,
        profile: MarketProfile | None = None,
    ) -> None:
        self._config = config if config is not None else get_config()
        self._store = store
        self._model_loader = model_loader
        self._reader = (
            reader if reader is not None else L1Reader(self._config.paths.vipdoc)
        )
        self._combiner = combiner if combiner is not None else _EqualWeightCombiner()
        self._filter = filter_ if filter_ is not None else ScreenFilter()
        self._profile = profile
        self._results_db = self._config.paths.db / "screen_results.db"
        self._ensure_schema()

    # ------------------------------------------------------------------ #
    # run
    # ------------------------------------------------------------------ #

    def run(self, req: ScreenRequest) -> list[ScreenResult]:
        """执行选股（旧口径：返回排序清单并落盘 SQLite + JSON + CSV）。

        Args:
            req: 选股请求。

        Returns:
            按评分降序的 :class:`ScreenResult` 列表（已截断 top_n）。

        Raises:
            MissingConfigError: store 未注入且 model_name 指定时 model_loader 缺失。
            DataIntegrityError: 因子库无可用数据。
        """
        profile = self._profile or get_market_profile(req.market)
        results, _, as_of = self._score(req, profile)
        top = results[: req.top_n] if req.top_n > 0 else results
        self._persist(top, req, as_of)
        return top

    def run_batch(
        self,
        req: ScreenRequest,
        *,
        pool_codes: set[str] | None = None,
        excluded_codes: set[str] | None = None,
        filters: list[dict[str, Any]] | None = None,
        combine: str = "and",
    ) -> ScreenBatchResult:
        """执行选股并落**批次**（S2–S6 用：batch + 带 batch_id 的结果行）。

        与 :meth:`run` 的区别：产出携带 ``batch_id``，结果按批次可查
        （S4/S5/S6），并对被过滤剔除的标的**显式计数**（``excluded_count``，
        NF-26/NF-27）。

        Args:
            req: 选股请求。
            pool_codes: 代码池白名单；``None`` 表示全市场。
            excluded_codes: 被隔离区排除的代码集合（NF-27）。
            filters: 请求条件回显（原样存批次）。
            combine: 条件组合方式（``and`` / ``or``，原样存批次）。

        Returns:
            :class:`ScreenBatchResult`。

        Raises:
            MissingConfigError: store 未注入且 model_name 指定时 model_loader 缺失。
            DataIntegrityError: 因子库无可用数据。
        """
        profile = self._profile or get_market_profile(req.market)
        started = time.perf_counter()
        results, excluded, as_of = self._score(req, profile, excluded_codes=excluded_codes)
        if pool_codes is not None:
            results = [r for r in results if r.code in pool_codes]
        top = results[: req.top_n] if req.top_n > 0 else results
        created = dt.datetime.now().astimezone()
        elapsed_ms = int(round((time.perf_counter() - started) * 1000))
        batch_id = f"batch_{created.strftime('%Y%m%d%H%M%S')}_{secrets.token_hex(4)}"
        echo_filters = list(filters) if filters is not None else []
        self._persist_batch(
            batch_id, req, top, excluded, as_of, created, elapsed_ms, echo_filters, combine
        )
        return ScreenBatchResult(
            batch_id=batch_id,
            market=req.market,
            model_name=req.model_name,
            top_n=req.top_n,
            filters=echo_filters,
            combine=combine,
            status="done",
            result_count=len(top),
            excluded_count=excluded,
            as_of=as_of,
            created_at=created,
            elapsed_ms=elapsed_ms,
            results=top,
        )

    def screen_factor(
        self,
        *,
        factor: str,
        market: str,
        pool_codes: set[str] | None = None,
        excluded_codes: set[str] | None = None,
        top_n: int = 50,
        order: str = "desc",
        as_of: dt.date | None = None,
        tech_cond: dict[str, Any] | None = None,
        chanlun_cond: dict[str, Any] | None = None,
        combine: str = "and",
    ) -> tuple[list[ScreenResult], int, dt.date]:
        """单因子选股（基于最新数据，非回测，同步返回，速度快）。

        只加载单个因子的最新截面，按其取值排序取 TopN，避免多因子
        全量加载与模型打分，适合快速按近期数据筛选符合条件的股票。

        Args:
            factor: 单因子名（因子库内部 id）。
            market: 市场代码。
            pool_codes: 代码池白名单；``None`` 表示全市场。
            excluded_codes: 被隔离区排除的代码集合。
            top_n: 返回条数（``<=0`` 表示全部）。
            order: 排序方向，``desc`` 高值在前，``asc`` 低值在前。
            as_of: 数据基准日（含）；``None`` 取因子库最新日。
            tech_cond / chanlun_cond: 技术 / 缠论过滤条件字典。
            combine: 条件组合方式（仅回显，单因子不依赖）。

        Returns:
            ``(results, total, as_of)``；total 为过滤后候选总数（可能 > top_n）。

        Raises:
            MissingConfigError: store 未注入。
            DataIntegrityError: 因子不存在 / 无因子数据。
        """
        if self._store is None:
            raise MissingConfigError("[fail-loud/NF-26] 未注入因子 store，无法读取因子值")
        available = set(self._store.list_factors())
        if factor not in available:
            raise DataIntegrityError(f"[fail-loud/NF-26] 因子不存在或无因子数据: {factor}")

        profile = self._profile or get_market_profile(market)

        date_int = None
        if as_of is not None:
            date_int = as_of.year * 10000 + as_of.month * 100 + as_of.day

        # R6：流式取每码最新一行，替代「全量 load 6 年 + _latest_row_per_code」。
        # 原路径读 871 万行（峰值 468MB / 常驻 590MB）；新路径按分区从新到旧
        # 逐区读取，每区只保留每码最新行，命中全部 code 即停。as_of 语义
        # 保持一致：取日期 ≤ as_of 的最新一行。
        frame = self._store.load_latest_per_code(factor, as_of=date_int)
        if frame.empty:
            return [], 0, (as_of or dt.date.today())
        if date_int is not None and not (frame["date"] <= date_int).any():
            return [], 0, (as_of or dt.date.today())
        max_int = int(frame["date"].max())
        as_of_eff = dt.date(max_int // 10000, (max_int // 100) % 100, max_int % 100)

        if pool_codes is not None:
            frame = frame[frame["code"].isin(pool_codes)]
        if excluded_codes:
            frame = frame[~frame["code"].isin(excluded_codes)]

        frame = frame.copy()
        frame["score"] = pd.to_numeric(frame["value"], errors="coerce")
        frame = frame.dropna(subset=["score"])

        tech_cond = tech_cond or {}
        chanlun_cond = chanlun_cond or {}
        reverse = order != "asc"
        conditions_text = self._factor_conditions_text(tech_cond, chanlun_cond)

        codes = [str(c) for c in frame["code"].tolist()]
        scores = [float(v) for v in frame["score"].tolist()]

        # ---- 快路径：无过滤条件时只需最新收盘价，不必读历史 ----
        # 原实现为拿 close.iloc[-1] 逐只读全历史（7582 次 × 数千根）；
        # 这里用一条聚合查询取回全市场最新收盘价，语义完全等价：
        # 「有最新价」⇔「read_daily_frame 非空」⇔ 计入 total。
        if not tech_cond and not chanlun_cond:
            price_map = self._latest_close_map(market, codes)
            kept = [
                ScreenResult(
                    code=code, name="", market=market, score=score,
                    sub_scores={factor: score}, conditions=conditions_text,
                    price=price_map[code], as_of=as_of_eff,
                )
                for code, score in zip(codes, scores)
                if code in price_map
            ]
            kept.sort(key=lambda r: r.score, reverse=reverse)
            total = len(kept)
            return (kept[:top_n] if top_n and top_n > 0 else kept), total, as_of_eff

        # ---- 慢路径：有过滤条件，需逐只判定，但改批量读 + 按需截尾 ----
        need_tail = self._required_tail(tech_cond, chanlun_cond)
        kept = []
        for chunk_codes, chunk_scores in self._chunks(codes, scores, _READ_CHUNK):
            frames = self._batch_frames(market, chunk_codes, tail=need_tail)
            for code, score in zip(chunk_codes, chunk_scores):
                f = frames.get(code)
                if f is None or f.empty:
                    continue
                if not self._filter.tech_filter(f, tech_cond):
                    continue
                if not self._filter.chanlun_filter(f, chanlun_cond):
                    continue
                kept.append(
                    ScreenResult(
                        code=code, name="", market=market, score=score,
                        sub_scores={factor: score}, conditions=conditions_text,
                        price=float(f["close"].iloc[-1]), as_of=as_of_eff,
                    )
                )

        kept.sort(key=lambda r: r.score, reverse=reverse)
        total = len(kept)
        top = kept[:top_n] if top_n and top_n > 0 else kept
        return top, total, as_of_eff

    # ------------------------------------------------------------------ #
    # 内部：批量读取辅助（选股主循环性能）
    # ------------------------------------------------------------------ #

    @staticmethod
    def _chunks(
        codes: list[str], scores: list[float], size: int
    ) -> Iterator[tuple[list[str], list[float]]]:
        """把 (codes, scores) 切成固定大小的批次。"""
        for i in range(0, len(codes), size):
            yield codes[i : i + size], scores[i : i + size]

    @staticmethod
    def _required_tail(
        tech_cond: dict[str, Any], chanlun_cond: dict[str, Any]
    ) -> int | None:
        """计算过滤条件所需的最小尾部窗口长度。

        ``rolling(w).mean().iloc[-1]`` 只依赖最后 w 个样本，故截尾到
        ``max(w)`` 与读全量结果**完全一致**（样本不足 w 时两者同样得
        NaN → 同样判 False）。缠论分析依赖完整走势，返回 ``None`` 表示
        不截尾。

        Returns:
            所需尾部根数；``None`` 表示需要全量历史。
        """
        if chanlun_cond:
            return None
        need = 1
        for key in ("ma_fast", "ma_slow"):
            if key in tech_cond:
                need = max(need, int(tech_cond[key]))
        if "min_vol_ratio" in tech_cond:
            need = max(need, _VOL_RATIO_WINDOW)
        return need

    def _batch_frames(
        self, market: str, codes: list[str], *, tail: int | None
    ) -> dict[str, pd.DataFrame]:
        """批量读一批代码的日线；读侧不支持批量时回退逐只。"""
        batch = getattr(self._reader, "read_daily_frames", None)
        if callable(batch):
            try:
                return batch(codes, market, tail=tail)
            except TypeError:
                # 注入的测试替身可能没有 tail 形参，退化为不截尾批量读
                return batch(codes, market)
        profile = self._profile or get_market_profile(market)
        out: dict[str, pd.DataFrame] = {}
        for code in codes:
            f = self._safe_frame(profile, code)
            if f is not None and not f.empty:
                out[code] = f if tail is None else f.tail(tail).reset_index(drop=True)
        return out

    def _latest_close_map(self, market: str, codes: list[str]) -> dict[str, float]:
        """取一批代码的最新收盘价；读侧不支持聚合查询时回退逐只读。"""
        store = getattr(self._reader, "store", None)
        latest = getattr(store, "latest_closes", None) if store is not None else None
        if callable(latest):
            return {c: float(v[1]) for c, v in latest(market).items()}
        profile = self._profile or get_market_profile(market)
        out: dict[str, float] = {}
        for code in codes:
            f = self._safe_frame(profile, code)
            if f is not None and not f.empty:
                out[code] = float(f["close"].iloc[-1])
        return out

    @staticmethod
    def _factor_conditions_text(tech_cond: dict[str, Any], chanlun_cond: dict[str, Any]) -> str:
        parts: list[str] = []
        if tech_cond:
            parts.append(f"tech:{json.dumps(tech_cond, ensure_ascii=False)}")
        if chanlun_cond:
            parts.append(f"chanlun:{json.dumps(chanlun_cond, ensure_ascii=False)}")
        return ";".join(parts)

    def _score(
        self,
        req: ScreenRequest,
        profile: MarketProfile,
        excluded_codes: set[str] | None = None,
    ) -> tuple[list[ScreenResult], int, dt.date]:
        """打分 + 过滤主循环（run / run_batch 共用）。

        Returns:
            ``(results, excluded_count, as_of)``；``excluded_count`` 为被
            隔离区排除 + 过滤未命中的标的数（NF-26/NF-27 显式计数）。
        """
        weights, method, factor_list = self._resolve_model(req, profile)
        values, as_of = self._load_factor_values(factor_list, req.as_of)
        if values.empty:
            raise DataIntegrityError(
                "[fail-loud/NF-26] 因子库无可用数据，请先 Kuantix factor compute"
            )
        score = self._combiner.combine(values, method, weights=weights)

        blocked = excluded_codes if excluded_codes is not None else set()
        results: list[ScreenResult] = []
        excluded = 0
        conditions_text = self._conditions_text(req)

        candidates = [str(c) for c in score.index if str(c) not in blocked]
        excluded += len(score.index) - len(candidates)
        # 逐 code 的 score.loc / _sub_scores 是标签查找，全市场规模下
        # 累积开销可观；先一次性转成 dict，循环内 O(1) 取值。
        score_map = {str(k): float(v) for k, v in score.items()}
        sub_map = self._sub_scores_map(values)

        tech_cond = req.tech_cond or {}
        chanlun_cond = req.chanlun_cond or {}

        # 快路径：无过滤条件时只需最新收盘价（与 screen_factor 同理）
        if not tech_cond and not chanlun_cond:
            price_map = self._latest_close_map(req.market, candidates)
            for code in candidates:
                price = price_map.get(code)
                if price is None:
                    excluded += 1
                    continue
                results.append(
                    ScreenResult(
                        code=code, name="", market=req.market,
                        score=score_map[code], sub_scores=sub_map.get(code, {}),
                        conditions=conditions_text, price=price, as_of=as_of,
                    )
                )
            results.sort(key=lambda r: r.score, reverse=True)
            return results, excluded, as_of

        need_tail = self._required_tail(tech_cond, chanlun_cond)
        for chunk_codes, _ in self._chunks(candidates, [0.0] * len(candidates), _READ_CHUNK):
            frames = self._batch_frames(req.market, chunk_codes, tail=need_tail)
            for code in chunk_codes:
                frame = frames.get(code)
                if frame is None or frame.empty:
                    excluded += 1
                    continue
                if not self._filter.tech_filter(frame, tech_cond):
                    excluded += 1
                    continue
                if not self._filter.chanlun_filter(frame, chanlun_cond):
                    excluded += 1
                    continue
                results.append(
                    ScreenResult(
                        code=code, name="", market=req.market,
                        score=score_map[code], sub_scores=sub_map.get(code, {}),
                        conditions=conditions_text,
                        price=float(frame["close"].iloc[-1]), as_of=as_of,
                    )
                )

        results.sort(key=lambda r: r.score, reverse=True)
        return results, excluded, as_of

    def list_conditions(self) -> dict[str, list[str]]:
        """列出支持的过滤条件键（CLI ``screen list`` 用）。"""
        return {
            "tech": ["ma_fast", "ma_slow", "min_close", "max_close", "min_vol_ratio"],
            "chanlun": ["require_buy_point"],
            "combine_methods": list(_SCREEN_COMBINE_METHODS),
        }

    # ------------------------------------------------------------------ #
    # 内部：模型解析
    # ------------------------------------------------------------------ #

    def _resolve_model(
        self, req: ScreenRequest, profile: MarketProfile
    ) -> tuple[dict[str, float] | None, str, list[str]]:
        """解析模型权重 / 方法 / 因子列表。

        Returns:
            ``(weights, method, factor_list)``；weights 为 None 时等权。
        """
        if req.model_name is not None:
            if self._model_loader is None:
                raise MissingConfigError(
                    "[fail-loud/NF-26] 指定了 model_name 但未注入 model_loader，"
                    "CLI 层应把 FactorService.load_model 接入"
                )
            handle = self._model_loader(req.model_name)
            weights = dict(handle.weights)
            method = handle.method
            factor_list = sorted(weights)
        else:
            factor_list = (
                list(req.factors)
                if req.factors is not None
                else self._available_factors()
            )
            require_non_empty(factor_list, "screen.factors")
            weights = None
            method = "equal"
        return weights, method, factor_list

    def _available_factors(self) -> list[str]:
        if self._store is None:
            raise MissingConfigError(
                "[fail-loud/NF-26] 未注入因子 store，无法列出可用因子"
            )
        return sorted(self._store.list_factors())

    def _load_factor_values(
        self, factor_list: list[str], as_of: dt.date | None
    ) -> tuple[pd.DataFrame, dt.date]:
        """读取因子最新截面（index=code，columns=factor）。

        Returns:
            ``(values, as_of)``；as_of 为数据基准日（入参优先，否则取存储最新日）。
        """
        if self._store is None:
            raise MissingConfigError(
                "[fail-loud/NF-26] 未注入因子 store，无法读取因子值"
            )
        date_int = None
        if as_of is not None:
            date_int = as_of.year * 10000 + as_of.month * 100 + as_of.day
        frames: dict[str, pd.Series] = {}
        latest_dates: list[int] = []
        for factor in factor_list:
            if date_int is None:
                # R6：as_of 未指定时流式取每码最新行，替代全量 load 6 年
                # （单因子峰值 468MB → 73MB，多因子叠加显著下降）。
                df = self._store.load_latest_per_code(factor)
            else:
                # as_of 指定：保持「as_of 当天截面」语义（单日分区裁剪已快）
                df = self._store.load(factor, date=date_int)
            if df.empty:
                continue
            # 每个 (date, code) 只取最新一行（idxmax 写法，见 _latest_row_per_code）
            latest = self._latest_row_per_code(df)
            frames[factor] = latest.set_index("code")["value"]
            latest_dates.append(int(df["date"].max()))
        if not frames:
            return pd.DataFrame(), (as_of if as_of is not None else dt.date.today())
        effective_as_of = as_of
        if effective_as_of is None and latest_dates:
            max_int = max(latest_dates)
            effective_as_of = dt.date(max_int // 10000, (max_int // 100) % 100, max_int % 100)
        return pd.DataFrame(frames), (effective_as_of or dt.date.today())

    @staticmethod
    def _latest_row_per_code(frame: pd.DataFrame) -> pd.DataFrame:
        """取每个 ``code`` 日期最大的那一行（等价 ``sort+groupby.tail(1)``）。

        **性能**：原写法 ``frame.sort_values("date").groupby("code").tail(1)``
        要对全量因子历史（momentum_60d 为 871 万行）做一次全表排序，再走
        groupby 的 ``_cumcount_array`` 生成位置掩码，实测 768.8ms。改用
        ``groupby("code")["date"].idxmax()`` 只做一次分组聚合 + 一次
        ``loc`` 取行，实测 203.3ms（**3.8×**），行集合完全一致（已按 code
        归一化后 ``DataFrame.equals`` 验证）。

        **顺序确定性**：原写法的行顺序由 ``sort_values`` 的 quicksort 决定，
        同日期行之间的相对次序是不稳定的；下游 ``kept.sort(key=score)`` 为
        稳定排序，因此同分标的的输出次序会依赖这个不稳定顺序。这里显式按
        ``(date, code)`` 稳定排序，用确定性顺序替代原先的实现细节依赖。
        """
        if frame.empty:
            return frame
        idx = frame.groupby("code")["date"].idxmax()
        return (
            frame.loc[idx]
            .sort_values(["date", "code"], kind="stable")
            .reset_index(drop=True)
        )

    def _sub_scores(self, values: pd.DataFrame, code: str) -> dict[str, float]:
        if code not in values.index:
            return {}
        row = values.loc[code]
        return {str(k): round(float(v), 6) for k, v in row.items()}

    def _sub_scores_map(self, values: pd.DataFrame) -> dict[str, dict[str, float]]:
        """一次性构造 ``{code: {factor: value}}``，与 :meth:`_sub_scores` 等价。

        逐 code 调用 ``values.loc[code]`` 在全市场（~5k 标的）规模下会产生
        数千次 pandas 标签查找 + Series 构造；这里改为一次 ``to_numpy()``
        后按行组装，数值处理保持 ``round(float(v), 6)`` 完全一致，
        避免引入任何精度差异。
        """
        if values.empty:
            return {}
        columns = [str(c) for c in values.columns]
        matrix = values.to_numpy(dtype=float, copy=False)
        return {
            str(code): {
                col: round(float(v), 6) for col, v in zip(columns, row, strict=True)
            }
            for code, row in zip(values.index, matrix, strict=True)
        }

    def _safe_frame(self, profile: MarketProfile, code: str):
        """读取单标的日线；缺失/异常返回 None（跳过，不隔离——非数据损坏）。"""
        try:
            exchange = profile.exchange_for_code(str(code))
            return self._reader.read_daily_frame(exchange, str(code))
        except Exception:  # noqa: BLE001 - 单标的读取失败跳过，不静默
            return None

    @staticmethod
    def _conditions_text(req: ScreenRequest) -> str:
        parts: list[str] = []
        if req.tech_cond:
            parts.append(f"tech:{json.dumps(req.tech_cond, ensure_ascii=False)}")
        if req.chanlun_cond:
            parts.append(f"chanlun:{json.dumps(req.chanlun_cond, ensure_ascii=False)}")
        return ";".join(parts)

    # ------------------------------------------------------------------ #
    # 落盘
    # ------------------------------------------------------------------ #

    def _persist(
        self,
        results: list[ScreenResult],
        req: ScreenRequest,
        as_of: dt.date,
    ) -> None:
        """SQLite + JSON + CSV（GBK，同花顺兼容）。"""
        if not results:
            return
        created = dt.datetime.now()
        self._persist_sqlite(results, created)
        self._persist_json(results, req, as_of, created)
        self._persist_csv(results, created)

    def _persist_sqlite(
        self, results: list[ScreenResult], created: dt.datetime
    ) -> None:
        with self._connect() as conn:
            self._insert_result_rows(conn, results, created, batch_id=None)

    def _insert_result_rows(
        self,
        conn: sqlite3.Connection,
        results: list[ScreenResult],
        created: dt.datetime,
        batch_id: str | None,
    ) -> None:
        for r in results:
            conn.execute(
                """
                INSERT INTO screen_result
                    (batch_id, created_at, market, code, name, score, sub_scores,
                     conditions, price, as_of)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    created.isoformat(timespec="seconds"),
                    r.market,
                    r.code,
                    r.name,
                    r.score,
                    json.dumps(r.sub_scores, ensure_ascii=False),
                    r.conditions,
                    r.price,
                    r.as_of.isoformat(),
                ),
            )

    def _persist_batch(
        self,
        batch_id: str,
        req: ScreenRequest,
        results: list[ScreenResult],
        excluded: int,
        as_of: dt.date,
        created: dt.datetime,
        elapsed_ms: int,
        filters: list[dict[str, Any]],
        combine: str,
    ) -> None:
        """批次表 + 带 batch_id 的结果行。"""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO screen_batch
                    (batch_id, market, model, top_n, filters, combine, status,
                     result_count, excluded_count, as_of, created_at, elapsed_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(batch_id) DO UPDATE SET
                    status=excluded.status,
                    result_count=excluded.result_count,
                    excluded_count=excluded.excluded_count
                """,
                (
                    batch_id,
                    req.market,
                    req.model_name,
                    req.top_n,
                    json.dumps(filters, ensure_ascii=False),
                    combine,
                    "done",
                    len(results),
                    excluded,
                    as_of.isoformat(),
                    created.isoformat(timespec="seconds"),
                    elapsed_ms,
                ),
            )
            self._insert_result_rows(conn, results, created, batch_id=batch_id)

    def _persist_json(
        self,
        results: list[ScreenResult],
        req: ScreenRequest,
        as_of: dt.date,
        created: dt.datetime,
    ) -> Path:
        exports = self._config.paths.exports
        exports.mkdir(parents=True, exist_ok=True)
        target = exports / f"screen_{created.strftime('%Y%m%d_%H%M%S')}.json"
        payload = {
            "market": req.market,
            "as_of": as_of.isoformat(),
            "model_name": req.model_name,
            "top_n": req.top_n,
            "count": len(results),
            "results": [r.to_dict() for r in results],
        }
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return target

    def _persist_csv(self, results: list[ScreenResult], created: dt.datetime) -> Path:
        """同花顺兼容 CSV（GBK 编码），列序与 :meth:`export_csv_bytes` 一致
        （契约 §3.4）：``代码,名称,最新价,综合得分,触发条件,数据日期``（6 列，
        含 NF-22 免责头）。CLI 落盘与 API 导出全链路一种口径。"""
        exports = self._config.paths.exports
        exports.mkdir(parents=True, exist_ok=True)
        target = exports / f"screen_{created.strftime('%Y%m%d_%H%M%S')}.csv"
        as_of = results[0].as_of.isoformat() if results else created.date().isoformat()
        with target.open("w", encoding="gbk", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(
                [
                    f"# Kuantix 选股结果 {as_of} · "
                    "仅供人工核对参考，非自动交易指令 (NF-22)"
                ]
            )
            writer.writerow(["代码", "名称", "最新价", "综合得分", "触发条件", "数据日期"])
            for r in results:
                writer.writerow(
                    [
                        r.code,
                        r.name,
                        f"{float(r.price):.2f}",
                        f"{float(r.score):.2f}",
                        r.conditions,
                        r.as_of.isoformat(),
                    ]
                )
        return target

    # ------------------------------------------------------------------ #
    # 批次查询（S4 / S5 / S6）
    # ------------------------------------------------------------------ #

    def list_batches(
        self,
        market: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        """分页列出选股批次（按创建时间降序）。

        Returns:
            ``{items: [ScreenBatch], page, page_size, total, total_pages}``。
        """
        with self._connect() as conn:
            if market is None:
                rows = conn.execute(
                    "SELECT * FROM screen_batch ORDER BY created_at DESC, batch_id DESC"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM screen_batch WHERE market = ? "
                    "ORDER BY created_at DESC, batch_id DESC",
                    (market,),
                ).fetchall()
        batches = [self._batch_to_dict(row) for row in rows]
        return self._paginate(batches, page, page_size)

    def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        """按 id 取 ScreenBatch；不存在返回 ``None``。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM screen_batch WHERE batch_id = ?", (batch_id,)
            ).fetchone()
        return self._batch_to_dict(row) if row is not None else None

    def _all_results(self, batch_id: str) -> list[dict[str, Any]]:
        """某批次的全部结果（带 rank，按 score 降序）。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM screen_result WHERE batch_id = ? ORDER BY score DESC",
                (batch_id,),
            ).fetchall()
        items = [self._result_to_view(row) for row in rows]
        for index, item in enumerate(items, start=1):
            item["rank"] = index
        return items

    def get_batch_results(
        self,
        batch_id: str,
        page: int = 1,
        page_size: int = 50,
        sort_by: str = "score",
        order: str = "desc",
    ) -> dict[str, Any] | None:
        """分页返回批次结果（ScreenResultView 含 rank，可排序）。

        Args:
            batch_id: 批次 id。
            page: 页码（1 起）。
            page_size: 每页条数。
            sort_by: 排序字段（``score`` / ``code`` / ``name`` / ``price``）。
            order: ``asc`` / ``desc``。

        Returns:
            ``{items, page, page_size, total, total_pages}``；批次不存在
            返回 ``None``（路由层映射 404）。
        """
        if self.get_batch(batch_id) is None:
            return None
        items = self._all_results(batch_id)
        items.sort(key=lambda item: item[sort_by], reverse=(order == "desc"))
        for index, item in enumerate(items, start=1):
            item["rank"] = index
        return self._paginate(items, page, page_size)

    def export_json_payload(self, batch_id: str) -> dict[str, Any] | None:
        """S6 JSON 导出的信封载荷（与 S5 同一 schema，一次返回全部）。"""
        if self.get_batch(batch_id) is None:
            return None
        items = self._all_results(batch_id)
        return {
            "items": items,
            "page": 1,
            "page_size": len(items),
            "total": len(items),
            "total_pages": 1 if items else 0,
        }

    def export_csv_bytes(self, batch_id: str) -> bytes | None:
        """S6 CSV 导出的 GBK 字节（同花顺兼容 + NF-22 免责头）。

        列序对齐契约 §3.4 示例：``代码,名称,最新价,综合得分,触发条件,数据日期``
        （6 列，含 ``as_of`` 数据日期）。批次不存在返回 ``None``（路由层映射 404）。
        """
        batch = self.get_batch(batch_id)
        if batch is None:
            return None
        items = self._all_results(batch_id)
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(
            [
                f"# Kuantix 选股结果 {batch['as_of']} · "
                "仅供人工核对参考，非自动交易指令 (NF-22)"
            ]
        )
        writer.writerow(["代码", "名称", "最新价", "综合得分", "触发条件", "数据日期"])
        for item in items:
            writer.writerow(
                [
                    item["code"],
                    item["name"],
                    f"{float(item['price']):.2f}",
                    f"{float(item['score']):.2f}",
                    item["conditions"],
                    item["as_of"],
                ]
            )
        return buffer.getvalue().encode("gbk")

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    @staticmethod
    def _batch_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "batch_id": str(row["batch_id"]),
            "market": str(row["market"]),
            "model": row["model"],
            "top_n": int(row["top_n"]),
            "filters": json.loads(str(row["filters"])),
            "combine": str(row["combine"]),
            "status": str(row["status"]),
            "result_count": int(row["result_count"]),
            "excluded_count": int(row["excluded_count"]),
            "as_of": str(row["as_of"]),
            "created_at": str(row["created_at"]),
            "elapsed_ms": int(row["elapsed_ms"]),
        }

    @staticmethod
    def _result_to_view(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "code": str(row["code"]),
            "name": str(row["name"]),
            "market": str(row["market"]),
            "score": float(row["score"]),
            "sub_scores": json.loads(str(row["sub_scores"])),
            "conditions": str(row["conditions"]),
            "price": float(row["price"]),
            "as_of": str(row["as_of"]),
            "rank": 0,
        }

    @staticmethod
    def _paginate(items: list[Any], page: int, page_size: int) -> dict[str, Any]:
        total = len(items)
        total_pages = (total + page_size - 1) // page_size if total else 0
        start = (page - 1) * page_size
        return {
            "items": items[start : start + page_size],
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
        }

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(_SCREEN_SCHEMA)
            columns = [
                str(r["name"])
                for r in conn.execute("PRAGMA table_info(screen_result)").fetchall()
            ]
            if "batch_id" not in columns:
                conn.execute("ALTER TABLE screen_result ADD COLUMN batch_id TEXT")
            conn.execute(_SCREEN_BATCH_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        """P1-1：选股结果库裸连修复 —— connect_sqlite 自动建父目录 + 并发基线。"""
        return connect_sqlite(self._results_db)
