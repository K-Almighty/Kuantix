"""Pydantic 请求/响应模型（契约 §3 DTO 的 Pydantic 版本）。

请求体模型被三个 router 实际用于校验（非法枚举/缺字段 → FastAPI
RequestValidationError → 400 信封）。响应模型主要供 OpenAPI 文档
（NF-11）使用——实际响应一律经 :func:`Kuantix.api.deps.respond` /
:class:`Kuantix.core.envelope.Envelope` 序列化，不走 FastAPI
``response_model`` 序列化，保证 NF-9/NF-12 双保险。

字段名、单位、比例口径与 ``docs/api-contract.md`` §3 逐字段对齐
（比例一律小数，``0.05`` = 5%；日期 ``YYYY-MM-DD``；时间戳 ISO8601 带时区）。
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, Field

__all__ = [
    "SyncRequest",
    "ComputeRequest",
    "CombineRequest",
    "ScreenFilterInput",
    "ScreenRunRequest",
    "ScreenFactorRunRequest",
    "WatchlistAddRequest",
    "RuleInput",
    "PositionInput",
    "BacktestRunRequest",
    "PortfolioRunRequest",
    "PortfolioResult",
    "MultiStrategyItemInput",
    "MultiStrategyRunRequest",
    "StrategyCreate",
    "StrategyView",
    "OptimizeRunRequest",
    "OptimizeGridPoint",
    "OptimizeResult",
    "KlineBar",
    "SignalPoint",
    "KlineWithSignals",
    "TestConnectionRequest",
    "TestConnectionResult",
    "SettingsKnownHostItem",
    "SettingsStatus",
    "MetaModel",
    "EnvelopeModel",
    "PageModel",
    "SyncProgressModel",
    "JobModel",
    "DataLakeStatusModel",
    "VerifyReportModel",
    "SecurityHitModel",
    "QuarantineEntryModel",
    "FactorInfoModel",
    "FactorReportModel",
    "FactorModel",
    "ScreenBatchModel",
    "ScreenResultViewModel",
    "FilterInfoModel",
    "MonitorStatusModel",
    "WatchlistItemModel",
    "RuleModel",
    "CriterionInfoModel",
    "PositionViewModel",
    "AlertModel",
    "ChannelInfoModel",
]

T = TypeVar("T")


# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------


class SyncRequest(BaseModel):
    """D2 请求体（契约 §2.1）。"""

    mode: Literal["full", "incremental"] = "full"
    market: str = "CN"
    years: int = Field(default=10, ge=1, le=30)
    workers: int | None = Field(default=None, ge=1, le=16)
    #: NF-28：交易时段内全量回补需显式确认
    force: bool = False


class ComputeRequest(BaseModel):
    """F2 请求体（契约 §2.2）。"""

    factors: list[str] = Field(min_length=1)
    market: str = "CN"
    start: dt.date | None = None
    end: dt.date | None = None
    pool: str | list[str] = "all"
    force: bool = False


class ReportRequest(BaseModel):
    """F4 请求体：因子有效性报告（后台异步 Job，子进程隔离）。"""

    name: str = Field(min_length=1, description="因子名（内部标识符）")
    market: str = "CN"
    start: dt.date | None = None
    end: dt.date | None = None


class CombineRequest(BaseModel):
    """F5 请求体（契约 §2.2）。"""

    factors: list[str] = Field(min_length=1)
    method: Literal["equal", "ic", "ir"] = "equal"
    save_model: bool = False
    model_name: str | None = None
    market: str = "CN"


class RankingRequest(BaseModel):
    """F7 多因子排名请求体（对标 easy_tdx 多因子综合对比）。"""

    factors: list[str] = Field(min_length=2, description="待排名因子（≥2）")
    market: str = "CN"
    start: dt.date | None = None
    end: dt.date | None = None
    forward_period: int = Field(default=5, ge=1, le=20, description="前向收益周期（交易日）")
    n_quantiles: int = Field(default=5, ge=2, le=10, description="分层数")
    top_fraction: float = Field(default=0.2, gt=0, le=1, description="top 组合占比")


class ScreenFilterInput(BaseModel):
    """S2 请求体 filters 元素（契约 §3.4）。"""

    type: Literal["tech", "chanlun"]
    condition: str
    params: dict[str, Any] = Field(default_factory=dict)


class ScreenRunRequest(BaseModel):
    """S2 请求体（契约 §3.4 ScreenRunRequest）。"""

    model: str | None = None
    market: str = "CN"
    pool: str | list[str] = "all"
    top_n: int = Field(default=50, ge=1, le=500)
    filters: list[ScreenFilterInput] = Field(default_factory=list)
    combine: Literal["and", "or"] = "and"
    exclude_st: bool = True
    exclude_suspended: bool = True
    exclude_new: bool = True
    as_of: dt.date | None = None


class ScreenFactorRunRequest(BaseModel):
    """单因子选股请求体（基于最新数据，非回测，同步返回）。

    与 :class:`ScreenRunRequest` 的区别：只取一个因子的最新截面，
    按该因子取值排序取 TopN，避免多因子全量加载与模型打分，速度更快。
    """

    factor: str
    market: str = "CN"
    pool: str | list[str] = "all"
    top_n: int = Field(default=50, ge=1, le=500)
    order: Literal["desc", "asc"] = "desc"
    as_of: dt.date | None = None
    days_back: int | None = Field(default=None, ge=1, le=365)
    filters: list[ScreenFilterInput] = Field(default_factory=list)
    combine: Literal["and", "or"] = "and"
    exclude_st: bool = True
    exclude_suspended: bool = True
    exclude_new: bool = True


# ---------------------------------------------------------------------------
# 监控层请求体（契约 §2.4 / §3.5）
# ---------------------------------------------------------------------------


class WatchlistAddRequest(BaseModel):
    """M5 请求体（契约 §2.4 M5）。"""

    market: str = "CN"
    codes: list[str] = Field(min_length=1)
    source: str = "manual"


class RuleInput(BaseModel):
    """M9/M10 请求体（契约 §3.5 Rule；M9 全必填，M10 部分字段可省）。

    判据参数按 criterion_type（price/indicator/stop_loss）在路由层校验。
    """

    name: str | None = None
    market: str | None = None
    codes: list[str] | None = None
    criterion_type: Literal["price", "indicator", "stop_loss"] | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    level: Literal["info", "warning", "critical"] | None = None
    cooldown_seconds: float | None = Field(default=None, ge=0)
    enabled: bool | None = None


class PositionInput(BaseModel):
    """M13 请求体（契约 §3.5 PositionInput；shares 单位=股，非手）。"""

    code: str
    market: str = "CN"
    shares: float = Field(gt=0)
    cost_price: float = Field(gt=0)
    opened_at: dt.date | None = None
    name: str | None = None


class BacktestRunRequest(BaseModel):
    """B2 请求体（契约 §3.6，v1.2 增量；v1.4 增 data_source）。"""

    market: str = "CN"
    codes: list[str] = Field(min_length=1, max_length=20, description="标的代码池（6 位代码）")
    strategy: str = "ma_cross"
    params: dict[str, Any] = Field(default_factory=dict)
    start: dt.date = dt.date(2020, 1, 1)
    end: dt.date = dt.date(2025, 12, 31)
    cash: float = Field(default=1_000_000.0, gt=0)
    commission: float = Field(default=0.0003, ge=0, le=0.01)
    min_commission: float = Field(default=5.0, ge=0)
    stamp_tax: float = Field(default=0.001, ge=0, le=0.01)
    slippage: float = Field(default=0.0, ge=0, le=0.05)
    execution: Literal["next_open", "next_close"] = "next_open"
    #: v1.4：数据源。local=只读本地湖；live=强制实时拉取（仅单标的，多标的 422）；
    #: auto=本地优先、缺失转实时（默认，向后兼容既有调用方）。
    data_source: Literal["auto", "local", "live"] = "auto"


class PortfolioRunRequest(BaseModel):
    """P1 请求体（契约 §2.1，v1.3 增量；组合回测·资金分仓）。"""

    market: str = "CN"
    codes: list[str] = Field(min_length=1, max_length=20, description="组合标的池（6 位代码）")
    strategy: str = "ma_cross"
    params: dict[str, Any] = Field(default_factory=dict)
    start: dt.date = dt.date(2020, 1, 1)
    end: dt.date = dt.date(2025, 12, 31)
    cash: float = Field(default=1_000_000.0, gt=0, description="组合总资金（按 N 均分）")
    commission: float = Field(default=0.0003, ge=0, le=0.01)
    min_commission: float = Field(default=5.0, ge=0)
    stamp_tax: float = Field(default=0.001, ge=0, le=0.01)
    slippage: float = Field(default=0.0, ge=0, le=0.05)
    execution: Literal["next_open", "next_close"] = "next_open"


class PortfolioResult(BaseModel):
    """P3 / S5 响应 DTO（契约 §2.1，v1.3 增量；上游 PortfolioResult 形状）。"""

    total_performance: dict[str, Any]
    individual_results: dict[str, Any]
    equity_allocation: dict[str, float]
    combined_equity: list[dict[str, Any]]


class MultiStrategyItemInput(BaseModel):
    """S5 请求体 items 元素（契约 §2.2，v1.3 增量）。"""

    strategy: str
    label: str = Field(min_length=1, max_length=120)
    code: str
    params: dict[str, Any] = Field(default_factory=dict)


class MultiStrategyRunRequest(BaseModel):
    """S5 请求体（契约 §2.2，v1.3 增量；多策略组合回测，资金 1/N 均分）。"""

    market: str = "CN"
    items: list[MultiStrategyItemInput] = Field(min_length=1, max_length=10)
    cash: float = Field(default=1_000_000.0, gt=0, description="总资金（1/N 均分到各槽位）")
    commission: float = Field(default=0.0003, ge=0, le=0.01)
    min_commission: float = Field(default=5.0, ge=0)
    stamp_tax: float = Field(default=0.001, ge=0, le=0.01)
    slippage: float = Field(default=0.0, ge=0, le=0.05)
    execution: Literal["next_open", "next_close"] = "next_open"
    start: dt.date = dt.date(2020, 1, 1)
    end: dt.date = dt.date(2025, 12, 31)


class StrategyCreate(BaseModel):
    """S2 请求体（契约 §2.2，v1.3 增量；保存策略/组合/多策略方案）。"""

    name: str = Field(min_length=1, max_length=120)
    kind: Literal["single", "portfolio", "multi"]
    strategy: str
    strategy_label: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)
    trade_config: dict[str, Any] = Field(default_factory=dict)
    snapshot: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    notes: str = ""


class StrategyView(BaseModel):
    """SavedStrategy 响应 DTO（契约 §2.2，v1.3 增量；含服务端生成字段）。"""

    id: str
    name: str
    kind: str
    strategy: str
    strategy_label: str
    params: dict[str, Any]
    context: dict[str, Any]
    trade_config: dict[str, Any]
    snapshot: dict[str, Any]
    tags: list[str]
    notes: str
    created_at: str
    updated_at: str
    app_version: str


class OptimizeRunRequest(BaseModel):
    """O1 请求体（契约 §2.1e，v1.3 增量 P1；单标的参数网格寻优）。"""

    market: str = "CN"
    code: str = Field(min_length=1, description="单标的代码（6 位）")
    strategy: str = "ma_cross"
    param_grid: dict[str, list[Any]] = Field(
        default_factory=dict, description="参数取值网格（1-2 个参数，笛卡尔积 ≤200）"
    )
    start: dt.date = dt.date(2020, 1, 1)
    end: dt.date = dt.date(2025, 12, 31)
    cash: float = Field(default=1_000_000.0, gt=0)
    commission: float = Field(default=0.0003, ge=0, le=0.01)
    min_commission: float = Field(default=5.0, ge=0)
    stamp_tax: float = Field(default=0.001, ge=0, le=0.01)
    slippage: float = Field(default=0.0, ge=0, le=0.05)
    execution: Literal["next_open", "next_close"] = "next_open"


class OptimizeAllRunRequest(BaseModel):
    """O4 请求体（对标 easy_tdx ``optimize-all``：一键寻优所有策略）。"""

    market: str = "CN"
    code: str = Field(min_length=1, description="单标的代码（6 位）")
    start: dt.date = dt.date(2020, 1, 1)
    end: dt.date = dt.date(2025, 12, 31)
    cash: float = Field(default=1_000_000.0, gt=0)
    commission: float = Field(default=0.0003, ge=0, le=0.01)
    min_commission: float = Field(default=5.0, ge=0)
    stamp_tax: float = Field(default=0.001, ge=0, le=0.01)
    slippage: float = Field(default=0.0, ge=0, le=0.05)
    execution: Literal["next_open", "next_close"] = "next_open"
    workers: int = Field(default=0, ge=0, le=32, description="并行进程数：0/1=串行，2+=进程池")


class OptimizeGridPoint(BaseModel):
    """O3 results 元素（契约 §2.1e，v1.3 增量 P1；单网格点绩效摘要）。"""

    params: dict[str, Any]
    total_return: float | None = None
    sharpe: float | None = None
    max_drawdown: float | None = None
    total_trades: int = 0
    win_rate: float | None = None
    profit_factor: float | None = None


class OptimizeResult(BaseModel):
    """O3 响应 DTO（契约 §2.1e，v1.3 增量 P1；寻优完整结果）。"""

    strategy: str
    param_names: list[str]
    results: list[OptimizeGridPoint]
    best: OptimizeGridPoint | None = None
    heatmap: dict[str, Any] | None = None


class KlineBar(BaseModel):
    """K 线元素（契约 §3.8，v1.3 增量 P1；B5 单标的 K 线下钻）。"""

    date: str
    open: float
    high: float
    low: float
    close: float
    vol: float
    amount: float


class SignalPoint(BaseModel):
    """买卖点标注（契约 §3.8，v1.3 增量 P1；**信号标注**非下单动作，R5）。"""

    date: str
    price: float | None = None


class KlineWithSignals(BaseModel):
    """B5 响应 DTO（契约 §3.8，v1.3 增量 P1；K 线 + 买卖点叠加）。"""

    code: str
    market: str
    start_date: str
    end_date: str
    strategy: str
    kline: list[KlineBar]
    buy_points: list[SignalPoint]
    sell_points: list[SignalPoint]


# ---------------------------------------------------------------------------
# Settings 层 DTO（契约 §2.1f，v1.3 增量 P2；只读数据源状态）
# ---------------------------------------------------------------------------


class TestConnectionRequest(BaseModel):
    """E2 请求体（契约 §2.1f，P2；主机连通性测试，**只测不写**）。"""

    kind: Literal["std", "mac", "mac_ex"]
    host: str = Field(min_length=1, description="服务器 IP/域名（显式，禁 from_best_host）")
    port: int = Field(ge=1, le=65535, description="端口（std/mac 为 7709，mac_ex 为 7727）")


class TestConnectionResult(BaseModel):
    """E2 响应 DTO（契约 §2.1f；``ok=false`` 是**业务结果**，非 HTTP 错误）。

    连接失败时 ``code=0`` 信封 + ``ok=false`` + ``error`` 明细
    （fail-loud 体现在 error 字段，而不是 HTTP 错误码）。
    """

    ok: bool
    host: str
    port: int
    kind: str
    latency_ms: int | None = None
    error: str | None = None


class SettingsKnownHostItem(BaseModel):
    """E1 known_hosts 行（契约 §2.1f；**只读展示**，``read_only`` 恒为 true）。"""

    host: str
    port: int
    kind: str
    read_only: bool = True


class SettingsStatus(BaseModel):
    """E1 响应 DTO（契约 §2.1f；数据源状态，整页只读，NF-20）。"""

    read_only: bool
    config: dict[str, Any]
    known_hosts: dict[str, Any]
    data: dict[str, Any]
    versions: dict[str, Any]


# ---------------------------------------------------------------------------
# 响应（文档用；真实响应经 Envelope 序列化）
# ---------------------------------------------------------------------------


class MetaModel(BaseModel):
    """信封 meta（NF-9 五字段）。"""

    generated_at: str
    data_date: str | None = None
    market: str
    elapsed_ms: int = 0
    version: str


class EnvelopeModel(BaseModel, Generic[T]):
    """统一信封 ``{code, message, data, meta}``（NF-9）。"""

    code: int
    message: str
    data: T | None = None
    meta: MetaModel


class PageModel(BaseModel, Generic[T]):
    """分页壳（契约 §1.6）。"""

    items: list[T]
    page: int
    page_size: int
    total: int
    total_pages: int


class SyncProgressModel(BaseModel):
    """SyncProgress（契约 §3.2）。"""

    total: int
    done: int
    failed: int
    quarantined: int
    current: str
    percent: float
    started_at: str
    updated_at: str


class JobModel(BaseModel):
    """Job（契约 §3.1）。"""

    job_id: str
    module: str
    action: str
    status: str
    market: str
    progress: SyncProgressModel | None = None
    result_summary: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    created_at: str
    updated_at: str


class QuarantineEntryModel(BaseModel):
    """QuarantineEntry（契约 §3.2）。"""

    code: str
    market: str
    reason: str
    detail: str
    occurred_at: str
    last_try: str
    attempts: int


class DataLakeStatusModel(BaseModel):
    """DataLakeStatus（契约 §3.2；v1.4 增 last_sync/schedule 可空字段）。"""

    market: str
    data_date: str | None = None
    coverage: dict[str, Any]
    quarantine_count: int
    latest_job: JobModel | None = None
    in_sync_window: bool
    #: v1.4：最近一次同步事件（任意来源：cron/startup/manual），无记录为 null
    last_sync: dict[str, Any] | None = None
    #: v1.4：调度配置视图 {enabled, time, startup_check}
    schedule: dict[str, Any] | None = None


class VerifyReportModel(BaseModel):
    """VerifyReport（契约 §3.2）。"""

    market: str
    coverage: dict[str, Any]
    missing_days: list[str]
    corrupt: list[str]
    quarantined: list[QuarantineEntryModel]
    excluded_count: int
    generated_at: str


class SecurityHitModel(BaseModel):
    """SecurityHit（契约 §3.2 D8，v1.2 增量）。"""

    code: str
    name: str
    exchange: str
    market: str
    security_type: str


class FactorInfoModel(BaseModel):
    """FactorInfo（契约 §3.3）。"""

    name: str
    category: str
    display_name: str | None = None
    description: str
    source: str
    status: str
    years: list[int]


class FactorReportModel(BaseModel):
    """FactorReport（契约 §3.3，比例口径 0.05 = 5%）。"""

    factor: str
    market: str
    start_date: str
    end_date: str
    sample_count: int
    excluded_count: int
    ic_mean: float | None = None
    ic_std: float | None = None
    ir: float | None = None
    ic_positive_rate: float | None = None
    quantile_returns: list[float]
    top_minus_bottom: float | None = None
    turnover_rate: float | None = None
    autocorr: float | None = None
    ic_series: list[dict[str, Any]]


class FactorModel(BaseModel):
    """FactorModel（契约 §3.3）。"""

    name: str
    method: str
    weights: dict[str, float]
    created_at: str


class FilterInfoModel(BaseModel):
    """FilterInfo（契约 §3.4）。"""

    type: str
    condition: str
    display_name: str
    description: str
    params_schema: dict[str, Any]


class ScreenBatchModel(BaseModel):
    """ScreenBatch（契约 §3.4，含 v1.1 R1.1-1 的 excluded_count）。"""

    batch_id: str
    market: str
    model: str | None = None
    top_n: int
    filters: list[dict[str, Any]]
    combine: str
    status: str
    result_count: int
    excluded_count: int
    as_of: str
    created_at: str
    elapsed_ms: int


class ScreenResultViewModel(BaseModel):
    """ScreenResultView（= ScreenResult + rank，契约 §3.4）。"""

    code: str
    name: str
    market: str
    score: float
    sub_scores: dict[str, float]
    conditions: str
    price: float
    as_of: str
    rank: int


# ---------------------------------------------------------------------------
# 监控层响应（文档用；真实响应经 Envelope 序列化）
# ---------------------------------------------------------------------------


class MonitorStatusModel(BaseModel):
    """MonitorStatus（契约 §3.5，M1/M2/M3）。"""

    running: bool
    started_at: str | None = None
    poll_interval_seconds: float
    trading_hours_only: bool
    in_trading_session: bool
    last_poll_at: str | None = None
    last_poll_ok: bool | None = None
    consecutive_errors: int
    watchlist_count: int
    rules_enabled_count: int
    channels: list[dict[str, Any]]


class WatchlistItemModel(BaseModel):
    """WatchlistItem（契约 §3.5）。"""

    code: str
    name: str
    market: str
    added_at: str
    source: str


class RuleModel(BaseModel):
    """Rule（契约 §3.5）。"""

    id: str
    name: str
    scope: dict[str, Any]
    criterion_type: str
    params: dict[str, Any]
    level: str
    cooldown_seconds: float
    enabled: bool
    created_at: str
    updated_at: str
    last_triggered_at: str | None = None


class CriterionInfoModel(BaseModel):
    """CriterionInfo（契约 §3.5，M7）。"""

    type: str
    display_name: str
    description: str
    params_schema: dict[str, Any]


class PositionViewModel(BaseModel):
    """PositionView（契约 §3.5；change_pct/pnl_pct 小数比例，0.05 = 5%）。"""

    code: str
    name: str
    market: str
    shares: float
    cost_price: float
    last: float
    change_pct: float
    market_value: float
    pnl: float
    pnl_pct: float
    as_of: str


class AlertModel(BaseModel):
    """Alert（契约 §3.5）。"""

    id: str
    code: str
    market: str
    rule: str
    level: str
    message: str
    ts: str
    payload: dict[str, Any]


class ChannelInfoModel(BaseModel):
    """ChannelInfo（契约 §3.5，M16）。"""

    name: str
    display_name: str
    enabled: bool
    healthy: bool | None = None
