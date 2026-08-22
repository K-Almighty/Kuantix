"""跨模块数据契约（NF-3 / NF-9）。

``data`` / ``factor`` / ``screen`` / ``monitor`` 四个模块**互不 import**，
一切跨模块传递的数据结构都定义在此处，且：

- 全部是 **frozen dataclass**（不可变，避免跨模块被就地修改）；
- 构造时即做 fail-loud 校验（NaN / 空值 / 负数在入口就被拦下，NF-26）；
- 不依赖 ``easy_tdx``、``pandas``、``pydantic``（core 层零重依赖，NF-4）。

单位口径（RD-8，全项目唯一定义处）
----------------------------------
- :attr:`Bar.vol` 单位为 **手（lot）**，与 vipdoc ``.day`` 读侧口径一致；
- 在线接口（``MacClient``）返回的成交量单位是 **股**，
  必须在 :mod:`Kuantix.adapters.quotation` 内除以
  ``MarketProfile.lot_size`` 转成手之后才能构造 :class:`Bar`；
- :attr:`Bar.amount` 单位为 **元**。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .fail_loud import DataIntegrityError, require_finite

__all__ = [
    "Bar",
    "Security",
    "Quote",
    "AlertLevel",
    "Alert",
    "Position",
    "ScreenResult",
    "QuarantineEntry",
    "VerifyReport",
    "SyncProgress",
    "ModelHandle",
    "NewsItem",
    "FundamentalProfile",
    "TechnicalAnalysis",
    "LimitEntry",
    "LimitUpDownSummary",
    "PreOpenReport",
    "PostCloseReport",
    "FundamentalGrade",
    "LimitType",
    "TrendDirection",
    "NewsCategory",
]


def _require_positive(value: float, name: str, context: str) -> float:
    """校验数值有限且 > 0。"""
    num = require_finite(value, f"{context}.{name}")
    if num <= 0:
        raise DataIntegrityError(
            f"[fail-loud/NF-26] {context}.{name} 必须为正数，实际 {num!r}"
        )
    return num


def _require_non_negative(value: float, name: str, context: str) -> float:
    """校验数值有限且 >= 0。"""
    num = require_finite(value, f"{context}.{name}")
    if num < 0:
        raise DataIntegrityError(
            f"[fail-loud/NF-26] {context}.{name} 不能为负，实际 {num!r}"
        )
    return num


# ---------------------------------------------------------------------------
# 行情契约
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Bar:
    """一根日线 K 线（Kuantix 内部口径，市场无关）。

    Attributes:
        date: 交易日。
        open: 开盘价（原始未复权，RD-5）。
        high: 最高价。
        low: 最低价。
        close: 收盘价。
        vol: 成交量，单位 **手**（RD-8）。
        amount: 成交额，单位 **元**。
    """

    date: dt.date
    open: float
    high: float
    low: float
    close: float
    vol: float
    amount: float

    def __post_init__(self) -> None:
        ctx = f"Bar({self.date})"
        # 价格必须为正：0 价意味着停牌占位或解码错误，一律拒绝入 L1
        for name in ("open", "high", "low", "close"):
            object.__setattr__(self, name, _require_positive(getattr(self, name), name, ctx))
        object.__setattr__(self, "vol", _require_non_negative(self.vol, "vol", ctx))
        object.__setattr__(self, "amount", _require_non_negative(self.amount, "amount", ctx))
        if self.high < self.low:
            raise DataIntegrityError(
                f"[fail-loud/NF-26] {ctx}: high={self.high} < low={self.low}，K 线自相矛盾"
            )
        if not (self.low <= self.open <= self.high):
            raise DataIntegrityError(
                f"[fail-loud/NF-26] {ctx}: open={self.open} 不在 [low={self.low}, "
                f"high={self.high}] 区间内"
            )
        if not (self.low <= self.close <= self.high):
            raise DataIntegrityError(
                f"[fail-loud/NF-26] {ctx}: close={self.close} 不在 [low={self.low}, "
                f"high={self.high}] 区间内"
            )

    @property
    def date_int(self) -> int:
        """``YYYYMMDD`` 整数形式（vipdoc 编码使用）。"""
        return self.date.year * 10000 + self.date.month * 100 + self.date.day

    def to_dict(self) -> dict[str, Any]:
        """转为 JSON 安全字典。"""
        return {
            "date": self.date.isoformat(),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "vol": self.vol,
            "amount": self.amount,
        }


@dataclass(frozen=True)
class Security:
    """一只证券的静态信息。

    Attributes:
        code: 证券代码（不含交易所前缀）。
        exchange: 交易所前缀（``sh`` / ``sz`` / ``bj`` / ``hk`` / ``us``）。
        market: 市场代码（``CN`` / ``HK`` / ``US``）。
        security_type: 上游 ``_SECURITY_COEFFICIENTS`` 口径的证券类型
            （如 ``SH_A_STOCK`` / ``SZ_FUND``）。**绝不允许是 ``UNKNOWN``**：
            未知类型在枚举阶段就必须被拒绝并入隔离区（NF-25/NF-26）。
        name: 证券名称。
    """

    code: str
    exchange: str
    market: str
    security_type: str
    name: str = ""

    def __post_init__(self) -> None:
        if not str(self.code).strip():
            raise DataIntegrityError("[fail-loud/NF-26] Security.code 不能为空")
        if str(self.security_type).strip().upper() == "UNKNOWN":
            raise DataIntegrityError(
                f"[fail-loud/NF-26] Security({self.code}).security_type=UNKNOWN，"
                f"未知证券类型禁止进入业务链路（会导致 vipdoc 系数误判）"
            )

    @property
    def vipdoc_stem(self) -> str:
        """vipdoc 文件名主干，如 ``sh600000``。"""
        return f"{self.exchange}{self.code}"

    def to_dict(self) -> dict[str, Any]:
        """转为 JSON 安全字典。"""
        return {
            "code": self.code,
            "exchange": self.exchange,
            "market": self.market,
            "security_type": self.security_type,
            "name": self.name,
        }


@dataclass(frozen=True)
class Quote:
    """一条实时报价快照。

    Attributes:
        code: 证券代码。
        market: 市场代码。
        last: 最新价。
        prev_close: 昨收价。
        change_pct: 涨跌幅（比例，``0.05`` = 5%）。
        vol: 成交量，单位 **手**。
        amount: 成交额，单位 **元**。
        ts: 快照时刻。
    """

    code: str
    market: str
    last: float
    prev_close: float
    change_pct: float
    vol: float
    amount: float
    ts: dt.datetime

    def __post_init__(self) -> None:
        ctx = f"Quote({self.code})"
        object.__setattr__(self, "last", _require_positive(self.last, "last", ctx))
        object.__setattr__(
            self, "prev_close", _require_positive(self.prev_close, "prev_close", ctx)
        )
        object.__setattr__(
            self, "change_pct", require_finite(self.change_pct, f"{ctx}.change_pct")
        )
        object.__setattr__(self, "vol", _require_non_negative(self.vol, "vol", ctx))
        object.__setattr__(self, "amount", _require_non_negative(self.amount, "amount", ctx))

    def to_dict(self) -> dict[str, Any]:
        """转为 JSON 安全字典。"""
        return {
            "code": self.code,
            "market": self.market,
            "last": self.last,
            "prev_close": self.prev_close,
            "change_pct": self.change_pct,
            "vol": self.vol,
            "amount": self.amount,
            "ts": self.ts.isoformat(timespec="seconds"),
        }


# ---------------------------------------------------------------------------
# 监控契约
# ---------------------------------------------------------------------------


class AlertLevel(str, Enum):
    """告警级别。"""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(frozen=True)
class Alert:
    """一条监控告警。

    Attributes:
        id: 告警唯一 ID。
        code: 触发标的代码。
        market: 市场代码。
        rule: 触发的规则名。
        level: 告警级别。
        message: 告警正文。
        ts: 触发时刻。
        payload: 规则求值上下文快照（用于复盘）。
    """

    id: str
    code: str
    market: str
    rule: str
    level: AlertLevel
    message: str
    ts: dt.datetime
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """转为 JSON 安全字典。"""
        return {
            "id": self.id,
            "code": self.code,
            "market": self.market,
            "rule": self.rule,
            "level": self.level.value,
            "message": self.message,
            "ts": self.ts.isoformat(timespec="seconds"),
            "payload": self.payload,
        }


@dataclass(frozen=True)
class Position:
    """一笔持仓。

    Attributes:
        code: 证券代码。
        market: 市场代码。
        shares: 持仓数量，单位 **股**（持仓天然按股计，不换算成手）。
        cost_price: 持仓成本价。
        opened_at: 建仓日期。
    """

    code: str
    market: str
    shares: float
    cost_price: float
    opened_at: dt.date

    def __post_init__(self) -> None:
        ctx = f"Position({self.code})"
        object.__setattr__(self, "shares", _require_positive(self.shares, "shares", ctx))
        object.__setattr__(
            self, "cost_price", _require_positive(self.cost_price, "cost_price", ctx)
        )

    def market_value(self, last: float) -> float:
        """按最新价计算市值。"""
        return self.shares * require_finite(last, f"Position({self.code}).last")

    def pnl(self, last: float) -> float:
        """按最新价计算浮动盈亏。"""
        return (require_finite(last, f"Position({self.code}).last") - self.cost_price) * self.shares

    def to_dict(self) -> dict[str, Any]:
        """转为 JSON 安全字典。"""
        return {
            "code": self.code,
            "market": self.market,
            "shares": self.shares,
            "cost_price": self.cost_price,
            "opened_at": self.opened_at.isoformat(),
        }


# ---------------------------------------------------------------------------
# 选股 / 因子契约
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScreenResult:
    """一条选股结果。

    Attributes:
        code: 证券代码。
        name: 证券名称。
        market: 市场代码。
        score: 综合得分。
        sub_scores: 各因子分项得分。
        conditions: 命中的过滤条件描述。
        price: 打分时点的收盘价。
        as_of: 数据基准日。
    """

    code: str
    name: str
    market: str
    score: float
    sub_scores: dict[str, float]
    conditions: str
    price: float
    as_of: dt.date

    def __post_init__(self) -> None:
        ctx = f"ScreenResult({self.code})"
        object.__setattr__(self, "score", require_finite(self.score, f"{ctx}.score"))
        object.__setattr__(self, "price", _require_positive(self.price, "price", ctx))

    def to_dict(self) -> dict[str, Any]:
        """转为 JSON 安全字典。"""
        return {
            "code": self.code,
            "name": self.name,
            "market": self.market,
            "score": self.score,
            "sub_scores": dict(self.sub_scores),
            "conditions": self.conditions,
            "price": self.price,
            "as_of": self.as_of.isoformat(),
        }


@dataclass(frozen=True)
class ModelHandle:
    """一个已保存的因子合成模型。

    Attributes:
        name: 模型名。
        weights: 因子 → 权重。
        method: 合成方法（``equal`` / ``ic`` / ``ir``）。
        created_at: 创建时刻。
    """

    name: str
    weights: dict[str, float]
    method: str
    created_at: dt.datetime

    def to_dict(self) -> dict[str, Any]:
        """转为 JSON 安全字典。"""
        return {
            "name": self.name,
            "weights": dict(self.weights),
            "method": self.method,
            "created_at": self.created_at.isoformat(timespec="seconds"),
        }


# ---------------------------------------------------------------------------
# 数据层契约（隔离区 / 校验 / 进度）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QuarantineEntry:
    """隔离区条目（NF-26/NF-27）。

    任何被显式拒绝的数据（UNKNOWN 证券类型、uint32 越界、回读不一致、
    NaN 字段…）都落成一条本记录，不允许静默丢弃。

    Attributes:
        code: 标的代码（或 vipdoc 文件名）。
        market: 市场代码。
        reason: 机器可读的拒绝原因分类。
        detail: 人类可读的详细信息（含异常消息）。
        occurred_at: 首次发生时刻。
        last_try: 最近一次重试时刻。
        attempts: 累计尝试次数。
    """

    code: str
    market: str
    reason: str
    detail: str
    occurred_at: dt.datetime
    last_try: dt.datetime
    attempts: int = 1

    def to_dict(self) -> dict[str, Any]:
        """转为 JSON 安全字典。"""
        return {
            "code": self.code,
            "market": self.market,
            "reason": self.reason,
            "detail": self.detail,
            "occurred_at": self.occurred_at.isoformat(timespec="seconds"),
            "last_try": self.last_try.isoformat(timespec="seconds"),
            "attempts": self.attempts,
        }


@dataclass(frozen=True)
class VerifyReport:
    """数据湖完整性校验报告。

    Attributes:
        market: 市场代码。
        coverage: 覆盖统计（标的数 / 文件数 / 总条数 / 磁盘占用）。
        missing_days: 缺失的交易日。
        corrupt: 损坏的文件列表。
        quarantined: 隔离区条目。
        generated_at: 生成时刻。
    """

    market: str
    coverage: dict[str, Any]
    missing_days: list[dt.date]
    corrupt: list[str]
    quarantined: list[QuarantineEntry]
    generated_at: dt.datetime

    def to_dict(self) -> dict[str, Any]:
        """转为 JSON 安全字典。"""
        return {
            "market": self.market,
            "coverage": dict(self.coverage),
            "missing_days": [d.isoformat() for d in self.missing_days],
            "corrupt": list(self.corrupt),
            "quarantined": [q.to_dict() for q in self.quarantined],
            "generated_at": self.generated_at.isoformat(timespec="seconds"),
        }

    @property
    def is_clean(self) -> bool:
        """是否无缺失、无损坏、无隔离。"""
        return not self.missing_days and not self.corrupt and not self.quarantined


@dataclass(frozen=True)
class SyncProgress:
    """回补进度快照。

    Attributes:
        total: 计划处理的标的总数。
        done: 已成功处理数。
        failed: 失败数。
        quarantined: 进隔离区数。
        current: 当前处理的标的代码。
        started_at: 开始时刻。
        updated_at: 最近更新时刻。
    """

    total: int
    done: int
    failed: int
    quarantined: int
    current: str
    started_at: dt.datetime
    updated_at: dt.datetime

    @property
    def percent(self) -> float:
        """完成百分比（0–100，保留 2 位）。"""
        if self.total <= 0:
            return 0.0
        return round((self.done + self.failed + self.quarantined) * 100.0 / self.total, 2)

    def to_dict(self) -> dict[str, Any]:
        """转为 JSON 安全字典。"""
        return {
            "total": self.total,
            "done": self.done,
            "failed": self.failed,
            "quarantined": self.quarantined,
            "current": self.current,
            "percent": self.percent,
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "updated_at": self.updated_at.isoformat(timespec="seconds"),
        }


# ---------------------------------------------------------------------------
# 盘前 / 盘后分析：枚举与常量
# ---------------------------------------------------------------------------


class NewsCategory(str, Enum):
    """消息面新闻分类。"""

    NEWS = "news"
    ANNOUNCEMENT = "announcement"
    POLICY = "policy"


class FundamentalGrade(str, Enum):
    """基本面画像评级（越高越好，A=优质）。"""

    A = "A"
    B = "B"
    C = "C"
    D = "D"


class TrendDirection(str, Enum):
    """技术面趋势方向。"""

    UP = "up"
    DOWN = "down"
    FLAT = "flat"


class LimitType(str, Enum):
    """涨停板类型（六类 + 兜底「其他」）。"""

    EARNINGS_DRIVEN = "业绩驱动"
    THEME_HYPE = "概念炒作"
    TECH_BREAKOUT = "技术突破"
    NEW_LISTING = "新股上市"
    ST_REMOVAL = "ST摘帽"
    OTHER = "其他"


#: LimitType 的严格优先级（主类型判定顺序）
LIMIT_TYPE_PRIORITY: tuple[LimitType, ...] = (
    LimitType.NEW_LISTING,
    LimitType.ST_REMOVAL,
    LimitType.EARNINGS_DRIVEN,
    LimitType.TECH_BREAKOUT,
    LimitType.THEME_HYPE,
    LimitType.OTHER,
)


def _require_enum(value: str | Enum, enum_cls: type[Enum], context: str) -> Enum:
    """校验枚举取值，非法 → DataIntegrityError（fail-loud）。"""
    if isinstance(value, enum_cls):
        return value
    text = str(value).strip()
    for member in enum_cls:
        if member.value == text:
            return member
    raise DataIntegrityError(
        f"[fail-loud/NF-26] {context} 取值非法: {value!r}；"
        f"允许: {[m.value for m in enum_cls]}"
    )


# ---------------------------------------------------------------------------
# 盘前 / 盘后：核心 DTO
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NewsItem:
    """一条消息面条目（新闻 / 公告 / 政策）。"""

    id: str
    source: str
    category: NewsCategory
    title: str
    url: str
    publish_ts: dt.datetime
    codes: tuple[str, ...]
    importance: int
    matched_keywords: tuple[str, ...]
    summary: str

    def __post_init__(self) -> None:
        ctx = f"NewsItem({self.id!r})"
        if not str(self.id).strip():
            raise DataIntegrityError(f"[fail-loud/NF-26] {ctx}: id 不能为空")
        if not str(self.title).strip():
            raise DataIntegrityError(f"[fail-loud/NF-26] {ctx}: title 不能为空")
        object.__setattr__(
            self, "category", _require_enum(self.category, NewsCategory, f"{ctx}.category")
        )
        # importance ∈ [0, 10]
        if not isinstance(self.importance, int) or not (0 <= int(self.importance) <= 10):
            raise DataIntegrityError(
                f"[fail-loud/NF-26] {ctx}: importance 必须 ∈ [0,10]，实际 {self.importance!r}"
            )
        object.__setattr__(
            self,
            "codes",
            tuple(str(c).strip() for c in (self.codes or ()) if str(c).strip()),
        )
        object.__setattr__(
            self,
            "matched_keywords",
            tuple(str(k).strip() for k in (self.matched_keywords or ()) if str(k).strip()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "category": self.category.value,
            "title": self.title,
            "url": self.url,
            "publish_ts": self.publish_ts.isoformat(timespec="seconds"),
            "codes": list(self.codes),
            "importance": int(self.importance),
            "matched_keywords": list(self.matched_keywords),
            "summary": self.summary,
        }


@dataclass(frozen=True)
class FundamentalProfile:
    """公司基本面画像（整合财务 / 行业地位 / 公告 → 标准化报告）。"""

    code: str
    name: str
    market: str
    sector: str
    industry: str
    market_cap: float
    pe: float | None
    pb: float | None
    roe: float | None
    revenue_growth: float | None
    net_profit_growth: float | None
    debt_ratio: float | None
    dividend_yield: float | None
    latest_announcements: tuple[str, ...]
    grade: FundamentalGrade
    summary_lines: tuple[str, ...]

    def __post_init__(self) -> None:
        ctx = f"FundamentalProfile({self.code})"
        if not str(self.code).strip() or not str(self.market).strip():
            raise DataIntegrityError(f"[fail-loud/NF-26] {ctx}: code/market 不能为空")
        object.__setattr__(
            self, "market_cap", _require_positive(float(self.market_cap), "market_cap", ctx)
        )
        for f_name in ("pe", "pb", "roe", "revenue_growth", "net_profit_growth",
                       "debt_ratio", "dividend_yield"):
            current = object.__getattribute__(self, f_name)
            if current is not None:
                object.__setattr__(
                    self, f_name, require_finite(float(current), f"{ctx}.{f_name}")
                )
        object.__setattr__(
            self, "grade", _require_enum(self.grade, FundamentalGrade, f"{ctx}.grade")
        )
        object.__setattr__(
            self,
            "latest_announcements",
            tuple(str(x) for x in (self.latest_announcements or ()) if str(x).strip()),
        )
        object.__setattr__(
            self,
            "summary_lines",
            tuple(str(x) for x in (self.summary_lines or ()) if str(x).strip()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "market": self.market,
            "sector": self.sector,
            "industry": self.industry,
            "market_cap": float(self.market_cap),
            "pe": None if self.pe is None else float(self.pe),
            "pb": None if self.pb is None else float(self.pb),
            "roe": None if self.roe is None else float(self.roe),
            "revenue_growth": None if self.revenue_growth is None else float(self.revenue_growth),
            "net_profit_growth": None if self.net_profit_growth is None else float(self.net_profit_growth),
            "debt_ratio": None if self.debt_ratio is None else float(self.debt_ratio),
            "dividend_yield": None if self.dividend_yield is None else float(self.dividend_yield),
            "latest_announcements": list(self.latest_announcements),
            "grade": self.grade.value,
            "summary_lines": list(self.summary_lines),
        }


@dataclass(frozen=True)
class TechnicalAnalysis:
    """单标的技术面分析结果（MACD/RSI/KDJ/BOLL + 均线 + 趋势 + 支撑压力位 + 信号）。"""

    code: str
    last_date: dt.date
    ma5: float | None
    ma10: float | None
    ma20: float | None
    ma60: float | None
    ma120: float | None
    ma250: float | None
    macd_dif_last: float | None
    macd_dea_last: float | None
    macd_hist_last: float | None
    rsi_last: float | None
    kdj_k_last: float | None
    kdj_d_last: float | None
    kdj_j_last: float | None
    boll_upper_last: float | None
    boll_mid_last: float | None
    boll_lower_last: float | None
    trend_direction: TrendDirection
    trend_strength: float
    support_levels: tuple[float, ...]
    resistance_levels: tuple[float, ...]
    signals: tuple[str, ...]
    #: 证券名称（来自 lake 元信息；缺失时空串，由前端兜底显示代码）
    name: str = ""
    #: K 线数据来源：lake（本地）/ tdx_realtime（tdx 实时补全）
    data_source: str = "lake"

    def __post_init__(self) -> None:
        ctx = f"TechnicalAnalysis({self.code})"
        if not str(self.code).strip():
            raise DataIntegrityError(f"[fail-loud/NF-26] {ctx}: code 不能为空")
        # 标量浮点做有限性校验（None 表示样本不足，允许）
        for f_name in (
            "ma5", "ma10", "ma20", "ma60", "ma120", "ma250",
            "macd_dif_last", "macd_dea_last", "macd_hist_last", "rsi_last",
            "kdj_k_last", "kdj_d_last", "kdj_j_last",
            "boll_upper_last", "boll_mid_last", "boll_lower_last",
        ):
            current = object.__getattribute__(self, f_name)
            if current is not None:
                object.__setattr__(self, f_name, require_finite(float(current), f"{ctx}.{f_name}"))
        object.__setattr__(
            self,
            "trend_direction",
            _require_enum(self.trend_direction, TrendDirection, f"{ctx}.trend_direction"),
        )
        object.__setattr__(
            self, "trend_strength", require_finite(float(self.trend_strength), f"{ctx}.trend_strength")
        )
        if not (0.0 <= float(self.trend_strength) <= 1.0):
            raise DataIntegrityError(
                f"[fail-loud/NF-26] {ctx}: trend_strength 必须 ∈ [0,1]，实际 {self.trend_strength!r}"
            )
        object.__setattr__(
            self, "support_levels",
            tuple(require_finite(float(x), f"{ctx}.support_levels.item") for x in self.support_levels),
        )
        object.__setattr__(
            self, "resistance_levels",
            tuple(require_finite(float(x), f"{ctx}.resistance_levels.item") for x in self.resistance_levels),
        )
        object.__setattr__(
            self, "signals",
            tuple(str(s).strip() for s in (self.signals or ()) if str(s).strip()),
        )

    def to_dict(self) -> dict[str, Any]:
        def _maybe(v):
            return None if v is None else float(v)
        return {
            "code": self.code,
            "name": self.name,
            "data_source": self.data_source,
            "last_date": self.last_date.isoformat(),
            "ma5": _maybe(self.ma5),
            "ma10": _maybe(self.ma10),
            "ma20": _maybe(self.ma20),
            "ma60": _maybe(self.ma60),
            "ma120": _maybe(self.ma120),
            "ma250": _maybe(self.ma250),
            "macd_dif_last": _maybe(self.macd_dif_last),
            "macd_dea_last": _maybe(self.macd_dea_last),
            "macd_hist_last": _maybe(self.macd_hist_last),
            "rsi_last": _maybe(self.rsi_last),
            "kdj_k_last": _maybe(self.kdj_k_last),
            "kdj_d_last": _maybe(self.kdj_d_last),
            "kdj_j_last": _maybe(self.kdj_j_last),
            "boll_upper_last": _maybe(self.boll_upper_last),
            "boll_mid_last": _maybe(self.boll_mid_last),
            "boll_lower_last": _maybe(self.boll_lower_last),
            "trend_direction": self.trend_direction.value,
            "trend_strength": float(self.trend_strength),
            "support_levels": [round(float(x), 2) for x in self.support_levels],
            "resistance_levels": [round(float(x), 2) for x in self.resistance_levels],
            "signals": list(self.signals),
        }


@dataclass(frozen=True)
class LimitEntry:
    """一条涨停 / 跌停条目。"""

    code: str
    name: str
    sector: str
    limit_type: LimitType
    close: float
    change_pct: float
    volume_ratio: float | None
    continuous_days: int
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        ctx = f"LimitEntry({self.code})"
        if not str(self.code).strip():
            raise DataIntegrityError(f"[fail-loud/NF-26] {ctx}: code 不能为空")
        object.__setattr__(
            self, "close", _require_positive(float(self.close), "close", ctx)
        )
        object.__setattr__(
            self, "change_pct", require_finite(float(self.change_pct), f"{ctx}.change_pct")
        )
        vr = object.__getattribute__(self, "volume_ratio")
        if vr is not None:
            object.__setattr__(
                self, "volume_ratio", _require_non_negative(float(vr), "volume_ratio", ctx)
            )
        if not isinstance(self.continuous_days, int) or self.continuous_days < 1:
            raise DataIntegrityError(
                f"[fail-loud/NF-26] {ctx}: continuous_days 必须 ≥ 1，实际 {self.continuous_days!r}"
            )
        object.__setattr__(
            self, "limit_type",
            _require_enum(self.limit_type, LimitType, f"{ctx}.limit_type"),
        )
        object.__setattr__(
            self, "reasons",
            tuple(str(r).strip() for r in (self.reasons or ()) if str(r).strip()),
        )

    @property
    def is_up(self) -> bool:
        return float(self.change_pct) >= 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "sector": self.sector,
            "limit_type": self.limit_type.value,
            "close": float(self.close),
            "change_pct": float(self.change_pct),
            "volume_ratio": None if self.volume_ratio is None else float(self.volume_ratio),
            "continuous_days": int(self.continuous_days),
            "reasons": list(self.reasons),
            "is_up": self.is_up,
        }


@dataclass(frozen=True)
class LimitUpDownSummary:
    """当日涨跌停的汇总视图。"""

    date: dt.date
    market: str
    up_count: int
    down_count: int
    flat_count: int
    total_count: int
    up_ratio: float
    down_ratio: float
    by_sector: tuple[dict[str, Any], ...]  # [{"sector":"xx","up":N,"down":N}]
    by_type: tuple[dict[str, Any], ...]    # [{"limit_type":"xx","count":N}]
    generated_at: dt.datetime

    def __post_init__(self) -> None:
        ctx = "LimitUpDownSummary"
        for f_name in ("up_count", "down_count", "flat_count", "total_count"):
            v = object.__getattribute__(self, f_name)
            if not isinstance(v, int) or v < 0:
                raise DataIntegrityError(
                    f"[fail-loud/NF-26] {ctx}.{f_name} 必须为非负整数，实际 {v!r}"
                )
        for f_name in ("up_ratio", "down_ratio"):
            object.__setattr__(
                self, f_name,
                require_finite(float(object.__getattribute__(self, f_name)), f"{ctx}.{f_name}"),
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date.isoformat(),
            "market": self.market,
            "up_count": int(self.up_count),
            "down_count": int(self.down_count),
            "flat_count": int(self.flat_count),
            "total_count": int(self.total_count),
            "up_ratio": float(self.up_ratio),
            "down_ratio": float(self.down_ratio),
            "by_sector": [dict(x) for x in self.by_sector],
            "by_type": [dict(x) for x in self.by_type],
            "generated_at": self.generated_at.isoformat(timespec="seconds"),
        }


@dataclass(frozen=True)
class PreOpenReport:
    """盘前分析报告（消息面 + 自选基本面画像 + 大盘抽样技术 Top）。"""

    date: dt.date
    market: str
    generated_at: dt.datetime
    news_feed_summary: dict[str, Any]  # {total, by_category:[], top_news:[NewsItem.to_dict]}
    watchlist_profiles: tuple[dict[str, Any], ...]
    broad_market_scan_top: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date.isoformat(),
            "market": self.market,
            "generated_at": self.generated_at.isoformat(timespec="seconds"),
            "news_feed_summary": dict(self.news_feed_summary),
            "watchlist_profiles": [dict(p) for p in self.watchlist_profiles],
            "broad_market_scan_top": [dict(t) for t in self.broad_market_scan_top],
        }


@dataclass(frozen=True)
class PostCloseReport:
    """盘后复盘报告（涨跌停 + 技术亮点 + 今日信号 + 自选浮盈）。"""

    date: dt.date
    market: str
    generated_at: dt.datetime
    limit_summary: dict[str, Any]  # LimitUpDownSummary.to_dict
    tech_highlights: tuple[dict[str, Any], ...]
    signals_today: tuple[dict[str, Any], ...]
    watchlist_pnl: tuple[dict[str, Any], ...]
    data_source: str = "lake"  # lake | tdx_realtime | lake_fallback
    data_as_of: dt.date | None = None  # 降级展示时标记「数据实际截至」

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date.isoformat(),
            "market": self.market,
            "generated_at": self.generated_at.isoformat(timespec="seconds"),
            "data_source": self.data_source,
            "data_as_of": self.data_as_of.isoformat() if self.data_as_of else None,
            "limit_summary": dict(self.limit_summary),
            "tech_highlights": [dict(x) for x in self.tech_highlights],
            "signals_today": [dict(x) for x in self.signals_today],
            "watchlist_pnl": [dict(x) for x in self.watchlist_pnl],
        }
