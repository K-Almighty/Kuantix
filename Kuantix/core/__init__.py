"""Kuantix 核心契约层。

分层单向依赖（NF-4）：``adapters → core → services → api``。

本包**不得** import ``easy_tdx``：所有上游调用必须收敛在
:mod:`Kuantix.adapters` 内（NF-1）。本包也不 import :mod:`Kuantix.config`，
以避免 ``config → core.fail_loud`` 的反向循环依赖。
"""

from __future__ import annotations

from .contracts import (
    Alert,
    AlertLevel,
    Bar,
    ModelHandle,
    Position,
    QuarantineEntry,
    Quote,
    ScreenResult,
    Security,
    SyncProgress,
    VerifyReport,
)
from .envelope import (
    CODE_DATA_ERROR,
    CODE_INTERNAL_ERROR,
    CODE_INVALID_ARGUMENT,
    CODE_NOT_FOUND,
    CODE_NOT_IMPLEMENTED,
    CODE_OK,
    FLOAT_PRECISION,
    Envelope,
    Meta,
    Timer,
    sanitize,
)
from .eventbus import (
    EVENT_BUS,
    TOPIC_ALERT,
    TOPIC_QUARANTINE,
    TOPIC_SYNC_PROGRESS,
    DeliveryReport,
    EventBus,
    EventDeliveryError,
)
from .fail_loud import (
    DataIntegrityError,
    FailLoudError,
    MissingConfigError,
    MissingKeyError,
    NotSupportedError,
    KuantixError,
    UnknownValueError,
    UpstreamContractError,
    reject_unknown,
    require_attr,
    require_finite,
    require_in_range,
    require_key,
    require_known,
    require_non_empty,
)
from .market import (
    MARKET_REGISTRY,
    CalendarCoverageError,
    CNMarketProfile,
    HKMarketProfile,
    MarketProfile,
    MarketRegistry,
    Session,
    TradingWindow,
    USMarketProfile,
    get_market_profile,
    known_markets,
)
from .db import (
    SQLITE_BUSY_TIMEOUT_MS,
    SQLITE_SYNCHRONOUS_FULL,
    SQLITE_SYNCHRONOUS_NORMAL,
    SQLITE_SYNCHRONOUS_OFF,
    apply_sqlite_pragmas,
    connect_sqlite,
    ensure_wal,
    set_synchronous,
)
from .plugins import (
    REGISTRY,
    PluginConflictError,
    PluginKind,
    PluginLoadError,
    PluginRegistry,
    discover_plugins,
    register_plugin,
)

__all__ = [
    # SQLite 并发调优（P1-1）
    "connect_sqlite",
    "apply_sqlite_pragmas",
    "ensure_wal",
    "set_synchronous",
    "SQLITE_BUSY_TIMEOUT_MS",
    "SQLITE_SYNCHRONOUS_NORMAL",
    "SQLITE_SYNCHRONOUS_OFF",
    "SQLITE_SYNCHRONOUS_FULL",
    # fail_loud
    "KuantixError",
    "FailLoudError",
    "UnknownValueError",
    "MissingKeyError",
    "MissingConfigError",
    "DataIntegrityError",
    "NotSupportedError",
    "UpstreamContractError",
    "require_known",
    "reject_unknown",
    "require_key",
    "require_attr",
    "require_finite",
    "require_non_empty",
    "require_in_range",
    # envelope
    "Envelope",
    "Meta",
    "Timer",
    "sanitize",
    "FLOAT_PRECISION",
    "CODE_OK",
    "CODE_INVALID_ARGUMENT",
    "CODE_NOT_FOUND",
    "CODE_DATA_ERROR",
    "CODE_NOT_IMPLEMENTED",
    "CODE_INTERNAL_ERROR",
    # market
    "MarketProfile",
    "CNMarketProfile",
    "HKMarketProfile",
    "USMarketProfile",
    "MarketRegistry",
    "MARKET_REGISTRY",
    "get_market_profile",
    "known_markets",
    "Session",
    "TradingWindow",
    "CalendarCoverageError",
    # plugins
    "PluginKind",
    "PluginRegistry",
    "REGISTRY",
    "register_plugin",
    "discover_plugins",
    "PluginLoadError",
    "PluginConflictError",
    # eventbus
    "EventBus",
    "EVENT_BUS",
    "DeliveryReport",
    "EventDeliveryError",
    "TOPIC_ALERT",
    "TOPIC_SYNC_PROGRESS",
    "TOPIC_QUARANTINE",
    # contracts
    "Bar",
    "Security",
    "Quote",
    "Alert",
    "AlertLevel",
    "Position",
    "ScreenResult",
    "ModelHandle",
    "QuarantineEntry",
    "VerifyReport",
    "SyncProgress",
]
