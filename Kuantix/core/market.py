"""市场档案抽象（NF-5 / NF-7）。

交易日历、涨跌幅限制、货币、时区、每手股数、复权口径 —— 一律经
:class:`MarketProfile` 获取，**业务代码禁止硬编码 A 股常量**。

P0 范围：
- :class:`CNMarketProfile` 完整实现；
- :class:`HKMarketProfile` / :class:`USMarketProfile` 仅占位，
  未实现方法**显式抛** :class:`~Kuantix.core.fail_loud.NotSupportedError`
  （``NotImplementedError`` 的子类），**不静默降级**（NF-7 / U3）。
"""

from __future__ import annotations

import datetime as dt
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .fail_loud import (
    FailLoudError,
    MissingConfigError,
    NotSupportedError,
    UnknownValueError,
    require_key,
    require_known,
)

__all__ = [
    "Session",
    "CalendarCoverageError",
    "TradingWindow",
    "MarketProfile",
    "CNMarketProfile",
    "HKMarketProfile",
    "USMarketProfile",
    "MarketRegistry",
    "MARKET_REGISTRY",
    "get_market_profile",
    "known_markets",
    "DEFAULT_CN_HOLIDAY_FILE",
]

#: 内置 A 股休市日历文件
DEFAULT_CN_HOLIDAY_FILE = Path(__file__).parent / "data" / "cn_holidays.json"


class CalendarCoverageError(FailLoudError):
    """请求的日期超出已加载交易日历的覆盖年份（NF-26）。

    我们**拒绝**用"非周末即交易日"来近似兜底：那会让缺失交易日核对
    （T03 verify）产出错误结论。调用方必须先用
    :meth:`MarketProfile.calendar_years` 判定覆盖范围。
    """


class Session(str, Enum):
    """交易时段。"""

    CLOSED = "closed"  # 非交易日 / 收盘后
    PRE_AUCTION = "pre_auction"  # 集合竞价
    MORNING = "morning"  # 上午连续竞价
    LUNCH_BREAK = "lunch_break"  # 午间休市
    AFTERNOON = "afternoon"  # 下午连续竞价
    POST_CLOSE = "post_close"  # 盘后固定价交易 / 收盘集合竞价后


@dataclass(frozen=True)
class TradingWindow:
    """一个连续交易窗口（本地市场时间）。

    Attributes:
        session: 该窗口对应的时段类型。
        start: 起始时刻（含）。
        end: 结束时刻（不含）。
    """

    session: Session
    start: dt.time
    end: dt.time

    def contains(self, moment: dt.time) -> bool:
        """判断时刻是否落在 ``[start, end)`` 内。"""
        return self.start <= moment < self.end


class MarketProfile(ABC):
    """市场档案抽象基类。

    子类必须提供市场的全部规则常量与交易日历判定。所有业务模块
    （data / factor / screen / monitor）只能经本接口取市场规则。
    """

    #: 市场代码，如 ``CN`` / ``HK`` / ``US``
    market: str = ""
    #: 结算货币 ISO 代码
    currency: str = ""
    #: IANA 时区名
    timezone: str = ""
    #: 默认涨跌幅限制（比例，``0.1`` = 10%）；无限制市场为 ``None``
    price_limit: float | None = None
    #: 最小报价单位
    tick_size: float = 0.0
    #: 每手股数（在线行情 ``股`` → vipdoc ``手`` 的换算基数，RD-8）
    lot_size: int = 0
    #: 年化交易日数（用于年化收益/波动换算）
    trading_days_per_year: int = 0
    #: 该市场规则是否已完整实现。P1 占位市场为 ``False``，供前端顶栏置灰
    #: 与上层预检使用（NF-7）；**它只描述"能不能用"，不提供任何降级默认值**。
    is_implemented: bool = True

    # -- 交易日历 ---------------------------------------------------------

    @abstractmethod
    def calendar_years(self) -> frozenset[int]:
        """返回交易日历已覆盖的年份集合。"""

    @abstractmethod
    def is_trading_day(self, date: dt.date) -> bool:
        """判断给定日期是否为交易日。

        Args:
            date: 日期。

        Returns:
            是否交易日。

        Raises:
            CalendarCoverageError: 日期年份超出日历覆盖范围（NF-26，不做近似）。
        """

    @abstractmethod
    def trading_days_between(self, start: dt.date, end: dt.date) -> list[dt.date]:
        """返回 ``[start, end]`` 区间内的全部交易日（升序）。

        Args:
            start: 起始日期（含）。
            end: 结束日期（含）。

        Returns:
            交易日列表。

        Raises:
            CalendarCoverageError: 区间跨出日历覆盖范围。
        """

    # -- 交易时段 ---------------------------------------------------------

    @abstractmethod
    def trading_windows(self) -> tuple[TradingWindow, ...]:
        """返回该市场一天内的全部交易窗口（本地市场时间，升序）。"""

    def now(self) -> dt.datetime:
        """返回该市场本地时区的当前时刻。"""
        return dt.datetime.now(ZoneInfo(self.timezone))

    def session_now(self, moment: dt.datetime | None = None) -> Session:
        """判定给定时刻所处的交易时段。

        Args:
            moment: 时刻；``None`` 表示"现在"。带时区的入参会被转换到
                市场本地时区，naive 入参按市场本地时区解释。

        Returns:
            :class:`Session` 成员。非交易日恒为 :attr:`Session.CLOSED`。

        Raises:
            CalendarCoverageError: 该日期超出日历覆盖范围。
        """
        tz = ZoneInfo(self.timezone)
        local = self.now() if moment is None else (
            moment.astimezone(tz) if moment.tzinfo is not None else moment.replace(tzinfo=tz)
        )
        if not self.is_trading_day(local.date()):
            return Session.CLOSED
        clock = local.time()
        windows = self.trading_windows()
        for window in windows:
            if window.contains(clock):
                return window.session
        first, last = windows[0], windows[-1]
        if clock < first.start:
            return Session.CLOSED
        if clock >= last.end:
            return Session.CLOSED
        # 落在两个窗口之间（如午休），归为 LUNCH_BREAK
        return Session.LUNCH_BREAK

    def is_open_now(self, moment: dt.datetime | None = None) -> bool:
        """当前是否处于可撮合的连续竞价时段（供监控轮询判定，NF-28）。"""
        return self.session_now(moment) in (Session.MORNING, Session.AFTERNOON)

    # -- 代码规则 ---------------------------------------------------------

    @abstractmethod
    def exchange_for_code(self, code: str) -> str:
        """把证券代码映射到交易所前缀（vipdoc 目录名，如 ``sh``/``sz``）。

        Args:
            code: 证券代码。

        Returns:
            交易所前缀（小写）。

        Raises:
            UnknownValueError: 代码段无法识别（NF-26，禁止默认成主板 A 股）。
        """

    @abstractmethod
    def price_limit_for(self, code: str) -> float | None:
        """返回给定证券的涨跌幅限制比例。

        Args:
            code: 证券代码。

        Returns:
            比例（``0.1`` = 10%）；无限制返回 ``None``。

        Raises:
            UnknownValueError: 代码段无法识别。
        """

    def describe(self) -> dict[str, Any]:
        """导出市场档案摘要（供 REST/CLI 展示）。"""
        return {
            "market": self.market,
            "currency": self.currency,
            "timezone": self.timezone,
            "price_limit": self.price_limit,
            "tick_size": self.tick_size,
            "lot_size": self.lot_size,
            "trading_days_per_year": self.trading_days_per_year,
            "implemented": self.is_implemented,
            "calendar_years": sorted(self.calendar_years()),
        }

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<{type(self).__name__} market={self.market!r}>"


# ---------------------------------------------------------------------------
# CN —— 完整实现
# ---------------------------------------------------------------------------


def _load_cn_holidays(path: Path) -> tuple[frozenset[dt.date], frozenset[int]]:
    """从 JSON 文件加载 A 股休市日历。

    Args:
        path: 日历 JSON 路径。

    Returns:
        ``(休市日集合, 覆盖年份集合)``。

    Raises:
        MissingConfigError: 文件不存在或结构非法。
    """
    if not path.is_file():
        raise MissingConfigError(f"[fail-loud/NF-26] A 股休市日历文件不存在: {path}")
    with path.open("r", encoding="utf-8") as fh:
        payload = json.load(fh)
    years_raw = require_key(payload, "years", f"休市日历 {path}")
    if not isinstance(years_raw, dict) or not years_raw:
        raise MissingConfigError(f"[fail-loud/NF-26] 休市日历 {path} 的 years 字段为空或非对象")
    holidays: set[dt.date] = set()
    years: set[int] = set()
    for year_str, dates in years_raw.items():
        year = int(year_str)
        years.add(year)
        if not isinstance(dates, list):
            raise MissingConfigError(
                f"[fail-loud/NF-26] 休市日历 {path} 年份 {year} 的取值必须是日期数组"
            )
        for item in dates:
            day = dt.date.fromisoformat(str(item))
            if day.year != year:
                raise MissingConfigError(
                    f"[fail-loud/NF-26] 休市日历 {path}：日期 {item} 与所属年份 {year} 不符"
                )
            holidays.add(day)
    return frozenset(holidays), frozenset(years)


class CNMarketProfile(MarketProfile):
    """A 股市场档案（上交所 / 深交所 / 北交所）。

    Attributes:
        market: ``"CN"``。
        currency: ``"CNY"``。
        timezone: ``"Asia/Shanghai"``。
        lot_size: ``100``（每手 100 股）——这正是 RD-8 中在线 ``股`` → vipdoc
            ``手`` 的换算基数，写盘链路必须经本字段取值而非硬编码 100。
    """

    market = "CN"
    currency = "CNY"
    timezone = "Asia/Shanghai"
    price_limit = 0.10
    tick_size = 0.01
    lot_size = 100
    trading_days_per_year = 243

    #: 代码段 → 交易所前缀（依据沪深北交易所代码段分配指南）
    _SH_PREFIXES = ("60", "68", "90", "51", "50", "52", "53", "55", "56", "58", "11", "01", "20")
    _SH_INDEX_PREFIXES = ("000", "880", "999")
    _SZ_PREFIXES = ("00", "30", "20", "39", "15", "16", "17", "18", "10", "12", "13", "14")
    _BJ_PREFIXES = ("43", "83", "87", "88", "92")

    #: 板块 → 涨跌幅限制
    _LIMIT_20_PCT_PREFIXES = ("30", "688", "689")  # 创业板 / 科创板
    _LIMIT_30_PCT_PREFIXES = ("43", "83", "87", "88", "92")  # 北交所

    def __init__(self, holiday_file: Path | str | None = None) -> None:
        """初始化 A 股档案。

        Args:
            holiday_file: 自定义休市日历 JSON 路径；``None`` 使用内置日历。
        """
        path = Path(holiday_file).expanduser() if holiday_file else DEFAULT_CN_HOLIDAY_FILE
        self._holiday_file = path
        self._holidays, self._years = _load_cn_holidays(path)

    # -- 日历 -------------------------------------------------------------

    def calendar_years(self) -> frozenset[int]:
        """已覆盖的日历年份。"""
        return self._years

    @property
    def holiday_file(self) -> Path:
        """当前使用的休市日历文件路径。"""
        return self._holiday_file

    def is_trading_day(self, date: dt.date) -> bool:
        """A 股交易日判定：工作日且不在休市日历内。

        Args:
            date: 日期。

        Returns:
            是否交易日。

        Raises:
            CalendarCoverageError: 年份超出日历覆盖范围。
        """
        if date.year not in self._years:
            raise CalendarCoverageError(
                f"[fail-loud/NF-26] A 股交易日历未覆盖 {date.year} 年"
                f"（已覆盖 {sorted(self._years)}），拒绝用'非周末即交易日'近似兜底。"
                f"请扩展 {self._holiday_file}"
            )
        if date.weekday() >= 5:  # 5=周六, 6=周日
            return False
        return date not in self._holidays

    def trading_days_between(self, start: dt.date, end: dt.date) -> list[dt.date]:
        """区间内全部交易日（升序）。

        Args:
            start: 起始日期（含）。
            end: 结束日期（含）。

        Returns:
            交易日列表；``start > end`` 时返回空列表。

        Raises:
            CalendarCoverageError: 区间任一端跨出日历覆盖范围。
        """
        if start > end:
            return []
        for year in range(start.year, end.year + 1):
            if year not in self._years:
                raise CalendarCoverageError(
                    f"[fail-loud/NF-26] A 股交易日历未覆盖 {year} 年"
                    f"（已覆盖 {sorted(self._years)}），无法枚举 {start}~{end} 的交易日"
                )
        days: list[dt.date] = []
        cursor = start
        one_day = dt.timedelta(days=1)
        while cursor <= end:
            if cursor.weekday() < 5 and cursor not in self._holidays:
                days.append(cursor)
            cursor += one_day
        return days

    # -- 时段 -------------------------------------------------------------

    def trading_windows(self) -> tuple[TradingWindow, ...]:
        """A 股交易窗口：集合竞价 09:15–09:25，上午 09:30–11:30，下午 13:00–15:00。"""
        return (
            TradingWindow(Session.PRE_AUCTION, dt.time(9, 15), dt.time(9, 25)),
            TradingWindow(Session.MORNING, dt.time(9, 30), dt.time(11, 30)),
            TradingWindow(Session.AFTERNOON, dt.time(13, 0), dt.time(15, 0)),
        )

    # -- 代码规则 ---------------------------------------------------------

    def exchange_for_code(self, code: str) -> str:
        """A 股代码 → ``sh`` / ``sz`` / ``bj``。

        Args:
            code: 6 位证券代码。

        Returns:
            交易所前缀。

        Raises:
            UnknownValueError: 代码非 6 位或代码段无法识别（NF-26）。
        """
        raw = str(code).strip().lower()
        # 允许带交易所前缀的写法（sh600000 / sz000001 / bj430047）
        for prefix in ("sh", "sz", "bj"):
            if raw.startswith(prefix) and len(raw) == 8:
                return prefix
        if not raw.isdigit() or len(raw) != 6:
            raise UnknownValueError(
                f"[fail-loud/NF-26] A 股代码格式非法: {code!r}（期望 6 位数字）"
            )
        head3, head2 = raw[:3], raw[:2]
        if head2 in self._BJ_PREFIXES:
            return "bj"
        if head3 in self._SH_INDEX_PREFIXES:
            return "sh"
        if head2 in self._SZ_PREFIXES:
            return "sz"
        if head2 in self._SH_PREFIXES:
            return "sh"
        raise UnknownValueError(
            f"[fail-loud/NF-26] 无法识别 A 股代码段: {code!r}，"
            f"拒绝默认归为沪市/深市主板（会导致 vipdoc 系数误判、价格错 10 倍）"
        )

    def price_limit_for(self, code: str) -> float | None:
        """按板块返回涨跌幅限制：主板 10%，创业板/科创板 20%，北交所 30%。

        Args:
            code: 6 位证券代码。

        Returns:
            涨跌幅比例。

        Raises:
            UnknownValueError: 代码段无法识别。
        """
        exchange = self.exchange_for_code(code)  # 顺带完成代码合法性校验
        raw = str(code).strip().lower()
        digits = raw[2:] if raw[:2] in ("sh", "sz", "bj") else raw
        if exchange == "bj" or digits[:2] in self._LIMIT_30_PCT_PREFIXES:
            return 0.30
        if digits[:2] == "30" or digits[:3] in ("688", "689"):
            return 0.20
        return 0.10


# ---------------------------------------------------------------------------
# HK / US —— P0 占位（NF-7 / U3：显式抛错，不静默降级）
# ---------------------------------------------------------------------------


class _UnimplementedMarketProfile(MarketProfile):
    """P1 市场的占位基类：所有能力显式抛错。

    刻意**不**提供任何"合理默认值"。静默降级会让上层拿到 A 股口径的
    交易日历/涨跌幅去处理港美股数据，属于典型的静默数据损坏（NF-26）。
    """

    #: P1 计划说明，写入异常消息便于定位
    _plan: str = "P1"

    #: 占位市场：能力未实现（NF-7）
    is_implemented: bool = False

    def _reject(self, capability: str) -> None:
        raise NotSupportedError(
            f"[fail-loud/NF-7] {self.market} 市场的 {capability} 尚未实现（{self._plan}）。"
            f"接口先行、拒绝静默降级：不要用 A 股规则代替 {self.market}。"
        )

    def calendar_years(self) -> frozenset[int]:
        self._reject("交易日历")
        raise AssertionError("unreachable")  # pragma: no cover

    def is_trading_day(self, date: dt.date) -> bool:
        self._reject("交易日判定 is_trading_day")
        raise AssertionError("unreachable")  # pragma: no cover

    def trading_days_between(self, start: dt.date, end: dt.date) -> list[dt.date]:
        self._reject("交易日区间枚举 trading_days_between")
        raise AssertionError("unreachable")  # pragma: no cover

    def trading_windows(self) -> tuple[TradingWindow, ...]:
        self._reject("交易时段 trading_windows")
        raise AssertionError("unreachable")  # pragma: no cover

    def exchange_for_code(self, code: str) -> str:
        self._reject("代码→交易所映射 exchange_for_code")
        raise AssertionError("unreachable")  # pragma: no cover

    def price_limit_for(self, code: str) -> float | None:
        self._reject("涨跌幅限制 price_limit_for")
        raise AssertionError("unreachable")  # pragma: no cover

    def describe(self) -> dict[str, Any]:
        """占位市场的摘要（不触发未实现能力，供 UI 置灰展示）。"""
        return {
            "market": self.market,
            "currency": self.currency,
            "timezone": self.timezone,
            "price_limit": self.price_limit,
            "tick_size": self.tick_size,
            "lot_size": self.lot_size,
            "trading_days_per_year": self.trading_days_per_year,
            "implemented": self.is_implemented,
            "plan": self._plan,
        }


class HKMarketProfile(_UnimplementedMarketProfile):
    """港股市场档案（P0 占位，P1 实现）。

    数据链路已在 S4 实测可行：``MacExClient(7727)`` + ``goods_kline``，
    与 A 股 ``MacClient(7709)`` 物理隔离。但交易日历/涨跌幅规则未落地，
    故所有规则方法显式抛错。
    """

    market = "HK"
    currency = "HKD"
    timezone = "Asia/Hong_Kong"
    price_limit = None  # 港股无涨跌幅限制
    tick_size = 0.0  # 港股为阶梯式最小价位，P1 落地
    lot_size = 0  # 每手股数按标的而异，P1 落地
    trading_days_per_year = 0
    _plan = "P1：需落地港股交易日历、阶梯 tick、逐标的每手股数"


class USMarketProfile(_UnimplementedMarketProfile):
    """美股市场档案（P0 占位，P1 实现）。"""

    market = "US"
    currency = "USD"
    timezone = "America/New_York"
    price_limit = None  # 美股无涨跌幅限制（有熔断，规则不同）
    tick_size = 0.0
    lot_size = 0
    trading_days_per_year = 0
    _plan = "P1：需落地美股交易日历（含夏令时）、熔断规则"


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------


class MarketRegistry:
    """市场档案注册表（进程内单例，fail-loud 解析）。"""

    def __init__(self) -> None:
        self._factories: dict[str, Any] = {}
        self._instances: dict[str, MarketProfile] = {}

    def register(self, market: str, factory: Any) -> None:
        """注册市场档案工厂。

        Args:
            market: 市场代码（大写）。
            factory: 无参可调用对象，返回 :class:`MarketProfile` 实例。

        Raises:
            ValueError: 市场码为空。
        """
        key = str(market).strip().upper()
        if not key:
            raise ValueError("市场代码不能为空")
        self._factories[key] = factory
        self._instances.pop(key, None)

    def get(self, market: str) -> MarketProfile:
        """按市场码取档案实例（惰性构造 + 缓存）。

        Args:
            market: 市场代码，大小写不敏感。

        Returns:
            :class:`MarketProfile` 实例。

        Raises:
            UnknownValueError: 市场码未注册（NF-26，不回落到 CN）。
        """
        key = require_known(str(market).strip().upper(), "MarketProfile 解析")
        if key in self._instances:
            return self._instances[key]
        factory = require_key(self._factories, key, "MarketProfile 注册表")
        instance = factory()
        self._instances[key] = instance
        return instance

    def known(self) -> frozenset[str]:
        """已注册的市场码集合。"""
        return frozenset(self._factories)


#: 全局市场注册表
MARKET_REGISTRY = MarketRegistry()
MARKET_REGISTRY.register("CN", CNMarketProfile)
MARKET_REGISTRY.register("HK", HKMarketProfile)
MARKET_REGISTRY.register("US", USMarketProfile)


def get_market_profile(market: str) -> MarketProfile:
    """全局便捷入口：按市场码取 :class:`MarketProfile`。

    Args:
        market: 市场代码（``CN`` / ``HK`` / ``US``）。

    Returns:
        对应的市场档案。

    Raises:
        UnknownValueError: 市场码未注册。
    """
    return MARKET_REGISTRY.get(market)


def known_markets() -> frozenset[str]:
    """返回已注册市场码集合。"""
    return MARKET_REGISTRY.known()
