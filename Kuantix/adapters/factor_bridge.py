"""上游因子引擎 / L1 读侧的薄包装（NF-1 收敛）。

本模块是**数据湖读侧到因子引擎的桥**：
- :class:`L1Reader` —— 从 vipdoc 读 L1 日线（``read_daily_bars`` / ``read_ex_daily_bars``），
  转成 :class:`~Kuantix.core.contracts.Bar` 或带 ``datetime`` 列的 DataFrame
  （上游 :class:`~easy_tdx.factor.engine.FactorEngine` 的输入要求）；
- :class:`FactorEngineBridge` —— 包装上游 ``FactorEngine``（compute_single /
  compute_cross_section / compute_forward_returns）；
- :class:`FactorAnalyzerBridge` —— 包装上游 ``FactorAnalyzer.full_report``。

所有上游调用集中在本模块（唯一允许 import easy_tdx 的适配层，NF-1/R2）。
业务层（factor/ screen/ data/）只能经本模块访问因子引擎与 L1 读侧。
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
from easy_tdx.factor.analysis import FactorAnalyzer, FactorReport
from easy_tdx.factor.base import FACTORY_REGISTRY, Factor, register_factor
from easy_tdx.factor.engine import _ALL_DATES, FactorEngine
from easy_tdx.offline.daily_bar import read_daily_bars
from easy_tdx.offline.ex_daily_bar import read_ex_daily_bars

from Kuantix.core.contracts import Bar, Security
from Kuantix.core.fail_loud import (
    DataIntegrityError,
    MissingKeyError,
    UpstreamContractError,
)

__all__ = [
    "Factor",
    "register_factor",
    "FACTORY_REGISTRY",
    "L1Reader",
    "FactorEngineBridge",
    "FactorAnalyzerBridge",
    "bars_to_frame",
]

#: L1Reader 后端取值（auto / sqlite / mirror）。
L1_BACKEND_VALUES: tuple[str, ...] = ("auto", "sqlite", "mirror")

#: vipdoc 交易所前缀 → 市场码（SQLite daily_bars 的 market 列）。
_EXCHANGE_TO_MARKET: dict[str, str] = {
    "sh": "CN",
    "sz": "CN",
    "hk": "HK",
    "us": "US",
}


# ---------------------------------------------------------------------------
# Bar → DataFrame 共享工具（实时/本地同构，设计一 D1.4）
# ---------------------------------------------------------------------------


def bars_to_frame(bars: list[Bar]) -> pd.DataFrame:
    """把 :class:`Bar` 列表转成带 ``datetime`` 列的 DataFrame。

    **实时与本地同构的落点**：:meth:`L1Reader._bars_to_frame` 委托本函数，
    :func:`Kuantix.backtest.data_source.fetch_live_frame` 也调用它 —— 保证
    live 拉取的数据与本地 vipdoc 读出的列格式逐列一致
    （``datetime/open/high/low/close/vol/amount``），喂同一上游引擎，回测可比。

    Args:
        bars: 升序 Bar 列表（vol 单位=手，RD-8 已在适配层保证）。

    Returns:
        带 ``datetime`` 列的 DataFrame；``bars`` 为空时返回空骨架（列齐）。
    """
    if not bars:
        return pd.DataFrame(
            columns=["datetime", "open", "high", "low", "close", "vol", "amount"]
        )
    rows = [
        {
            "datetime": pd.Timestamp(bar.date),
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "vol": bar.vol,
            "amount": bar.amount,
        }
        for bar in bars
    ]
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# L1 读侧
# ---------------------------------------------------------------------------


class L1Reader:
    """从 vipdoc（镜像）或 SQLite（主存储）读取 L1 日线（数据湖读侧桥）。

    **双后端**（设计文档 08，NF-26 语义）：
    - ``backend="auto"``（默认）：SQLite 有数据 → 读 SQLite；无数据 → 镜像
      （vipdoc 文件）兜底；两处都无 → 显式 :class:`DataIntegrityError`；
    - ``backend="sqlite"``：只读 SQLite（无数据直接报错，不降级）；
    - ``backend="mirror"``：只读 vipdoc 文件（旧行为）。

    旧签名 ``L1Reader(vipdoc_root)`` 保持兼容 —— 不传 ``store`` 时自动退化为
    纯镜像后端，现有调用点零改动。

    Args:
        vipdoc_root: vipdoc 根目录（如 ``~/.Kuantix/vipdoc``）。
        backend: ``auto`` / ``sqlite`` / ``mirror``。
        store: :class:`~Kuantix.data.market_store.MarketStore`；``None`` 时
            SQLite 相关方法不可用（auto 退化为 mirror）。
    """

    def __init__(
        self,
        vipdoc_root: Path | str,
        *,
        backend: str = "auto",
        store: Any | None = None,
    ) -> None:
        self._root = Path(vipdoc_root).expanduser()
        self._backend = str(backend).strip().lower()
        if self._backend not in L1_BACKEND_VALUES:
            raise MissingKeyError(
                f"[fail-loud/NF-26] L1Reader backend 取值非法: {backend!r}"
                f"（允许 {list(L1_BACKEND_VALUES)}）"
            )
        self._store = store

    @property
    def root(self) -> Path:
        """vipdoc 根目录。"""
        return self._root

    @property
    def backend(self) -> str:
        """当前后端（``auto`` / ``sqlite`` / ``mirror``）。"""
        return self._backend

    @property
    def store(self) -> Any | None:
        """SQLite 存储（可能为 ``None``，表示未装配）。"""
        return self._store

    @classmethod
    def from_config(cls, config: Any, *, backend: str = "auto") -> "L1Reader":
        """从配置构造双后端 reader（SQLite 主 + 镜像兜底，T04）。

        Args:
            config: :class:`~Kuantix.config.Config`。
            backend: ``auto`` / ``sqlite`` / ``mirror``。

        Returns:
            装配好 :class:`~Kuantix.data.market_store.MarketStore` 的 reader。
        """
        from Kuantix.data.market_store import MarketStore

        store = MarketStore(config.paths.db / config.storage.market_db)
        return cls(config.paths.vipdoc, backend=backend, store=store)

    def day_path(self, exchange: str, code: str) -> Path:
        """A 股日线文件路径 ``<root>/<exchange>/lday/<exchange><code>.day``。"""
        return self._root / exchange / "lday" / f"{exchange}{code}.day"

    def ex_day_path(self, market_code: int, code: str) -> Path:
        """扩展市场日线文件路径 ``<root>/ds/<market>#<code>.day``。"""
        return self._root / "ds" / f"{int(market_code)}#{code}.day"

    @staticmethod
    def _market_for_exchange(exchange: str) -> str:
        """交易所前缀 → 市场码（SQLite market 列）。

        Args:
            exchange: ``sh`` / ``sz`` / ``hk`` / ``us``。

        Returns:
            市场码。

        Raises:
            DataIntegrityError: 未知交易所前缀。
        """
        key = str(exchange).strip().lower()
        if key not in _EXCHANGE_TO_MARKET:
            raise DataIntegrityError(
                f"[fail-loud/NF-26] 未知交易所前缀 {exchange!r}，无法映射市场码"
            )
        return _EXCHANGE_TO_MARKET[key]

    # ------------------------------------------------------------------ #
    # 读 A 股日线
    # ------------------------------------------------------------------ #

    def read_daily_bars(self, exchange: str, code: str) -> list[Bar]:
        """读取 A 股日线并转为 :class:`Bar` 列表（按后端路由）。

        Args:
            exchange: ``sh`` / ``sz``。
            code: 证券代码。

        Returns:
            升序 Bar 列表（vol 单位=手，RD-8 已在适配层保证）。

        Raises:
            DataIntegrityError: 文件不存在/为空/**损坏（上游解析抛裸异常时统一
                归一为数据完整性错误，NF-26，避免 500 穿透）**；SQLite 后端
                无数据（auto 模式已先尝试镜像兜底）。
            UpstreamContractError: 上游返回结构异常。
        """
        if self._backend == "mirror":
            return self._read_mirror_bars(exchange, code)
        if self._backend == "sqlite":
            return self._read_store_bars(exchange, code)
        # auto：SQLite 优先，无数据 → 镜像兜底（NF-26 显式链）
        try:
            return self._read_store_bars(exchange, code)
        except DataIntegrityError:
            return self._read_mirror_bars(exchange, code)

    def _read_store_bars(self, exchange: str, code: str) -> list[Bar]:
        """从 SQLite 读日线（auto/sqlite 后端共用）。"""
        if self._store is None:
            raise DataIntegrityError(
                "[fail-loud/NF-26] L1Reader 未装配 SQLite store，无法走 sqlite 后端"
            )
        market = self._market_for_exchange(exchange)
        try:
            bars = self._store.read_daily_bars(market, code)
        except sqlite3.Error as exc:
            raise DataIntegrityError(
                f"[fail-loud/NF-26] SQLite 行情库损坏: {self._store.db_path}"
                f"（{type(exc).__name__}: {exc}）"
            ) from exc
        if not bars:
            raise DataIntegrityError(
                f"[fail-loud/NF-26] SQLite 无 {market}:{code} 数据"
            )
        return bars

    def _read_mirror_bars(self, exchange: str, code: str) -> list[Bar]:
        """从 vipdoc 文件读日线（auto/mirror 后端共用，旧逻辑）。"""
        path = self.day_path(exchange, code)
        if not path.is_file():
            raise DataIntegrityError(
                f"[fail-loud/NF-27] L1 文件不存在: {path}"
            )
        try:
            raw = read_daily_bars(path)
            if not isinstance(raw, list):
                raise UpstreamContractError(
                    f"[fail-loud/NF-1] read_daily_bars({path.name}) 返回 "
                    f"{type(raw).__name__}，期望 list"
                )
            return [self._to_bar(item, source=path.name) for item in raw]
        except (DataIntegrityError, UpstreamContractError):
            raise
        except Exception as exc:  # noqa: BLE001 - 上游解析裸异常（ValueError/struct）统一归一
            raise DataIntegrityError(
                f"[fail-loud/NF-26] L1 日线文件损坏: {path}"
                f"（{type(exc).__name__}: {exc}）"
            ) from exc

    def read_daily_frame(self, exchange: str, code: str) -> pd.DataFrame:
        """读取 A 股日线并转为带 ``datetime`` 列的 DataFrame（因子引擎输入）。

        SQLite 与镜像后端返回**同构**列
        （``datetime/open/high/low/close/vol/amount``），上游引擎零改动。
        """
        bars = self.read_daily_bars(exchange, code)
        return self._bars_to_frame(bars)

    def read_daily_frames(
        self,
        codes: Iterable[str],
        market: str = "CN",
        *,
        start_date: int | None = None,
        end_date: int | None = None,
        tail: int | None = None,
    ) -> dict[str, pd.DataFrame]:
        """批量读取多只标的日线 DataFrame（问题 3 B4：因子喂数据向量化）。

        SQLite 后端一次 ``WHERE code IN (...)?`` 取回；**auto 后端在 SQLite
        缺码时逐只回退镜像**（"仅镜像"状态 SQLite 空 → 全部走镜像，保证
        因子/选股/回测在未迁移时仍可用）；mirror 后端逐只读兜底。

        **区间过滤（数据量大优化）**：``start_date``/``end_date``（``YYYYMMDD``）
        透传给 SQLite 主存储，SQL 层用索引限定区间 —— 因子计算只取目标区间
        样本，避免全量历史入内存；镜像兜底段返回全量（区间已在调用方截取）。

        Args:
            codes: 代码列表。
            market: 市场码（SQLite 的 market 列）。
            start_date: 起始日期（含，``YYYYMMDD``）；``None`` 不限。
            end_date: 结束日期（含，``YYYYMMDD``）；``None`` 不限。
            tail: 每只标的只取最后 N 根；``None`` 取区间内全部。选股技术
                过滤只依赖尾部窗口（``rolling(20)`` 级），传 ``tail`` 可避免
                整段历史读放大；镜像兜底段读全量后按 ``tail`` 截尾，两条
                链路语义一致。

        Returns:
            ``{code: DataFrame}``（带 ``datetime`` 列，升序）。
        """
        code_list = list(dict.fromkeys(str(c) for c in codes))
        if not code_list:
            return {}
        result: dict[str, pd.DataFrame] = {}
        if self._store is not None and self._backend == "sqlite":
            # sqlite 后端：只读主存储，缺码不降级（NF-26 显式语义）
            return self._store.read_daily_frames(
                code_list, market, start_date=start_date, end_date=end_date, tail=tail
            )
        if self._store is not None and self._backend == "auto":
            # auto：SQLite 批量读（限区间），缺码补镜像（与单只 read_daily_bars 同链）
            result.update(
                self._store.read_daily_frames(
                    code_list, market, start_date=start_date, end_date=end_date,
                    tail=tail,
                )
            )
            missing = [
                code
                for code in code_list
                if code not in result or result[code].empty
            ]
            if not missing:
                return result
            code_list = missing
        # 镜像兜底：按代码经 MarketProfile 推导交易所逐只读
        from Kuantix.core.market import get_market_profile

        profile = get_market_profile(market)
        for code in code_list:
            exchange = profile.exchange_for_code(code)
            frame = self.read_daily_frame(exchange, code)
            if not frame.empty:
                # 镜像后端无法在 IO 层截尾，读全量后按 tail 对齐 SQLite 语义
                if tail is not None and int(tail) > 0 and len(frame) > int(tail):
                    frame = frame.tail(int(tail)).reset_index(drop=True)
                result[code] = frame
        return result

    def has_data(self, exchange: str, code: str) -> bool:
        """本地是否存在该标的日线（auto 数据源分支的判定，O(1)）。"""
        if self._backend == "sqlite":
            if self._store is None:
                return False
            return self._store.has_data(self._market_for_exchange(exchange), code)
        if self._backend == "mirror":
            return self.day_path(exchange, code).is_file()
        # auto：SQLite 优先，其次镜像
        if self._store is not None and self._store.has_data(
            self._market_for_exchange(exchange), code
        ):
            return True
        return self.day_path(exchange, code).is_file()

    def list_securities(self, market: str = "CN") -> list[Security]:
        """列出证券清单（证券本地化落点，问题 1）。

        SQLite 后端读 ``securities`` 表；镜像后端（未装配 store）由日线文件
        推导（``name`` 为空 —— 文件系统不携带名称，仅供代码池使用）。

        Args:
            market: 市场码。

        Returns:
            :class:`Security` 列表。
        """
        if self._store is not None:
            return self._store.list_securities(market)
        from Kuantix.adapters.coefficients import detect_security_type

        result: list[Security] = []
        for exchange, code, _path in self.list_day_files():
            sec_type = detect_security_type(f"{exchange}{code}.day")
            if sec_type == "UNKNOWN":
                continue
            result.append(
                Security(
                    code=code,
                    exchange=exchange,
                    market=market,
                    security_type=sec_type,
                    name="",
                )
            )
        return result

    def read_ex_daily_bars(self, market_code: int, code: str) -> list[Bar]:
        """读取扩展市场（港股/美股）日线。

        Raises:
            DataIntegrityError: 文件不存在/为空/**损坏（上游解析裸异常统一归一，
                NF-26，与 :meth:`read_daily_bars` 同口径）**。
            UpstreamContractError: 上游返回结构异常。
        """
        path = self.ex_day_path(market_code, code)
        if not path.is_file():
            raise DataIntegrityError(
                f"[fail-loud/NF-27] L1 扩展市场文件不存在: {path}"
            )
        try:
            raw = read_ex_daily_bars(path)
            if not isinstance(raw, list):
                raise UpstreamContractError(
                    f"[fail-loud/NF-1] read_ex_daily_bars({path.name}) 返回 "
                    f"{type(raw).__name__}，期望 list"
                )
            return [self._to_bar(item, source=path.name) for item in raw]
        except (DataIntegrityError, UpstreamContractError):
            raise
        except Exception as exc:  # noqa: BLE001 - 上游解析裸异常统一归一
            raise DataIntegrityError(
                f"[fail-loud/NF-26] L1 扩展市场日线文件损坏: {path}"
                f"（{type(exc).__name__}: {exc}）"
            ) from exc

    def list_day_files(
        self, exchanges: Iterable[str] = ("sh", "sz")
    ) -> list[tuple[str, str, Path]]:
        """列出 A 股全部日线文件 ``(exchange, code, path)``。

        注意：vipdoc 的 ``sh/lday``/``sz/lday`` 目录可能混有北交所指数
        （如 ``880xxx``）等非沪深 A 股文件，这些代码在 SQLite 主存储中
        不存在，作为因子代码池会导致 auto 后端回退镜像时镜像文件缺失。
        需要「真实存在样本」的代码池应优先用 :meth:`list_codes`（SQLite）。
        """
        out: list[tuple[str, str, Path]] = []
        for exchange in exchanges:
            lday = self._root / exchange / "lday"
            if not lday.is_dir():
                continue
            for path in sorted(lday.glob("*.day")):
                code = path.name.lower()[2:8]
                out.append((str(exchange), code, path))
        return out

    def list_codes(self, market: str = "CN") -> list[str]:
        """从 SQLite 主存储列出该市场 daily_bars 的去重代码（**零文件 IO**）。

        与 :meth:`list_day_files`（vipdoc 文件系统，可能含北交所指数等无
        真实样本的代码）不同，这里直接读 ``daily_bars`` 的 ``DISTINCT code``，
        只返回 SQLite 里真实存在日线样本的代码 —— 与因子/选股「从 SQLite
        取样本」的口径完全一致，避免镜像回退时北交所 ``bj/*.day`` 缺失而
        fail-loud（NF-27）。

        Args:
            market: 市场码。

        Returns:
            升序去重代码列表。
        """
        if self._store is None:
            return [c for _, c, _ in self.list_day_files()]
        return self._store.list_daily_bar_codes(str(market).upper())

    # ------------------------------------------------------------------ #

    @staticmethod
    def _to_bar(item: Any, *, source: str) -> Bar:
        """把上游 Bar 对象转成 :class:`~Kuantix.core.contracts.Bar`。

        支持属性访问（SecurityBar / ExDailyBar）与字典两种形态。
        """
        if isinstance(item, dict):
            bar_date = dt.date(
                int(item["year"]), int(item["month"]), int(item["day"])
            )
            return Bar(
                date=bar_date,
                open=float(item["open"]),
                high=float(item["high"]),
                low=float(item["low"]),
                close=float(item["close"]),
                vol=float(item["vol"]),
                amount=float(item["amount"]),
            )
        try:
            bar_date = dt.date(
                int(item.year), int(item.month), int(item.day)
            )
        except AttributeError as exc:
            raise UpstreamContractError(
                f"[fail-loud/NF-1] {source} 上游 Bar 缺少 year/month/day 属性"
            ) from exc
        return Bar(
            date=bar_date,
            open=float(item.open),
            high=float(item.high),
            low=float(item.low),
            close=float(item.close),
            vol=float(item.vol),
            amount=float(item.amount),
        )

    @staticmethod
    def _bars_to_frame(bars: list[Bar]) -> pd.DataFrame:
        """把 Bar 列表转成带 ``datetime`` 列的 DataFrame（委托模块级 bars_to_frame）。"""
        return bars_to_frame(bars)


# ---------------------------------------------------------------------------
# 因子引擎 / 分析器
# ---------------------------------------------------------------------------


class FactorEngineBridge:
    """上游 :class:`FactorEngine` 的薄包装。

    Args:
        engine: 上游引擎实例；``None`` 时新建默认实例。
    """

    def __init__(self, engine: FactorEngine | None = None) -> None:
        self._engine = engine if engine is not None else FactorEngine()

    def compute_single(
        self, df: pd.DataFrame, factors: list[str]
    ) -> pd.DataFrame:
        """单标的多因子计算（原地新增因子列）。"""
        if df.empty:
            return df.copy()
        return self._engine.compute_single(df, factors)

    def compute_cross_section(
        self,
        data: dict[str, pd.DataFrame],
        factors: list[str],
        date: int | None = None,
    ) -> pd.DataFrame:
        """多标的截面因子计算。

        语义与上游一致：
        - ``date`` 传具体 YYYYMMDD → 只保留该日；
        - ``date=None`` → **全部日期**（上游 ``_ALL_DATES`` 默认）；
        - 若要「仅最新一行」，传 ``date=_ALL_DATES`` 之外的哨兵不可行，
          请改传一个超大日期（如 99991231）由上游兜底（内部实现细节）。
        """
        if date is None:
            return self._engine.compute_cross_section(data, factors)
        return self._engine.compute_cross_section(data, factors, date)

    def compute_forward_returns(
        self, data: dict[str, pd.DataFrame], period: int = 5
    ) -> pd.DataFrame:
        """计算前向收益（长表 date/code/forward_Nd）。"""
        return self._engine.compute_forward_returns(data, period)


class FactorAnalyzerBridge:
    """上游 :class:`FactorAnalyzer` 的薄包装（需 scipy，``easy_tdx[science]``）。"""

    @staticmethod
    def analyze(
        factor_df: pd.DataFrame,
        return_df: pd.DataFrame,
        factor_col: str,
        return_col: str,
        n_quantiles: int = 5,
    ) -> FactorReport:
        """生成因子有效性报告。

        Args:
            factor_df: 截面因子长表（date/code/factor_col）。
            return_df: 前向收益长表（date/code/return_col）。
            factor_col: 因子列名。
            return_col: 收益列名。
            n_quantiles: 分层数。

        Returns:
            上游 :class:`FactorReport`。
        """
        return FactorAnalyzer(
            factor_df,
            return_df,
            factor_col=factor_col,
            return_col=return_col,
            n_quantiles=n_quantiles,
        ).full_report()

    @staticmethod
    def _safe_float(value: Any, precision: int = 6) -> float | None:
        """浮点转 JSON 安全值：NaN/Inf → None（NF-12，边界兜底）。"""
        try:
            num = float(value)
        except (TypeError, ValueError):
            return None
        if pd.isna(num) or num in (float("inf"), float("-inf")):
            return None
        return round(num, precision)

    @staticmethod
    def report_to_dict(report: FactorReport) -> dict[str, Any]:
        """把 :class:`FactorReport` 转成 JSON 安全字典。

        注意：``ic_series`` 是 pandas Series，NF-12 要求 JSON 不含 NaN/Inf，
        这里只输出最后 30 个非空 IC 值，避免污染信封。
        """
        ic_series = report.ic_series
        ic_tail: list[dict[str, Any]] = []
        for idx, value in ic_series.items():
            safe = FactorAnalyzerBridge._safe_float(value)
            if safe is None:
                continue
            ic_tail.append({"date": str(idx), "ic": safe})
            if len(ic_tail) >= 30:
                break
        safe = FactorAnalyzerBridge._safe_float
        return {
            "name": report.name,
            "ic_mean": safe(report.ic_mean),
            "ic_std": safe(report.ic_std),
            "ir": safe(report.ir),
            "ic_positive_rate": safe(report.ic_positive_rate),
            "quantile_returns": {
                k: safe(v) for k, v in report.quantile_returns.items()
            },
            "top_minus_bottom": safe(report.top_minus_bottom),
            "turnover_rate": safe(report.turnover_rate),
            "autocorr": safe(report.autocorr),
            "ic_series_tail": ic_tail,
        }
