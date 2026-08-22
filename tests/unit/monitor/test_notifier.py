"""Notifier / 通道白盒单测。

- WebhookChannel：本地假 HTTP server 验证 POST JSON 载荷 + 失败重试；
- DesktopChannel：monkeypatch subprocess.run 验证 osascript 调用与失败记录；
- Notifier.send：多通道并发，任一失败显式记录（返回 False 不抛）。
"""

from __future__ import annotations

import datetime as dt
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from Kuantix.core.contracts import Alert, AlertLevel
from Kuantix.monitor import Notifier, NotifyChannel
from Kuantix.monitor.channels import DesktopChannel, WebhookChannel


def _alert() -> Alert:
    return Alert(
        id="al_test",
        code="600519",
        market="CN",
        rule="止损-成本-8%",
        level=AlertLevel.CRITICAL,
        message="600519 跌破止损线（-8%）",
        ts=dt.datetime(2026, 8, 1, 14, 52, 11),
        payload={"last": 1545.6, "cost": 1680.0},
    )


# ---------------------------------------------------------------------------
# 假 HTTP server
# ---------------------------------------------------------------------------


class _Recorder(BaseHTTPRequestHandler):
    """把请求体与路径记录下来，响应码由外部控制。"""

    protocol_version = "HTTP/1.0"  # 关闭 keep-alive，避免 handler 线程挂起
    received: list[dict] = []
    status_code = 200
    fail_first = 0

    def do_POST(self):  # noqa: N802 - HTTP 方法名
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        type(self).received.append(
            {"path": self.path, "body": json.loads(body), "content_type": self.headers.get("Content-Type")}
        )
        if type(self).fail_first > 0:
            type(self).fail_first -= 1
            self.send_response(500)
        else:
            self.send_response(type(self).status_code)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()

    def log_message(self, *args):  # pragma: no cover - 测试期静默
        pass


@pytest.fixture()
def http_server():
    _Recorder.received = []
    _Recorder.fail_first = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/hook"
    yield server, url
    server.shutdown()
    server.server_close()


# ---------------------------------------------------------------------------
# WebhookChannel
# ---------------------------------------------------------------------------


def test_webhook_send_payload(http_server):
    server, url = http_server
    channel = WebhookChannel(url=url, timeout_seconds=5.0, retry_attempts=1)
    assert channel.send(_alert()) is True

    assert len(_Recorder.received) == 1
    payload = _Recorder.received[0]
    assert payload["path"] == "/hook"
    assert payload["content_type"].startswith("application/json")
    assert payload["body"]["alert"]["code"] == "600519"
    assert payload["body"]["alert"]["level"] == "critical"
    assert payload["body"]["alert"]["rule"] == "止损-成本-8%"


def test_webhook_retry_on_failure(http_server):
    server, url = http_server
    _Recorder.fail_first = 2  # 前两次 500，第三次成功
    channel = WebhookChannel(url=url, timeout_seconds=5.0, retry_attempts=3, retry_backoff_seconds=0.01)
    assert channel.send(_alert()) is True
    assert len(_Recorder.received) == 3


def test_webhook_gives_up_returns_false(http_server):
    server, url = http_server
    _Recorder.status_code = 503
    channel = WebhookChannel(url=url, timeout_seconds=5.0, retry_attempts=2, retry_backoff_seconds=0.01)
    assert channel.send(_alert()) is False
    assert len(_Recorder.received) == 2


def test_webhook_requires_url():
    with pytest.raises(Exception, match="url"):
        WebhookChannel(url="")


# ---------------------------------------------------------------------------
# DesktopChannel
# ---------------------------------------------------------------------------


class _ProcResult:
    returncode = 0
    stdout = ""
    stderr = ""


def test_desktop_send_ok(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _ProcResult()

    monkeypatch.setattr("Kuantix.monitor.channels.desktop.subprocess.run", fake_run)
    channel = DesktopChannel()
    assert channel.send(_alert()) is True
    assert calls and calls[0][0] == "osascript"
    # 脚本内容含告警代码与标题
    script = " ".join(calls[0][1:])
    assert "600519" in script


def test_desktop_send_nonzero_returns_false(monkeypatch):
    def fake_run(cmd, **kwargs):
        result = _ProcResult()
        result.returncode = 1
        result.stderr = "boom"
        return result

    monkeypatch.setattr("Kuantix.monitor.channels.desktop.subprocess.run", fake_run)
    channel = DesktopChannel()
    assert channel.send(_alert()) is False


def test_desktop_send_missing_osascript_returns_false(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("osascript not found")

    monkeypatch.setattr("Kuantix.monitor.channels.desktop.subprocess.run", fake_run)
    channel = DesktopChannel()
    assert channel.send(_alert()) is False


# ---------------------------------------------------------------------------
# Notifier 并发分发
# ---------------------------------------------------------------------------


class _FakeChannel(NotifyChannel):
    name = "fake"
    display_name = "假通道"

    def __init__(self, ok: bool = True, name: str = "fake"):
        super().__init__()
        self.name = name
        self._ok = ok
        self.sent = 0

    def send(self, alert: Alert) -> bool:
        self.sent += 1
        if not self._ok:
            raise RuntimeError("channel exploded")
        return True


def test_notifier_send_all_channels():
    ok_ch = _FakeChannel(ok=True, name="ok")
    bad_ch = _FakeChannel(ok=False, name="bad")
    notifier = Notifier(channels=[ok_ch, bad_ch], max_workers=2)
    results = notifier.send(_alert())
    assert results == {"ok": True, "bad": False}
    assert ok_ch.sent == 1
    assert bad_ch.sent == 1


def test_notifier_channels_info():
    ok_ch = _FakeChannel(ok=True, name="fake")
    notifier = Notifier(channels=[ok_ch])
    info = notifier.channels_info()
    assert info[0]["name"] == "fake"
    assert info[0]["display_name"] == "假通道"


def test_notifier_plugin_discovery_registers_channels():
    """channels 包 import 后应注册 desktop/webhook 到全局插件表。"""
    from Kuantix.core.plugins import REGISTRY, PluginKind

    names = REGISTRY.names(PluginKind.NOTIFY_CHANNEL)
    assert "desktop" in names
    assert "webhook" in names
