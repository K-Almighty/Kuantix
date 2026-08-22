"""因子服务（compute / report / combine，T04 主入口）。

- :meth:`FactorService.compute_factors` —— 全程读本地 L1（vipdoc，经
  :class:`~Kuantix.adapters.factor_bridge.L1Reader`），**不走网络**；
- :meth:`FactorService.report` —— 经上游 ``FactorAnalyzer``（需 scipy）
  输出 IC / IR / 分层收益 / 换手率；
- :meth:`FactorService.combine` —— 等权 / IC 加权 / IR 加权合成并保存模型；
- :meth:`FactorService.list_factors` —— 上游注册表 + 自定义因子自动发现。

模型存储：``~/.Kuantix/db/models.db`` 表 ``models``
（name / method / weights_json / created_at），:class:`ModelHandle` 契约。
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable

import pandas as pd

from Kuantix.adapters.factor_bridge import (
    FACTORY_REGISTRY,
    FactorAnalyzerBridge,
    FactorEngineBridge,
    L1Reader,
)
from Kuantix.config import Config, get_config
from Kuantix.core.contracts import ModelHandle
from Kuantix.core.db import connect_sqlite
from Kuantix.core.fail_loud import (
    DataIntegrityError,
    MissingConfigError,
    require_key,
    require_non_empty,
)
from Kuantix.core.market import MarketProfile, get_market_profile
from Kuantix.factor.combiner import FactorCombiner
from Kuantix.factor.store import FactorStore

__all__ = ["FactorService", "ComputeRequest", "JobResult"]

_MODELS_SCHEMA = """
CREATE TABLE IF NOT EXISTS models (
    name TEXT PRIMARY KEY,
    method TEXT NOT NULL,
    weights TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""

#: 因子计算向前读取的缓冲天数（自然日）。因子如 momentum_60d / vol_surge
#: 的滚动窗口需要 start 前的历史数据；加载样本时把 start 往前多取该天数，
#: 保证区间首部因子值不因缺前置数据而产生 NaN（区间截取仍由
#: :meth:`FactorStore.compute` 完成）。60 个交易日 ≈ 84 个自然日，取 180
#: 自然日覆盖 60 日窗口且留余量。
_LOOKBACK_DAYS = 180


@dataclass(frozen=True)
class ComputeRequest:
    """一次因子计算请求。

    Attributes:
        market: 市场码。
        factors: 因子名列表。
        start: 起始日期（含）。
        end: 结束日期（含）。
        codes: 代码池；``None`` 表示 vipdoc 中全部 A 股。
        force: 是否忽略已算区间强制重算。
    """

    market: str = "CN"
    factors: tuple[str, ...] = ()
    start: dt.date = dt.date(2020, 1, 1)
    end: dt.date = dt.date(2025, 12, 31)
    codes: tuple[str, ...] | None = None
    force: bool = False


@dataclass(frozen=True)
class JobResult:
    """一次因子计算的结果。

    Attributes:
        factor: 因子名。
        dates_computed: 本次计算的日期数。
        rows: 落库行数。
        elapsed_ms: 耗时（毫秒）。
        force: 是否强制重算。
    """

    factor: str
    dates_computed: int
    rows: int
    elapsed_ms: int
    force: bool = False

    def to_dict(self) -> dict[str, Any]:
        """转为 JSON 安全字典。"""
        return {
            "factor": self.factor,
            "dates_computed": self.dates_computed,
            "rows": self.rows,
            "elapsed_ms": self.elapsed_ms,
            "force": self.force,
        }


class FactorService:
    """因子服务门面。

    Args:
        config: 配置对象；``None`` 时取全局配置。
        reader: L1 读侧；``None`` 时用 ``~/.Kuantix/vipdoc``。
        store: 因子存储；``None`` 时用 ``~/.Kuantix/factors``。
        engine: 因子引擎；``None`` 时新建。
        combiner: 合成器；``None`` 时新建。
    """

    def __init__(
        self,
        config: Config | None = None,
        *,
        reader: L1Reader | None = None,
        store: FactorStore | None = None,
        engine: FactorEngineBridge | None = None,
        combiner: FactorCombiner | None = None,
    ) -> None:
        self._config = config if config is not None else get_config()
        self._reader = (
            reader
            if reader is not None
            else L1Reader.from_config(self._config, backend="auto")
        )
        self._store = (
            store
            if store is not None
            else FactorStore(self._config.paths.factors, self._config.paths.db)
        )
        self._engine = engine if engine is not None else FactorEngineBridge()
        self._combiner = combiner if combiner is not None else FactorCombiner()
        self._models_db = self._config.paths.db / "models.db"
        self._ensure_models_schema()

    # ------------------------------------------------------------------ #
    # compute
    # ------------------------------------------------------------------ #

    def compute_factors(self, req: ComputeRequest) -> list[JobResult]:
        """计算因子并落 L2 parquet（全程读本地，不走网络）。

        Args:
            req: 计算请求。

        Returns:
            :class:`JobResult` 列表（每个因子一条）。

        Raises:
            MissingConfigError: 未指定任何因子。
            DataIntegrityError: 代码池为空。
        """
        require_non_empty(req.factors, "compute_factors.factors")
        # 先做自定义因子自动发现（注册到上游 FACTORY_REGISTRY），
        # 否则 compute 时新因子尚未注册会报「未知因子」。
        from Kuantix.factor.factors import discover_factors

        discover_factors()
        profile = get_market_profile(req.market)
        pool = self._load_pool(req, profile)
        import time

        # 一次性把全部因子交给 store.compute：上游截面计算对 K 个因子
        # 只遍历一次标的池，逐因子调用会把「遍历 5030 只 × 建 DataFrame ×
        # datetime→int」重复 K 遍（实测 5 因子 4.01× 冗余）。
        # 注意：elapsed_ms 由此变为**批次均摊耗时**（总和 = 真实墙钟耗时），
        # 因为批量计算下无法再拆出单因子的独立耗时。
        started = time.perf_counter()
        counts = self._store.compute(
            pool,
            list(req.factors),
            req.start,
            req.end,
            engine=self._engine,
            force=req.force,
        )
        elapsed = int(round((time.perf_counter() - started) * 1000))
        share = elapsed // max(1, len(req.factors))

        results: list[JobResult] = []
        for factor in req.factors:
            rows = int(require_key(counts, factor, "factor compute count"))
            results.append(
                JobResult(
                    factor=factor,
                    dates_computed=rows,
                    rows=rows,
                    elapsed_ms=share,
                    force=req.force,
                )
            )
        return results

    # ------------------------------------------------------------------ #
    # report
    # ------------------------------------------------------------------ #

    def report(
        self,
        factor: str,
        market: str = "CN",
        *,
        forward_period: int | None = None,
        quantiles: int | None = None,
        end: dt.date | None = None,
    ) -> dict[str, Any]:
        """输出因子有效性报告（IC / IR / 分层 / 换手率）。

        Args:
            factor: 因子名。
            market: 市场码。
            forward_period: 前向收益周期（交易日）；``None`` 用配置。
            quantiles: 分层数；``None`` 用配置。
            end: 数据截止日；``None`` 用最新可用日。

        Returns:
            JSON 安全字典（经 :func:`FactorAnalyzerBridge.report_to_dict`）。

        Raises:
            DataIntegrityError: 因子无数据。
        """
        profile = get_market_profile(market)
        forward = forward_period if forward_period is not None else self._config.factor.forward_period
        nq = quantiles if quantiles is not None else self._config.factor.quantiles

        factor_df = self._store.load(factor)
        if factor_df.empty:
            raise DataIntegrityError(
                f"[fail-loud/NF-26] 因子 {factor} 无数据，请先 compute"
            )
        # store 统一存 value 列；FactorAnalyzer 需要 factor 列名
        if "value" not in factor_df.columns:
            raise DataIntegrityError(
                f"[fail-loud/NF-26] 因子 {factor} 数据缺少 value 列，存储结构异常"
            )
        factor_df = factor_df.rename(columns={"value": factor})
        pool = self._load_pool(
            ComputeRequest(market=market, factors=(factor,)), profile
        )
        return_df = self._engine.compute_forward_returns(pool, period=int(forward))

        report = FactorAnalyzerBridge.analyze(
            factor_df=factor_df,
            return_df=return_df,
            factor_col=factor,
            return_col=f"forward_{forward}d",
            n_quantiles=int(nq),
        )
        payload = FactorAnalyzerBridge.report_to_dict(report)
        payload["forward_period"] = int(forward)
        payload["quantiles"] = int(nq)
        payload["market"] = market
        return payload

    # ------------------------------------------------------------------ #
    # combine
    # ------------------------------------------------------------------ #

    def combine(
        self,
        factors: Iterable[str],
        method: str,
        *,
        name: str | None = None,
        save_model: bool = False,
        market: str = "CN",
    ) -> ModelHandle:
        """多因子合成并（可选）保存模型。

        Args:
            factors: 待合成因子名列表。
            method: 合成方法（``equal`` / ``ic`` / ``ir``）。
            name: 模型名；``None`` 自动生成。
            save_model: 是否保存到 ``models.db``。
            market: 市场码。

        Returns:
            :class:`ModelHandle`。

        Raises:
            MissingConfigError: 因子列表为空。
            DataIntegrityError: ic/ir 需要权重但对应报告缺失。
        """
        require_non_empty(list(factors), "combine.factors")
        factor_list = list(dict.fromkeys(factors))

        if method == "equal":
            weights = {f: 1.0 for f in factor_list}
        else:
            weights = {}
            for f in factor_list:
                rep = self.report(f, market=market)
                key = "ic_mean" if method == "ic" else "ir"
                weight = float(rep[key])
                if weight is None or pd.isna(weight):
                    raise DataIntegrityError(
                        f"[fail-loud/NF-26] 因子 {f} 的 {key} 无效，无法按 {method} 加权"
                    )
                weights[f] = weight

        model_name = name or self._default_model_name(method, factor_list)
        handle = ModelHandle(
            name=model_name,
            weights=weights,
            method=method,
            created_at=dt.datetime.now().astimezone(),
        )
        if save_model:
            self.save_model(handle)
        return handle

    # ------------------------------------------------------------------ #
    # 模型存取
    # ------------------------------------------------------------------ #

    def save_model(self, handle: ModelHandle) -> None:
        """保存模型句柄到 ``models.db``。"""
        payload = {
            "name": handle.name,
            "method": handle.method,
            "weights": handle.weights,
            "created_at": handle.created_at.isoformat(timespec="seconds"),
        }
        with self._models_connect() as conn:
            conn.execute(
                """
                INSERT INTO models (name, method, weights, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    method=excluded.method,
                    weights=excluded.weights,
                    created_at=excluded.created_at
                """,
                (
                    handle.name,
                    handle.method,
                    json.dumps(payload["weights"], ensure_ascii=False),
                    payload["created_at"],
                ),
            )

    def load_model(self, name: str) -> ModelHandle:
        """按名加载模型。

        Args:
            name: 模型名。

        Returns:
            :class:`ModelHandle`。

        Raises:
            MissingConfigError: 模型不存在。
        """
        with self._models_connect() as conn:
            row = conn.execute(
                "SELECT name, method, weights, created_at FROM models WHERE name = ?",
                (name,),
            ).fetchone()
        if row is None:
            raise MissingConfigError(
                f"[fail-loud/NF-26] 模型 {name!r} 不存在。可用: {self.list_models()}"
            )
        weights = json.loads(str(row["weights"]))
        return ModelHandle(
            name=str(row["name"]),
            weights=weights,
            method=str(row["method"]),
            created_at=dt.datetime.fromisoformat(str(row["created_at"])),
        )

    def list_models(self) -> list[str]:
        """列出已保存模型名（升序）。"""
        with self._models_connect() as conn:
            rows = conn.execute("SELECT name FROM models ORDER BY name").fetchall()
        return [str(row["name"]) for row in rows]

    def list_model_handles(self) -> list[ModelHandle]:
        """列出全部已保存模型句柄（含权重/方法/创建时间，F6 用）。

        Returns:
            按模型名升序的 :class:`ModelHandle` 列表。
        """
        with self._models_connect() as conn:
            rows = conn.execute(
                "SELECT name, method, weights, created_at FROM models ORDER BY name"
            ).fetchall()
        handles: list[ModelHandle] = []
        for row in rows:
            handles.append(
                ModelHandle(
                    name=str(row["name"]),
                    weights=json.loads(str(row["weights"])),
                    method=str(row["method"]),
                    created_at=dt.datetime.fromisoformat(str(row["created_at"])),
                )
            )
        return handles

    # ------------------------------------------------------------------ #
    # 因子注册表
    # ------------------------------------------------------------------ #

    def list_factors(self) -> list[str]:
        """列出全部可用因子（上游注册表 + 自定义发现，升序）。"""
        from Kuantix.factor.factors import discover_factors

        discover_factors()
        return sorted(FACTORY_REGISTRY)

    @property
    def store(self) -> FactorStore:
        """因子存储（供 CLI/API 组合根注入到 ScreenService，NF-3 解耦）。"""
        return self._store

    def describe(self) -> dict[str, Any]:
        """服务摘要（JSON 安全）。"""
        return {
            "factors": self.list_factors(),
            "models": self.list_models(),
            "store": self._store.describe(),
        }

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def _load_pool(
        self, req: ComputeRequest, profile: MarketProfile
    ) -> dict[str, pd.DataFrame]:
        """从本地 L1 读取代码池 DataFrame（全程本地，不走网络）。

        **批量读（问题 3 B4 / T04）**：reader 具备 ``read_daily_frames``
        （SQLite 后端）时一次 ``WHERE code IN (...)?`` 取回，按 code 分组；
        否则逐只 ``read_daily_frame``（镜像兜底，行为一致仅性能不同）。

        **区间样本（数据量大优化）**：按 ``req.start``/``req.end`` 只加载
        目标区间（并向前多取 ``_LOOKBACK_DAYS`` 作为因子滚动窗口的前视缓冲），
        SQL 层经 ``(market, code, date)`` 索引限定 —— 全市场 5030 只 A 股
        从加载全部历史（~976 万行）降为只取区间（约 1/4 数据量），是
        ``/api/v1/factor/compute`` 慢的主因（原先 `_load_pool` 全量读耗时
        ~96s）。区间截取由 :meth:`FactorStore.compute` 完成（含缓冲段）。

        Args:
            req: 计算请求（含 codes）。
            profile: 市场档案（提供代码→交易所映射，NF-5）。

        Returns:
            ``{code: DataFrame}``（带 datetime 列）。
        """
        if req.codes is not None and req.codes:
            codes = list(req.codes)
        else:
            # 代码池优先取 SQLite 主存储（与「从 SQLite 取样本」同源）：只返回
            # daily_bars 真实存在样本的代码，避免 ``list_day_files``（vipdoc
            # 文件系统）混入北交所指数（``880xxx``）等无镜像样本的代码，
            # 否则 auto 后端回退镜像时 bj 目录缺失而 fail-loud（NF-27）。
            list_codes = getattr(self._reader, "list_codes", None)
            if list_codes is not None and getattr(self._reader, "store", None) is not None:
                codes = list_codes(market=req.market)
            else:
                codes = [c for _, c, _ in self._reader.list_day_files()]

        batch_reader = getattr(self._reader, "read_daily_frames", None)
        if batch_reader is not None and getattr(self._reader, "store", None) is not None:
            # 只取 [start - 缓冲, end] 区间，避免全量历史入内存。
            # 注意：缓冲起点必须用 ``datetime.date`` 减 ``timedelta`` 再转
            # ``YYYYMMDD``，直接 ``start_int - 天数`` 会在跨年/跨月时产生
            # 非法日期（如 20240101-180=20239921），SQLite 区间比较会失效。
            lookback = req.start - dt.timedelta(days=_LOOKBACK_DAYS)
            lookback_start = lookback.year * 10000 + lookback.month * 100 + lookback.day
            end_int = req.end.year * 10000 + req.end.month * 100 + req.end.day
            return batch_reader(
                codes,
                market=req.market,
                start_date=lookback_start,
                end_date=end_int,
            )

        pool: dict[str, pd.DataFrame] = {}
        for code in codes:
            exchange = profile.exchange_for_code(code)
            frame = self._reader.read_daily_frame(exchange, code)
            if not frame.empty:
                pool[code] = frame
        return pool

    @staticmethod
    def _default_model_name(method: str, factors: list[str]) -> str:
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        joined = "_".join(factors[:3])
        return f"{method}_{joined}_{stamp}"

    def _ensure_models_schema(self) -> None:
        with self._models_connect() as conn:
            conn.execute(_MODELS_SCHEMA)

    def _models_connect(self) -> sqlite3.Connection:
        """P1-1：connect_sqlite 自动建父目录并应用 WAL + busy_timeout + synchronous=NORMAL。

        原代码每次连接前手动 ``mkdir``，现由 ``connect_sqlite`` 统一处理。
        """
        return connect_sqlite(self._models_db)
