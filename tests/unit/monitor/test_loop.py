"""MonitorLoop 白盒单测：一轮完整编排（假 feed/engine/notifier 注入）。

验证：
- alert 落库（M15 数据源）；
- EventBus publish 到 TOPIC_ALERT，帧形状符合契约 §2.4.1 WS alert 帧 data；
- start/stop 后台线程优雅；status 含 consecutive_errors（M3 语义）。
"""

from __future__ import annotations

import datetime as dt

import pytest

from Kuantix.core.contracts import Alert, AlertLevel, Quote
from Kuantix.core.eventbus import EVENT_BUS, TOPIC_ALERT
from Kuantix.monitor import MonitorLoop, MonitorStore

from tests.unit.monitor._helpers import make_quote


class _FakeFeed:
    def __init__(self, quotes=None):
        self._quotes = list(quotes) if quotes is not None else []
        self.polled = []

    def poll(self, codes, market=None):
        self.polled.append(list(codes))
        return list(self._quotes)


class _FakeEngine:
    def __init__(self, alerts=None):
        self._alerts = list(alerts) if alerts is not None else []
        self.evaluated = []

    def enabled_rules(self, market=None):
        return []

    def evaluate(self, quotes, rules=None):
        self.evaluated.append(list(quotes))
        return [a for a in self._alerts if any(q.code == a.code for q in quotes)]


class _FakeNotifier:
    def __init__(self):
        self.sent: list[Alert] = []

    def send(self, alert: Alert):
        self.sent.append(alert)
        return {"desktop": True}

    def channels_info(self):
        return [{"name": "desktop", "display_name": "桌面通知", "enabled": True, "healthy": True}]


def _alert(code="600519", rule="止损-成本-8%"):
    return Alert(
        id=f"al_{code}",
        code=code,
        market="CN",
        rule=rule,
        level=AlertLevel.CRITICAL,
        message=f"{code} 跌破止损线（-8%）",
        ts=dt.datetime(2026, 8, 1, 14, 52, 11),
        payload={"last": 1545.6, "cost": 1680.0},
    )


def _loop(tmp_path, quotes=None, alerts=None):
    store = MonitorStore(tmp_path / "monitor.db")
    feed = _FakeFeed(quotes=quotes)
    engine = _FakeEngine(alerts=alerts)
    notifier = _FakeNotifier()
    loop = MonitorLoop(
        feed=feed,
        engine=engine,
        notifier=notifier,
        store=store,
        market="CN",
        poll_interval_seconds=0.05,
    )
    return loop, store, feed, engine, notifier


def test_tick_persists_alert_and_publishes_event(tmp_path):
    loop, store, feed, engine, notifier = _loop(
        tmp_path,
        quotes=[make_quote(code="600519", last=1545.6)],
        alerts=[_alert()],
    )
    loop.add_watch("600519", market="CN")

    received: list[dict] = []
    unsub = EVENT_BUS.subscribe(TOPIC_ALERT, lambda topic, event: received.append(event))
    try:
        result = loop.tick_once()
    finally:
        unsub()

    # 编排统计
    assert result["polled"] == 1
    assert result["alerts"] == 1
    assert result["delivered"] == 1
    assert result["published"] == 1

    # 落库（M15）
    stored = store.list_alerts()
    assert len(stored) == 1
    assert stored[0].code == "600519"
    assert stored[0].level.value == "critical"

    # EventBus 主题正确 + 帧形状（契约 §2.4.1 alert 帧 data）
    assert len(received) == 1
    frame = received[0]
    assert frame["type"] == "alert"
    assert frame["alert"]["code"] == "600519"
    assert frame["alert"]["rule"] == "止损-成本-8%"
    assert frame["alert"]["level"] == "critical"
    assert frame["alert"]["payload"]["last"] == 1545.6


def test_tick_without_alerts_no_publish(tmp_path):
    loop, store, feed, engine, notifier = _loop(tmp_path, quotes=[make_quote()])
    loop.add_watch("600519", market="CN")

    received: list[dict] = []
    unsub = EVENT_BUS.subscribe(TOPIC_ALERT, lambda topic, event: received.append(event))
    try:
        result = loop.tick_once()
    finally:
        unsub()

    assert result["alerts"] == 0
    assert received == []
    assert store.list_alerts() == []


def test_start_stop_lifecycle(tmp_path):
    loop, store, feed, engine, notifier = _loop(
        tmp_path,
        quotes=[make_quote(code="600519")],
        alerts=[],
    )
    loop.add_watch("600519", market="CN")

    status = loop.start()
    assert status["running"] is True
    assert status["watchlist_count"] == 1
    assert status["consecutive_errors"] == 0

    import time

    time.sleep(0.15)  # 让后台线程至少跑 2 tick
    assert feed.polled, "后台线程应已轮询"
    assert loop.status().to_dict()["last_poll_ok"] is True

    stopped = loop.stop()
    assert stopped["running"] is False


def test_start_requires_watchlist(tmp_path):
    loop, store, feed, engine, notifier = _loop(tmp_path)
    with pytest.raises(Exception, match="自选"):
        loop.start()


def test_consecutive_errors_on_tick_failure(tmp_path):
    class _ExplodingFeed(_FakeFeed):
        def poll(self, codes, market=None):
            raise ConnectionError("network down")

    store = MonitorStore(tmp_path / "monitor.db")
    loop = MonitorLoop(
        feed=_ExplodingFeed(),
        engine=_FakeEngine(),
        notifier=_FakeNotifier(),
        store=store,
        market="CN",
        poll_interval_seconds=0.02,
    )
    loop.add_watch("600519", market="CN")
    loop.start()
    import time

    time.sleep(0.1)
    loop.stop()
    status = loop.status().to_dict()
    assert status["last_poll_ok"] is False
    assert status["consecutive_errors"] >= 1


def test_alert_frame_shape_matches_ws_contract():
    """静态断言：_alert_frame 输出 data 形状与 §2.4.1 示例一致。"""
    alert = _alert()
    frame = MonitorLoop._alert_frame(alert)
    assert set(frame.keys()) == {"type", "alert"}
    assert frame["type"] == "alert"
    assert frame["alert"]["id"] == alert.id
    assert frame["alert"]["market"] == "CN"
    assert "ts" in frame["alert"]
    assert "payload" in frame["alert"]
