"""MonitorLoop —— 监控编排（轮询 → 规则 → 推送 → 落库 → EventBus 分发）。

流程（每 tick）::

    QuoteFeed.poll(codes)          # 仅交易时段批量报价（独立连接，NF-28）
        → RuleEngine.evaluate()    # 逐规则判定 + 冷却去重
        → Notifier.send(alert)     # 并发推送全部通道
        → MonitorStore.add_alert() # 告警历史落库（M15）
        → EVENT_BUS.publish(TOPIC_ALERT, frame)  # WS 桥（T05b）订阅推送

NF-28 资源隔离
--------------
- 监控轮询使用独立连接（``QuoteFeed`` 内部 ``QuotationFetcher(shared_connection=False)``，
  每次 ``TdxClientFactory.new_mac_client()``），与数据回补链路互不干扰；
- 交易时段禁全量回补属 DataLake 侧约束（错峰），监控自身 ``trading_hours_only``
  只决定是否轮询。

后台运行
--------
:meth:`start` 返回后主线程可继续（守护线程循环），:meth:`stop` 优雅停止，
:meth:`status` 返回契约 §3.5 MonitorStatus（含 ``consecutive_errors``，M3 语义）。
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from collections.abc import Sequence
from typing import Any, Callable

from Kuantix.core.contracts import Alert
from Kuantix.core.eventbus import EVENT_BUS, TOPIC_ALERT
from Kuantix.core.fail_loud import MissingConfigError, require_finite, require_non_empty
from Kuantix.core.market import MarketProfile, get_market_profile

from Kuantix.monitor.feed import QuoteFeed
from Kuantix.monitor.notifier import Notifier
from Kuantix.monitor.position import PositionTracker
from Kuantix.monitor.rules import Rule, RuleEngine
from Kuantix.monitor.store import MonitorStore, WatchlistItem

__all__ = ["MonitorLoop", "MonitorStatus", "DEFAULT_POLL_INTERVAL"]

logger = logging.getLogger(__name__)

#: 默认轮询间隔（秒；config.toml [monitor].poll_interval_seconds 默认 5.0）
DEFAULT_POLL_INTERVAL = 5.0


class MonitorStatus:
    """监控运行状态快照（契约 §3.5 MonitorStatus，M1/M2/M3 端点）。"""

    __slots__ = (
        "running",
        "started_at",
        "poll_interval_seconds",
        "trading_hours_only",
        "in_trading_session",
        "last_poll_at",
        "last_poll_ok",
        "consecutive_errors",
        "watchlist_count",
        "rules_enabled_count",
        "channels",
    )

    def __init__(
        self,
        *,
        running: bool,
        started_at: dt.datetime | None,
        poll_interval_seconds: float,
        trading_hours_only: bool,
        in_trading_session: bool,
        last_poll_at: dt.datetime | None,
        last_poll_ok: bool | None,
        consecutive_errors: int,
        watchlist_count: int,
        rules_enabled_count: int,
        channels: list[dict[str, Any]],
    ) -> None:
        self.running = bool(running)
        self.started_at = started_at
        self.poll_interval_seconds = float(poll_interval_seconds)
        self.trading_hours_only = bool(trading_hours_only)
        self.in_trading_session = bool(in_trading_session)
        self.last_poll_at = last_poll_at
        self.last_poll_ok = last_poll_ok
        self.consecutive_errors = int(consecutive_errors)
        self.watchlist_count = int(watchlist_count)
        self.rules_enabled_count = int(rules_enabled_count)
        self.channels = list(channels)

    def to_dict(self) -> dict[str, Any]:
        """转为契约 §3.5 MonitorStatus 字典。"""
        return {
            "running": self.running,
            "started_at": (
                self.started_at.isoformat(timespec="seconds")
                if self.started_at is not None
                else None
            ),
            "poll_interval_seconds": self.poll_interval_seconds,
            "trading_hours_only": self.trading_hours_only,
            "in_trading_session": self.in_trading_session,
            "last_poll_at": (
                self.last_poll_at.isoformat(timespec="seconds")
                if self.last_poll_at is not None
                else None
            ),
            "last_poll_ok": self.last_poll_ok,
            "consecutive_errors": self.consecutive_errors,
            "watchlist_count": self.watchlist_count,
            "rules_enabled_count": self.rules_enabled_count,
            "channels": self.channels,
        }


class MonitorLoop:
    """监控主循环。

    Args:
        feed: 报价轮询器；``None`` 时默认构建（独立连接）。
        engine: 规则引擎；``None`` 时默认构建。
        notifier: 推送器；``None`` 时默认加载全部通道。
        tracker: 持仓追踪器；``None`` 时默认构建。
        store: 持久化存储；``None`` 时使用默认库。
        profile: 市场档案（交易时段判定）；``None`` 按 market 取。
        market: 默认市场码。
        poll_interval_seconds: 轮询间隔（秒）。
        trading_hours_only: 是否仅交易时段轮询。
    """

    def __init__(
        self,
        *,
        feed: QuoteFeed | None = None,
        engine: RuleEngine | None = None,
        notifier: Notifier | None = None,
        tracker: PositionTracker | None = None,
        store: MonitorStore | None = None,
        profile: MarketProfile | None = None,
        market: str = "CN",
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL,
        trading_hours_only: bool = True,
        name_lookup: Callable[[str, str], str | None] | None = None,
    ) -> None:
        self._store = store if store is not None else MonitorStore()
        self._market = str(market).strip().upper()
        self._profile = profile if profile is not None else get_market_profile(self._market)
        self._feed = feed if feed is not None else QuoteFeed(profile=self._profile, market=self._market)
        self._engine = engine if engine is not None else RuleEngine(store=self._store)
        self._notifier = notifier if notifier is not None else Notifier()
        self._tracker = tracker if tracker is not None else PositionTracker(store=self._store)
        self._poll_interval = require_finite(poll_interval_seconds, "poll_interval_seconds")
        self._trading_hours_only = bool(trading_hours_only)
        #: 证券名称查询回调 ``(code, market) -> name``；注入后添加/列出自选时
        #: 自动补全空名称（从证券清单解析，解决自选表格名称列空白）。
        self._name_lookup = name_lookup

        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.RLock()
        self._running = False
        self._started_at: dt.datetime | None = None
        self._last_poll_at: dt.datetime | None = None
        self._last_poll_ok: bool | None = None
        self._consecutive_errors = 0

    # ------------------------------------------------------------------ #
    # 自选 CRUD（M4-M6）
    # ------------------------------------------------------------------ #

    def _resolve_name(self, code: str, market: str) -> str:
        """从注入的名称查询回调解析证券名称；无回调或未收录返回空串。"""
        if self._name_lookup is None:
            return ""
        try:
            return str(self._name_lookup(code, market) or "")
        except Exception:  # noqa: BLE001 - 名称解析失败不阻断自选流程
            return ""

    def add_watch(self, code: str, *, name: str = "", market: str | None = None, source: str = "manual") -> WatchlistItem:
        """新增自选（M5）；name 为空时自动从证券清单补全。"""
        resolved_market = str(market if market is not None else self._market).upper()
        if not str(name).strip():
            name = self._resolve_name(str(code).strip(), resolved_market)
        item = WatchlistItem(
            code=str(code).strip(),
            name=str(name),
            market=resolved_market,
            source=source,
        )
        self._store.add_watch(item)
        return item

    def remove_watch(self, code: str) -> bool:
        """删除自选（M6）；返回是否确实删除了。"""
        return self._store.delete_watch(code)

    def list_watch(
        self,
        market: str | None = None,
        *,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[WatchlistItem]:
        """列出自选（M4）。P1-2：支持 LIMIT/OFFSET DB 级分页；name 为空的历史
        自选按证券清单补全名称（仅对一页条目做回填，反而更省）。

        早期版本自选可能只存了代码（前端未传名称）；这里对空名称项从证券
        清单解析并回填，保证表格名称列不再空白。
        """
        resolved_market = str(market if market is not None else self._market).upper()
        items = self._store.list_watch(resolved_market, limit=limit, offset=offset)
        for item in items:
            if not item.name.strip():
                resolved = self._resolve_name(item.code, item.market)
                if resolved:
                    item.name = resolved
                    self._store.add_watch(item)
        return items

    def count_watch(self, market: str | None = None) -> int:
        """P1-2：自选匹配条目总数。"""
        resolved_market = str(market if market is not None else self._market).upper()
        return self._store.count_watch(resolved_market)

    def watchlist_codes(self, market: str | None = None) -> list[str]:
        """返回自选代码列表。"""
        return self._store.watch_codes(market if market is not None else self._market)

    # ------------------------------------------------------------------ #
    # 规则 CRUD 代理（M8-M11 便捷入口）
    # ------------------------------------------------------------------ #

    def add_rule(self, rule: Rule) -> Rule:
        """新增规则。"""
        return self._engine.add_rule(rule)

    def list_rules(
        self,
        market: str | None = None,
        *,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[Rule]:
        """列出规则（P1-2：支持 DB 级 LIMIT/OFFSET）。"""
        return self._engine.list_rules(market, limit=limit, offset=offset)

    def count_rules(self, market: str | None = None) -> int:
        """P1-2：规则匹配条目总数。"""
        return self._engine.count_rules(market)

    def delete_rule(self, rule_id: str) -> bool:
        """删除规则。"""
        return self._engine.delete_rule(rule_id)

    def get_rule(self, rule_id: str) -> Rule | None:
        """按 id 取规则。"""
        return self._engine.get_rule(rule_id)

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    def start(
        self,
        watchlist: Sequence[str] | None = None,
        rules: Sequence[Rule] | None = None,
    ) -> dict[str, Any]:
        """启动监控（后台线程，不阻塞调用方）。

        Args:
            watchlist: 待监控代码列表；提供时先写入自选（M1 无 watchlist → 422）。
            rules: 待启用规则；提供时先持久化。

        Returns:
            MonitorStatus 字典（running=true）。

        Raises:
            MissingConfigError: 自选为空（M1 语义）。
        """
        with self._lock:
            if self._running:
                return self.status().to_dict()
            if watchlist is not None:
                for code in watchlist:
                    self.add_watch(code, source="cli")
            require_non_empty(self.watchlist_codes(), "监控启动自选列表（M1）")
            if rules is not None:
                for rule in rules:
                    self._engine.add_rule(rule)
            self._running = True
            self._started_at = dt.datetime.now().astimezone()
            self._consecutive_errors = 0
            self._last_poll_ok = None
            self._last_poll_at = None
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run_loop,
                name="Kuantix-monitor-loop",
                daemon=True,
            )
            self._thread.start()
        logger.info("监控已启动 market=%s interval=%ss", self._market, self._poll_interval)
        return self.status().to_dict()

    def stop(self) -> dict[str, Any]:
        """优雅停止（M2）。

        Returns:
            MonitorStatus 字典（running=false）。
        """
        with self._lock:
            self._running = False
            self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=max(self._poll_interval * 2, 1.0))
        logger.info("监控已停止")
        return self.status().to_dict()

    def status(self) -> MonitorStatus:
        """返回运行状态（M3）。"""
        with self._lock:
            return MonitorStatus(
                running=self._running,
                started_at=self._started_at,
                poll_interval_seconds=self._poll_interval,
                trading_hours_only=self._trading_hours_only,
                in_trading_session=self._profile.is_open_now(),
                last_poll_at=self._last_poll_at,
                last_poll_ok=self._last_poll_ok,
                consecutive_errors=self._consecutive_errors,
                watchlist_count=len(self.watchlist_codes()),
                rules_enabled_count=len(self._engine.enabled_rules(self._market)),
                channels=self._notifier.channels_info(),
            )

    # ------------------------------------------------------------------ #
    # 内部：主循环
    # ------------------------------------------------------------------ #

    def _run_loop(self) -> None:
        """后台守护线程主体：按间隔反复 tick。"""
        while not self._stop_event.is_set():
            tick_start = time.monotonic()
            try:
                self.tick_once()
                self._last_poll_ok = True
                self._consecutive_errors = 0
            except Exception as exc:  # noqa: BLE001 - 单轮失败不终止循环，显式记录
                self._last_poll_ok = False
                self._consecutive_errors += 1
                logger.exception("监控轮询失败（连续第 %s 次）: %s", self._consecutive_errors, exc)
            self._last_poll_at = dt.datetime.now().astimezone()
            elapsed = time.monotonic() - tick_start
            sleep_for = max(0.0, self._poll_interval - elapsed)
            if self._stop_event.wait(sleep_for):
                break

    def tick_once(self) -> dict[str, Any]:
        """执行一轮完整编排（可被测试直接调用）。

        Returns:
            本轮统计 ``{polled, alerts, delivered, published}``。

        Raises:
            任何上游失败直接向上抛（由 ``_run_loop`` 记录并发数连续错误）。
        """
        codes = self.watchlist_codes(self._market)
        require_non_empty(codes, "监控轮询代码列表")
        quotes = self._feed.poll(codes, market=self._market)
        rules = self._engine.enabled_rules(self._market)
        alerts = self._engine.evaluate(quotes, rules)

        delivered_total = 0
        published_total = 0
        for alert in alerts:
            # 1) 推送（并发分发，任一失败显式记录）
            results = self._notifier.send(alert)
            delivered_total += sum(1 for ok in results.values() if ok)
            # 2) 告警历史落库（M15）
            self._store.add_alert(alert)
            # 3) EventBus 分发（WS 桥 T05b 订阅 TOPIC_ALERT）
            frame = self._alert_frame(alert)
            report = EVENT_BUS.publish(TOPIC_ALERT, frame)
            if report.failures:
                logger.error(
                    "告警事件投递失败 topic=%s failures=%s",
                    report.topic,
                    [f.to_dict() for f in report.failures],
                )
            published_total += report.delivered

        return {
            "polled": len(quotes),
            "alerts": len(alerts),
            "delivered": delivered_total,
            "published": published_total,
        }

    @staticmethod
    def _alert_frame(alert: Alert) -> dict[str, Any]:
        """构造契约 §2.4.1 WS alert 帧的 ``data`` 形状（T05b 直接桥接 WS）。

        即 ``{"type": "alert", "alert": Alert.to_dict()}``。
        """
        return {"type": "alert", "alert": alert.to_dict()}
