"""T05a monitor 独立验收（C1-C5）。

与工程师白盒单测（tests/unit/monitor/）**刻意错开样本与路径**：
- C1 RuleEngine：工程师用 ``make_quote`` 逐条测价格/指标/止损；本验收用
  **显式 ts + now 注入**在同一引擎上串测三类判据 + 冷却去重（含冷却期过后再触发）。
- C2 PositionTracker：工程师用 cost=1680 的亏损样本；本验收用 cost=10 加仓
  （100→200 股）的**盈利样本**断言 pnl/pnl_pct 小数比例 + 跨实例重启不丢。
- C3 Notifier Webhook：本验收用独立实现的本地 HTTP server，精确断言
  「首 500 → 重试 → 次 200」两次请求、载荷为契约 Alert 信封；并补「永远失败 → False」。
- C4 MonitorLoop：工程师用全假 engine；本验收注入**真 RuleEngine**（假 feed/notifier），
  一轮 tick 后断言告警落库 + EVENT_BUS TOPIC_ALERT 收到 ``data.type=="alert"`` 帧。
- C5 QuoteFeed：断言默认构造的 fetcher 使用非池化独立连接（``_shared is False``，
  NF-28 监控链路与回补链路资源隔离）。

红线自查：本文件无 ``except: pass`` / 双参 ``.get(k, 默认)``（R4）；全部离线。
"""
from __future__ import annotations

import datetime as dt
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from Kuantix.core.contracts import Bar, Position, Quote
from Kuantix.monitor.rules import RuleEngine
from Kuantix.monitor.store import MonitorStore


def _bars(values: list[float]) -> list[Bar]:
    """把收盘价序列变成 Bar 列表（date 递增，指标判据用）。"""
    base = dt.date(2026, 1, 1)
    out: list[Bar] = []
    for index, close in enumerate(values):
        out.append(
            Bar(
                date=base + dt.timedelta(days=index),
                open=float(close),
                high=float(close),
                low=float(close),
                close=float(close),
                vol=100.0,
                amount=float(close) * 1000.0,
            )
        )
    return out


def _quote(code: str, last: float, prev_close: float, ts: dt.datetime) -> Quote:
    return Quote(
        code=code,
        market="CN",
        last=last,
        prev_close=prev_close,
        change_pct=(last / prev_close) - 1.0,
        vol=100.0,
        amount=last * 1000.0,
        ts=ts,
    )


# ---------------------------------------------------------------------------
# C1 RuleEngine 三类判据 + 冷却去重
# ---------------------------------------------------------------------------


def test_acc_rules_three_criteria_and_cooldown(tmp_path) -> None:
    store = MonitorStore(tmp_path / "monitor.db")
    engine = RuleEngine(
        store=store,
        bar_provider=lambda market, code, count: _bars([10, 10, 10, 10, 10, 11, 12]),
        cost_provider=lambda code: 10.0,
    )
    t0 = dt.datetime(2026, 8, 1, 10, 30, 0)

    # 1) 价格突破
    rule_price = engine.create_rule(
        name="突破1600",
        market="CN",
        codes=["600519"],
        criterion_type="price",
        params={"op": "above", "threshold": 1600.0},
        level="warning",
        cooldown_seconds=3600,
    )
    q_price = _quote("600519", last=1610.0, prev_close=1500.0, ts=t0)
    a1 = engine.evaluate([q_price], rules=[rule_price], now=t0)
    assert len(a1) == 1
    assert a1[0].code == "600519"
    assert a1[0].level.value == "warning"
    assert a1[0].payload["last"] == 1610.0

    # 冷却去重：同 code+rule 冷却期内只出 1 条
    a2 = engine.evaluate(
        [q_price], rules=[rule_price], now=t0 + dt.timedelta(minutes=1)
    )
    assert a2 == []
    # 冷却期过后（3600s）再次触发
    a3 = engine.evaluate(
        [q_price], rules=[rule_price], now=t0 + dt.timedelta(hours=1, minutes=1)
    )
    assert len(a3) == 1

    # 2) 指标金叉：fast=2 均值 11.5 > slow=3 均值 11.0
    rule_ma = engine.create_rule(
        name="MA金叉",
        market="CN",
        codes=["600036"],
        criterion_type="indicator",
        params={"indicator": "ma", "op": "cross_above", "fast": 2, "slow": 3},
        level="info",
        cooldown_seconds=60,
    )
    q_ma = _quote("600036", last=12.0, prev_close=11.0, ts=t0)
    a_ma = engine.evaluate([q_ma], rules=[rule_ma], now=t0)
    assert len(a_ma) == 1
    assert a_ma[0].rule == "MA金叉"

    # 3) 止损：成本 10 元，回撤 10% → 阈值 9.0；现价 8.5 → 触发
    rule_sl = engine.create_rule(
        name="止损10%",
        market="CN",
        codes=["601318"],
        criterion_type="stop_loss",
        params={"base": "cost", "pct": 0.1},
        level="critical",
        cooldown_seconds=60,
    )
    q_sl = _quote("601318", last=8.5, prev_close=9.0, ts=t0)
    a_sl = engine.evaluate([q_sl], rules=[rule_sl], now=t0)
    assert len(a_sl) == 1
    assert a_sl[0].level.value == "critical"


# ---------------------------------------------------------------------------
# C2 PositionTracker：增删改 + pnl 小数比例 + 重启不丢
# ---------------------------------------------------------------------------


def test_acc_position_crud_pnl_and_restart(tmp_path) -> None:
    from Kuantix.monitor.position import PositionTracker

    db_path = tmp_path / "monitor.db"
    tracker = PositionTracker(store=MonitorStore(db_path))

    tracker.add_position(
        Position(
            code="600519",
            market="CN",
            shares=100.0,
            cost_price=10.0,
            opened_at=dt.date(2026, 1, 5),
        ),
        name="贵州茅台",
    )
    # 加仓：100 → 200 股
    tracker.update_position("600519", shares=200.0)
    assert tracker.get_position("600519").shares == 200.0

    # 盈利样本：last=12, cost=10, shares=200
    q = _quote("600519", last=12.0, prev_close=11.0, ts=dt.datetime(2026, 8, 1, 15, 0, 0))
    view = tracker.pnl_for("600519", q)
    assert view["market_value"] == pytest.approx(2400.0)
    assert view["pnl"] == pytest.approx(400.0)
    assert view["pnl_pct"] == pytest.approx(0.2)  # 小数比例，不是 20
    assert view["change_pct"] == pytest.approx(12.0 / 11.0 - 1.0)
    assert view["as_of"] == "2026-08-01"

    # 重启（新实例 + 同一 SQLite）不丢
    tracker2 = PositionTracker(store=MonitorStore(db_path))
    pos2 = tracker2.get_position("600519")
    assert pos2 is not None
    assert pos2.shares == 200.0

    # 删除
    assert tracker2.remove_position("600519") is True
    assert tracker2.get_position("600519") is None


# ---------------------------------------------------------------------------
# C3 Notifier Webhook：本地 HTTP server 载荷 + 失败重试
# ---------------------------------------------------------------------------


def test_acc_webhook_retry_then_delivered() -> None:
    from Kuantix.core.contracts import Alert, AlertLevel
    from Kuantix.monitor.channels.webhook import WebhookChannel

    received: list[dict[str, object]] = []
    state = {"fail_first": 1}

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"  # 关闭 keep-alive，避免 handler 线程挂起

        def do_POST(self):  # noqa: N802 - HTTP 方法名
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8")
            received.append(
                {
                    "path": self.path,
                    "body": json.loads(body),
                    "content_type": self.headers.get("Content-Type"),
                }
            )
            if state["fail_first"] > 0:
                state["fail_first"] -= 1
                self.send_response(500)
            else:
                self.send_response(200)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()

        def log_message(self, *args):  # noqa: A002 - 静音访问日志
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        alert = Alert(
            id="al_wh",
            code="600519",
            market="CN",
            rule="止损-成本-8%",
            level=AlertLevel.CRITICAL,
            message="600519 跌破止损线（-8%）",
            ts=dt.datetime(2026, 8, 1, 10, 0, 0),
            payload={"last": 8.5, "cost": 10.0},
        )
        channel = WebhookChannel(
            url=f"http://127.0.0.1:{server.server_port}/hook",
            retry_attempts=2,
            retry_backoff_seconds=0.0,
        )
        ok = channel.send(alert)
        assert ok is True
        # 第一次 500 → 重试 → 第二次 200：恰好 2 次请求
        assert len(received) == 2
        for item in received:
            assert item["body"] == {"alert": alert.to_dict()}  # 契约 §3.5 Alert 信封
            assert item["content_type"] == "application/json"
            assert item["path"] == "/hook"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_acc_webhook_always_fails_returns_false() -> None:
    from Kuantix.core.contracts import Alert, AlertLevel
    from Kuantix.monitor.channels.webhook import WebhookChannel

    state = {"fail": True}

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def do_POST(self):  # noqa: N802 - HTTP 方法名
            state["fail"] = True
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()

        def log_message(self, *args):  # noqa: A002 - 静音访问日志
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        alert = Alert(
            id="al_wh_fail",
            code="600036",
            market="CN",
            rule="突破",
            level=AlertLevel.WARNING,
            message="x",
            ts=dt.datetime(2026, 8, 1, 10, 0, 0),
            payload={},
        )
        channel = WebhookChannel(
            url=f"http://127.0.0.1:{server.server_port}/hook",
            retry_attempts=2,
            retry_backoff_seconds=0.0,
        )
        # 重试耗尽仍失败 → 返回 False（fail-loud，不静默吞）
        assert channel.send(alert) is False
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# C4 MonitorLoop 完整编排（真 RuleEngine + 假 feed/notifier）
# ---------------------------------------------------------------------------


def test_acc_loop_tick_real_engine_publishes_alert(tmp_path) -> None:
    from Kuantix.core.eventbus import EVENT_BUS, TOPIC_ALERT
    from Kuantix.monitor.loop import MonitorLoop

    store = MonitorStore(tmp_path / "monitor.db")
    engine = RuleEngine(store=store)
    engine.create_rule(
        name="突破-编排",
        market="CN",
        codes=["600000"],
        criterion_type="price",
        params={"op": "above", "threshold": 10.0},
        level="warning",
        cooldown_seconds=60,
    )

    class _Feed:
        def poll(self, codes, market=None):
            return [
                _quote("600000", last=11.0, prev_close=10.0, ts=dt.datetime(2026, 8, 1, 10, 30, 0))
            ]

    class _Notifier:
        def __init__(self) -> None:
            self.sent: list[object] = []

        def send(self, alert):
            self.sent.append(alert)
            return {"desktop": True}

        def channels_info(self):
            return [
                {"name": "desktop", "display_name": "桌面通知", "enabled": True, "healthy": True}
            ]

    notifier = _Notifier()
    loop = MonitorLoop(
        feed=_Feed(),
        engine=engine,
        notifier=notifier,
        store=store,
        poll_interval_seconds=0.1,
        trading_hours_only=False,
    )
    loop.add_watch("600000", source="cli")

    frames: list[dict[str, object]] = []
    unsub = EVENT_BUS.subscribe(TOPIC_ALERT, lambda topic, event: frames.append(event))
    try:
        stats = loop.tick_once()
    finally:
        unsub()

    assert stats["polled"] == 1
    assert stats["alerts"] == 1
    assert stats["delivered"] == 1
    assert stats["published"] == 1

    # EVENT_BUS 收到 TOPIC_ALERT 帧：data.type == "alert"（契约 §2.4.1）
    assert len(frames) == 1
    frame = frames[0]
    assert frame["type"] == "alert"
    alert_dict = frame["alert"]
    assert alert_dict["code"] == "600000"
    assert alert_dict["rule"] == "突破-编排"
    assert alert_dict["level"] == "warning"

    # 告警历史落库（M15）
    alerts = store.list_alerts()
    assert len(alerts) == 1
    assert alerts[0].code == "600000"


# ---------------------------------------------------------------------------
# C5 QuoteFeed 独立连接（NF-28）
# ---------------------------------------------------------------------------


def test_acc_quote_feed_default_fetcher_is_non_shared() -> None:
    from Kuantix.monitor.feed import QuoteFeed

    # 默认构建（不注入 fetcher）→ 必须使用非池化独立连接（NF-28 监控链路隔离）
    feed = QuoteFeed(market="CN", trading_hours_only=False)
    assert feed._fetcher._shared is False  # noqa: SLF001 - 验收断言适配层连接策略
