"""RuleEngine —— 预警规则引擎 + 判据插件（NF-2 / NF-26 / NF-5）。

设计
----
- 判据走插件机制（``PluginKind.CRITERION``）：``register_criterion`` 装饰器
  注册到全局 :class:`PluginRegistry`，未知判据类型**显式报错**（fail-loud，
  不静默跳过）；
- P0 三类判据：
    * ``price`` —— 价格突破/跌破阈值；
    * ``indicator`` —— MA 金叉死叉 / MACD / RSI（经 :class:`IndicatorBridge`
      间接调用上游 easy_tdx，R2 合规）；
    * ``stop_loss`` —— 止损线（相对成本价或区间最高价回撤）；
- ``evaluate(quotes, rules) -> list[Alert]``：逐规则判定，命中生成
  :class:`Alert`，**冷却去重**（同 code + rule 在冷却期内不重复告警）；
- 规则 CRUD + SQLite 持久化（``MonitorStore``）。

NF-5 / R6：判据参数里不硬编码涨跌停（0.1/0.2）、每手（100）等 A 股常量；
涨跌幅限制一律经 :class:`MarketProfile`（本层只做阈值比较，不做市场特化）。
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from Kuantix.adapters.indicator_bridge import IndicatorBridge
from Kuantix.core.contracts import Alert, AlertLevel, Quote
from Kuantix.core.fail_loud import (
    DataIntegrityError,
    MissingConfigError,
    MissingKeyError,
    UnknownValueError,
    require_finite,
    require_key,
    require_known,
    require_non_empty,
)
from Kuantix.core.market import MarketProfile
from Kuantix.core.plugins import REGISTRY, PluginKind, PluginRegistry, register_plugin

from Kuantix.monitor.store import MonitorStore

__all__ = [
    "Rule",
    "Criterion",
    "CriterionContext",
    "RuleEngine",
    "register_criterion",
    "PriceCriterion",
    "IndicatorCriterion",
    "StopLossCriterion",
    "KNOWN_CRITERION_TYPES",
]

logger = logging.getLogger(__name__)

#: P0 支持的判据类型（M7 CriterionInfo 枚举）
KNOWN_CRITERION_TYPES: tuple[str, ...] = ("price", "indicator", "stop_loss")


# ---------------------------------------------------------------------------
# 规则 DTO（契约 §3.5 Rule）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    """一条预警规则。

    Attributes:
        id: 规则 id（``rule_...``）。
        name: 规则名。
        market: 市场码（``CN``）。
        codes: 适用代码列表；``("*",)`` 表示全部。
        criterion_type: 判据类型（``price``/``indicator``/``stop_loss``）。
        params: 判据参数（见契约 §3.5 Rule.params）。
        level: 告警级别（``info``/``warning``/``critical``）。
        cooldown_seconds: 冷却期（秒），同 code+rule 冷却期内不重复告警。
        enabled: 是否启用。
        source: 规则来源（``manual`` 用户自建 / ``preset`` 预设注入），默认 ``manual``。
        preset_key: 若是预设规则，记录其预设 key（见 ``presets.PRESETS``）。
        created_at / updated_at / last_triggered_at: 时间戳。
    """

    id: str
    name: str
    market: str
    codes: tuple[str, ...]
    criterion_type: str
    params: dict[str, Any]
    level: str
    cooldown_seconds: float
    enabled: bool = True
    source: str = "manual"
    preset_key: str | None = None
    created_at: dt.datetime = field(default_factory=lambda: dt.datetime.now().astimezone())
    updated_at: dt.datetime = field(default_factory=lambda: dt.datetime.now().astimezone())
    last_triggered_at: dt.datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        """转为契约 §3.5 Rule 字典。"""
        return {
            "id": self.id,
            "name": self.name,
            "scope": {"market": self.market, "codes": list(self.codes)},
            "criterion_type": self.criterion_type,
            "params": dict(self.params),
            "level": self.level,
            "cooldown_seconds": self.cooldown_seconds,
            "enabled": self.enabled,
            "source": self.source,
            "preset_key": self.preset_key,
            "created_at": self.created_at.isoformat(timespec="seconds"),
            "updated_at": self.updated_at.isoformat(timespec="seconds"),
            "last_triggered_at": (
                self.last_triggered_at.isoformat(timespec="seconds")
                if self.last_triggered_at is not None
                else None
            ),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Rule":
        """从持久化字典还原（R4-B：全部用 require_key，禁止 .get 兜底）。"""
        scope = require_key(data, "scope", "rule.from_dict.scope")
        market = require_key(scope, "market", "rule.from_dict.scope.market")
        codes = tuple(require_key(scope, "codes", "rule.from_dict.scope.codes"))
        return cls(
            id=str(require_key(data, "id", "rule.from_dict.id")),
            name=str(require_key(data, "name", "rule.from_dict.name")),
            market=str(market).upper(),
            codes=codes,
            criterion_type=str(require_key(data, "criterion_type", "rule.from_dict.criterion_type")),
            params=dict(require_key(data, "params", "rule.from_dict.params")),
            level=str(require_key(data, "level", "rule.from_dict.level")),
            cooldown_seconds=float(require_key(data, "cooldown_seconds", "rule.from_dict.cooldown_seconds")),
            enabled=bool(data["enabled"]) if "enabled" in data else True,
            source=str(data["source"]) if data.get("source") else "manual",
            preset_key=str(data["preset_key"]) if data.get("preset_key") else None,
            created_at=_parse_ts(data["created_at"]) if "created_at" in data else dt.datetime.now().astimezone(),
            updated_at=_parse_ts(data["updated_at"]) if "updated_at" in data else dt.datetime.now().astimezone(),
            last_triggered_at=_parse_ts(data["last_triggered_at"]) if data.get("last_triggered_at") else None,
        )


def _parse_ts(value: str | None) -> dt.datetime | None:
    if value is None or not value:
        return None
    return dt.datetime.fromisoformat(value)


def _new_rule_id() -> str:
    return f"rule_{uuid.uuid4().hex}"


# ---------------------------------------------------------------------------
# 判据插件基类
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CriterionContext:
    """判据求值上下文。

    Attributes:
        quote: 实时报价。
        rule: 当前规则。
        profile: 市场档案（NF-5）。
        bar_provider: 取历史 K 线回调 ``(market, code, count) -> list[Bar]``；
            指标判据需要，未注入时显式报错。
        cost_provider: 取持仓成本回调 ``(code) -> float | None``；
            止损判据 base=cost 需要。
        peak_provider: 取区间最高价回调 ``(code) -> float | None``；
            止损判据 base=peak 需要（由引擎注入维护的峰值表）。
    """

    quote: Quote
    rule: Rule
    profile: MarketProfile
    bar_provider: Callable[[str, str, int], Sequence[Any]] | None = None
    cost_provider: Callable[[str], float | None] = None
    peak_provider: Callable[[str], float | None] = None


class Criterion(ABC):
    """判据插件抽象基类（注册到 PluginKind.CRITERION）。

    子类需定义 ``type`` / ``display_name`` / ``description`` / ``params_schema``
    并实现 :meth:`matches`。
    """

    #: 判据类型标识（注册名）
    type: str = ""
    #: 展示名（M7 CriterionInfo）
    display_name: str = ""
    #: 描述（NF-13）
    description: str = ""
    #: 参数 JSON Schema（前端表单用）
    params_schema: dict[str, Any] = field(default_factory=dict)

    @abstractmethod
    def matches(self, ctx: CriterionContext) -> bool:
        """判断是否命中。命中由引擎生成 :class:`Alert`。"""

    def message(self, ctx: CriterionContext) -> str:
        """告警正文（默认实现可被子类覆盖）。"""
        return f"{ctx.quote.code} {ctx.rule.name} 触发"

    def payload(self, ctx: CriterionContext) -> dict[str, Any]:
        """告警 payload（求值上下文快照，复盘用）。"""
        return {"last": ctx.quote.last}


# ---------------------------------------------------------------------------
# P0 判据：价格
# ---------------------------------------------------------------------------


@register_plugin(PluginKind.CRITERION, "price")
class PriceCriterion(Criterion):
    """价格阈值判据：最新价突破/跌破阈值。"""

    type = "price"
    display_name = "价格阈值"
    description = "最新价突破（above）或跌破（below）给定阈值"
    params_schema = {
        "type": "object",
        "properties": {
            "op": {"type": "string", "enum": ["above", "below"]},
            "threshold": {"type": "number"},
        },
        "required": ["op", "threshold"],
    }

    def matches(self, ctx: CriterionContext) -> bool:
        params = ctx.rule.params
        op = require_known(
            require_key(params, "op", "price_criterion.op"),
            "price_criterion.op",
            allowed={"above", "below"},
        )
        threshold = require_finite(
            require_key(params, "threshold", "price_criterion.threshold"),
            "price_criterion.threshold",
        )
        last = ctx.quote.last
        if op == "above":
            return last > threshold
        return last < threshold

    def message(self, ctx: CriterionContext) -> str:
        params = ctx.rule.params
        op = params["op"]
        threshold = params["threshold"]
        action = "突破" if op == "above" else "跌破"
        return f"{ctx.quote.code} 价格{action}阈值 {threshold}（现价 {ctx.quote.last}）"

    def payload(self, ctx: CriterionContext) -> dict[str, Any]:
        return {"last": ctx.quote.last, "threshold": ctx.rule.params.get("threshold")}


# ---------------------------------------------------------------------------
# P0 判据：涨跌幅（预设规则常用，读 quote.change_pct，无需历史）
# ---------------------------------------------------------------------------


@register_plugin(PluginKind.CRITERION, "change_pct")
class ChangePctCriterion(Criterion):
    """涨跌幅判据：最新涨跌幅突破（above）/跌破（below）给定比例。

    用于涨停 / 跌停 / 涨跌幅异常等预设。``threshold`` 为比例（``0.095`` = 9.5%）。
    """

    type = "change_pct"
    display_name = "涨跌幅"
    description = "最新涨跌幅突破（above）或跌破（below）给定比例（0.095 = 9.5%）"
    params_schema = {
        "type": "object",
        "properties": {
            "op": {"type": "string", "enum": ["above", "below"]},
            "threshold": {"type": "number"},
        },
        "required": ["op", "threshold"],
    }

    def matches(self, ctx: CriterionContext) -> bool:
        params = ctx.rule.params
        op = require_known(
            require_key(params, "op", "change_pct.op"),
            "change_pct.op",
            allowed={"above", "below"},
        )
        threshold = require_finite(
            require_key(params, "threshold", "change_pct.threshold"),
            "change_pct.threshold",
        )
        pct = ctx.quote.change_pct
        if op == "above":
            return pct > threshold
        return pct < threshold

    def message(self, ctx: CriterionContext) -> str:
        params = ctx.rule.params
        op = params["op"]
        threshold = params["threshold"]
        action = "涨超" if op == "above" else "跌超"
        return f"{ctx.quote.code} 涨跌幅{action} {threshold * 100:.1f}%（现价 {ctx.quote.last}，涨跌 {ctx.quote.change_pct * 100:.2f}%）"

    def payload(self, ctx: CriterionContext) -> dict[str, Any]:
        return {
            "change_pct": ctx.quote.change_pct,
            "threshold": ctx.rule.params.get("threshold"),
        }


# ---------------------------------------------------------------------------
# P0 判据：成交量（成交额放量/缩量异常，读 quote.amount，无需历史）
# ---------------------------------------------------------------------------


@register_plugin(PluginKind.CRITERION, "volume")
class VolumeCriterion(Criterion):
    """成交量判据：成交额放量（above）/缩量（below）给定阈值。

    用于成交量异常预设。``threshold`` 为成交额阈值（元），``above`` 表示放量异动。
    """

    type = "volume"
    display_name = "成交量"
    description = "成交额放量（above）/缩量（below）给定阈值（元）"
    params_schema = {
        "type": "object",
        "properties": {
            "op": {"type": "string", "enum": ["above", "below"]},
            "threshold": {"type": "number"},
        },
        "required": ["op", "threshold"],
    }

    def matches(self, ctx: CriterionContext) -> bool:
        params = ctx.rule.params
        op = require_known(
            require_key(params, "op", "volume.op"),
            "volume.op",
            allowed={"above", "below"},
        )
        threshold = require_finite(
            require_key(params, "threshold", "volume.threshold"),
            "volume.threshold",
        )
        amount = ctx.quote.amount
        if op == "above":
            return amount > threshold
        return amount < threshold

    def message(self, ctx: CriterionContext) -> str:
        params = ctx.rule.params
        op = params["op"]
        threshold = params["threshold"]
        action = "放量" if op == "above" else "缩量"
        return f"{ctx.quote.code} 成交额{action}超 {threshold / 1e8:.2f} 亿元（现价 {ctx.quote.last}）"

    def payload(self, ctx: CriterionContext) -> dict[str, Any]:
        return {
            "amount": ctx.quote.amount,
            "threshold": ctx.rule.params.get("threshold"),
        }


# ---------------------------------------------------------------------------
# P0 判据：指标（MA 金叉死叉 / MACD / RSI，经 IndicatorBridge 间接调用上游）
# ---------------------------------------------------------------------------


@register_plugin(PluginKind.CRITERION, "indicator")
class IndicatorCriterion(Criterion):
    """指标判据：MA 交叉 / MACD / RSI 信号。

    params（契约 §3.5 indicator）::

        {"indicator": "ma" | "macd" | "rsi",
         "op": "cross_above" | "cross_below" | "gt" | "lt",
         "value": 70.0, "period": 14}

    - ``ma`` 金叉/死叉：需要 ``fast`` / ``slow``（默认 5 / 20），
      经 ``sma_cross`` 判定当前快慢线相对关系；
    - ``ma`` gt/lt：最新 SMA(period) 与 value 比较；
    - ``macd`` cross：DIF 上穿/下穿 DEA；
    - ``rsi`` gt/lt：最新 RSI(period) 与 value 比较。
    """

    type = "indicator"
    display_name = "技术指标"
    description = "MA 金叉死叉 / MACD / RSI 指标信号"
    params_schema = {
        "type": "object",
        "properties": {
            "indicator": {"type": "string", "enum": ["ma", "macd", "rsi"]},
            "op": {"type": "string", "enum": ["cross_above", "cross_below", "gt", "lt"]},
            "value": {"type": "number"},
            "period": {"type": "integer"},
            "fast": {"type": "integer"},
            "slow": {"type": "integer"},
        },
        "required": ["indicator", "op"],
    }

    def matches(self, ctx: CriterionContext) -> bool:
        params = ctx.rule.params
        indicator = require_known(
            require_key(params, "indicator", "indicator_criterion.indicator"),
            "indicator_criterion.indicator",
            allowed={"ma", "macd", "rsi"},
        )
        op = require_known(
            require_key(params, "op", "indicator_criterion.op"),
            "indicator_criterion.op",
            allowed={"cross_above", "cross_below", "gt", "lt"},
        )
        if ctx.bar_provider is None:
            raise MissingConfigError(
                f"[fail-loud/NF-26] 指标判据 {ctx.rule.name} 需要历史 K 线提供者 "
                f"(bar_provider)，未注入。R2 要求经 adapters 间接调用上游指标"
            )
        bars = list(ctx.bar_provider(ctx.quote.market, ctx.quote.code, self._bar_count(params)))
        if not bars:
            raise DataIntegrityError(
                f"[fail-loud/NF-26] 指标判据 {ctx.rule.name} 取不到历史 K 线 "
                f"({ctx.quote.market}:{ctx.quote.code})，拒绝静默判定"
            )
        closes = [float(b.close) for b in bars]

        if indicator == "ma":
            return self._match_ma(closes, params, op)
        if indicator == "macd":
            return self._match_macd(closes, params, op)
        return self._match_rsi(closes, params, op)

    @staticmethod
    def _bar_count(params: dict[str, Any]) -> int:
        """计算拉取 K 线条数（覆盖慢线窗口 + 缓冲）。"""
        period = int(params.get("period") or 0)
        slow = int(params.get("slow") or 0)
        base = max(period, slow)
        if base <= 0:
            base = 20
        return base * 2 + 10

    @staticmethod
    def _match_ma(closes: Sequence[float], params: dict[str, Any], op: str) -> bool:
        period = int(params.get("period") or 0)
        fast = int(params.get("fast") or 0)
        slow = int(params.get("slow") or 0)
        if op in ("cross_above", "cross_below"):
            f = fast if fast > 0 else 5
            s = slow if slow > 0 else (period if period > 0 else 20)
            relation = IndicatorBridge.sma_cross(closes, f, s)
            if op == "cross_above":
                return relation == "above"
            return relation == "below"
        # gt / lt：最新 SMA(period) 与 value 比较
        if period <= 0:
            raise DataIntegrityError(
                f"[fail-loud/NF-26] MA gt/lt 判据缺少 period，params={params}"
            )
        value = require_finite(
            require_key(params, "value", "indicator_criterion.ma.value"),
            "indicator_criterion.ma.value",
        )
        latest = IndicatorBridge.sma(closes, period)
        if op == "gt":
            return latest > value
        return latest < value

    @staticmethod
    def _match_macd(closes: Sequence[float], params: dict[str, Any], op: str) -> bool:
        fast = int(params.get("fast") or 12)
        slow = int(params.get("slow") or 26)
        signal = int(params.get("period") or 9)
        dif, dea, _hist = IndicatorBridge.macd(
            closes, fast=fast, slow=slow, signal=signal
        )
        latest_dif = dif[-1]
        latest_dea = dea[-1]
        prev_dif = dif[-2]
        prev_dea = dea[-2]
        if op == "cross_above":
            return prev_dif <= prev_dea and latest_dif > latest_dea
        if op == "cross_below":
            return prev_dif >= prev_dea and latest_dif < latest_dea
        value = require_finite(
            require_key(params, "value", "indicator_criterion.macd.value"),
            "indicator_criterion.macd.value",
        )
        if op == "gt":
            return latest_dif > value
        return latest_dif < value

    @staticmethod
    def _match_rsi(closes: Sequence[float], params: dict[str, Any], op: str) -> bool:
        period = int(params.get("period") or 14)
        series = IndicatorBridge.rsi(closes, period)
        latest = series[-1]
        if op in ("cross_above", "cross_below"):
            value = require_finite(
                require_key(params, "value", "indicator_criterion.rsi.value"),
                "indicator_criterion.rsi.value",
            )
            prev = series[-2]
            if op == "cross_above":
                return prev <= value and latest > value
            return prev >= value and latest < value
        value = require_finite(
            require_key(params, "value", "indicator_criterion.rsi.value"),
            "indicator_criterion.rsi.value",
        )
        if op == "gt":
            return latest > value
        return latest < value

    def message(self, ctx: CriterionContext) -> str:
        params = ctx.rule.params
        # 单参 .get() 返回 None 时显式给占位符（R4-B 禁双参兜底）
        indicator = params.get("indicator")
        op = params.get("op")
        indicator_text = indicator if indicator is not None else "?"
        op_text = op if op is not None else "?"
        return f"{ctx.quote.code} {indicator_text} {op_text} 信号触发"

    def payload(self, ctx: CriterionContext) -> dict[str, Any]:
        return {"last": ctx.quote.last, "params": dict(ctx.rule.params)}


# ---------------------------------------------------------------------------
# P0 判据：止损 / 最大回撤
# ---------------------------------------------------------------------------


@register_plugin(PluginKind.CRITERION, "stop_loss")
class StopLossCriterion(Criterion):
    """风险判据：止损线（相对成本价或区间最高价回撤）。

    params（契约 §3.5 stop_loss）::

        {"base": "cost" | "peak", "pct": 0.08}   # 相对成本价或区间最高价回撤 8%

    - ``base=cost``：``last <= cost_price × (1 - pct)`` 时触发；需要
      ``cost_provider``；
    - ``base=peak``：``last <= peak × (1 - pct)`` 时触发；peak 由引擎维护的
      区间最高价（``_peaks``）提供。
    """

    type = "stop_loss"
    display_name = "止损 / 最大回撤"
    description = "最新价相对成本价或区间最高价回撤超过阈值触发"
    params_schema = {
        "type": "object",
        "properties": {
            "base": {"type": "string", "enum": ["cost", "peak"]},
            "pct": {"type": "number"},
        },
        "required": ["base", "pct"],
    }

    def matches(self, ctx: CriterionContext) -> bool:
        params = ctx.rule.params
        base = require_known(
            require_key(params, "base", "stop_loss_criterion.base"),
            "stop_loss_criterion.base",
            allowed={"cost", "peak"},
        )
        pct = require_finite(
            require_key(params, "pct", "stop_loss_criterion.pct"),
            "stop_loss_criterion.pct",
        )
        if not (0.0 < pct < 1.0):
            raise DataIntegrityError(
                f"[fail-loud/NF-26] stop_loss pct 必须落在 (0, 1)，实际 {pct!r}"
            )
        if base == "cost":
            if ctx.cost_provider is None:
                raise MissingConfigError(
                    f"[fail-loud/NF-26] 止损判据 base=cost 需要持仓成本提供者 "
                    f"(cost_provider)，未注入"
                )
            cost = ctx.cost_provider(ctx.quote.code)
            if cost is None:
                return False
            threshold = cost * (1.0 - pct)
            return ctx.quote.last <= threshold
        # base == peak：需要引擎维护的区间最高价
        peak = self._current_peak(ctx)
        if peak is None:
            return False
        threshold = peak * (1.0 - pct)
        return ctx.quote.last <= threshold

    @staticmethod
    def _current_peak(ctx: CriterionContext) -> float | None:
        """取当前区间最高价（来自引擎注入的 peak_provider）。"""
        if ctx.peak_provider is None:
            raise MissingConfigError(
                f"[fail-loud/NF-26] 止损判据 base=peak 需要区间最高价提供者 "
                f"(peak_provider)，未注入"
            )
        return ctx.peak_provider(ctx.quote.code)

    def message(self, ctx: CriterionContext) -> str:
        params = ctx.rule.params
        pct_raw = params.get("pct")
        pct = float(pct_raw) if pct_raw is not None else 0.0
        return f"{ctx.quote.code} 触发止损线（回撤 {pct:.2%}）"

    def payload(self, ctx: CriterionContext) -> dict[str, Any]:
        return {"last": ctx.quote.last, "params": dict(ctx.rule.params)}


# ---------------------------------------------------------------------------
# 装饰器：判据注册
# ---------------------------------------------------------------------------


def register_criterion(
    cls: type[Criterion],
    *,
    registry: PluginRegistry | None = None,
) -> type[Criterion]:
    """类装饰器：把判据注册到插件表（默认全局 REGISTRY）。

    Examples:
        >>> @register_criterion
        ... class MyCriterion(Criterion):
        ...     type = "my"
        ...     display_name = "My"
        ...     description = ""
        ...     def matches(self, ctx): return False
    """
    target = registry if registry is not None else REGISTRY
    name = getattr(cls, "type", "")
    if not name:
        raise ValueError(f"判据插件必须定义 type，实际 {cls!r}")
    target.register(PluginKind.CRITERION, name, cls)
    return cls


# ---------------------------------------------------------------------------
# 规则引擎
# ---------------------------------------------------------------------------


class RuleEngine:
    """规则引擎：CRUD + 冷却去重 + 批量求值。

    Args:
        store: 持久化存储；``None`` 时使用默认库。
        registry: 判据插件表；``None`` 使用全局 REGISTRY。
        profile: 市场档案（求值上下文默认用）；``None`` 时按 quote.market 取。
        bar_provider: 历史 K 线提供者 ``(market, code, count) -> list[Bar]``；
            指标判据需要。注入便于测试（离线注入假 K 线）。
        cost_provider: 持仓成本提供者 ``(code) -> float | None``；
            止损判据 base=cost 需要。注入便于测试。
    """

    def __init__(
        self,
        store: MonitorStore | None = None,
        registry: PluginRegistry | None = None,
        *,
        profile: MarketProfile | None = None,
        bar_provider: Callable[[str, str, int], Sequence[Any]] | None = None,
        cost_provider: Callable[[str], float | None] | None = None,
    ) -> None:
        self._store = store if store is not None else MonitorStore()
        self._registry = registry if registry is not None else REGISTRY
        self._profile = profile
        self._bar_provider = bar_provider
        self._cost_provider = cost_provider
        #: 区间最高价（stop_loss base=peak 用），按 code 维护
        self._peaks: dict[str, float] = {}
        #: 冷却去重记忆：{(rule_id, code): last_triggered_ts}
        self._cooldown_mem: dict[tuple[str, str], dt.datetime] = {}

    # ------------------------------------------------------------------ #
    # 判据注册 / 发现（M7）
    # ------------------------------------------------------------------ #

    def register_criterion(self, cls: type[Criterion]) -> type[Criterion]:
        """向本引擎注册判据（委托给插件表）。"""
        return register_criterion(cls, registry=self._registry)

    def criteria_info(self) -> list[dict[str, Any]]:
        """返回全部已注册判据的 CriterionInfo（契约 §3.5，M7 端点）。"""
        infos: list[dict[str, Any]] = []
        for name, cls in sorted(self._registry.all(PluginKind.CRITERION).items()):
            infos.append(
                {
                    "type": name,
                    "display_name": getattr(cls, "display_name", name),
                    "description": getattr(cls, "description", ""),
                    "params_schema": getattr(cls, "params_schema", {}),
                }
            )
        return infos

    # ------------------------------------------------------------------ #
    # 规则 CRUD（M8-M11）
    # ------------------------------------------------------------------ #

    def create_rule(
        self,
        *,
        name: str,
        market: str,
        codes: Sequence[str],
        criterion_type: str,
        params: dict[str, Any],
        level: str,
        cooldown_seconds: float,
        enabled: bool = True,
        source: str = "manual",
        preset_key: str | None = None,
    ) -> Rule:
        """创建并持久化规则（判据类型/级别/参数在入口校验，fail-loud）。

        Args:
            source: 规则来源（``manual`` 用户自建 / ``preset`` 预设注入）。
            preset_key: 预设规则的 key（仅 ``source="preset"`` 时非空）。

        Raises:
            UnknownValueError: 判据类型或级别未知。
            DataIntegrityError: 参数非法。
        """
        require_non_empty(name, "rule.name")
        known_levels = {lv.value for lv in AlertLevel}
        level_value = require_known(level, "rule.level", allowed=known_levels)
        # 判据类型必须在插件表存在，否则显式报错（不静默跳过）
        self._resolve_criterion(criterion_type)
        rule = Rule(
            id=_new_rule_id(),
            name=str(name).strip(),
            market=str(market).strip().upper(),
            codes=tuple(str(c).strip() for c in codes),
            criterion_type=criterion_type,
            params=dict(params),
            level=level_value,
            cooldown_seconds=require_finite(cooldown_seconds, "rule.cooldown_seconds"),
            enabled=bool(enabled),
            source=str(source),
            preset_key=preset_key,
        )
        self._store.add_rule(self._to_store_dict(rule))
        return rule

    def add_rule(self, rule: Rule) -> Rule:
        """持久化一条已构造的规则。"""
        self._resolve_criterion(rule.criterion_type)
        self._store.add_rule(self._to_store_dict(rule))
        return rule

    def update_rule(self, rule_id: str, **fields: Any) -> Rule:
        """按 id 更新规则（部分字段；未提供字段保持原值）。

        Raises:
            MissingKeyError: 规则不存在。
        """
        current = self.get_rule(rule_id)
        if current is None:
            raise MissingKeyError(f"[fail-loud/NF-26] 规则 {rule_id} 不存在")
        data = current.to_dict()
        if "name" in fields:
            data["name"] = str(fields["name"])
        if "market" in fields:
            data["market"] = str(fields["market"]).strip().upper()
        if "codes" in fields:
            data["scope"]["codes"] = [str(c) for c in fields["codes"]]
        if "criterion_type" in fields:
            data["criterion_type"] = str(fields["criterion_type"])
        if "params" in fields:
            data["params"] = dict(fields["params"])
        if "level" in fields:
            data["level"] = require_known(
                str(fields["level"]), "rule.level", allowed={lv.value for lv in AlertLevel}
            )
        if "cooldown_seconds" in fields:
            data["cooldown_seconds"] = require_finite(
                float(fields["cooldown_seconds"]), "rule.cooldown_seconds"
            )
        if "enabled" in fields:
            data["enabled"] = bool(fields["enabled"])
        data["updated_at"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        updated = Rule.from_dict(data)
        self._resolve_criterion(updated.criterion_type)
        self._store.update_rule(self._to_store_dict(updated))
        return updated

    def delete_rule(self, rule_id: str) -> bool:
        """删除规则；返回是否确实删除了。"""
        return self._store.delete_rule(rule_id)

    def get_rule(self, rule_id: str) -> Rule | None:
        """按 id 取规则。"""
        raw = self._store.get_rule(rule_id)
        if raw is None:
            return None
        return self._from_store_dict(raw)

    def list_rules(
        self,
        market: str | None = None,
        *,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[Rule]:
        """列出全部规则（含已禁用）。P1-2：支持 DB 级 LIMIT/OFFSET。"""
        return [
            self._from_store_dict(raw)
            for raw in self._store.list_rules(market, limit=limit, offset=offset)
        ]

    def count_rules(self, market: str | None = None) -> int:
        """P1-2：规则匹配条目总数。"""
        return self._store.count_rules(market)

    def enabled_rules(
        self,
        market: str | None = None,
        *,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[Rule]:
        """列出启用规则（P1-2：支持 DB 级 LIMIT/OFFSET）。"""
        return [
            self._from_store_dict(raw)
            for raw in self._store.list_rules(
                market, enabled_only=True, limit=limit, offset=offset
            )
        ]

    # ------------------------------------------------------------------ #
    # 预设规则（开箱即用 / 一键开关，见 presets.py）
    # ------------------------------------------------------------------ #

    def ensure_presets(self) -> list[str]:
        """首次启动注入全部预设规则（默认开启，幂等）。返回本次新注入的 key。"""
        from Kuantix.monitor import presets

        return presets.ensure_presets(self._store)

    def apply_preset(self, key: str) -> Rule:
        """将某预设注入为真实规则（默认开启）。若已存在则直接返回现有规则。

        用途：用户首次启用某个曾被关闭/删除的预设。
        """
        from Kuantix.monitor import presets

        tpl = presets.get_preset(key)
        existing = self._store.find_rule_by_preset_key(key)
        if existing is not None:
            return self._from_store_dict(existing)
        kwargs = tpl.build_rule_kwargs()
        return self.create_rule(**kwargs)

    def toggle_preset(self, key: str) -> Rule:
        """一键开关预设规则。

        - 已存在 → 切换其 ``enabled``；
        - 不存在 → 先注入（默认开启），即视为"开启"。
        """
        from Kuantix.monitor import presets

        presets.get_preset(key)  # 未知 key 显式报错（NF-26）
        existing = self._store.find_rule_by_preset_key(key)
        if existing is None:
            return self.apply_preset(key)
        rule = self._from_store_dict(existing)
        return self.update_rule(rule.id, enabled=not rule.enabled)

    def list_preset_statuses(self) -> list[dict[str, Any]]:
        """列出全部预设及其当前状态（用于前端渲染开关控件）。

        Returns:
            ``[{"key","name","description","criterion_type","level",
            "default_enabled","applied": bool,"enabled": bool|None,
            "rule_id": str|null}]`` —— ``applied=False`` 表示尚未注入，
            ``enabled`` 为 ``None``。
        """
        from Kuantix.monitor import presets

        out: list[dict[str, Any]] = []
        for tpl in presets.list_preset_templates():
            raw = self._store.find_rule_by_preset_key(tpl.key)
            if raw is None:
                out.append(
                    {
                        "key": tpl.key,
                        "name": tpl.name,
                        "description": tpl.description,
                        "criterion_type": tpl.criterion_type,
                        "params": dict(tpl.params),
                        "level": tpl.level,
                        "default_enabled": tpl.default_enabled,
                        "applied": False,
                        "enabled": None,
                        "rule_id": None,
                    }
                )
            else:
                rule = self._from_store_dict(raw)
                out.append(
                    {
                        "key": tpl.key,
                        "name": tpl.name,
                        "description": tpl.description,
                        "criterion_type": tpl.criterion_type,
                        "params": dict(rule.params),
                        "level": rule.level,
                        "default_enabled": tpl.default_enabled,
                        "applied": True,
                        "enabled": rule.enabled,
                        "rule_id": rule.id,
                    }
                )
        return out

    # ------------------------------------------------------------------ #
    # 求值（M15 触发源）
    # ------------------------------------------------------------------ #

    def evaluate(
        self,
        quotes: Sequence[Quote],
        rules: Sequence[Rule] | None = None,
        *,
        now: dt.datetime | None = None,
    ) -> list[Alert]:
        """对一批报价逐规则求值，生成告警（冷却去重）。

        Args:
            quotes: 实时报价列表。
            rules: 待判定的规则；``None`` 使用已启用规则。
            now: 当前时刻；``None`` 使用系统时间（测试可注入）。

        Returns:
            命中的 :class:`Alert` 列表。
        """
        moment = now if now is not None else dt.datetime.now().astimezone()
        target_rules = list(rules) if rules is not None else self.enabled_rules()
        alerts: list[Alert] = []
        for quote in quotes:
            self._update_peak(quote.code, quote.last)
            for rule in target_rules:
                if not rule.enabled:
                    continue
                if not self._rule_applies(rule, quote):
                    continue
                if self._in_cooldown(rule, quote.code, moment):
                    continue
                criterion_cls = self._resolve_criterion(rule.criterion_type)
                criterion = criterion_cls()
                ctx = self._build_context(quote, rule)
                try:
                    hit = criterion.matches(ctx)
                except Exception:
                    raise
                if hit:
                    alert = Alert(
                        id=f"al_{uuid.uuid4().hex}",
                        code=quote.code,
                        market=quote.market,
                        rule=rule.name,
                        level=AlertLevel(rule.level),
                        message=criterion.message(ctx),
                        ts=moment,
                        payload=criterion.payload(ctx),
                    )
                    alerts.append(alert)
                    self._mark_triggered(rule, quote.code, moment)
        return alerts

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    @staticmethod
    def _to_store_dict(rule: Rule) -> dict[str, Any]:
        """Rule（契约 scope 形状）→ 存储扁平字典（market/codes 顶层，时间戳为 datetime）。"""
        return {
            "id": rule.id,
            "name": rule.name,
            "market": rule.market,
            "codes": list(rule.codes),
            "criterion_type": rule.criterion_type,
            "params": dict(rule.params),
            "level": rule.level,
            "cooldown_seconds": rule.cooldown_seconds,
            "enabled": rule.enabled,
            "created_at": rule.created_at,
            "updated_at": rule.updated_at,
            "last_triggered_at": rule.last_triggered_at,
        }

    @staticmethod
    def _from_store_dict(raw: dict[str, Any]) -> Rule:
        """存储扁平字典 → Rule（契约 scope 形状；时间戳已是 datetime 对象）。"""
        return Rule(
            id=str(raw["id"]),
            name=str(raw["name"]),
            market=str(raw["market"]),
            codes=tuple(str(c) for c in raw["codes"]),
            criterion_type=str(raw["criterion_type"]),
            params=dict(raw["params"]),
            level=str(raw["level"]),
            cooldown_seconds=float(raw["cooldown_seconds"]),
            enabled=bool(raw["enabled"]),
            source=str(raw["source"]) if raw.get("source") else "manual",
            preset_key=str(raw["preset_key"]) if raw.get("preset_key") else None,
            created_at=raw["created_at"] or dt.datetime.now().astimezone(),
            updated_at=raw["updated_at"] or dt.datetime.now().astimezone(),
            last_triggered_at=raw["last_triggered_at"],
        )

    def _resolve_criterion(self, criterion_type: str) -> type[Criterion]:
        """解析判据类；未知类型显式抛（NF-26，不静默跳过）。"""
        cls = self._registry.resolve(PluginKind.CRITERION, criterion_type)
        if not issubclass(cls, Criterion):
            raise DataIntegrityError(
                f"[fail-loud/NF-26] 插件 {criterion_type!r} 不是 Criterion 子类"
            )
        return cls

    def _build_context(self, quote: Quote, rule: Rule) -> CriterionContext:
        profile = self._profile if self._profile is not None else self._profile_for(quote.market)
        return CriterionContext(
            quote=quote,
            rule=rule,
            profile=profile,
            bar_provider=self._bar_provider,
            cost_provider=self._cost_provider,
            peak_provider=lambda code: self._peaks.get(code),
        )

    def _profile_for(self, market: str) -> MarketProfile:
        from Kuantix.core.market import get_market_profile

        return get_market_profile(market)

    @staticmethod
    def _rule_applies(rule: Rule, quote: Quote) -> bool:
        """规则是否适用于该报价（市场 + 代码范围）。"""
        if rule.market != quote.market:
            return False
        if "*" in rule.codes:
            return True
        return quote.code in rule.codes

    def _update_peak(self, code: str, last: float) -> None:
        """维护区间最高价（stop_loss base=peak 用）。"""
        current = self._peaks.get(code)
        if current is None or last > current:
            self._peaks[code] = last

    def _in_cooldown(self, rule: Rule, code: str, now: dt.datetime) -> bool:
        """冷却去重：同 code+rule 冷却期内不重复告警。"""
        key = (rule.id, code)
        last = self._cooldown_mem.get(key)
        if last is None and rule.last_triggered_at is not None:
            last = rule.last_triggered_at
        if last is None:
            return False
        return (now - last).total_seconds() < rule.cooldown_seconds

    def _mark_triggered(self, rule: Rule, code: str, now: dt.datetime) -> None:
        """记录触发时刻（内存 + 持久化）。"""
        key = (rule.id, code)
        self._cooldown_mem[key] = now
        self._store.set_last_triggered(rule.id, now)
