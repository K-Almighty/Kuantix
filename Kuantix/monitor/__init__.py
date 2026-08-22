"""Kuantix 监控模块（T05a）。

监控链路（NF-28 独立连接，与数据回补互不干扰）：:

    QuoteFeed.poll(codes) → RuleEngine.evaluate() → Notifier.send()
        → MonitorStore.add_alert() → EVENT_BUS.publish(TOPIC_ALERT, frame)

对外暴露：
- :func:`build_monitor_components` —— P0 修复：统一监控组件装配（CLI / API 共享单一入口）；
- :class:`MonitorLoop` —— 主循环（start/stop/status + 自选/规则 CRUD）；
- :class:`QuoteFeed` —— 仅交易时段批量报价轮询；
- :class:`PositionTracker` —— 持仓盈亏（内存 + SQLite）；
- :class:`RuleEngine` / :class:`Rule` —— 规则引擎 + 判据插件；
- :class:`Notifier` / :class:`NotifyChannel` —— 多通道并发推送；
- :class:`MonitorStore` —— SQLite 持久化（规则/持仓/自选/告警）。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from Kuantix.monitor.channels import DesktopChannel, WebhookChannel
from Kuantix.monitor.feed import DEFAULT_BATCH_SIZE, QuoteFeed, QuoteFetchError
from Kuantix.monitor.loop import DEFAULT_POLL_INTERVAL, MonitorLoop, MonitorStatus
from Kuantix.monitor.notifier import NotifyChannel, Notifier
from Kuantix.monitor.position import PositionTracker
from Kuantix.monitor.rules import (
    Criterion,
    CriterionContext,
    IndicatorCriterion,
    KNOWN_CRITERION_TYPES,
    PriceCriterion,
    Rule,
    RuleEngine,
    StopLossCriterion,
    register_criterion,
)
from Kuantix.monitor.store import DEFAULT_DB_FILENAME, MonitorStore, WatchlistItem

if TYPE_CHECKING:  # pragma: no cover - 仅类型引用，避免循环 import
    from Kuantix.config import Config


__all__ = [
    # 装配（P0 修复：单一入口）
    "build_monitor_components",
    # loop
    "MonitorLoop",
    "MonitorStatus",
    "DEFAULT_POLL_INTERVAL",
    # feed
    "QuoteFeed",
    "QuoteFetchError",
    "DEFAULT_BATCH_SIZE",
    # position
    "PositionTracker",
    # rules
    "Rule",
    "RuleEngine",
    "Criterion",
    "CriterionContext",
    "register_criterion",
    "PriceCriterion",
    "IndicatorCriterion",
    "StopLossCriterion",
    "KNOWN_CRITERION_TYPES",
    # notifier
    "Notifier",
    "NotifyChannel",
    # store
    "MonitorStore",
    "WatchlistItem",
    "DEFAULT_DB_FILENAME",
]


def build_monitor_components(
    config: "Config",
    *,
    market: str | None = None,
    name_lookup: Callable[[str, str], str | None] | None = None,
) -> tuple[MonitorLoop, RuleEngine, PositionTracker, MonitorStore, Notifier]:
    """P0 修复：CLI/REST 共享的监控组件装配（消除重复代码与漂移风险）。

    统一构造 ``MonitorLoop + RuleEngine + PositionTracker + MonitorStore + Notifier``
    5 件套，确保两组合根使用同一通道策略、同一参数语义、同一监控库路径。

    Args:
        config: 应用配置（路径 / 市场 / 监控节缺一不可）。
        market: 监控目标市场代码；``None`` 时默认 ``config.markets.default``。
        name_lookup: 证券名称查询回调 ``(code, market) -> name``。
            * ``None``（CLI 常用）：内部临时打开 MarketStore 提供 ``security_name``；
            * 非 ``None``（API 常用）：使用注入的回调（避免重复连接已存在的 MarketStore）。

    Returns:
        ``(loop, engine, tracker, store, notifier)``；
        ``loop`` 已装配独立 QuoteFeed（NF-28 资源隔离）。
    """
    # 延迟 import 避免量化包（easy_tdx）在此处被拉入 monitor 契约层
    from Kuantix.core.market import get_market_profile
    from Kuantix.data.market_store import MarketStore

    resolved_market = str(market if market is not None else config.markets.default).strip().upper()
    profile = get_market_profile(resolved_market)

    store = MonitorStore(config.paths.db / "monitor.db")
    engine = RuleEngine(store=store, profile=profile)
    tracker = PositionTracker(store=store)

    # 通道装配（契约 M16 / PRD P0 双通道）：
    #   - desktop 零配置可用（永远在列表中）；
    #   - webhook 由 [monitor].webhook_url 控制：非空才启用，空串 = 显式未配置
    #     （NF-26 不伪造 URL，M16 只列出实际启用的通道）。
    channels: list[NotifyChannel] = [DesktopChannel()]
    if config.monitor.webhook_url:
        channels.append(WebhookChannel(url=config.monitor.webhook_url))
    notifier = Notifier(channels=channels)

    # 名称解析：调用方没注入时，临时打开共享行情库提供 security_name
    if name_lookup is None:
        market_store = MarketStore(config.paths.db / config.storage.market_db)
        name_lookup = market_store.security_name

    feed = QuoteFeed(profile=profile, market=resolved_market)
    loop = MonitorLoop(
        feed=feed,
        engine=engine,
        notifier=notifier,
        tracker=tracker,
        store=store,
        profile=profile,
        market=resolved_market,
        poll_interval_seconds=config.monitor.poll_interval_seconds,
        trading_hours_only=config.monitor.trading_hours_only,
        name_lookup=name_lookup,
    )
    return loop, engine, tracker, store, notifier

